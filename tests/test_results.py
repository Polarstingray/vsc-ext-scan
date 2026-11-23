import csv
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (sets sys.path)
from untrustide_scan import results as res


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


class NamingTests(unittest.TestCase):
    def test_ext_dir_round_trip(self):
        name = res.ext_dir_name("pub.my-ext", "1.2.3")
        self.assertEqual(name, "pub.my-ext@1.2.3")
        self.assertEqual(res.parse_ext_dir_name(name), ("pub.my-ext", "1.2.3"))
        self.assertEqual(res.parse_ext_dir_name(res.ext_dir_name("local", "")), ("local", ""))

    def test_unsafe_characters(self):
        self.assertNotIn("/", res.ext_dir_name("../x", "1"))


class InterpretRowTests(unittest.TestCase):
    def test_placeholder_substitution(self):
        header = ["sink", "sink__location", "msg", "source", "source__location", "label"]
        row = ["exec(x)", "/a.js:1:1:1:5", "Flow from $@ to shell", "readFile()",
               "/a.js:9:1:9:9", "a file read"]
        message, location, details = res.interpret_row(header, row)
        self.assertEqual(message, "Flow from a file read to shell")
        self.assertEqual(location, "/a.js:1:1:1:5")
        self.assertIn("source__location=/a.js:9:1:9:9", details)
        self.assertNotIn("sink__location", details)

    def test_fallback_csv_location_regex(self):
        message, location, _ = res.interpret_row(
            ["col0", "col1"], ["file:///x/y.js:4:2:4:8", "found it"])
        self.assertEqual(message, "found it")
        self.assertEqual(location, "/x/y.js:4:2:4:8")


class CollectSummarizeTests(unittest.TestCase):
    QUERIES = ["dataflow/config-to-shell.ql", "terminal-addon/terminal-sendText.ql",
               "envVar-addons/envVar.ql"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        raw = self.dir / "raw"
        a = raw / "pub.alpha@1.0.0"
        write_csv(a / "dataflow__config-to-shell.csv", [
            ["call", "call__location", "col1"],
            ["exec(cmd)", "/s/a.js:3:5:3:13", "shell"],
            ["spawn(x)", "/s/b.js:7:1:7:9", "shell"],
        ])
        write_csv(a / "terminal-addon__terminal-sendText.csv",
                  [["t", "t__location", "col1"]])  # header only: zero hits
        (a / "envVar-addons__envVar.failed").write_text("boom")
        b = raw / "pub.beta@2.0.0"
        write_csv(b / "terminal-addon__terminal-sendText.csv", [
            ["t", "t__location", "col1"],
            ["sendText(c)", "/s/t.js:1:1:1:9", "terminal"],
        ])
        (b / "empty.csv").write_text("")  # completely empty decode
        self.manifest = [
            {"extension_id": "pub.alpha", "version": "1.0.0", "install_count": 100,
             "status": "db_ok"},
            {"extension_id": "pub.beta", "version": "2.0.0", "install_count": 50,
             "status": "db_ok"},
            {"extension_id": "pub.gamma", "version": "3.0.0", "install_count": 7,
             "status": "db_failed"},
            {"extension_id": "pub.delta", "version": "0.1", "install_count": 1,
             "status": "no_js"},
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect(self):
        out = self.dir / "results" / "results.csv"
        rows, counts = res.collect(self.dir / "raw", out)
        self.assertEqual(len(rows), 3)
        self.assertEqual(counts["pub.alpha@1.0.0"], {
            "dataflow/config-to-shell.ql": 2,
            "terminal-addon/terminal-sendText.ql": 0,
            "envVar-addons/envVar.ql": None,
        })
        self.assertEqual(counts["pub.beta@2.0.0"]["terminal-addon/terminal-sendText.ql"], 1)
        written = read_csv(out)
        self.assertEqual(list(written[0]), res.RESULTS_COLUMNS)
        first = written[0]
        self.assertEqual(first["extension_id"], "pub.alpha")
        self.assertEqual(first["version"], "1.0.0")
        self.assertEqual(first["query"], "dataflow/config-to-shell.ql")
        self.assertEqual(first["query_category"], "dataflow")
        self.assertEqual(first["message"], "shell")
        self.assertEqual(first["location"], "/s/a.js:3:5:3:13")
        self.assertEqual(first["result_index"], "0")

    def test_collect_missing_dir(self):
        rows, counts = res.collect(self.dir / "nope")
        self.assertEqual((rows, counts), ([], {}))

    def test_summarize(self):
        _, counts = res.collect(self.dir / "raw")
        summary_csv = self.dir / "summary.csv"
        totals_csv = self.dir / "query_totals.csv"
        res.summarize(counts, summary_csv, totals_csv,
                      manifest_rows=self.manifest, queries=self.QUERIES)

        summary = {r["extension_id"]: r for r in read_csv(summary_csv)}
        self.assertEqual(list(read_csv(summary_csv)[0]),
                         ["extension_id", "version", "install_count", "db_status"]
                         + self.QUERIES + ["total_results"])
        alpha = summary["pub.alpha"]
        self.assertEqual(alpha["db_status"], "ok")
        self.assertEqual(alpha["install_count"], "100")
        self.assertEqual(alpha["dataflow/config-to-shell.ql"], "2")
        self.assertEqual(alpha["terminal-addon/terminal-sendText.ql"], "0")
        self.assertEqual(alpha["envVar-addons/envVar.ql"], "")  # failed -> blank
        self.assertEqual(alpha["total_results"], "2")
        self.assertEqual(summary["pub.beta"]["total_results"], "1")
        self.assertEqual(summary["pub.gamma"]["db_status"], "failed")
        self.assertEqual(summary["pub.gamma"]["total_results"], "")
        self.assertEqual(summary["pub.delta"]["db_status"], "skipped")

        totals = {r["query"]: r for r in read_csv(totals_csv)}
        self.assertEqual(totals["dataflow/config-to-shell.ql"],
                         {"query": "dataflow/config-to-shell.ql", "extensions_run": "1",
                          "extensions_with_hits": "1", "total_results": "2"})
        self.assertEqual(totals["terminal-addon/terminal-sendText.ql"]["extensions_run"], "2")
        self.assertEqual(totals["terminal-addon/terminal-sendText.ql"]["extensions_with_hits"], "1")
        self.assertEqual(totals["envVar-addons/envVar.ql"]["extensions_run"], "0")

    def test_summarize_without_manifest(self):
        _, counts = res.collect(self.dir / "raw")
        summary_rows, _ = res.summarize(counts, self.dir / "s.csv", self.dir / "t.csv")
        self.assertEqual({r["extension_id"] for r in summary_rows}, {"pub.alpha", "pub.beta"})


if __name__ == "__main__":
    unittest.main()
