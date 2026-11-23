"""Shared test helpers (no network, no codeql)."""

import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def make_vsix(path, files):
    """Write a zip at ``path`` with ``{member_name: text}``."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return path


def make_db(path):
    """Create a directory that looks like a finalized CodeQL database."""
    path = Path(path)
    (path / "db-javascript").mkdir(parents=True, exist_ok=True)
    (path / "codeql-database.yml").write_text("primaryLanguage: javascript\n")
    return path


def raw_extension(publisher, name, version, installs=10, with_files=True):
    """A gallery API extension object like the real extensionquery reply."""
    files = []
    if with_files:
        files.append({
            "assetType": "Microsoft.VisualStudio.Services.VSIXPackage",
            "source": "https://cdn.example/%s.%s/%s.vsix" % (publisher, name, version),
        })
    return {
        "publisher": {"publisherName": publisher},
        "extensionName": name,
        "displayName": name.title(),
        "lastUpdated": "2024-01-01T00:00:00Z",
        "versions": [{"version": version, "files": files}],
        "statistics": [{"statisticName": "averagerating", "value": 4.5},
                       {"statisticName": "install", "value": installs}],
    }


def fake_response(exts):
    return {"results": [{"extensions": exts, "resultMetadata": []}]}
