"""Thin wrappers around the CodeQL CLI.

Everything goes through ``_run`` (one subprocess helper, never ``shell=True``,
with timeout handling).  Queries are executed with ``codeql query run`` and
decoded from BQRS ourselves rather than via ``codeql database analyze``,
because some UntrustIDE queries have no ``@kind``/``@id`` metadata.

Decoded CSV format (written by ``decode_bqrs``)
-----------------------------------------------
One header row with the BQRS column names, then one row per result.  Entity
columns are rendered as their label; for every entity column an extra column
``<name>__location`` is appended right after it, holding
``/abs/path/file.js:startLine:startColumn:endLine:endColumn`` (empty if the
entity has no location).  Non-entity columns (strings, ints) are written as-is.
If JSON decoding fails we fall back to plain ``--format=csv`` output (no
``__location`` columns).
"""

import csv
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

FALLBACK_CODEQL = ("/home/penguin/.vscode-server-insiders/data/User/globalStorage/"
                   "github.vscode-codeql/distribution4/codeql/codeql")
LOCATION_SUFFIX = "__location"
SLUG_SEP = "__"


class CodeQLError(RuntimeError):
    pass


class RunResult:
    """Result of one CLI invocation."""

    def __init__(self, cmd, returncode, stdout, stderr, timed_out, elapsed):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.elapsed = elapsed

    @property
    def ok(self):
        return not self.timed_out and self.returncode == 0

    def log_text(self):
        parts = ["$ " + " ".join(str(c) for c in self.cmd)]
        if self.stdout:
            parts.append(self.stdout.rstrip())
        if self.stderr:
            parts.append(self.stderr.rstrip())
        if self.timed_out:
            parts.append("[untrustide-scan] TIMED OUT after %.0fs" % self.elapsed)
        else:
            parts.append("[untrustide-scan] exit code %s after %.1fs"
                         % (self.returncode, self.elapsed))
        return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------
# subprocess helper
# --------------------------------------------------------------------------

def _run(cmd, timeout=None, cwd=None, log_path=None):
    """Run ``cmd`` (a list) and return a RunResult.

    The child runs in its own session so that on timeout the whole process
    group (codeql launcher + JVM) is killed.  Never raises on non-zero exit or
    timeout; raises CodeQLError only if the executable cannot be started.
    """
    cmd = [str(c) for c in cmd]
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", start_new_session=True)
    except OSError as e:
        raise CodeQLError("could not execute %s: %s" % (cmd[0], e)) from e

    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        stdout, stderr = proc.communicate()
    result = RunResult(cmd, proc.returncode, stdout or "", stderr or "",
                       timed_out, time.monotonic() - start)
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(result.log_text())
    return result


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            proc.kill()


# --------------------------------------------------------------------------
# locating the CLI
# --------------------------------------------------------------------------

def find_codeql(explicit=None):
    """Return the path of the codeql executable.

    Order: explicit argument, $CODEQL, ``codeql`` on PATH, then the VS Code
    extension's bundled CLI (shell aliases are invisible to subprocesses).
    """
    candidates = []
    if explicit:
        candidates.append(("--codeql argument", explicit))
    if os.environ.get("CODEQL"):
        candidates.append(("$CODEQL", os.environ["CODEQL"]))
    on_path = shutil.which("codeql")
    if on_path:
        candidates.append(("PATH", on_path))
    candidates.append(("fallback", FALLBACK_CODEQL))

    for source, cand in candidates:
        path = os.path.expanduser(cand)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
        resolved = shutil.which(path)
        if resolved:
            return resolved
        if source != "fallback" and source != "PATH":
            # An explicitly requested binary that doesn't exist is an error,
            # not something to silently skip.
            raise CodeQLError("codeql executable from %s not found or not "
                              "executable: %s" % (source, cand))
    raise CodeQLError(
        "could not find the codeql CLI. Pass --codeql /path/to/codeql, set "
        "the CODEQL environment variable, or put codeql on PATH "
        "(shell aliases are not visible to this tool).")


def codeql_version(codeql):
    r = _run([codeql, "version", "--format=terse"], timeout=120)
    return r.stdout.strip() if r.ok else None


# --------------------------------------------------------------------------
# databases
# --------------------------------------------------------------------------

def database_is_finalized(db_dir):
    db_dir = Path(db_dir)
    yml = db_dir / "codeql-database.yml"
    if not yml.is_file():
        return False
    try:
        text = yml.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    # Unfinalized databases carry an "inProgress:" section.
    if "inProgress" in text:
        return False
    return any(p.is_dir() and p.name.startswith("db-") for p in db_dir.iterdir())


def database_log_path(db_dir):
    db_dir = Path(db_dir)
    return db_dir.parent / (db_dir.name + ".create.log")


