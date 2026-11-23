"""VS Code Marketplace scraping: search, download and extract extensions.

Uses only the standard library. The public gallery API used here is the same
one the VS Code client uses:

    POST https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery
"""

import csv
import gzip
import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

GALLERY_URL = "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
VSPACKAGE_URL = ("https://marketplace.visualstudio.com/_apis/public/gallery/publishers/"
                 "{publisher}/vsextensions/{name}/{version}/vspackage")
USER_AGENT = "untrustide-scan/0.1"
VSIX_ASSET_TYPE = "Microsoft.VisualStudio.Services.VSIXPackage"

# Gallery API constants.
FILTER_CATEGORY = 5
FILTER_TARGET = 8
FILTER_SEARCH_TEXT = 10
FILTER_EXCLUDE_FLAGS = 12
EXCLUDE_NON_VALIDATED = "4096"
# IncludeFiles | IncludeVersionProperties | IncludeAssetUri
# | IncludeStatistics | IncludeLatestVersionOnly
QUERY_FLAGS = 914

SORT_BY = {
    "installs": 4,
    "rating": 12,
    "updated": 1,
    "name": 2,
}

# Seconds to sleep between HTTP requests (politeness). Can be overridden by
# callers, e.g. ``marketplace.REQUEST_DELAY = 0`` in tests.
REQUEST_DELAY = 1.0
MAX_RETRIES = 4
BACKOFF_BASE = 2.0  # seconds; doubles after every retry

MANIFEST_COLUMNS = [
    "extension_id", "publisher", "name", "display_name", "version",
    "install_count", "last_updated", "vsix_url", "source_dir", "status",
    "db_dir", "error",
]

JS_EXTENSIONS = (".js", ".ts", ".mjs", ".cjs")


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def _is_retryable(err):
    if isinstance(err, urllib.error.HTTPError):
        return err.code == 429 or 500 <= err.code < 600
    # URLError (connection reset, DNS hiccup) and timeouts.
    return isinstance(err, (urllib.error.URLError, TimeoutError, ConnectionError))


def _retry_after(err):
    """Seconds requested by a Retry-After header, or None."""
    if isinstance(err, urllib.error.HTTPError) and err.headers:
        value = err.headers.get("Retry-After")
        if value and value.isdigit():
            return int(value)
    return None


def _open(req, timeout, delay=None):
    """urlopen with a politeness delay and retry/backoff on 429/5xx.

    Returns the open response object (caller must close it).
    """
    delay = REQUEST_DELAY if delay is None else delay
    attempt = 0
    while True:
        if delay:
            time.sleep(delay)
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except Exception as err:  # noqa: BLE001 - filtered by _is_retryable
            attempt += 1
            if attempt > MAX_RETRIES or not _is_retryable(err):
                raise
            wait = _retry_after(err) or BACKOFF_BASE * (2 ** (attempt - 1))
            time.sleep(wait)


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

def _build_query_body(page_number, page_size, sort_by="installs",
                      query=None, category=None):
    """Return the JSON body (as a dict) for one extensionquery page."""
    if sort_by not in SORT_BY:
        raise ValueError("unknown sort_by %r (choose from %s)"
                         % (sort_by, ", ".join(sorted(SORT_BY))))
    criteria = [
        {"filterType": FILTER_TARGET, "value": "Microsoft.VisualStudio.Code"},
        {"filterType": FILTER_EXCLUDE_FLAGS, "value": EXCLUDE_NON_VALIDATED},
    ]
    if query:
        criteria.append({"filterType": FILTER_SEARCH_TEXT, "value": query})
    if category:
        criteria.append({"filterType": FILTER_CATEGORY, "value": category})
    return {
        "filters": [{
            "criteria": criteria,
            "pageNumber": page_number,
            "pageSize": page_size,
            "sortBy": SORT_BY[sort_by],
            "sortOrder": 0,
        }],
        "flags": QUERY_FLAGS,
    }


def _parse_extension(raw):
    """Convert one raw gallery extension object into a flat dict."""
    publisher = (raw.get("publisher") or {}).get("publisherName", "")
    name = raw.get("extensionName", "")
    versions = raw.get("versions") or [{}]
    latest = versions[0]
    version = latest.get("version", "")

    install_count = 0
    for stat in raw.get("statistics") or []:
        if stat.get("statisticName") == "install":
            install_count = int(stat.get("value") or 0)
            break

    vsix_url = None
    for f in latest.get("files") or []:
        if f.get("assetType") == VSIX_ASSET_TYPE and f.get("source"):
            vsix_url = f["source"]
            break
    if not vsix_url:
        vsix_url = VSPACKAGE_URL.format(publisher=publisher, name=name,
                                        version=version)

    return {
        "extension_id": "%s.%s" % (publisher, name),
        "publisher": publisher,
        "name": name,
        "display_name": raw.get("displayName", ""),
        "version": version,
        "install_count": install_count,
        "last_updated": raw.get("lastUpdated", ""),
        "vsix_url": vsix_url,
    }


