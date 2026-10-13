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
    identity = hashlib.sha256((str(Path(sys._base_executable).resolve()) + "|" + str(private_root.resolve()) + "|" + platform.python_version() + "|" + lock_hash + "|" + str(repo.resolve())).encode()).hexdigest()[:16]
    environment = private_root / "python" / ("cp314-" + identity)
    executable = environment / "Scripts" / "python.exe"
    stamp = environment / ".reference-library-environment.json"
    expected = {"programRoot": str(repo.resolve()), "baseExecutable": str(Path(sys._base_executable).resolve()), "version": platform.python_version(), "requirementsSha256": lock_hash}
    if stamp.exists():
        if json.loads(stamp.read_text(encoding="utf-8")) != expected or not executable.is_file():
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
    native_bin = (repo / "runtime" / "postgresql" / "windows-x64" / "bin").resolve()
    (site_packages / "reference_library_native.pth").write_text("import os; os._reference_library_pg_dll_handle = os.add_dll_directory(" + repr(str(native_bin)) + ")\n", encoding="utf-8")
    env["PSYCOPG_IMPL"] = "c"
    subprocess.run([str(executable), "-c", "import psycopg,psycopg_pool,tzdata,apsw; assert psycopg.pq.__impl__ == 'c'; assert psycopg.pq.version() == 180006; assert apsw.sqlitelibversion() == '3.54.0'"], env=env, check=True, capture_output=True, creationflags=flags, timeout=20)
    stamp.write_text(json.dumps(expected, indent=2), encoding="utf-8")
    return executable
