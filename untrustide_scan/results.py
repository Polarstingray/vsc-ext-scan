"""Combine per-extension, per-query decoded CSVs into analysis-friendly tables.

Expected layout (produced by the pipeline)::

    work/raw/<ext-dir>/<query-slug>.csv     decoded results (see codeql.py)
    work/raw/<ext-dir>/<query-slug>.failed  optional marker: query run/decode failed

``<ext-dir>`` is ``ext_dir_name(extension_id, version)`` and ``<query-slug>``
is ``codeql.query_slug(relpath)`` (``dataflow/config-to-shell.ql`` ->
``dataflow__config-to-shell``).

Outputs:
    results.csv       long format, one row per query hit
    summary.csv       one row per extension, one count column per query
    query_totals.csv  one row per query
"""

import csv
import re
from pathlib import Path

from .codeql import LOCATION_SUFFIX, slug_to_query

RESULTS_COLUMNS = ["extension_id", "version", "query", "query_category",
                   "result_index", "message", "location", "details"]
TOTALS_COLUMNS = ["query", "extensions_run", "extensions_with_hits", "total_results"]
FAILED_MARKER_SUFFIX = ".failed"

# "/abs/file.js:1:2:3:4" or "file:///abs/file.js:1:2" inside fallback CSV cells.
_LOC_RE = re.compile(r"((?:file:)?/[^\s:,;\"']+):(\d+)(?::(\d+))?(?::(\d+):(\d+))?")


# --------------------------------------------------------------------------
# naming helpers
# --------------------------------------------------------------------------

def ext_dir_name(extension_id, version):
    """Directory name for one extension version: ``publisher.name@1.2.3``.

    ``@`` cannot appear in extension ids or versions, so the name can be
    parsed back unambiguously.
    """
    raw = "%s@%s" % (extension_id, version) if version else str(extension_id)
    return re.sub(r"[^A-Za-z0-9._@+-]", "_", raw)


def parse_ext_dir_name(name):
    """Inverse of ``ext_dir_name``: returns ``(extension_id, version)``."""
    ext_id, sep, version = name.rpartition("@")
    if not sep:
        return name, ""
    return ext_id, version


def query_category(query_rel):
    """Top-level folder of a query relpath (``dataflow``), or ``""``."""
    parts = query_rel.split("/")
    return parts[0] if len(parts) > 1 else ""


# --------------------------------------------------------------------------
# row interpretation (pure; unit-testable)
# --------------------------------------------------------------------------

def _substitute_placeholders(message, labels):
    """Replace successive ``$@`` with the given labels (like VS Code does)."""
    if "$@" not in message or not labels:
        return message
    it = iter(labels)

    def repl(m):
        try:
            return next(it)
        except StopIteration:
            return m.group(0)
    return re.sub(r"\$@", repl, message)


def interpret_row(header, row):
    """Return ``(message, location, details)`` for one decoded result row."""
    row = list(row) + [""] * (len(header) - len(row))
    pairs = list(zip(header, row))
    loc_cols = {h for h in header if h.endswith(LOCATION_SUFFIX)}
    entity_cols = {h[:-len(LOCATION_SUFFIX)] for h in loc_cols}

    # location: first non-empty __location column, else regex over all cells.
    location = ""
    location_col = None
    for h, v in pairs:
        if h in loc_cols and v:
            location, location_col = v, h
            break
    if not location:
        for _, v in pairs:
            m = _LOC_RE.search(v or "")
            if m:
                location = m.group(0)
                if location.startswith("file:"):
                    location = re.sub(r"^file:(//)?", "", location)
                break

    # message
    if loc_cols:
        # Structured decode: message = first non-entity column, preferring one
        # with $@ placeholders.
        plain = [(i, h, v) for i, (h, v) in enumerate(pairs)
                 if h not in loc_cols and h not in entity_cols]
    else:
        # Fallback CodeQL CSV: we can't tell kinds apart; skip column 0 (the
        # alert element) when there is more than one column.
        plain = [(i, h, v) for i, (h, v) in enumerate(pairs)
                 if i > 0 or len(pairs) == 1]
    msg_idx = None
    for i, _, v in plain:
        if "$@" in (v or ""):
            msg_idx = i
            break
    if msg_idx is None and plain:
        msg_idx = plain[0][0]

    message = ""
    if msg_idx is not None:
        message = pairs[msg_idx][1]
        # Labels for $@ are the plain (string) columns after the message.
        labels = [v for i, _, v in plain if i > msg_idx]
        message = _substitute_placeholders(message, labels)

    details = "; ".join("%s=%s" % (h, v) for i, (h, v) in enumerate(pairs)
                        if i != msg_idx and v != "" and h != location_col)
    return message, location, details