def _parse_response(data):
    """Return the list of raw extension objects from an extensionquery reply."""
    results = data.get("results") or []
    if not results:
        return []
    return results[0].get("extensions") or []


def _query_page(body, timeout=60, delay=None):
    req = urllib.request.Request(
        GALLERY_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json;api-version=3.0-preview.1",
            "User-Agent": USER_AGENT,
        },
    )
    with _open(req, timeout, delay) as resp:
        return json.loads(resp.read().decode("utf-8"))


def search_extensions(count, query=None, sort_by="installs", page_size=100,
                      category=None, delay=None):
    """Search the marketplace and return up to ``count`` extension dicts."""
    if count <= 0:
        return []
    page_size = max(1, min(page_size, count))
    found = []
    seen = set()
    page = 1
    while len(found) < count:
        body = _build_query_body(page, page_size, sort_by, query, category)
        raw_exts = _parse_response(_query_page(body, delay=delay))
        for raw in raw_exts:
            ext = _parse_extension(raw)
            if ext["extension_id"] in seen:
                continue
            seen.add(ext["extension_id"])
            found.append(ext)
            if len(found) >= count:
                break
        if len(raw_exts) < page_size:
            break  # no more results
        page += 1
    return found


# --------------------------------------------------------------------------
# Download / extract
# --------------------------------------------------------------------------

def vsix_filename(ext):
    return "%s-%s.vsix" % (ext["extension_id"], ext["version"])


def download_vsix(ext, dest_dir, timeout=120, delay=None):
    """Download the extension's VSIX into dest_dir (cached). Returns its path."""
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, vsix_filename(ext))
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path

    req = urllib.request.Request(ext["vsix_url"], headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/octet-stream",
    })
    fd, tmp = tempfile.mkstemp(suffix=".part", dir=dest_dir)
    try:
        with os.fdopen(fd, "wb") as out, _open(req, timeout, delay) as resp:
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            shutil.copyfileobj(resp, out)
        with open(tmp, "rb") as f:
            magic = f.read(2)
        if encoding == "gzip" or magic == b"\x1f\x8b":
            _gunzip_in_place(tmp)
        if not zipfile.is_zipfile(tmp):
            raise ValueError("downloaded file for %s is not a valid VSIX"
                             % ext["extension_id"])
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def _gunzip_in_place(path):
    plain = path + ".gunzip"
    with gzip.open(path, "rb") as src, open(plain, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.replace(plain, path)


def _safe_member_path(name):
    """Return the relative path for a zip member under ``extension/``, or None
    if the member should be skipped. Raises ValueError on zip-slip attempts."""
    norm = name.replace("\\", "/")
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
        raise ValueError("absolute path in VSIX: %r" % name)
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError("path traversal in VSIX: %r" % name)
    if not parts or parts[0] != "extension":
        return None
    return os.path.join(*parts)


def extract_vsix(vsix_path, out_dir):
    """Extract the ``extension/`` folder of a VSIX into out_dir.

    Returns the path to ``out_dir/extension``. Skips work if already extracted.
    """
    ext_dir = os.path.join(out_dir, "extension")
    if os.path.isdir(ext_dir) and os.listdir(ext_dir):
        return ext_dir

    os.makedirs(out_dir, exist_ok=True)
    tmp_root = tempfile.mkdtemp(prefix=".extract-", dir=out_dir)
    try:
        with zipfile.ZipFile(vsix_path) as zf:
            for info in zf.infolist():
                rel = _safe_member_path(info.filename)
                if rel is None:
                    continue
                target = os.path.join(tmp_root, rel)
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                # Write regular files only; never create symlinks.
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        extracted = os.path.join(tmp_root, "extension")
        if not os.path.isdir(extracted):
            raise ValueError("VSIX has no extension/ folder: %s" % vsix_path)
        if os.path.isdir(ext_dir):
            shutil.rmtree(ext_dir)
        os.replace(extracted, ext_dir)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    return ext_dir


def filter_has_js(source_dir):
    """True if source_dir has a .js/.ts/.mjs/.cjs file outside node_modules."""
    for root, dirs, files in os.walk(source_dir):
        dirs[:] = [d for d in dirs if d != "node_modules"]
        for f in files:
            if f.lower().endswith(JS_EXTENSIONS):
                return True
    return False


# --------------------------------------------------------------------------
# Manifest CSV
# --------------------------------------------------------------------------

def write_manifest(exts, csv_path):
    """Write extension dicts to csv_path (missing columns left blank)."""
    parent = os.path.dirname(csv_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()
        for ext in exts:
            row = {col: ext.get(col, "") for col in MANIFEST_COLUMNS}
            for col in MANIFEST_COLUMNS:
                if row[col] is None:
                    row[col] = ""
            writer.writerow(row)


def read_manifest(csv_path):
    """Read a manifest written by write_manifest into a list of dicts."""
    exts = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ext = {col: row.get(col, "") or "" for col in MANIFEST_COLUMNS}
            try:
                ext["install_count"] = int(ext["install_count"] or 0)
            except ValueError:
                ext["install_count"] = 0
            exts.append(ext)
    return exts