def create_database(codeql, source_dir, db_dir, threads=0, timeout=None,
                    overwrite=False):
    """Build a JavaScript database for ``source_dir`` at ``db_dir``.

    Returns ``(ok, log_text)``.  Output is also appended to
    ``<db_dir>.create.log`` next to the database.  An existing finalized
    database is reused unless ``overwrite``.
    """
    db_dir = Path(db_dir)
    log_path = database_log_path(db_dir)
    if not overwrite and database_is_finalized(db_dir):
        return True, "database already exists: %s\n" % db_dir

    db_dir.parent.mkdir(parents=True, exist_ok=True)
    cmd = [codeql, "database", "create", str(db_dir),
           "--language=javascript", "--source-root", str(source_dir),
           "--threads=%d" % threads]
    # A leftover (unfinalized/partial) directory also needs --overwrite.
    if overwrite or db_dir.exists():
        cmd.append("--overwrite")
    if log_path.exists():
        log_path.unlink()
    r = _run(cmd, timeout=timeout, log_path=log_path)
    ok = r.ok and database_is_finalized(db_dir)
    return ok, r.log_text()


# --------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------

EXCLUDED_QUERY_NAMES = {"example.ql"}


def discover_queries(paths):
    """Expand files/directories into a sorted list of ``.ql`` query paths.

    Directories are searched recursively; ``*.qll`` and ``example.ql`` are
    skipped, as are hidden directories and ``.codeql`` pack caches.
    """
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    found = set()
    for p in paths:
        p = Path(p).expanduser().resolve()
        if p.is_file():
            if p.suffix == ".ql" and p.name not in EXCLUDED_QUERY_NAMES:
                found.add(p)
        elif p.is_dir():
            for q in p.rglob("*.ql"):
                rel_parts = q.relative_to(p).parts
                if any(part.startswith(".") for part in rel_parts[:-1]):
                    continue
                if q.is_file() and q.name not in EXCLUDED_QUERY_NAMES:
                    found.add(q)
        else:
            raise FileNotFoundError("query path not found: %s" % p)
    return sorted(found)


def query_relpath(query_path, queries_root):
    """Query path relative to the queries dir, posix style
    (e.g. ``dataflow/config-to-shell.ql``).  Falls back to the file name."""
    query_path = Path(query_path).resolve()
    if queries_root:
        try:
            return query_path.relative_to(Path(queries_root).resolve()).as_posix()
        except ValueError:
            pass
    return query_path.name


def query_slug(query_relpath_str):
    """``dataflow/config-to-shell.ql`` -> ``dataflow__config-to-shell``."""
    rel = query_relpath_str[:-3] if query_relpath_str.endswith(".ql") else query_relpath_str
    return rel.replace("/", SLUG_SEP)


def slug_to_query(slug):
    """Inverse of ``query_slug``."""
    return slug.replace(SLUG_SEP, "/") + ".ql"


def install_packs(codeql, queries_dir, timeout=None):
    """Run ``codeql pack install`` for the query pack (downloads
    codeql/javascript-all if needed).  Returns (ok, log_text)."""
    r = _run([codeql, "pack", "install", str(queries_dir)], timeout=timeout)
    return r.ok, r.log_text()


def run_query(codeql, query_path, db_dir, out_bqrs, timeout=None, threads=0,
              additional_packs=None, log_path=None):
    """``codeql query run`` one query against one database.

    Returns ``(ok, log_text)``.  On failure a partial BQRS is removed.
    The query is passed by absolute path so that pack/library resolution
    happens relative to its location inside the UntrustIDE repo.
    """
    out_bqrs = Path(out_bqrs)
    out_bqrs.parent.mkdir(parents=True, exist_ok=True)
    cmd = [codeql, "query", "run", str(Path(query_path).resolve()),
           "--database", str(db_dir), "--output", str(out_bqrs),
           "--threads=%d" % threads]
    if additional_packs:
        cmd += ["--additional-packs", str(additional_packs)]
    r = _run(cmd, timeout=timeout, log_path=log_path)
    ok = r.ok and out_bqrs.is_file()
    if not ok and out_bqrs.exists():
        out_bqrs.unlink()
    return ok, r.log_text()


def bqrs_result_sets(codeql, bqrs):
    """List of ``{"name", "rows", "columns"}`` dicts from ``bqrs info``."""
    r = _run([codeql, "bqrs", "info", "--format=json", str(bqrs)], timeout=300)
    if not r.ok:
        raise CodeQLError("bqrs info failed:\n" + r.log_text())
    data = json.loads(r.stdout)
    return data.get("result-sets") or data.get("resultSets") or []


def _pick_result_set(result_sets):
    names = [rs.get("name") for rs in result_sets]
    if "#select" in names:
        return "#select"
    return names[0] if names else None