def read_decoded_csv(path):
    """Return ``(header, rows)`` of a decoded CSV (empty file -> ([], []))."""
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return [], []
        return header, [r for r in reader if r]


# --------------------------------------------------------------------------
# collect / summarize
# --------------------------------------------------------------------------

def _write_csv(path, columns, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def collect(raw_root, out_csv=None):
    """Walk ``raw_root/<ext-dir>/<query-slug>.csv`` and build the long table.

    Writes ``out_csv`` (if given) with ``RESULTS_COLUMNS`` and returns
    ``(rows, counts)`` where ``counts[ext_dir][query_rel]`` is the number of
    results, or ``None`` if the query failed for that extension (a
    ``<slug>.failed`` marker without a CSV).
    """
    raw_root = Path(raw_root)
    rows, counts = [], {}
    if raw_root.is_dir():
        for ext_dir in sorted(p for p in raw_root.iterdir() if p.is_dir()):
            ext_id, version = parse_ext_dir_name(ext_dir.name)
            ext_counts = counts.setdefault(ext_dir.name, {})
            for marker in sorted(ext_dir.glob("*" + FAILED_MARKER_SUFFIX)):
                slug = marker.name[:-len(FAILED_MARKER_SUFFIX)]
                if not (ext_dir / (slug + ".csv")).exists():
                    ext_counts[slug_to_query(slug)] = None
            for csv_path in sorted(ext_dir.glob("*.csv")):
                query_rel = slug_to_query(csv_path.stem)
                header, data = read_decoded_csv(csv_path)
                ext_counts[query_rel] = len(data)
                for idx, r in enumerate(data):
                    message, location, details = interpret_row(header, r)
                    rows.append({
                        "extension_id": ext_id,
                        "version": version,
                        "query": query_rel,
                        "query_category": query_category(query_rel),
                        "result_index": idx,
                        "message": message,
                        "location": location,
                        "details": details,
                    })
    if out_csv:
        _write_csv(out_csv, RESULTS_COLUMNS, rows)
    return rows, counts


def _db_status(manifest_row, has_counts):
    status = (manifest_row or {}).get("status", "") or ""
    if status in ("db_ok", "analyzed", "ok"):
        return "ok"
    if status in ("db_failed", "failed"):
        return "failed"
    if has_counts:
        return "ok"
    return "skipped"


def summarize(counts, summary_csv, totals_csv, manifest_rows=None, queries=None):
    """Write ``summary.csv`` and ``query_totals.csv``.

    ``counts``: as returned by ``collect``.
    ``manifest_rows``: rows from ``marketplace.read_manifest`` (used for
    install_count and db status; ``status`` values db_ok/analyzed -> ok,
    db_failed -> failed, anything else -> skipped).
    ``queries``: query relpaths for the column order; defaults to every
    query seen in ``counts``.

    Per-query cells are the hit count, or empty if the query was not run or
    failed for that extension.  Returns ``(summary_rows, totals_rows)``.
    """
    manifest_rows = manifest_rows or []
    if queries is None:
        queries = sorted({q for c in counts.values() for q in c})
    else:
        queries = list(queries)

    by_dir = {}
    order = []
    for m in manifest_rows:
        key = ext_dir_name(m.get("extension_id", ""), m.get("version", ""))
        if key not in by_dir:
            order.append(key)
        by_dir[key] = m
    for key in sorted(counts):
        if key not in by_dir:
            order.append(key)

    summary_rows = []
    totals = {q: {"query": q, "extensions_run": 0, "extensions_with_hits": 0,
                  "total_results": 0} for q in queries}
    for key in order:
        m = by_dir.get(key)
        ext_counts = counts.get(key, {})
        if m:
            ext_id, version = m.get("extension_id", ""), m.get("version", "")
        else:
            ext_id, version = parse_ext_dir_name(key)
        db_status = _db_status(m, bool(ext_counts))
        row = {"extension_id": ext_id, "version": version,
               "install_count": (m or {}).get("install_count", ""),
               "db_status": db_status}
        total = 0
        for q in queries:
            n = ext_counts.get(q)
            if n is None or db_status != "ok":
                row[q] = ""
                continue
            row[q] = n
            total += n
            totals[q]["extensions_run"] += 1
            totals[q]["total_results"] += n
            if n > 0:
                totals[q]["extensions_with_hits"] += 1
        row["total_results"] = total if db_status == "ok" else ""
        summary_rows.append(row)

    columns = (["extension_id", "version", "install_count", "db_status"]
               + queries + ["total_results"])
    _write_csv(summary_csv, columns, summary_rows)
    totals_rows = [totals[q] for q in queries]
    _write_csv(totals_csv, TOTALS_COLUMNS, totals_rows)
    return summary_rows, totals_rows
