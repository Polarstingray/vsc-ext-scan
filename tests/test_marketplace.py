import gzip
import io
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import helpers
from untrustide_scan import marketplace as mp


class QueryBodyTests(unittest.TestCase):
    def test_default_body(self):
        body = mp._build_query_body(2, 50)
        f = body["filters"][0]
        self.assertEqual(f["pageNumber"], 2)
        self.assertEqual(f["pageSize"], 50)
        self.assertEqual(f["sortBy"], mp.SORT_BY["installs"])
        self.assertEqual(body["flags"], mp.QUERY_FLAGS)
        types = {c["filterType"]: c["value"] for c in f["criteria"]}
        self.assertEqual(types[mp.FILTER_TARGET], "Microsoft.VisualStudio.Code")
        self.assertNotIn(mp.FILTER_SEARCH_TEXT, types)
        self.assertNotIn(mp.FILTER_CATEGORY, types)

    def test_query_and_category(self):
        body = mp._build_query_body(1, 10, "rating", query="git", category="SCM Providers")
        f = body["filters"][0]
        types = {c["filterType"]: c["value"] for c in f["criteria"]}
        self.assertEqual(types[mp.FILTER_SEARCH_TEXT], "git")
        self.assertEqual(types[mp.FILTER_CATEGORY], "SCM Providers")
        self.assertEqual(f["sortBy"], mp.SORT_BY["rating"])

    def test_bad_sort(self):
        with self.assertRaises(ValueError):
            mp._build_query_body(1, 10, "popularity")


class ParseTests(unittest.TestCase):
    def test_parse_extension(self):
        ext = mp._parse_extension(helpers.raw_extension("pub", "tool", "1.2.3", installs=42))
        self.assertEqual(ext["extension_id"], "pub.tool")
        self.assertEqual(ext["version"], "1.2.3")
        self.assertEqual(ext["install_count"], 42)
        self.assertEqual(ext["display_name"], "Tool")
        self.assertEqual(ext["vsix_url"], "https://cdn.example/pub.tool/1.2.3.vsix")

    def test_parse_extension_fallback_url(self):
        raw = helpers.raw_extension("pub", "tool", "0.1.0", with_files=False)
        ext = mp._parse_extension(raw)
        self.assertIn("/publishers/pub/vsextensions/tool/0.1.0/vspackage", ext["vsix_url"])

    def test_parse_response(self):
        data = helpers.fake_response([helpers.raw_extension("a", "b", "1")])
        self.assertEqual(len(mp._parse_response(data)), 1)
        self.assertEqual(mp._parse_response({"results": []}), [])
        self.assertEqual(mp._parse_response({}), [])


class SearchTests(unittest.TestCase):
    def test_paginates_dedupes_and_caps(self):
        page1 = helpers.fake_response([helpers.raw_extension("p", "a", "1"),
                                       helpers.raw_extension("p", "b", "1")])
        page2 = helpers.fake_response([helpers.raw_extension("p", "b", "1"),  # dup
                                       helpers.raw_extension("p", "c", "1")])
        bodies = []

        def fake_query(body, timeout=60, delay=None):
            bodies.append(body)
            return [page1, page2][body["filters"][0]["pageNumber"] - 1]

        with mock.patch.object(mp, "_query_page", side_effect=fake_query):
            found = mp.search_extensions(3, page_size=2, query="x")
        self.assertEqual([e["extension_id"] for e in found], ["p.a", "p.b", "p.c"])
        self.assertEqual(len(bodies), 2)

    def test_stops_on_short_page(self):
        page = helpers.fake_response([helpers.raw_extension("p", "a", "1")])
        with mock.patch.object(mp, "_query_page", return_value=page) as q:
            found = mp.search_extensions(10, page_size=5)
        self.assertEqual(len(found), 1)
        self.assertEqual(q.call_count, 1)

    def test_zero_count(self):
        with mock.patch.object(mp, "_query_page") as q:
            self.assertEqual(mp.search_extensions(0), [])
        q.assert_not_called()


