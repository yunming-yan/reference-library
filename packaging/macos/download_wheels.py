from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import urllib.request
import zipfile
from download_verified import fetch, read_json

PACKAGES = {"psycopg": "3.3.6", "psycopg_pool": "3.3.3", "Markdown": "3.11", "apsw": "3.54.0.0", "typing_extensions": "4.16.0", "tzdata": "2026.5"}


def main(root: Path):
    downloads = root / "downloads" / "mac" / "wheels"
    evidence = root / "evidence" / "macos-pypi"
    downloads.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    records = {"x86_64": [], "arm64": []}
    for name, version in PACKAGES.items():
        request = f"https://pypi.org/pypi/{name}/{version}/json"
        metadata = read_json(request)
        (evidence / f"{name}-{version}.json").write_text(json.dumps(metadata, indent=2) + "\n", "utf-8")
        wheels = [item for item in metadata["urls"] if item["packagetype"] == "bdist_wheel" and not item.get("yanked")]
        for arch in records:
            matches = [item for item in wheels if item["filename"].endswith("-none-any.whl") or ("-cp314-cp314-macosx_" in item["filename"] and (item["filename"].endswith(f"_{arch}.whl") or item["filename"].endswith("_universal2.whl")))]
            if len(matches) != 1:
                raise RuntimeError(f"Expected one compatible wheel: {name} {arch}: {[i['filename'] for i in matches]}")
            selected = matches[0]
            source = downloads / selected["filename"]
            fetch(selected["url"], source, selected["digests"]["sha256"])
            target = root / "repo" / "wheels" / f"macos-{arch}-cp314"
            target.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target / source.name)
            with zipfile.ZipFile(source) as wheel:
                wheel_meta = next(n for n in wheel.namelist() if n.endswith(".dist-info/WHEEL"))
                tags = [line[5:] for line in wheel.read(wheel_meta).decode().splitlines() if line.startswith("Tag: ")]
                requires = next(n for n in wheel.namelist() if n.endswith(".dist-info/METADATA"))
                requirements = [line[15:] for line in wheel.read(requires).decode().splitlines() if line.startswith("Requires-Dist: ")]
            records[arch].append({"name": name, "version": version, "filename": source.name, "sha256": selected["digests"]["sha256"], "bytes": source.stat().st_size, "sourceUrl": selected["url"], "metadataUrl": request, "wheelTags": tags, "requiresDist": requirements})
    for arch, manifest in records.items():
        target = root / "repo" / "wheels" / f"macos-{arch}-cp314"
        (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
        (target / "requirements.lock").write_text("\n".join(f"{i['name']}=={i['version']} --hash=sha256:{i['sha256']}" for i in manifest) + "\n", "utf-8")
        print(json.dumps({"arch": arch, "wheels": [i["filename"] for i in manifest]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    main(parser.parse_args().root)