def decode_bqrs(codeql, bqrs, out_csv, log_path=None):
    """Decode the ``#select`` result set (or the first one if absent) of
    ``bqrs`` into ``out_csv`` (format described in the module docstring).

    Returns ``(ok, n_rows, log_text)``.
    """
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    try:
        result_set = _pick_result_set(bqrs_result_sets(codeql, bqrs))
    except (CodeQLError, ValueError) as e:
        # Couldn't inspect: just try the conventional name.
        result_set = "#select"
        info_err = str(e)
    else:
        info_err = ""
    if result_set is None:
        # A query with no result sets at all: write an empty file.
        out_csv.write_text("", encoding="utf-8")
        return True, 0, "no result sets in %s\n" % bqrs

    # Preferred path: JSON decode so we can recover entity locations.
    fd, tmp_json = tempfile.mkstemp(suffix=".json", dir=str(out_csv.parent))
    os.close(fd)
    try:
        r = _run([codeql, "bqrs", "decode", "--format=json",
                  "--entities=all", "--result-set=" + result_set,
                  "--output", tmp_json, str(bqrs)],
                 timeout=600, log_path=log_path)
        if r.ok:
            try:
                with open(tmp_json, encoding="utf-8") as f:
                    data = json.load(f)
                n = _json_to_csv(data, result_set, out_csv)
                return True, n, info_err + r.log_text()
            except (ValueError, KeyError, TypeError, AttributeError,
                    StopIteration) as e:
                info_err += "JSON decode unusable (%s); falling back to CSV\n" % e
    finally:
        if os.path.exists(tmp_json):
            os.unlink(tmp_json)

    # Fallback: CodeQL's own CSV rendering.
    r = _run([codeql, "bqrs", "decode", "--format=csv",
              "--result-set=" + result_set, "--output", str(out_csv), str(bqrs)],
             timeout=600, log_path=log_path)
    if not r.ok:
        if out_csv.exists():
            out_csv.unlink()
        return False, 0, info_err + r.log_text()
    n = max(0, _count_csv_rows(out_csv) - 1)  # minus header
    return True, n, info_err + r.log_text()


def _count_csv_rows(path):
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in csv.reader(f))


# --------------------------------------------------------------------------
# JSON -> CSV conversion (pure; unit-testable)
# --------------------------------------------------------------------------

def format_location(url):
    """Render a BQRS entity URL as ``path:sl:sc:el:ec`` (or ``""``).

    Handles both the object form ``{"uri": "file:/x.js", "startLine": 1, ...}``
    and the string form ``file:///x.js:1:2:3:4``.
    """
    if not url:
        return ""
    if isinstance(url, dict):
        uri = url.get("uri", "")
        path = _strip_file_scheme(uri)
        nums = [url.get(k) for k in ("startLine", "startColumn", "endLine", "endColumn")]
        if nums[0] is None:
            return path
        return path + ":" + ":".join(str(n) for n in nums if n is not None)
    if isinstance(url, str):
        return _strip_file_scheme(url)
    return str(url)


def _strip_file_scheme(uri):
    if uri.startswith("file://"):
        return uri[len("file://"):]
    if uri.startswith("file:"):
        return uri[len("file:"):]
    return uri


def _json_to_csv(data, result_set, out_csv):
    """Write one result set of a ``bqrs decode --format=json`` document as CSV.
    Returns the number of result rows."""
    if isinstance(data, dict) and "tuples" in data:
        rs = data  # `--result-set=X` output: the result set object itself
    elif result_set in data:
        rs = data[result_set]
    else:
        rs = next(v for v in data.values() if isinstance(v, dict))
    columns = rs.get("columns", [])
    tuples = rs.get("tuples", [])
    names = [c.get("name") or "col%d" % i for i, c in enumerate(columns)]
    kinds = [str(c.get("kind", "")).lower() for c in columns]
    # A column is an entity column if declared so or any value is an object.
    is_entity = []
    for i, k in enumerate(kinds):
        ent = k in ("e", "entity")
        if not ent:
            ent = any(isinstance(t[i], dict) for t in tuples if i < len(t))
        is_entity.append(ent)

    header = []
    for name, ent in zip(names, is_entity):
        header.append(name)
        if ent:
            header.append(name + LOCATION_SUFFIX)

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for t in tuples:
            row = []
            for i, ent in enumerate(is_entity):
                v = t[i] if i < len(t) else ""
                if ent:
                    if isinstance(v, dict):
                        label = v.get("label")
                        if label is None:
                            label = v.get("id", "")
                        row += [str(label), format_location(v.get("url"))]
                    else:
                        row += ["" if v is None else str(v), ""]
                else:
                    row.append("" if v is None else str(v))
            w.writerow(row)
    return len(tuples)
