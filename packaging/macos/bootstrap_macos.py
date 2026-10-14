"""Machine-local, hash-checked CPython 3.14 environment from offline wheels."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
import sysconfig
import venv


def check_python():
    if sys.platform != "darwin" or platform.python_implementation() != "CPython":
        raise RuntimeError("此启动器用于 macOS 的标准 CPython。")
    if sys.version_info[:2] != (3, 14) or sys.version_info[:3] < (3, 14, 8) or struct.calcsize("P") != 8:
        raise RuntimeError("请一次性安装标准 64 位 CPython 3.14.8 或更高的 3.14 补丁版本。")
    if sysconfig.get_config_var("Py_GIL_DISABLED") or sysconfig.get_config_var("Py_DEBUG"):
        raise RuntimeError("此发布包需要标准 CPython 3.14，不能使用 free-threaded 或 debug 构建。")
    arch = platform.machine().lower()
    if arch not in ("arm64", "x86_64"):
        raise RuntimeError("此包支持 Apple Silicon 与 Intel Mac。")
    if tuple(map(int, platform.mac_ver()[0].split(".")[:2])) < (13, 0):
        raise RuntimeError("此 PostgreSQL 18.6 包需要 macOS 13 或更新版本。")
    return arch


def child_environment(repo):
    # No global path, Python installation, libpq, PostgreSQL or pip changes.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PIP_", "PG", "PYTHON")) and k not in ("DYLD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "DYLD_FALLBACK_LIBRARY_PATH")}
    native = Path(repo) / "runtime" / "postgresql" / "macos-universal2"
    env.update(PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_CONFIG_FILE=os.devnull, PYTHONNOUSERSITE="1", PYTHONUTF8="1", PSYCOPG_IMPL="python", DYLD_LIBRARY_PATH=str(native / "lib"))
    return env


def ensure_environment(repo: Path, private_root: Path) -> Path:
    arch = check_python()
    repo, private_root = Path(repo).resolve(), Path(private_root).resolve()
    release = repo / "release-manifest.json"
    if release.exists() and json.loads(release.read_text("utf-8")).get("targetArchitecture", arch) != arch:
        raise RuntimeError("本机 Python 的 CPU 架构与此 Mac 包不同，请使用原生 Python 和对应架构的发布包。")
    wheels = repo / "wheels" / f"macos-{arch}-cp314"
    manifest = json.loads((wheels / "manifest.json").read_text("utf-8"))
    for item in manifest:
        file = wheels / item["filename"]
        if file.parent != wheels or not file.is_file() or hashlib.file_digest(file.open("rb"), "sha256").hexdigest() != item["sha256"]:
            raise RuntimeError("离线依赖校验失败，请重新解压完整发布包。")
    lock = wheels / "requirements.lock"
    lock_hash = hashlib.file_digest(lock.open("rb"), "sha256").hexdigest()
    base = Path(sys._base_executable).resolve()
    identity = hashlib.sha256((str(base) + "|" + str(repo) + "|" + str(private_root) + "|" + arch + "|" + platform.python_version() + "|" + lock_hash).encode()).hexdigest()[:16]
    environment = private_root / "python" / ("cp314-" + arch + "-" + identity)
    executable = environment / "bin" / "python3"
    stamp = environment / ".reference-library-environment.json"
    expected = {"baseExecutable": str(base), "programRoot": str(repo), "version": platform.python_version(), "architecture": arch, "requirementsSha256": lock_hash, "driverImplementation": "python", "libpqVersion": "18.6"}
    if stamp.exists():
        if json.loads(stamp.read_text("utf-8")) != expected or not executable.is_file():
            raise RuntimeError("本机环境标记不一致；请保留旧环境并使用新的私有运行目录。")
        return executable
    if environment.exists() and any(environment.iterdir()):
        raise RuntimeError("本机依赖初始化曾中断；请保留该目录并更换私有运行目录后重试。")
    venv.EnvBuilder(with_pip=True, clear=False, symlinks=True).create(environment)
    env = child_environment(repo)
    result = subprocess.run([str(executable), "-I", "-m", "pip", "--isolated", "install", "--no-index", "--no-cache-dir", "--require-hashes", "--only-binary=:all:", "--find-links", str(wheels), "-r", str(lock)], env=env, capture_output=True, text=True, encoding="utf-8", timeout=180)
    logs = private_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "offline-bootstrap.log").write_text(result.stdout + result.stderr, "utf-8")
    if result.returncode:
        raise RuntimeError("离线依赖初始化失败；详情见私有运行日志。")
    site = environment / "lib" / "python3.14" / "site-packages"
    native = repo / "runtime" / "postgresql" / "macos-universal2" / "lib" / "libpq.5.dylib"
    # Psycopg 3.3.6 has no LIBPQ_PATH override. Bind only its ctypes lookup to
    # this application's exact library; no Homebrew or pg_config discovery.
    hook = "import ctypes.util\nfrom pathlib import Path\n_original_find_library = ctypes.util.find_library\n_native_libpq = " + repr(str(native)) + "\ndef _find_library(name):\n    if name in ('libpq.dylib', 'pq', 'libpq'):\n        if not Path(_native_libpq).is_file():\n            raise RuntimeError('Application libpq is missing')\n        return _native_libpq\n    return _original_find_library(name)\nctypes.util.find_library = _find_library\n"
    (site / "reference_library_native.py").write_text(hook, "utf-8")
    (site / "reference_library_native.pth").write_text("import reference_library_native\n", "utf-8")
    check = subprocess.run([str(executable), "-I", "-c", "import psycopg,psycopg_pool,markdown,tzdata,apsw; assert psycopg.pq.__impl__ == 'python'; assert psycopg.pq.version() == 180006; assert apsw.sqlitelibversion() == '3.54.0'; assert markdown.__version__ == '3.11.1'"], env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    (logs / "native-dependency-check.log").write_text(check.stdout + check.stderr, "utf-8")
    if check.returncode:
        raise RuntimeError("Mac 原生依赖载入失败；详情见私有运行日志。")
    stamp.write_text(json.dumps(expected, indent=2) + "\n", "utf-8")
    return executable
