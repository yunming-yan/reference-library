"""Developer-only: native C extension build with offline pinned build inputs."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
import venv

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
from bootstrap_macos import check_python, child_environment


def main():
    arch = check_python()
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    support = HERE / "build-support"
    manifest = json.loads((support / "manifest.json").read_text("utf-8"))
    for item in manifest:
        path = support / item["filename"]
        if path.parent != support or hashlib.file_digest(path.open("rb"), "sha256").hexdigest() != item["sha256"]:
            raise RuntimeError("Native build input hash mismatch")
    # Release-host prerequisite only; this tool never installs a global compiler.
    subprocess.run(["/usr/bin/xcrun", "--find", "clang"], check=True, capture_output=True)
    environment = output / "developer-venv"
    if environment.exists(): raise RuntimeError("Use a fresh native build output directory")
    venv.EnvBuilder(with_pip=True, symlinks=True).create(environment)
    python = environment / "bin" / "python3"
    env = child_environment(REPO)
    env["PATH"] = str(REPO / "runtime" / "postgresql" / "macos-universal2" / "bin") + ":" + env.get("PATH", "/usr/bin:/bin")
    env.update(MACOSX_DEPLOYMENT_TARGET="13.0", ARCHFLAGS="-arch " + arch)
    command = [str(python), "-I", "-m", "pip", "--isolated", "install", "--no-index", "--require-hashes", "--only-binary=:all:", "--find-links", str(support), "-r", str(support / "requirements.lock")]
    install = subprocess.run(command, env=env, capture_output=True, text=True)
    (output / "developer-bootstrap.log").write_text(install.stdout + install.stderr, "utf-8")
    if install.returncode: raise RuntimeError("Offline developer build bootstrap failed")
    version = subprocess.check_output([str(REPO / "runtime" / "postgresql" / "macos-universal2" / "bin" / "pg_config"), "--version"], env=env, text=True).strip()
    if version != "PostgreSQL 18.6": raise RuntimeError("Expected PostgreSQL 18.6 build headers and pg_config")
    source = next(support / i["filename"] for i in manifest if i["name"] == "psycopg-c")
    wheels = output / "wheels"
    wheels.mkdir()
    build = subprocess.run([str(python), "-I", "-m", "pip", "--isolated", "wheel", "--no-index", "--no-deps", "--no-build-isolation", "--wheel-dir", str(wheels), str(source)], env=env, capture_output=True, text=True)
    (output / "native-build.log").write_text(build.stdout + build.stderr, "utf-8")
    if build.returncode: raise RuntimeError("Native psycopg-c build failed")
    created = list(wheels.glob("psycopg_c-3.3.6-cp314-cp314-*.whl"))
    if len(created) != 1: raise RuntimeError("Expected one CPython 3.14 psycopg-c wheel")
    app_wheels = REPO / "wheels" / f"macos-{arch}-cp314"
    subprocess.run([str(python), "-I", "-m", "pip", "--isolated", "install", "--no-index", "--require-hashes", "--find-links", str(app_wheels), "-r", str(app_wheels / "requirements.lock")], env=env, check=True, capture_output=True)
    subprocess.run([str(python), "-I", "-m", "pip", "--isolated", "install", "--no-index", "--no-deps", str(created[0])], env=env, check=True, capture_output=True)
    env["PSYCOPG_IMPL"] = "c"
    check = subprocess.run([str(python), "-I", "-c", "import psycopg,json; assert psycopg.pq.__impl__=='c'; assert psycopg.pq.version()==180006; print(json.dumps({'implementation':psycopg.pq.__impl__,'libpq':psycopg.pq.version()}))"], env=env, capture_output=True, text=True)
    (output / "native-import.log").write_text(check.stdout + check.stderr, "utf-8")
    if check.returncode: raise RuntimeError("Native driver import/libpq 18.6 match failed")
    # No automatic promotion: final release needs both CPU native QA and profiling.
    report = {"architecture": arch, "platform": platform.platform(), "python": platform.python_version(), "builtAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "wheel": created[0].name, "sha256": hashlib.file_digest(created[0].open("rb"), "sha256").hexdigest(), "bytes": created[0].stat().st_size, "nativeImport": json.loads(check.stdout), "status": "native-built; dependency closure, synthetic app test and performance comparison still required before promotion", "promoted": False}
    (output / "native-build-manifest.json").write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
