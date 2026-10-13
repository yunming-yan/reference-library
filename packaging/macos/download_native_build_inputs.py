from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import tarfile
import urllib.request
from download_verified import fetch, read_json


def main(root):
    target = root / "repo" / "packaging" / "macos" / "build-support"
    target.mkdir(parents=True, exist_ok=True)
    entries = []
    for name in ("pip", "setuptools", "wheel", "packaging"):
        # Upstream psycopg-c explicitly requires this build backend revision.
        metadata_url = f"https://pypi.org/pypi/{name}/83.0.0/json" if name == "setuptools" else f"https://pypi.org/pypi/{name}/json"
        metadata = read_json(metadata_url)
        matches = [item for item in metadata["urls"] if item["filename"].endswith("-none-any.whl") and not item.get("yanked")]
        if len(matches) != 1: raise RuntimeError(f"No unique build tool wheel: {name}")
        selected = matches[0]
        fetch(selected["url"], target / selected["filename"], selected["digests"]["sha256"])
        entries.append({"name": name, "version": metadata["info"]["version"], "filename": selected["filename"], "sha256": selected["digests"]["sha256"], "sourceUrl": selected["url"]})
    (target / "requirements.lock").write_text("\n".join(f"{i['name']}=={i['version']} --hash=sha256:{i['sha256']}" for i in entries) + "\n", "utf-8")
    metadata = read_json("https://pypi.org/pypi/psycopg-c/3.3.6/json")
    selected = next(item for item in metadata["urls"] if item["packagetype"] == "sdist" and not item.get("yanked"))
    archive = target / selected["filename"]
    fetch(selected["url"], archive, selected["digests"]["sha256"])
    entries.append({"name": "psycopg-c", "version": "3.3.6", "filename": selected["filename"], "sha256": selected["digests"]["sha256"], "sourceUrl": selected["url"]})
    (target / "manifest.json").write_text(json.dumps(entries, indent=2) + "\n", "utf-8")
    with tarfile.open(archive) as source:
        pyproject = next(item for item in source.getmembers() if item.name.endswith("/pyproject.toml"))
        print(source.extractfile(pyproject).read().decode())
    print(json.dumps({"nativeBuildInputs": [{"name": i["name"], "version": i["version"]} for i in entries]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("root", type=Path)
    main(parser.parse_args().root)
