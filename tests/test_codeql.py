import csv
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
from untrustide_scan import codeql as cq


def fake_run_result(returncode=0, stdout="", timed_out=False):
    return cq.RunResult(["codeql"], returncode, stdout, "", timed_out, 0.1)


class DiscoverQueriesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "queries"
        for rel in ["dataflow/a.ql", "dataflow/lib.qll", "dataflow/sub/b.ql",
                    "terminal/c.ql", "example.ql", "terminal/example.ql",
                    "dataflow/.codeql/cached.ql", "notes.txt"]:
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("select 1")

    def tearDown(self):
        self.tmp.cleanup()

    def rels(self, found):
        return [cq.query_relpath(q, self.root) for q in found]

    def test_directory(self):
        found = cq.discover_queries([self.root])
        self.assertEqual(self.rels(found),
                         ["dataflow/a.ql", "dataflow/sub/b.ql", "terminal/c.ql"])
        self.assertTrue(all(q.is_absolute() for q in found))

    def test_files_and_dirs_deduplicated(self):
        found = cq.discover_queries([self.root / "dataflow", self.root / "dataflow/a.ql",
                                     self.root / "dataflow/lib.qll"])
        self.assertEqual(self.rels(found), ["dataflow/a.ql", "dataflow/sub/b.ql"])

    def test_single_path_string(self):
        self.assertEqual(len(cq.discover_queries(str(self.root / "terminal"))), 1)

    def test_missing_path(self):
        with self.assertRaises(FileNotFoundError):
            cq.discover_queries([self.root / "nope"])

    def test_slugs(self):
        self.assertEqual(cq.query_slug("dataflow/config-to-shell.ql"),
                         "dataflow__config-to-shell")
        self.assertEqual(cq.slug_to_query("dataflow__config-to-shell"),
                         "dataflow/config-to-shell.ql")
        outside = Path(self.tmp.name) / "x.ql"
        self.assertEqual(cq.query_relpath(outside, self.root), "x.ql")


class FindCodeqlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.exe = Path(self.tmp.name) / "codeql"
        self.exe.write_text("#!/bin/sh\n")
        self.exe.chmod(self.exe.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.tmp.cleanup()

    def test_explicit(self):
        self.assertEqual(cq.find_codeql(str(self.exe)), str(self.exe))

    def test_env(self):
        with mock.patch.dict(os.environ, {"CODEQL": str(self.exe)}):
            self.assertEqual(cq.find_codeql(), str(self.exe))

    def test_explicit_missing_is_error(self):
        with self.assertRaises(cq.CodeQLError):
            cq.find_codeql(str(self.exe) + "-missing")

    def test_nothing_found(self):
        env = {k: v for k, v in os.environ.items() if k != "CODEQL"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("shutil.which", return_value=None), \
                mock.patch.object(cq, "FALLBACK_CODEQL", str(self.exe) + "-missing"):
            with self.assertRaises(cq.CodeQLError):
                cq.find_codeql()


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reuses_finalized_db(self):
        db = helpers.make_db(self.dir / "db")
        with mock.patch.object(cq, "_run") as run:
            ok, _ = cq.create_database("codeql", self.dir, db)
        self.assertTrue(ok)
        run.assert_not_called()

    def test_in_progress_not_finalized(self):
        db = helpers.make_db(self.dir / "db")
        (db / "codeql-database.yml").write_text("inProgress:\n  x: 1\n")
        self.assertFalse(cq.database_is_finalized(db))

    def test_create_command(self):
        db = self.dir / "db" / "ext"

        def fake_run(cmd, timeout=None, cwd=None, log_path=None):
            helpers.make_db(db)
            return fake_run_result()

        with mock.patch.object(cq, "_run", side_effect=fake_run) as run:
            ok, _ = cq.create_database("codeql", self.dir / "src", db, threads=2, timeout=9)
        self.assertTrue(ok)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:4], ["codeql", "database", "create", str(db)])
        self.assertIn("--language=javascript", cmd)
        self.assertIn("--threads=2", cmd)
        self.assertNotIn("--overwrite", cmd)
        self.assertEqual(run.call_args[1]["timeout"], 9)

    def test_create_failure(self):
        with mock.patch.object(cq, "_run", return_value=fake_run_result(timed_out=True)):
            ok, _ = cq.create_database("codeql", self.dir, self.dir / "db")
        self.assertFalse(ok)

    def test_run_query_removes_partial_bqrs(self):
        out = self.dir / "raw" / "q.bqrs"

        def fake_run(cmd, timeout=None, cwd=None, log_path=None):
            out.write_bytes(b"partial")
            return fake_run_result(returncode=2)

        with mock.patch.object(cq, "_run", side_effect=fake_run) as run:
            ok, _ = cq.run_query("codeql", self.dir / "q.ql", self.dir / "db", out,
                                 additional_packs="/repo")
        self.assertFalse(ok)
        self.assertFalse(out.exists())
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[1:3], ["query", "run"])
        self.assertIn("--additional-packs", cmd)