class RetryTests(unittest.TestCase):
    def test_retries_on_429(self):
        err = urllib.error.HTTPError("u", 429, "Too Many", {}, None)
        ok = object()
        with mock.patch("urllib.request.urlopen", side_effect=[err, ok]) as u, \
                mock.patch("time.sleep") as sleep:
            self.assertIs(mp._open("req", 5, delay=0), ok)
        self.assertEqual(u.call_count, 2)
        sleep.assert_called()

    def test_no_retry_on_404(self):
        err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err) as u, \
                mock.patch("time.sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                mp._open("req", 5, delay=0)
        self.assertEqual(u.call_count, 1)


class _FakeResp(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        src = helpers.make_vsix(os.path.join(self.dir, "src", "x.zip"),
                                {"extension/package.json": "{}"})
        with open(src, "rb") as f:
            self.zip_bytes = f.read()
        self.ext = {"extension_id": "p.a", "version": "1.0", "vsix_url": "https://x/y"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_download_and_cache(self):
        out = os.path.join(self.dir, "vsix")
        with mock.patch.object(mp, "_open", return_value=_FakeResp(self.zip_bytes)) as o:
            path = mp.download_vsix(self.ext, out)
            self.assertEqual(os.path.basename(path), "p.a-1.0.vsix")
            mp.download_vsix(self.ext, out)  # cached
        self.assertEqual(o.call_count, 1)
        self.assertEqual(os.listdir(out), ["p.a-1.0.vsix"])

    def test_gzip_body(self):
        resp = _FakeResp(gzip.compress(self.zip_bytes), {"Content-Encoding": "gzip"})
        with mock.patch.object(mp, "_open", return_value=resp):
            path = mp.download_vsix(self.ext, self.dir)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), self.zip_bytes)

    def test_not_a_zip(self):
        out = os.path.join(self.dir, "vsix")
        with mock.patch.object(mp, "_open", return_value=_FakeResp(b"<html>")):
            with self.assertRaises(ValueError):
                mp.download_vsix(self.ext, out)
        self.assertEqual(os.listdir(out), [])  # no partial files left


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_extracts_only_extension_folder(self):
        vsix = helpers.make_vsix(str(self.dir / "a.vsix"), {
            "extension.vsixmanifest": "<xml/>",
            "[Content_Types].xml": "<xml/>",
            "extension/package.json": "{}",
            "extension/out/main.js": "console.log(1)",
        })
        out = mp.extract_vsix(vsix, str(self.dir / "out"))
        self.assertEqual(out, str(self.dir / "out" / "extension"))
        self.assertTrue((self.dir / "out/extension/out/main.js").is_file())
        self.assertEqual(sorted(os.listdir(self.dir / "out")), ["extension"])

    def _assert_rejected(self, member):
        vsix = helpers.make_vsix(str(self.dir / "evil.vsix"), {
            "extension/package.json": "{}",
            member: "pwned",
        })
        with self.assertRaises(ValueError):
            mp.extract_vsix(vsix, str(self.dir / "out"))
        self.assertFalse((self.dir / "evil.txt").exists())
        self.assertFalse((self.dir / "out" / "extension").exists())
        # temporary extraction dir cleaned up
        self.assertEqual(os.listdir(self.dir / "out"), [])

    def test_zip_slip_dotdot(self):
        self._assert_rejected("extension/../../evil.txt")

    def test_zip_slip_absolute(self):
        self._assert_rejected("/tmp/evil.txt")

    def test_zip_slip_backslash(self):
        self._assert_rejected("extension\\..\\..\\evil.txt")

    def test_no_extension_folder(self):
        vsix = helpers.make_vsix(str(self.dir / "a.vsix"), {"readme.md": "x"})
        with self.assertRaises(ValueError):
            mp.extract_vsix(vsix, str(self.dir / "out"))

    def test_filter_has_js(self):
        d = self.dir / "src"
        (d / "node_modules/dep").mkdir(parents=True)
        (d / "node_modules/dep/index.js").write_text("x")
        (d / "package.json").write_text("{}")
        self.assertFalse(mp.filter_has_js(str(d)))
        (d / "lib").mkdir()
        (d / "lib/main.ts").write_text("x")
        self.assertTrue(mp.filter_has_js(str(d)))


class ManifestTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "manifest.csv")
            mp.write_manifest([{"extension_id": "p.a", "version": "1", "install_count": 5,
                                "status": "ok", "db_dir": None}], path)
            rows = mp.read_manifest(path)
        self.assertEqual(rows[0]["extension_id"], "p.a")
        self.assertEqual(rows[0]["install_count"], 5)
        self.assertEqual(rows[0]["db_dir"], "")
        self.assertEqual(set(rows[0]), set(mp.MANIFEST_COLUMNS))


if __name__ == "__main__":
    unittest.main()
