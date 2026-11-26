"""Command-line interface: ``python3 -m untrustide_scan <command> ...``.

Commands
    scrape  search the Marketplace, download + extract VSIX files
    local   add an extension source dir or an existing CodeQL DB to the manifest
    build   create a CodeQL database for every scraped/local extension
    query   run the UntrustIDE queries on every database and export CSVs
    run     scrape + build + query in one go

Work directory layout (default ``./work``)::

    vsix/<id>-<version>.vsix        downloaded packages
    src/<ext-dir>/extension/        extracted sources
    db/<ext-dir>/                   CodeQL databases (+ <ext-dir>.create.log)
    raw/<ext-dir>/<query-slug>.*    per-query bqrs / decoded csv / log / .failed
    results/manifest.csv            one row per extension, with its status
    results/results.csv             one row per query hit
    results/summary.csv             one row per extension, count per query
    results/query_totals.csv        one row per query

Every step is resumable: finished work is detected and skipped.  A failure
for one extension is recorded in the manifest and never aborts the run.
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

from . import __version__
from . import codeql as cq
from . import marketplace as mp
from . import results as res

# Manifest status values.
ST_DOWNLOAD_FAILED = "download_failed"
ST_EXTRACT_FAILED = "extract_failed"
ST_NO_JS = "no_js"
ST_OK = "ok"              # sources extracted, ready for `build`
ST_DB_OK = "db_ok"        # database ready, ready for `query`
ST_DB_FAILED = "db_failed"

# Statuses for which `scrape` has nothing left to do.
SCRAPE_DONE = {ST_OK, ST_NO_JS, ST_DB_OK, ST_DB_FAILED}

QUERY_SUBDIRS = ["dataflow", "source-and-sink", "terminal-addon", "envVar-addons"]

# The UntrustIDE repo is expected next to this package (repo root/UntrustIDE).
DEFAULT_UNTRUSTIDE_DIR = Path(__file__).resolve().parent.parent / "UntrustIDE"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def log(msg):
    print(msg, flush=True)


def warn(msg):
    print("  ! " + msg, file=sys.stderr, flush=True)


def short_error(err, limit=300):
    """One-line version of an exception or log text for the manifest."""
    text = str(err).strip().replace("\r", " ").replace("\n", " | ")
    return text if len(text) <= limit else text[:limit - 3] + "..."


def last_lines(text, n=5):
    lines = [l for l in (text or "").splitlines() if l.strip()]
    return " | ".join(lines[-n:])


def error_excerpt(text, n=2):
    """First ``ERROR`` line(s) of CodeQL output if any, else the last lines.

    CodeQL prints compile errors before trailing warnings, so the tail alone
    often shows only an irrelevant warning.
    """
    errors = [l.strip() for l in (text or "").splitlines()
              if l.lstrip().startswith("ERROR")]
    return " | ".join(errors[:n]) if errors else last_lines(text, n)


class Work:
    """Paths inside the work directory."""

    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.vsix = self.root / "vsix"
        self.src = self.root / "src"
        self.db = self.root / "db"
        self.raw = self.root / "raw"
        self.results = self.root / "results"
        self.manifest = self.results / "manifest.csv"

    def key(self, ext):
        return res.ext_dir_name(ext["extension_id"], ext.get("version", ""))

    def db_dir(self, ext):
        return Path(ext["db_dir"]) if ext.get("db_dir") else self.db / self.key(ext)

    def load(self):
        return mp.read_manifest(str(self.manifest)) if self.manifest.is_file() else []

    def save(self, exts):
        """Write the manifest atomically (safe against Ctrl-C mid-write)."""
        self.results.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".csv", dir=str(self.results))
        os.close(fd)
        try:
            mp.write_manifest(exts, tmp)
            os.chmod(tmp, 0o644)  # mkstemp creates 0600; match other outputs
            os.replace(tmp, str(self.manifest))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def merge_entry(exts, new):
    """Insert ``new`` into the manifest list, replacing an entry with the same
    extension id + version.  Returns the entry that is now in the list."""
    key = res.ext_dir_name(new["extension_id"], new.get("version", ""))
    for i, old in enumerate(exts):
        if res.ext_dir_name(old["extension_id"], old.get("version", "")) == key:
            exts[i] = new
            return new
    exts.append(new)
    return new


# --------------------------------------------------------------------------
# scrape
# --------------------------------------------------------------------------

def cmd_scrape(args):
    work = Work(args.work_dir)
    if args.delay is not None:
        mp.REQUEST_DELAY = args.delay
    log("Searching the Marketplace for %d extension(s) (sort=%s%s%s)..."
        % (args.count, args.sort,
           ", query=%r" % args.query if args.query else "",
           ", category=%r" % args.category if args.category else ""))
    try:
        found = mp.search_extensions(args.count, query=args.query,
                                     sort_by=args.sort, category=args.category)
    except Exception as e:  # noqa: BLE001
        warn("search failed: %s" % short_error(e))
        return 1
    log("Found %d extension(s)." % len(found))

    exts = work.load()
    existing = {work.key(e): e for e in exts}
    n = len(found)
    counts = {}
    for i, ext in enumerate(found, 1):
        key = work.key(ext)
        prefix = "[%d/%d] %s@%s:" % (i, n, ext["extension_id"], ext["version"])
        old = existing.get(key)
        if old and old.get("status") in SCRAPE_DONE:
            # Keep the old row (and its build status) but refresh metadata.
            for col in ("display_name", "install_count", "last_updated", "vsix_url"):
                old[col] = ext.get(col, old.get(col, ""))
            log("%s already scraped (%s), skipping" % (prefix, old["status"]))
            counts[old["status"]] = counts.get(old["status"], 0) + 1
            continue
        entry = merge_entry(exts, dict(ext, source_dir="", status="", db_dir="", error=""))
        scrape_one(work, entry, prefix)
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        work.save(exts)
    work.save(exts)
    log("Scrape done: %s. Manifest: %s" % (_fmt_counts(counts), work.manifest))
    return 0


def scrape_one(work, entry, prefix):
    """Download, extract and JS-filter one extension; sets entry status."""
    key = work.key(entry)
    try:
        log("%s downloading" % prefix)
        vsix = mp.download_vsix(entry, str(work.vsix))
    except Exception as e:  # noqa: BLE001
        entry["status"], entry["error"] = ST_DOWNLOAD_FAILED, short_error(e)
        warn("%s download failed: %s" % (prefix, entry["error"]))
        return
    try:
        log("%s extracting" % prefix)
        src = mp.extract_vsix(vsix, str(work.src / key))
    except Exception as e:  # noqa: BLE001
        entry["status"], entry["error"] = ST_EXTRACT_FAILED, short_error(e)
        warn("%s extract failed: %s" % (prefix, entry["error"]))
        return
    entry["source_dir"] = src
    if not mp.filter_has_js(src):
        entry["status"] = ST_NO_JS
        log("%s no JavaScript/TypeScript files, skipping" % prefix)
        return
    entry["status"], entry["error"] = ST_OK, ""
    log("%s ok" % prefix)


def _fmt_counts(counts):
    return ", ".join("%d %s" % (v, k) for k, v in sorted(counts.items())) or "nothing"


# --------------------------------------------------------------------------
# local
# --------------------------------------------------------------------------

def cmd_local(args):
    work = Work(args.work_dir)
    path = Path(args.db or args.source).expanduser().resolve()
    if not path.is_dir():
        warn("not a directory: %s" % path)
        return 1
    name = args.name or path.name
    entry = {
        "extension_id": name, "publisher": "local", "name": name,
        "display_name": name, "version": args.version or "",
        "install_count": "", "last_updated": "", "vsix_url": "",
        "source_dir": "", "status": "", "db_dir": "", "error": "",
    }
    if args.db:
        if not cq.database_is_finalized(path):
            warn("%s does not look like a finalized CodeQL database "
                 "(no codeql-database.yml / db-* dir)" % path)
            return 1
        entry["db_dir"] = str(path)
        entry["status"] = ST_DB_OK
    else:
        entry["source_dir"] = str(path)
        if mp.filter_has_js(str(path)):
            entry["status"] = ST_OK
        else:
            entry["status"] = ST_NO_JS
            warn("no .js/.ts files found outside node_modules; marked no_js")
    exts = work.load()
    merge_entry(exts, entry)
    work.save(exts)
    log("Added %s (%s) to %s" % (name, entry["status"], work.manifest))
    return 0


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def _find_codeql(args):
    try:
        path = cq.find_codeql(args.codeql)
    except cq.CodeQLError as e:
        warn(str(e))
        return None
    log("Using CodeQL: %s (%s)" % (path, cq.codeql_version(path) or "version unknown"))
    return path


def _timeout(seconds):
    return seconds if seconds and seconds > 0 else None


def cmd_build(args, codeql=None):
    work = Work(args.work_dir)
    exts = work.load()
    if not exts:
        warn("manifest is empty or missing (%s); run `scrape` or `local` first"
             % work.manifest)
        return 1
    wanted = {ST_OK, ST_DB_OK}
    if args.retry_failed:
        wanted.add(ST_DB_FAILED)
    todo = [e for e in exts if e.get("status") in wanted]
    if not todo:
        log("Nothing to build (no manifest entries with status %s)."
            % "/".join(sorted(wanted)))
        return 0
    codeql = codeql or _find_codeql(args)
    if not codeql:
        return 1

    n = len(todo)
    counts = {}
    for i, entry in enumerate(todo, 1):
        prefix = "[%d/%d] %s:" % (i, n, work.key(entry))
        build_one(work, entry, prefix, codeql, args)
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        work.save(exts)
    log("Build done: %s." % _fmt_counts(counts))
    return 0


def build_one(work, entry, prefix, codeql, args):
    db_dir = work.db_dir(entry)
    if entry.get("db_dir") and not entry.get("source_dir"):
        # A pre-built database added with `local --db`; nothing to build.
        ok = cq.database_is_finalized(db_dir)
        entry["status"] = ST_DB_OK if ok else ST_DB_FAILED
        entry["error"] = "" if ok else "database missing: %s" % db_dir
        log("%s pre-built database %s" % (prefix, "ok" if ok else "MISSING"))
        return
    if not args.overwrite and cq.database_is_finalized(db_dir):
        entry["status"], entry["db_dir"], entry["error"] = ST_DB_OK, str(db_dir), ""
        log("%s database exists, skipping" % prefix)
        return
    src = entry.get("source_dir", "")
    if not src or not Path(src).is_dir():
        entry["status"] = ST_DB_FAILED
        entry["error"] = "source dir missing: %s" % src
        warn("%s %s" % (prefix, entry["error"]))
        return
    log("%s creating database" % prefix)
    try:
        ok, text = cq.create_database(codeql, src, db_dir, threads=args.threads,
                                      timeout=_timeout(args.timeout),
                                      overwrite=args.overwrite,
                                      allow_minified=not args.skip_minified,
                                      ram=args.ram)
    except Exception as e:  # noqa: BLE001
        ok, text = False, str(e)
    if ok:
        entry["status"], entry["db_dir"], entry["error"] = ST_DB_OK, str(db_dir), ""
        log("%s database ok" % prefix)
    else:
        entry["status"] = ST_DB_FAILED
        entry["error"] = short_error(last_lines(text, 3))
        warn("%s database failed (see %s): %s"
             % (prefix, cq.database_log_path(db_dir), entry["error"]))


# --------------------------------------------------------------------------
# query
# --------------------------------------------------------------------------

def resolve_queries(args):
    """Return ``(queries_root, [query paths])`` from the CLI options."""
    root = Path(args.untrustide_dir or os.environ.get("UNTRUSTIDE_DIR")
                or DEFAULT_UNTRUSTIDE_DIR).expanduser().resolve()
    queries_root = root / "queries"
    if args.queries:
        paths = args.queries
    else:
        paths = [queries_root / d for d in QUERY_SUBDIRS
                 if (queries_root / d).is_dir()]
        if not paths:
            raise FileNotFoundError(
                "no UntrustIDE query directories found under %s; pass "
                "--untrustide-dir or --queries" % queries_root)
    return queries_root, cq.discover_queries(paths)


def cmd_query(args, codeql=None):
    work = Work(args.work_dir)
    try:
        queries_root, queries = resolve_queries(args)
    except FileNotFoundError as e:
        warn(str(e))
        return 1
    if not queries:
        warn("no .ql queries found")
        return 1
    rels = [cq.query_relpath(q, queries_root) for q in queries]
    exts = work.load()
    todo = [e for e in exts if e.get("status") == ST_DB_OK]
    log("%d quer%s, %d database(s) to analyze."
        % (len(queries), "y" if len(queries) == 1 else "ies", len(todo)))

    if todo:
        codeql = codeql or _find_codeql(args)
        if not codeql:
            return 1
        if args.install_packs:
            log("Installing query pack dependencies for %s" % queries_root)
            ok, text = cq.install_packs(codeql, queries_root)
            if not ok:
                warn("codeql pack install failed: %s" % short_error(last_lines(text)))
        additional = args.additional_packs
        n = len(todo)
        for i, entry in enumerate(todo, 1):
            prefix = "[%d/%d] %s:" % (i, n, work.key(entry))
            query_one(work, entry, prefix, codeql, queries, rels, args, additional)

    write_reports(work, exts, rels)
    return 0


def query_one(work, entry, prefix, codeql, queries, rels, args, additional):
    """Run every query against one database; failures become .failed markers."""
    db_dir = work.db_dir(entry)
    raw = work.raw / work.key(entry)
    raw.mkdir(parents=True, exist_ok=True)
    m = len(queries)
    done = failed = skipped = 0
    for j, (q, rel) in enumerate(zip(queries, rels), 1):
        slug = cq.query_slug(rel)
        out_csv = raw / (slug + ".csv")
        bqrs = raw / (slug + ".bqrs")
        marker = raw / (slug + res.FAILED_MARKER_SUFFIX)
        qlog = raw / (slug + ".log")
        if out_csv.is_file() and not args.rerun:
            skipped += 1
            continue
        for p in (out_csv, marker, qlog):
            if p.exists():
                p.unlink()
        log("%s query %d/%d %s" % (prefix, j, m, rel))
        try:
            ok, text = cq.run_query(codeql, q, db_dir, bqrs,
                                    timeout=_timeout(args.query_timeout),
                                    threads=args.threads,
                                    additional_packs=additional, log_path=qlog,
                                    ram=args.ram)
            if ok:
                ok, nrows, text = cq.decode_bqrs(codeql, bqrs, out_csv, log_path=qlog)
        except Exception as e:  # noqa: BLE001
            ok, text = False, str(e)
        if ok:
            done += 1
            if not args.keep_bqrs and bqrs.exists():
                bqrs.unlink()
        else:
            failed += 1
            if out_csv.exists():
                out_csv.unlink()
            marker.write_text(text or "failed\n", encoding="utf-8")
            warn("%s %s failed (see %s): %s"
                 % (prefix, rel, qlog, short_error(error_excerpt(text, 2))))
    log("%s %d ran, %d failed, %d already done" % (prefix, done, failed, skipped))


def write_reports(work, exts, rels):
    rows, counts = res.collect(work.raw, work.results / "results.csv")
    res.summarize(counts, work.results / "summary.csv",
                  work.results / "query_totals.csv",
                  manifest_rows=exts, queries=rels)
    log("Wrote %d result row(s) to %s" % (len(rows), work.results / "results.csv"))
    log("Summary: %s, totals: %s" % (work.results / "summary.csv",
                                     work.results / "query_totals.csv"))


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def cmd_run(args):
    rc = cmd_scrape(args)
    if rc != 0:
        return rc
    codeql = _find_codeql(args)
    if not codeql:
        return 1
    rc = cmd_build(args, codeql=codeql)
    if rc != 0:
        return rc
    return cmd_query(args, codeql=codeql)


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--work-dir", default="work", metavar="DIR",
                        help="working/output directory (default: ./work)")

    codeql_opts = argparse.ArgumentParser(add_help=False)
    codeql_opts.add_argument(
        "--codeql", metavar="PATH",
        help="path to the codeql executable (default: $CODEQL, then PATH, "
             "then the VS Code CodeQL extension's bundled CLI). Shell aliases "
             "are not visible to this tool.")
    codeql_opts.add_argument("--threads", type=int, default=0,
                             help="CodeQL --threads value (0 = one per core; default 0)")
    codeql_opts.add_argument("--ram", type=int, default=0, metavar="MB",
                             help="CodeQL --ram value in MB for builds and queries "
                                  "(0 = CodeQL's own default, which can be as low "
                                  "as 1800 MB; default 0)")

    scrape_opts = argparse.ArgumentParser(add_help=False)
    g = scrape_opts.add_argument_group("scrape options")
    g.add_argument("--count", type=int, required=True, metavar="N",
                   help="number of extensions to fetch")
    g.add_argument("--query", metavar="TEXT", help="search text (default: none)")
    g.add_argument("--sort", choices=sorted(mp.SORT_BY), default="installs",
                   help="sort order (default: installs)")
    g.add_argument("--category", metavar="C",
                   help='Marketplace category, e.g. "Programming Languages"')
    g.add_argument("--delay", type=float, metavar="SEC",
                   help="pause between HTTP requests (default: %.1f)" % mp.REQUEST_DELAY)

    build_opts = argparse.ArgumentParser(add_help=False)
    g = build_opts.add_argument_group("build options")
    g.add_argument("--timeout", type=int, default=1800, metavar="SEC",
                   help="max seconds per database build, 0 = none (default: 1800)")
    g.add_argument("--overwrite", action="store_true",
                   help="rebuild databases that already exist")
    g.add_argument("--retry-failed", action="store_true",
                   help="also retry extensions whose build failed before")
    g.add_argument("--skip-minified", action="store_true",
                   help="let CodeQL skip files that look minified (its default). "
                        "Without this flag they are analyzed, since most "
                        "extensions ship only bundled/minified JS")

    query_opts = argparse.ArgumentParser(add_help=False)
    g = query_opts.add_argument_group("query options")
    g.add_argument("--untrustide-dir", metavar="DIR",
                   help="UntrustIDE repo root; queries are taken from its "
                        "queries/{%s} (default: $UNTRUSTIDE_DIR or %s)"
                        % (",".join(QUERY_SUBDIRS), DEFAULT_UNTRUSTIDE_DIR))
    g.add_argument("--queries", nargs="+", metavar="PATH",
                   help="query files/directories to run instead of the defaults "
                        "(*.ql found recursively; *.qll and example.ql skipped)")
    g.add_argument("--query-timeout", type=int, default=1800, metavar="SEC",
                   help="max seconds per query per database, 0 = none (default: 1800)")
    g.add_argument("--rerun", action="store_true",
                   help="re-run queries that already have results")
    g.add_argument("--keep-bqrs", action="store_true",
                   help="keep the raw .bqrs files next to the decoded CSVs")
    g.add_argument("--install-packs", action="store_true",
                   help="run `codeql pack install` on the queries dir first")
    g.add_argument("--additional-packs", metavar="DIR",
                   help="passed to `codeql query run --additional-packs`")

    parser = argparse.ArgumentParser(
        prog="python3 -m untrustide_scan",
        description="Scrape VS Code Marketplace extensions, build CodeQL "
                    "databases and run the UntrustIDE queries on them, "
                    "exporting the results as CSV.",
        epilog="Example: python3 -m untrustide_scan run --count 20 "
               "--codeql ~/path/to/codeql")
    parser.add_argument("--version", action="version",
                        version="%(prog)s " + __version__)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("scrape", parents=[common, scrape_opts],
                       help="search, download and extract extensions",
                       description="Search the Marketplace, download and extract "
                                   "VSIX packages and write results/manifest.csv.")
    p.set_defaults(func=cmd_scrape)

    p = sub.add_parser("local", parents=[common],
                       help="add a local source dir or CodeQL DB to the manifest",
                       description="Add an already-extracted extension source dir "
                                   "(built by `build`) or an existing CodeQL database "
                                   "(used directly by `query`) to the manifest.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--source", metavar="DIR", help="extension source directory")
    src.add_argument("--db", metavar="DIR", help="existing CodeQL JavaScript database")
    p.add_argument("--name", help="extension id to use (default: directory name)")
    p.add_argument("--version", default="", help="version label (default: empty)")
    p.set_defaults(func=cmd_local)

    p = sub.add_parser("build", parents=[common, codeql_opts, build_opts],
                       help="create CodeQL databases",
                       description="Create a CodeQL database for every manifest "
                                   "entry with status ok.")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("query", parents=[common, codeql_opts, query_opts],
                       help="run queries and write the CSV reports",
                       description="Run every query on every database with status "
                                   "db_ok, then write results.csv, summary.csv and "
                                   "query_totals.csv.")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("run", parents=[common, codeql_opts, scrape_opts,
                                       build_opts, query_opts],
                       help="scrape + build + query in one go",
                       description="Run scrape, build and query in sequence.")
    p.set_defaults(func=cmd_run)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        warn("interrupted; progress so far is saved, re-run to resume")
        return 130
