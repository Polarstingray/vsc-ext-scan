import contextlib
import csv
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
from untrustide_scan import cli
from untrustide_scan import codeql as cq
from untrustide_scan import marketplace as mp


def run_cli(*argv):
    """Run the CLI, returning (exit_code, stdout+stderr)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = cli.main(list(argv))
        except SystemExit as e:
            rc = e.code
    return rc, buf.getvalue()


def ext(name, version="1.0"):
    return {"extension_id": "pub." + name, "publisher": "pub", "name": name,
            "display_name": name, "version": version, "install_count": 1,
            "last_updated": "", "vsix_url": "https://x/" + name}


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.work = str(self.dir / "work")
        self.manifest = Path(self.work) / "results" / "manifest.csv"

    def tearDown(self):
        self.tmp.cleanup()

    def statuses(self):
        return {r["extension_id"]: r["status"] for r in mp.read_manifest(str(self.manifest))}


class ParserTests(CliTestCase):
    def test_help(self):
        rc, out = run_cli("--help")
        self.assertEqual(rc, 0)
        for cmd in ("scrape", "build", "query", "run", "local"):
            self.assertIn(cmd, out)

    def test_run_requires_count(self):
        rc, _ = run_cli("run")
        self.assertEqual(rc, 2)

    def test_run_accepts_union_of_flags(self):
        args = cli.build_parser().parse_args(
            ["run", "--count", "3", "--query", "git", "--sort", "rating",
             "--codeql", "/x", "--timeout", "5", "--overwrite",
             "--queries", "a.ql", "b", "--query-timeout", "7"])
        self.assertEqual((args.count, args.query, args.sort), (3, "git", "rating"))
        self.assertEqual(args.queries, ["a.ql", "b"])
        self.assertEqual((args.timeout, args.query_timeout), (5, 7))


class LocalTests(CliTestCase):
    def test_local_source(self):
        src = self.dir / "myext"
        src.mkdir()
        (src / "extension.js").write_text("x")
        rc, _ = run_cli("local", "--work-dir", self.work, "--source", str(src))
        self.assertEqual(rc, 0)
        row = mp.read_manifest(str(self.manifest))[0]
        self.assertEqual((row["extension_id"], row["status"]), ("myext", "ok"))
        self.assertEqual(row["source_dir"], str(src))

    def test_local_db_and_replace(self):
        db = helpers.make_db(self.dir / "sample-db")
        run_cli("local", "--work-dir", self.work, "--db", str(db), "--name", "s", "--version", "1")
        run_cli("local", "--work-dir", self.work, "--db", str(db), "--name", "s", "--version", "1")
        rows = mp.read_manifest(str(self.manifest))
        self.assertEqual(len(rows), 1)  # same id+version replaced, not duplicated
        self.assertEqual((rows[0]["status"], rows[0]["db_dir"]), ("db_ok", str(db)))

    def test_local_rejects_non_db(self):
        (self.dir / "plain").mkdir()
        rc, _ = run_cli("local", "--work-dir", self.work, "--db", str(self.dir / "plain"))
        self.assertEqual(rc, 1)
        self.assertFalse(self.manifest.exists())


class ScrapeTests(CliTestCase):
    def fake_download(self, e, dest_dir, **kw):
        if e["name"] == "broken":
            raise OSError("connection reset")
        files = {"extension/package.json": "{}"}
        if e["name"] != "theme":
            files["extension/out/main.js"] = "x"
        return helpers.make_vsix(os.path.join(dest_dir, mp.vsix_filename(e)), files)

    def test_scrape_statuses_and_resume(self):
        found = [ext("good"), ext("theme"), ext("broken")]
        with mock.patch.object(mp, "search_extensions", return_value=found) as s, \
                mock.patch.object(mp, "download_vsix", side_effect=self.fake_download) as d:
            rc, out = run_cli("scrape", "--work-dir", self.work, "--count", "3",
                              "--sort", "updated", "--query", "q")
        self.assertEqual(rc, 0)
        self.assertEqual(s.call_args[1]["sort_by"], "updated")
        self.assertEqual(d.call_count, 3)
        self.assertIn("[1/3] pub.good@1.0:", out)
        self.assertEqual(self.statuses(), {"pub.good": "ok", "pub.theme": "no_js",
                                           "pub.broken": "download_failed"})
        rows = {r["extension_id"]: r for r in mp.read_manifest(str(self.manifest))}
        self.assertIn("connection reset", rows["pub.broken"]["error"])
        self.assertTrue(Path(rows["pub.good"]["source_dir"], "out/main.js").is_file())

        # Resume: only the failed one is retried.
        with mock.patch.object(mp, "search_extensions", return_value=found), \
                mock.patch.object(mp, "download_vsix", side_effect=self.fake_download) as d:
            rc, out = run_cli("scrape", "--work-dir", self.work, "--count", "3")
        self.assertEqual(d.call_count, 1)
        self.assertIn("already scraped", out)

    def test_search_failure(self):
        with mock.patch.object(mp, "search_extensions", side_effect=OSError("offline")):
            rc, out = run_cli("scrape", "--work-dir", self.work, "--count", "1")
        self.assertEqual(rc, 1)
        self.assertIn("offline", out)


class BuildTests(CliTestCase):
    def setUp(self):
        super().setUp()
        rows = []
        for name in ("a", "b", "c"):
            src = self.dir / "src" / name
            src.mkdir(parents=True)
            rows.append(dict(ext(name), source_dir=str(src), status="ok"))
        rows.append(dict(ext("nojs"), status="no_js"))
        mp.write_manifest(rows, str(self.manifest))

    def fake_create(self, codeql, source_dir, db_dir, **kw):
        if source_dir.endswith("/b"):
            return False, "line1\nERROR: extraction failed\n"
        helpers.make_db(db_dir)
        return True, ""

    def test_build_continues_after_failure_and_resumes(self):
        with mock.patch.object(cq, "find_codeql", return_value="codeql"), \
                mock.patch.object(cq, "codeql_version", return_value="2.0"), \
                mock.patch.object(cq, "create_database", side_effect=self.fake_create) as c:
            rc, out = run_cli("build", "--work-dir", self.work, "--threads", "2")
        self.assertEqual(rc, 0)
        self.assertEqual(c.call_count, 3)
        self.assertEqual(c.call_args[1]["threads"], 2)
        self.assertEqual(self.statuses(), {"pub.a": "db_ok", "pub.b": "db_failed",
                                           "pub.c": "db_ok", "pub.nojs": "no_js"})
        rows = {r["extension_id"]: r for r in mp.read_manifest(str(self.manifest))}
        self.assertIn("extraction failed", rows["pub.b"]["error"])
        self.assertTrue(rows["pub.a"]["db_dir"].endswith("db/pub.a@1.0"))

        # Resume: built DBs are skipped, failed ones only with --retry-failed.
        with mock.patch.object(cq, "find_codeql", return_value="codeql"), \
                mock.patch.object(cq, "codeql_version", return_value="2.0"), \
                mock.patch.object(cq, "create_database", side_effect=self.fake_create) as c:
            run_cli("build", "--work-dir", self.work)
            self.assertEqual(c.call_count, 0)
            run_cli("build", "--work-dir", self.work, "--retry-failed")
            self.assertEqual(c.call_count, 1)

    def test_missing_codeql(self):
        with mock.patch.object(cq, "find_codeql", side_effect=cq.CodeQLError("no codeql")):
            rc, out = run_cli("build", "--work-dir", self.work)
        self.assertEqual(rc, 1)
        self.assertIn("no codeql", out)

    def test_empty_manifest(self):
        rc, _ = run_cli("build", "--work-dir", str(self.dir / "other"))
        self.assertEqual(rc, 1)


class QueryTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.dir / "UntrustIDE"
        for rel in ("dataflow/x-to-shell.ql", "terminal-addon/send.ql",
                    "terminal-addon/Lib.qll", "example.ql", "other/ignored.ql"):
            p = self.repo / "queries" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("select 1")
        db1 = helpers.make_db(self.dir / "db1")
        db2 = helpers.make_db(self.dir / "db2")
        mp.write_manifest([
            dict(ext("one"), status="db_ok", db_dir=str(db1)),
            dict(ext("two"), status="db_ok", db_dir=str(db2)),
            dict(ext("bad"), status="db_failed"),
        ], str(self.manifest))
        self.query_calls = []

    def fake_run_query(self, codeql, query, db_dir, out_bqrs, **kw):
        self.query_calls.append((Path(query).name, Path(db_dir).name))
        if Path(db_dir).name == "db2" and Path(query).name == "send.ql":
            return False, "ERROR: could not resolve module"
        Path(out_bqrs).write_bytes(b"bqrs")
        return True, ""

    def fake_decode(self, codeql, bqrs, out_csv, log_path=None):
        with open(out_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["call", "call__location", "col1"])
            w.writerow(["exec(c)", "/s/a.js:1:1:1:7", "hit in " + Path(bqrs).stem])
        return True, 1, ""

    def run_query_cmd(self, *extra):
        with mock.patch.object(cq, "find_codeql", return_value="codeql"), \
                mock.patch.object(cq, "codeql_version", return_value="2.0"), \
                mock.patch.object(cq, "run_query", side_effect=self.fake_run_query), \
                mock.patch.object(cq, "decode_bqrs", side_effect=self.fake_decode):
            return run_cli("query", "--work-dir", self.work,
                           "--untrustide-dir", str(self.repo), *extra)

    def test_query_pipeline(self):
        rc, out = self.run_query_cmd()
        self.assertEqual(rc, 0, out)
        # 2 default queries (other/ not a default dir, .qll/example.ql skipped) x 2 DBs
        self.assertEqual(len(self.query_calls), 4)
        self.assertIn("send.ql failed", out)
        raw = Path(self.work) / "raw"
        self.assertTrue((raw / "pub.two@1.0" / "terminal-addon__send.failed").is_file())
        self.assertFalse((raw / "pub.one@1.0" / "dataflow__x-to-shell.bqrs").exists())

        results = Path(self.work) / "results"
        with open(results / "results.csv", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 3)
        with open(results / "summary.csv", newline="") as f:
            summary = {r["extension_id"]: r for r in csv.DictReader(f)}
        self.assertEqual(summary["pub.one"]["total_results"], "2")
        self.assertEqual(summary["pub.two"]["terminal-addon/send.ql"], "")
        self.assertEqual(summary["pub.bad"]["db_status"], "failed")
        self.assertTrue((results / "query_totals.csv").is_file())

        # Resume: only the failed (ext, query) pair is retried.
        self.query_calls.clear()
        rc, out = self.run_query_cmd()
        self.assertEqual(self.query_calls, [("send.ql", "db2")])
        # --rerun re-runs everything
        self.query_calls.clear()
        self.run_query_cmd("--rerun")
        self.assertEqual(len(self.query_calls), 4)

    def test_explicit_queries(self):
        q = self.repo / "queries" / "other" / "ignored.ql"
        rc, out = self.run_query_cmd("--queries", str(q))
        self.assertEqual(rc, 0, out)
        self.assertEqual({c[0] for c in self.query_calls}, {"ignored.ql"})
        self.assertTrue((Path(self.work) / "raw" / "pub.one@1.0" / "other__ignored.csv").is_file())

    def test_missing_untrustide_dir(self):
        rc, out = run_cli("query", "--work-dir", self.work,
                          "--untrustide-dir", str(self.dir / "missing"))
        self.assertEqual(rc, 1)
        self.assertIn("no UntrustIDE query directories", out)


class RunTests(CliTestCase):
    def test_run_chains_steps(self):
        calls = []
        with mock.patch.object(cli, "cmd_scrape", side_effect=lambda a: calls.append("s") or 0), \
                mock.patch.object(cli, "_find_codeql", return_value="codeql"), \
                mock.patch.object(cli, "cmd_build",
                                  side_effect=lambda a, codeql: calls.append("b") or 0), \
                mock.patch.object(cli, "cmd_query",
                                  side_effect=lambda a, codeql: calls.append("q") or 0):
            rc, _ = run_cli("run", "--work-dir", self.work, "--count", "1")
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["s", "b", "q"])


if __name__ == "__main__":
    unittest.main()
