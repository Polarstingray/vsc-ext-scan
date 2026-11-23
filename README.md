# Extension scraper and automated querying for UntrustIDE add-ons

`untrustide_scan` is a small command-line tool that:

1. searches the VS Code Marketplace and downloads extensions (`.vsix`),
2. builds a CodeQL JavaScript database for each one,
3. runs the UntrustIDE queries (ours and the originals) on every database,
4. writes the results to CSV files you can use for graphs and analysis.

See [docs/next-steps.md](docs/next-steps.md) for why we need it.

## Requirements

- Python 3.8 or newer. It uses only the standard library, so there is nothing to `pip install`.
- The [CodeQL CLI](https://github.com/github/codeql-cli-binaries). The one bundled
  with the VS Code CodeQL extension works.
- The UntrustIDE repo in `./UntrustIDE`. Its `ql/` folder provides the
  `codeql/javascript-all` library, so you don't need to download any packs.

### Pointing the tool at CodeQL

A shell `alias codeql=...` **does not work** here, because aliases only exist in
your interactive shell and are invisible to the processes this tool starts. Pick one:

```bash
# 1. pass it each time
python3 -m untrustide_scan build --codeql /path/to/codeql

# 2. or export it once (e.g. in ~/.bashrc, next to the alias)
export CODEQL=/home/penguin/.vscode-server-insiders/data/User/globalStorage/github.vscode-codeql/distribution4/codeql/codeql
```

If you set neither, the tool tries `codeql` on `PATH` and then the VS Code
extension's bundled CLI at the path above.

## Usage

Run every command from the repository root:

```bash
python3 -m untrustide_scan --help
python3 -m untrustide_scan <command> --help
```

| Command  | What it does |
|----------|--------------|
| `scrape` | Searches the Marketplace, then downloads and extracts the VSIX files. Extensions with no `.js/.ts` files (themes, language packs) are marked `no_js`. |
| `local`  | Adds a source folder you already have (`--source DIR`) or a finished CodeQL database (`--db DIR`) to the manifest. |
| `build`  | Creates a CodeQL database for every manifest entry with status `ok`. |
| `query`  | Runs every query on every database with status `db_ok`, then writes the reports. |
| `run`    | Runs `scrape`, `build` and `query` in that order. It accepts all of their options. |

### Examples

```bash
export CODEQL=/path/to/codeql

# The whole pipeline: the 50 most-installed extensions
python3 -m untrustide_scan run --count 50

# Step by step, with a search term and a separate work dir
python3 -m untrustide_scan scrape --count 200 --query "terminal" --sort installs --work-dir work-terminal
python3 -m untrustide_scan build  --work-dir work-terminal --threads 4 --timeout 900
python3 -m untrustide_scan query  --work-dir work-terminal

# Only one category, sorted by rating
python3 -m untrustide_scan scrape --count 100 --category "Programming Languages" --sort rating

# Run on the sample databases that come with UntrustIDE
for db in UntrustIDE/sample-data/*/; do
  python3 -m untrustide_scan local --work-dir work-samples --db "$db"
done
python3 -m untrustide_scan query --work-dir work-samples

# Run on a folder of hand-written test files
python3 -m untrustide_scan local --source path/to/my-test-ext --name test.malicious
python3 -m untrustide_scan build && python3 -m untrustide_scan query

# Try one or two queries only
python3 -m untrustide_scan query --queries UntrustIDE/queries/terminal-addon/terminal-sendText.ql
```

Useful options:

- `--work-dir DIR`: where everything is stored. The default is `./work`.
- `--sort installs|rating|updated|name`, `--query TEXT`, `--category C`: control the Marketplace search.
- `--threads N`: passed to CodeQL. The default, `0`, uses one thread per core.
- `--timeout SEC`: time limit for each database build. `--query-timeout SEC` sets the limit for each query on each database. Both default to 1800, and `0` turns the limit off.
- `--overwrite`: rebuild databases that already exist. `--retry-failed`: try failed builds again.
- `--rerun`: run queries again even if they already have results.
- `--keep-bqrs`: keep CodeQL's raw `.bqrs` result files.
- `--untrustide-dir DIR`: the UntrustIDE repo root (or set `$UNTRUSTIDE_DIR`). The default query folders are `queries/dataflow`, `queries/source-and-sink`, `queries/terminal-addon` and `queries/envVar-addons`.

### Resuming and failures

Every step skips work that is already done. That covers downloaded VSIX files,
extracted sources, finished databases, and queries that already have a CSV. If
a run is interrupted or crashes, run the same command again to continue.

When one extension fails to download, extract, build or run a query, the tool
prints a `!` line, records the problem, and moves on to the next one. The manifest
`status` and `error` columns hold the details, and so do the log files: `work/db/<ext>.create.log`
for builds and `work/raw/<ext>/<query>.log` for queries.

Expect database builds to take from about 20 seconds to several minutes per
extension. The first query run against a database also compiles the query,
which takes about a minute.

## Output files

Everything goes under the work directory:

```
work/
  vsix/                     downloaded .vsix packages
  src/<ext>/extension/      extracted source (<ext> = publisher.name@version)
  db/<ext>/                 CodeQL databases (+ <ext>.create.log)
  raw/<ext>/<query>.csv     decoded results of one query on one extension
  raw/<ext>/<query>.failed  marker: this query failed for this extension
  results/
    manifest.csv
    results.csv
    summary.csv
    query_totals.csv
```

Query names in the outputs are paths relative to `UntrustIDE/queries`, for example
`dataflow/config-to-shell.ql`.

**`manifest.csv`** has one row per extension: `extension_id, publisher, name,
display_name, version, install_count, last_updated, vsix_url, source_dir, status,
db_dir, error`. The `status` column is one of:

| status | meaning |
|--------|---------|
| `ok` | sources extracted, waiting for `build` |
| `no_js` | no JavaScript/TypeScript, skipped |
| `download_failed` / `extract_failed` | see `error` |
| `db_ok` | the database is ready, or was added with `local --db` |
| `db_failed` | the database build failed; see `error` and the create log |

**`results.csv`** has one row per query hit: `extension_id, version, query,
query_category, result_index, message, location, details`.
- `query_category` is the query's top-level folder, for example `dataflow`.
- `message` is the query's message, with any `$@` placeholders filled in.
- `location` is `/path/file.js:startLine:startCol:endLine:endCol`.
- `details` lists the other selected columns as `name=value; ...`.

**`summary.csv`** has one row per extension: `extension_id, version, install_count,
db_status`, one column per query holding the number of hits, and then `total_results`.
A blank cell means the query failed or never ran, which is different from `0`.
`db_status` is `ok`, `failed` or `skipped`.

**`query_totals.csv`** has one row per query: `query, extensions_run,
extensions_with_hits, total_results`.

## Adding new queries

1. Put a `.ql` file in one of the default folders under `UntrustIDE/queries/`,
   for example `UntrustIDE/queries/terminal-addon/my-query.ql`. Shared code
   goes in a `.qll` file next to it, and `.qll` files are never run as queries.
   `example.ql` is skipped as well.
2. Start the file with `import javascript`. Because the file sits inside the
   UntrustIDE workspace, the libraries are found automatically.
3. End it with a `select`. Put the element that should provide the location first,
   then a message string. For example: `select call, "terminal.sendText call"`.
   Path queries such as `select sink.getNode(), source, sink, "Flow from $@", source.getNode(), "here"`
   work too, because the `$@` placeholders get filled in from the later string columns.
4. Run `python3 -m untrustide_scan query`. Only the new query runs, since the
   existing results are reused. The summary gets a new column for it.

To use a folder that isn't a default, run
`--queries UntrustIDE/queries/my-folder UntrustIDE/queries/dataflow ...`. When
you pass `--queries`, only those queries run, and folders are searched
recursively.

## Tests

The unit tests run offline in under a second. They don't need the network or CodeQL:

```bash
python3 -m unittest discover -s tests -v
```
