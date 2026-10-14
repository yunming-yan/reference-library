"""Create a machine-local environment from verified offline wheels only."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
import venv

MINIMUM_PYTHON = (3, 14, 8)


def check_python():
    if platform.python_implementation() != "CPython" or sys.version_info[:2] != (3, 14) or sys.version_info[:3] < MINIMUM_PYTHON or struct.calcsize("P") != 8:
        raise RuntimeError("请使用已安装的标准 CPython 3.14.8 或更高的 3.14 补丁版本（64 位）。")
    if getattr(sys, "_is_gil_enabled", lambda: True)() is False:
        raise RuntimeError("此包需要标准 CPython 3.14，暂不支持 free-threaded 版本。")


WINDOWS_LOADER_FORMAT = 2


def native_loader_source(repo: Path) -> str:
    native_bin = (Path(repo) / "runtime" / "postgresql" / "windows-x64" / "bin").resolve()
    # Restrict normalization to this private venv's site-packages and app DLLs.
    # Host Python, registry and global PATH are not modified.
    return (
        "import os, sys\n"
        "_prefix = chr(92) * 2 + '?' + chr(92)\n"
        "def _extended(path):\n"
        "    value = os.path.abspath(os.fspath(path))\n"
        "    if value.startswith(_prefix): return value\n"
        "    if value.startswith(chr(92) * 2): return _prefix + 'UNC' + chr(92) + value[2:]\n"
        "    return _prefix + value\n"
        "_site = os.path.dirname(os.path.abspath(__file__))\n"
        "_site_key = os.path.normcase(_site)\n"
        "sys.path[:] = [_extended(value) if value and os.path.normcase(os.path.abspath(value)) == _site_key else value for value in sys.path]\n"
        "os._reference_library_pg_dll_handle = os.add_dll_directory(_extended(" + repr(str(native_bin)) + "))\n"
    )


def install_native_loader(repo: Path, site_packages: Path) -> dict:
    if os.name != "nt":
        raise RuntimeError("The private Windows native loader is Windows-only.")
    payload = native_loader_source(repo).encode("utf-8")
    site_packages = Path(site_packages)
    (site_packages / "reference_library_native.py").write_bytes(payload)
    (site_packages / "reference_library_native.pth").write_text("import reference_library_native\n", encoding="utf-8")
    return {"format": WINDOWS_LOADER_FORMAT, "sha256": hashlib.sha256(payload).hexdigest()}


def ensure_environment(repo: Path, private_root: Path) -> Path:
    check_python()
    if os.name != "nt":
        raise RuntimeError("此启动路径用于 Windows x64；请使用匹配的 macOS 发布包。")
    wheels = repo / "wheels" / "windows-x64-cp314"
    manifest = json.loads((wheels / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest:
        file = wheels / item["filename"]
        if hashlib.file_digest(file.open("rb"), "sha256").hexdigest() != item["sha256"]:
            raise RuntimeError("离线依赖校验失败，请重新解压完整发布包。")
    requirements = wheels / "requirements.lock"
    lock_hash = hashlib.file_digest(requirements.open("rb"), "sha256").hexdigest()
    # Never ship or move a venv. A changed interpreter/path gets a new local venv.
    loader_hash = hashlib.sha256(native_loader_source(repo).encode("utf-8")).hexdigest()
    identity = hashlib.sha256((str(Path(sys._base_executable).resolve()) + "|" + str(private_root.resolve()) + "|" + platform.python_version() + "|" + lock_hash + "|" + str(repo.resolve()) + "|" + loader_hash).encode()).hexdigest()[:16]
    environment = private_root / "python" / ("cp314-" + identity)
    executable = environment / "Scripts" / "python.exe"
    stamp = environment / ".reference-library-environment.json"
    expected = {"programRoot": str(repo.resolve()), "baseExecutable": str(Path(sys._base_executable).resolve()), "version": platform.python_version(), "requirementsSha256": lock_hash, "nativeLoaderFormat": WINDOWS_LOADER_FORMAT, "nativeLoaderSha256": loader_hash}
    if stamp.exists():
        loader = environment / "Lib" / "site-packages" / "reference_library_native.py"
        pth = loader.with_suffix(".pth")
        if (json.loads(stamp.read_text(encoding="utf-8")) != expected or not executable.is_file()
                or not loader.is_file() or hashlib.sha256(loader.read_bytes()).hexdigest() != loader_hash
                or not pth.is_file() or pth.read_text(encoding="utf-8") != "import reference_library_native\n"):
            raise RuntimeError("本机虚拟环境标记不一致；请保留旧环境并使用新的私有状态目录。")
        return executable
    if environment.exists() and any(environment.iterdir()):
        raise RuntimeError("发现未完成的本机环境，请保留该目录并更换 privateStateRoot 后重试。")
    venv.EnvBuilder(with_pip=False, clear=False, symlinks=False).create(environment)
    flags = subprocess.CREATE_NO_WINDOW
    temporary = private_root / "tmp"
    temporary.mkdir(exist_ok=True)
    env = dict(os.environ, TEMP=str(temporary), TMP=str(temporary), PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_CONFIG_FILE=os.devnull, PYTHONNOUSERSITE="1", PYTHONUTF8="1")
    result = subprocess.run([str(executable), "-m", "ensurepip", "--upgrade"], env=env, capture_output=True, text=True, encoding="utf-8", creationflags=flags, timeout=90)
    if result.returncode:
        raise RuntimeError("本机 Python 的离线 ensurepip 初始化失败；请使用标准 CPython 安装。")
    result = subprocess.run([str(executable), "-m", "pip", "install", "--no-index", "--no-cache-dir", "--require-hashes", "--only-binary=:all:", "--find-links", str(wheels), "-r", str(requirements)], env=env, capture_output=True, text=True, encoding="utf-8", creationflags=flags, timeout=180)
    log = private_root / "logs" / "offline-bootstrap.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError("离线依赖初始化失败；详情见私有状态目录的 logs/offline-bootstrap.log。")
    site_packages = environment / "Lib" / "site-packages"
    loader_info = install_native_loader(repo, site_packages)
    if loader_info["sha256"] != loader_hash:
        raise RuntimeError("Private native loader changed during environment preparation.")
    env["PSYCOPG_IMPL"] = "c"
    subprocess.run([str(executable), "-c", "import psycopg,psycopg_pool,tzdata,apsw,markdown; assert psycopg.pq.__impl__ == 'c'; assert psycopg.pq.version() == 180006; assert apsw.sqlitelibversion() == '3.54.0'; assert markdown.__version__ == '3.11.1'"], env=env, check=True, capture_output=True, creationflags=flags, timeout=20)
    stamp.write_text(json.dumps(expected, indent=2), encoding="utf-8")
    return executable