class DecodeTests(unittest.TestCase):
    RESULT_SET = {
        "columns": [{"name": "call", "kind": "Entity"}, {"kind": "String"}],
        "tuples": [
            [{"id": 1, "label": "exec(cmd)",
              "url": {"uri": "file:///src/a.js", "startLine": 3, "startColumn": 5,
                      "endLine": 3, "endColumn": 13}}, "shell"],
            [{"id": 2, "label": "x"}, "other"],
        ],
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, path):
        with open(path, newline="") as f:
            return list(csv.reader(f))

    def check_csv(self, path):
        rows = self.read(path)
        self.assertEqual(rows[0], ["call", "call__location", "col1"])
        self.assertEqual(rows[1], ["exec(cmd)", "/src/a.js:3:5:3:13", "shell"])
        self.assertEqual(rows[2], ["x", "", "other"])

    def test_json_to_csv_direct_result_set(self):
        out = self.dir / "a.csv"
        self.assertEqual(cq._json_to_csv(self.RESULT_SET, "#select", out), 2)
        self.check_csv(out)

    def test_json_to_csv_keyed(self):
        out = self.dir / "a.csv"
        cq._json_to_csv({"#select": self.RESULT_SET}, "#select", out)
        self.check_csv(out)

    def test_format_location(self):
        self.assertEqual(cq.format_location("file:///a/b.js:1:2:3:4"), "/a/b.js:1:2:3:4")
        self.assertEqual(cq.format_location({"uri": "file:/a.js"}), "/a.js")
        self.assertEqual(cq.format_location(None), "")

    def test_decode_bqrs(self):
        info = json.dumps({"result-sets": [{"name": "edges"}, {"name": "#select"}]})
        calls = []

        def fake_run(cmd, timeout=None, cwd=None, log_path=None):
            calls.append(cmd)
            if cmd[1:3] == ["bqrs", "info"]:
                return fake_run_result(stdout=info)
            out = cmd[cmd.index("--output") + 1]
            Path(out).write_text(json.dumps(self.RESULT_SET))
            return fake_run_result()

        out = self.dir / "q.csv"
        with mock.patch.object(cq, "_run", side_effect=fake_run):
            ok, n, _ = cq.decode_bqrs("codeql", self.dir / "q.bqrs", out)
        self.assertTrue(ok)
        self.assertEqual(n, 2)
        self.assertIn("--result-set=#select", calls[1])
        self.check_csv(out)
        self.assertEqual(sorted(os.listdir(self.dir)), ["q.csv"])  # temp json removed

    def test_decode_bqrs_csv_fallback(self):
        def fake_run(cmd, timeout=None, cwd=None, log_path=None):
            if cmd[1:3] == ["bqrs", "info"]:
                return fake_run_result(stdout=json.dumps({"result-sets": [{"name": "#select"}]}))
            out = cmd[cmd.index("--output") + 1]
            if "--format=json" in cmd:
                Path(out).write_text("not json")
            else:
                Path(out).write_text("col0,col1\na,b\n")
            return fake_run_result()

        out = self.dir / "q.csv"
        with mock.patch.object(cq, "_run", side_effect=fake_run):
            ok, n, _ = cq.decode_bqrs("codeql", self.dir / "q.bqrs", out)
        self.assertTrue(ok)
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
