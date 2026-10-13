from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import zipfile
from runtime_audit import macho

CORE = {"bin", "lib", "share", "include", "doc"}


def needs_languagepack(path):
    if not (path.startswith("lib/postgresql/") or path.startswith("share/postgresql/extension/")):
        return False
    name = PurePosixPath(path).name
    return any(fragment in name for fragment in ("_plperl", "_plpython3")) or name.startswith(("plperl.", "plperl--", "plperlu.", "plperlu--", "plpython3.", "plpython3u.", "plpython3u--", "pltcl.", "pltcl--", "pltclu.", "pltclu--"))


def main(root: Path):
    archive = root / "downloads" / "mac" / "postgresql-18.6-macos.zip"
    destination = root / "repo" / "runtime" / "postgresql" / "macos-universal2"
    destination.mkdir(parents=True, exist_ok=True)
    if (destination / "manifest.json").exists():
        raise RuntimeError("Runtime is already finalized")
    license_root = destination / "source-licenses"
    manifest, native, symlinks, excluded = [], [], [], []
    with zipfile.ZipFile(archive) as source:
        infos = {i.filename: i for i in source.infolist()}
        for name, info in sorted(infos.items()):
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or path.parts[0] != "pgsql":
                raise RuntimeError("Unsafe source ZIP path")
            include = len(path.parts) > 1 and (path.parts[1] in CORE or len(path.parts) == 2)
            is_license = not info.is_dir() and any(part in name.lower() for part in ("license", "licence", "copying", "copyright"))
            if not include and not is_license:
                continue
            relative = PurePosixPath(*path.parts[1:]) if include else PurePosixPath("source-licenses", *path.parts)
            target = destination.joinpath(*relative.parts)
            if needs_languagepack(relative.as_posix()):
                excluded.append(relative.as_posix())
                if target.exists() and target.is_file():
                    if not target.resolve().is_relative_to(destination.resolve()):
                        raise RuntimeError("Refusing removal outside owned runtime")
                    target.unlink()
                continue
            mode = info.external_attr >> 16
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if stat.S_ISLNK(mode):
                # No source symlink may become a text file posing as a dylib.
                raise RuntimeError("Unexpected symlink requires explicit safe resolution: " + name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.chmod(0o644)
            digest = hashlib.sha256()
            with source.open(info) as original, target.open("wb") as output:
                while block := original.read(1024 * 1024):
                    output.write(block); digest.update(block)
            permissions = stat.S_IMODE(mode) or (0o755 if relative.parts[0] == "bin" else 0o644)
            target.chmod(permissions)
            item = {"path": relative.as_posix(), "bytes": info.file_size, "sha256": digest.hexdigest(), "unixMode": oct(permissions), "sourceMember": name}
            manifest.append(item)
            if relative.parts[0] in {"bin", "lib"}:
                with target.open("rb") as stream:
                    first = stream.read(4)
                    if first in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce"):
                        records = macho(first + stream.read())
                        if records:
                            if {r["architecture"] for r in records} != {"arm64", "x86_64"}:
                                raise RuntimeError("Non-universal runtime Mach-O: " + str(relative))
                            native.append({"path": relative.as_posix(), "slices": records})
    paths = {item["path"] for item in manifest}
    unresolved = []
    for item in native:
        for slice in item["slices"]:
            for library in slice["libraries"]:
                dependency = library["path"]
                if library["kind"] == "id" or dependency.startswith(("/usr/lib/", "/System/Library/")):
                    continue
                if dependency.startswith("@rpath/"):
                    found = "lib/" + dependency[len("@rpath/"):]
                elif dependency.startswith("@loader_path/"):
                    parts = list(PurePosixPath(item["path"]).parent.parts)
                    for component in PurePosixPath(dependency[len("@loader_path/"):]).parts:
                        if component == "..":
                            if not parts: raise RuntimeError("dependency escapes runtime")
                            parts.pop()
                        elif component != ".": parts.append(component)
                    found = "/".join(parts)
                else:
                    unresolved.append({"file": item["path"], "dependency": dependency, "reason": "external non-system install name"})
                    continue
                if found not in paths:
                    unresolved.append({"file": item["path"], "dependency": dependency, "expected": found})
    if unresolved:
        raise RuntimeError("Unresolved non-system dependencies: " + json.dumps(unresolved))
    metadata = {"format": 1, "postgresqlVersion": "18.6", "architectures": ["x86_64", "arm64"], "minimumMacOSFromLoadCommands": "13.0", "source": json.loads(archive.with_suffix(".zip.source.json").read_text("utf-8")), "symlinksInSelectedPayload": symlinks, "symlinkStrategy": "EDB selected payload stores all aliases as complete regular Mach-O files; none copied as text", "excludedApplications": ["pgAdmin 4.app", "stackbuilder.app"], "excludedLanguagePackExtensions": excluded, "licenses": "All named license/copying/copyright files from original ZIP retained, including excluded app licenses", "nativeRuntimeFiles": len(native), "nativeSlices": dict(Counter(r["architecture"] for item in native for r in item["slices"])), "unresolvedNonSystemDependencies": unresolved, "nativeExecution": "blocked: no macOS host in this Windows execution environment", "files": manifest, "native": native}
    (destination / "manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps({"runtime": str(destination), "fileCount": len(manifest), "bytes": sum(i["bytes"] for i in manifest), "nativeCount": len(native), "nonSystemDependencyFailures": len(unresolved)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("root", type=Path)
    main(parser.parse_args().root)
