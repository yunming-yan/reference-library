"""Build real unsigned macOS .app candidates and Unix-mode ZIPs on any host."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import stat
import time
import zipfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
FIXED_ZIP_DATE = (2026, 10, 13, 0, 0, 0)
APP_RUNTIME_NAMES = {"bootstrap.py", "cache_sqlite.py", "ingest.py", "maintenance.py",
                     "original_store.py", "pg_catalog.py", "runtime_manager.py", "server.py"}


def app_runtime_file(path):
    parts = path.relative_to(REPO / "app").parts
    if len(parts) == 1:
        return parts[0] in APP_RUNTIME_NAMES
    if parts[0] == "web":
        return len(parts) == 2 and path.suffix in {".js", ".css", ".html", ".svg"}
    if parts[0] == "vendor":
        if len(parts) == 2:
            return parts[1] in {"Markdown-LICENSE.txt", "THIRD_PARTY.txt", "markdown-manifest.json"}
        return parts[1] == "markdown" and path.suffix == ".py"
    return False


def shared_source_snapshot():
    result = {}
    roots = ((REPO / "app", app_runtime_file),
             (REPO / "schema", lambda p: True),
             (REPO / "maintenance", lambda p: p.suffix in {".py", ".sql", ".json"}))
    for root, permitted in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts and permitted(path):
                result[path.relative_to(REPO).as_posix()] = digest(path)
    return result


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_tree(source, target, predicate=lambda path: True):
    for path in sorted(source.rglob("*")):
        if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts and predicate(path):
            relative = path.relative_to(source)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)


def app_info(display, identifier):
    return {"CFBundleDevelopmentRegion": "zh_CN", "CFBundleDisplayName": display, "CFBundleExecutable": "ReferenceLibrary", "CFBundleIdentifier": identifier, "CFBundleInfoDictionaryVersion": "6.0", "CFBundleName": display, "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "2.0.0", "CFBundleVersion": "2", "LSMinimumSystemVersion": "13.0", "LSUIElement": True, "NSHighResolutionCapable": True}


def skeleton(path, display, identifier):
    contents = path / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    (contents / "Info.plist").write_bytes(plistlib.dumps(app_info(display, identifier), sort_keys=True))
    launcher = contents / "MacOS" / "ReferenceLibrary"
    shutil.copyfile(HERE / "launcher.sh", launcher)
    launcher.chmod(0o755)
    (contents / "PkgInfo").write_bytes(b"APPL????")


def build(architecture, output):
    required_web = ("source-images.js", "app.js", "reader.js", "management.js", "index.html")
    for name in required_web:
        path = REPO / "app" / "web" / name
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("Required final web helper is missing: " + name)
    for name in APP_RUNTIME_NAMES:
        if not (REPO / "app" / name).is_file():
            raise RuntimeError("Required final shared module is missing: " + name)
    source_snapshot = shared_source_snapshot()
    parent = output / ("ReferenceLibrary-macos-" + architecture + "-cp314")
    if parent.exists():
        raise RuntimeError("Choose a new empty output directory; existing candidate is preserved")
    parent.mkdir(parents=True)
    app = parent / "综合资料库.app"
    skeleton(app, "综合资料库", "org.reference-library.desktop")
    skeleton(parent / "关闭综合资料库.app", "关闭综合资料库", "org.reference-library.desktop.stop")
    program = app / "Contents" / "Resources" / "Program"
    program.mkdir()
    copy_tree(REPO / "app", program / "app", app_runtime_file)
    copy_tree(REPO / "schema", program / "schema")
    copy_tree(REPO / "maintenance", program / "maintenance", lambda p: p.suffix in {".py", ".sql", ".json"})
    copy_tree(REPO / "licenses", program / "licenses", lambda p: "microsoft-vc-runtime" not in p.parts)
    copy_tree(REPO / "runtime" / "postgresql" / "macos-universal2", program / "runtime" / "postgresql" / "macos-universal2")
    copy_tree(REPO / "wheels" / f"macos-{architecture}-cp314", program / "wheels" / f"macos-{architecture}-cp314")
    copy_tree(HERE, program / "packaging" / "macos", lambda p: "build-support" not in p.parts and p.name in {"bootstrap_macos.py", "launch_macos.py", "server_entry.py", "native_smoke.py", "sign_and_notarize.sh", "refresh_signed_manifests.py"})
    (program / "docs").mkdir()
    shutil.copyfile(REPO / "docs" / "macos.md", program / "docs" / "macos.md")
    shutil.copyfile(REPO / "docs" / "macos.md", parent / "使用说明.md")
    for relative, expected in source_snapshot.items():
        if digest(program / relative) != expected or digest(REPO / relative) != expected:
            raise RuntimeError("Shared source changed during Mac snapshot: " + relative)
    runtime_manifest = json.loads((program / "runtime" / "postgresql" / "macos-universal2" / "manifest.json").read_text("utf-8"))
    runtime_modes = {"runtime/postgresql/macos-universal2/" + item["path"]: int(item["unixMode"], 8) for item in runtime_manifest["files"]}
    payload = []
    modes = {}
    for path in sorted(parent.rglob("*")):
        if not path.is_file(): continue
        relative = path.relative_to(parent).as_posix()
        if path.is_relative_to(program):
            mode = runtime_modes.get(path.relative_to(program).as_posix(), 0o644)
        else:
            mode = 0o755 if path.name == "ReferenceLibrary" else 0o644
        modes[relative] = mode
        payload.append({"path": relative, "sha256": digest(path), "bytes": path.stat().st_size, "unixMode": oct(mode)})
    manifest = {"sharedSourceSnapshot": source_snapshot, "format": 1, "application": "ReferenceLibrary", "candidateVersion": "2.0.0", "targetArchitecture": architecture, "python": "standard CPython 3.14.8+ within 3.14", "postgresql": "18.6", "libpq": "18.6", "psycopg": "3.3.6", "psycopgImplementation": "python fallback; native C extension still blocked", "psycopgPool": "3.3.3", "apsw": "3.54.0.0", "sqlite": "3.54.0", "markdown": "3.11", "opensslBundled": "3.5.9 LTS; newer overall stable 4.0.3 exists", "icuBundled": "77.1", "transitiveLatest": False, "minimumMacOSFromNativeLoadCommands": "13.0", "nativeExecution": "not run: Windows build host", "finderSafariAcceptance": "blocked: no Intel/Apple Silicon Mac host", "developerIdSignature": "not performed", "notarization": "not performed", "files": payload}
    (program / "release-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    archive = output / (parent.name + ".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for directory in sorted((p for p in parent.rglob("*") if p.is_dir())):
            name = parent.name + "/" + directory.relative_to(parent).as_posix() + "/"
            info = zipfile.ZipInfo(name, FIXED_ZIP_DATE); info.create_system = 3
            info.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
            z.writestr(info, b"")
        for path in sorted(p for p in parent.rglob("*") if p.is_file()):
            relative = path.relative_to(parent).as_posix()
            info = zipfile.ZipInfo(parent.name + "/" + relative, FIXED_ZIP_DATE)
            info.create_system = 3; info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | modes.get(relative, 0o644)) << 16
            with path.open("rb") as source, z.open(info, "w", force_zip64=True) as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
    # Validate archive bytes/modes against the concrete bundle snapshot.
    with zipfile.ZipFile(archive) as z:
        bad = z.testzip()
        if bad: raise RuntimeError("ZIP CRC failed: " + bad)
        for item in payload:
            info = z.getinfo(parent.name + "/" + item["path"])
            if stat.S_IMODE(info.external_attr >> 16) != int(item["unixMode"], 8):
                raise RuntimeError("ZIP Unix mode mismatch")
            with z.open(info) as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != item["sha256"]: raise RuntimeError("ZIP byte hash mismatch")
    result = {"architecture": architecture, "archive": str(archive), "bytes": archive.stat().st_size, "sha256": digest(archive), "payloadFiles": len(payload), "zipCrcAndByteHash": "passed", "unixModes": "passed", "nativeExecution": "blocked: no native Mac", "bundle": str(app), "buildCompletedAtUTC": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (output / (parent.name + "-build-evidence.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", choices=("arm64", "x86_64", "both"), default="both")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for arch in (("arm64", "x86_64") if args.arch == "both" else (args.arch,)):
        build(arch, args.output.resolve())
