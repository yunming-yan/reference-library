"""Private app-owned PostgreSQL lifecycle. No global installation or service."""
from __future__ import annotations
from contextlib import contextmanager
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sys
import socket
import subprocess
import time
import uuid

APP_ID = "reference-library-v2"
MARKER = ".reference-library-owner.json"
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(4))
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def overlaps(a, b):
    a, b = Path(a).resolve(), Path(b).resolve()
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def private_acl(path):
    if os.name == "nt":
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, creationflags=HIDDEN, check=True)
        sid = list(csv.reader(io.StringIO(result.stdout)))[0][-1].strip()
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", "*" + sid + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"], capture_output=True, creationflags=HIDDEN, check=True)
    else:
        path.chmod(0o700 if path.is_dir() else 0o600)


def _windows_local_appdata():
    """Read the current user's Known Folder; never create or redirect it."""
    import ctypes

    class GUID(ctypes.Structure):
        _fields_ = [('Data1', ctypes.c_uint32), ('Data2', ctypes.c_uint16),
                    ('Data3', ctypes.c_uint16), ('Data4', ctypes.c_ubyte * 8)]

    # FOLDERID_LocalAppData; flags 0 use the current (possibly redirected) path.
    # https://learn.microsoft.com/windows/win32/api/shlobj_core/nf-shlobj_core-shgetknownfolderpath
    folder = GUID.from_buffer_copy(uuid.UUID('F1B32785-6FBA-4FCF-9D55-7B8E7F157091').bytes_le)
    shell = ctypes.WinDLL('shell32', winmode=0x00000800)  # system DLLs only
    ole = ctypes.WinDLL('ole32', winmode=0x00000800)
    lookup = shell.SHGetKnownFolderPath
    lookup.argtypes = [ctypes.POINTER(GUID), ctypes.c_uint32,
                       ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    lookup.restype = ctypes.c_int32
    release = ole.CoTaskMemFree
    release.argtypes = [ctypes.c_void_p]
    release.restype = None
    allocated = ctypes.c_void_p()
    try:
        result = lookup(ctypes.byref(folder), 0, None, ctypes.byref(allocated))
        if result != 0 or not allocated.value:
            raise OSError('SHGetKnownFolderPath could not resolve LocalAppData.')
        return Path(ctypes.wstring_at(allocated.value))
    finally:
        # Microsoft requires freeing the output even when the lookup fails.
        if allocated.value:
            release(allocated)


def _mac_user_directories():
    """Read NSFileManager user-domain Application Support and Caches URLs."""
    import ctypes

    # The OS frameworks are present on supported macOS; no PyObjC dependency.
    foundation = ctypes.CDLL('/System/Library/Frameworks/Foundation.framework/Foundation')
    objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    address = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
    # Each message has its own ABI signature; no shared mutable argtypes.
    message = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(address)
    directories = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.c_size_t)(address)
    count = ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p)(address)
    file_url = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(address)
    representation = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p)(address)
    drain = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)(address)
    selector = objc.sel_registerName
    pool_class = objc.objc_getClass(b'NSAutoreleasePool')
    manager_class = objc.objc_getClass(b'NSFileManager')
    if not pool_class or not manager_class:
        raise OSError('Foundation directory classes are unavailable.')
    pool = message(message(pool_class, selector(b'alloc')), selector(b'init'))
    if not pool:
        raise OSError('Foundation autorelease pool could not be created.')
    try:
        manager = message(manager_class, selector(b'defaultManager'))
        if not manager:
            raise OSError('NSFileManager is unavailable.')
        values = []
        # NSApplicationSupportDirectory=14, NSCachesDirectory=13;
        # NSUserDomainMask=1. URLsForDirectory does not create the directory.
        # https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/FileSystemProgrammingGuide/AccessingFilesandDirectories/AccessingFilesandDirectories.html
        for directory in (14, 13):
            urls = directories(manager, selector(b'URLsForDirectory:inDomains:'), directory, 1)
            if not urls or count(urls, selector(b'count')) < 1:
                raise OSError('Foundation did not return a user directory.')
            url = message(urls, selector(b'firstObject'))
            if not url or not file_url(url, selector(b'isFileURL')):
                raise OSError('Foundation returned an invalid directory URL.')
            raw = representation(url, selector(b'fileSystemRepresentation'))
            if not raw:
                raise OSError('Foundation directory has no filesystem representation.')
            values.append(Path(os.fsdecode(raw)))  # copy before the pool drains
        return tuple(values)
    finally:
        drain(pool, selector(b'drain'))


def _default_user_directories():
    """Native first-use roots; an existing explicit profile bypasses this."""
    try:
        if sys.platform == 'darwin':
            support, cache = _mac_user_directories()
        elif os.name == 'nt':
            support = cache = _windows_local_appdata()
        else:
            raise OSError('Automatic folders require Windows or macOS.')
        # Keep the existing absolute/plain-path guard for native results too.
        support = MachineConfig._absolute(str(support), 'systemApplicationSupport')
        cache = MachineConfig._absolute(str(cache), 'systemCaches')
        if any(path.exists() and not path.is_dir() for path in (support, cache)):
            raise OSError('A system directory is not a directory.')
        return support, cache
    except (OSError, AttributeError, ValueError, RuntimeError) as error:
        # Guessing HOME/environment here could create a second empty library.
        # Existing/custom machine profiles are loaded before this function.
        raise RuntimeError('系统未能解析资料目录。请使用明确指定目录的机器配置继续启动。') from error


class MachineConfig:
    def __init__(self, repo, path=None):
        self.repo = self._absolute(str(Path(repo).absolute()), "programRoot")
        location = path or os.environ.get("REFERENCE_LIBRARY_MACHINE_CONFIG") or self.repo / "machine.json"
        self.config_path = Path(location).resolve()
        if (path or os.environ.get("REFERENCE_LIBRARY_MACHINE_CONFIG")) and not self.config_path.is_file():
            raise RuntimeError("The explicitly selected machine configuration file does not exist.")
        if self.config_path.exists():
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
        else:
            support, cache = _default_user_directories()
            base = support / "ReferenceLibrary"
            if sys.platform == "darwin":
                data = base / "Library"
                existing = data / MARKER
                identity = json.loads(existing.read_text(encoding="utf-8"))["libraryId"] if existing.exists() else str(uuid.uuid4())
                value = {"privateStateRoot": str(base / "Runtime" / identity), "dataRoot": str(data), "cacheRoot": str(cache / "ReferenceLibrary" / identity), "initialLibraryId": identity, "appPort": 18761, "pgPort": 18762}
            else:
                value = {"privateStateRoot": str(base / "runtime"), "dataRoot": str(base / "library"), "cacheRoot": str(cache / "ReferenceLibrary" / "cache"), "appPort": 18761, "pgPort": 18762}
        self.initial_library_id = value.get("initialLibraryId")
        self.private_root = self._absolute(value.get("privateStateRoot"), "privateStateRoot")
        self.data_root = self._absolute(value.get("dataRoot"), "dataRoot")
        self.cache_root = self._absolute(value.get("cacheRoot", str(self.private_root.parent / "cache")), "cacheRoot")
        self.pgdata = self._absolute(value.get("pgDataDir", str(self.data_root / "catalog" / "pg18-main")), "pgDataDir")
        if not self.pgdata.is_relative_to(self.data_root) and not value.get("allowProtectedQaPgdata", False):
            raise RuntimeError("生产 PGDATA 必须位于持久资料目录内，不能放在可清理运行状态目录。")
        if not self.pgdata.is_relative_to(self.data_root) and not self.pgdata.is_relative_to(self.private_root):
            raise RuntimeError("隔离 QA 的 PGDATA 也必须位于本次受保护目录内。")
        self.app_port, self.pg_port = int(value.get("appPort", 18761)), int(value.get("pgPort", 18762))
        if any(not 1024 <= p <= 65535 for p in (self.app_port, self.pg_port)):
            raise RuntimeError("本机端口需在 1024–65535 范围内。")
        roots = [self.repo, self.private_root, self.data_root, self.cache_root]
        if any(overlaps(a, b) for i, a in enumerate(roots) for b in roots[i + 1:]):
            raise RuntimeError("程序、资料与私有运行目录必须彼此独立，路径不能重叠。")
        self.forbidden = [self._absolute(p, "forbiddenRoot") for p in value.get("forbiddenRoots", [])]
        for forbidden in self.forbidden:
            if any(overlaps(forbidden, target) for target in (self.data_root, self.private_root, self.cache_root, self.pgdata)):
                raise RuntimeError("隔离路径与受保护原库重叠，已拒绝启动。")
        self.state_marker = self.private_root / ".reference-library-runtime-owner.json"
        self.library_marker = self.data_root / MARKER
        self.library_id = self._bind_roots()
        # First-use defaults become a persistent private machine profile before
        # the child is launched. The ProgramRoot stays read-only.
        if not self.config_path.exists():
            selected = dict(value, privateStateRoot=str(self.private_root), dataRoot=str(self.data_root), cacheRoot=str(self.cache_root), pgDataDir=str(self.pgdata), initialLibraryId=self.library_id)
            self.config_path = self.private_root / "machine-selected.json"
            atomic_json(self.config_path, selected)

    @staticmethod
    def _absolute(value, name):
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise RuntimeError(name + " 必须是绝对路径。")
        lexical = Path(value)
        for component in [lexical, *lexical.parents]:
            if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
                raise RuntimeError(name + " must use a plain path without symbolic links or junctions.")
        path = lexical.resolve()
        if path == Path(path.anchor):
            raise RuntimeError("不能把磁盘根目录用作应用目录。")
        return path

    def _bind_roots(self):
        library_owner = None
        if self.library_marker.exists():
            library_owner = json.loads(self.library_marker.read_text(encoding="utf-8"))
            if library_owner.get("owner") != "reference-library" or library_owner.get("format") != 1:
                raise RuntimeError("资料目录归属标记不合法，已拒绝连接现有资料库。")
            try: uuid.UUID(library_owner["libraryId"])
            except (KeyError, ValueError): raise RuntimeError("资料库身份标记无效。")
        elif self.data_root.exists() and any(self.data_root.iterdir()):
            raise RuntimeError("资料目录已有内容却没有归属标记，请通过迁移工具处理，启动器不会覆盖。")
        state_owner = None
        if self.state_marker.exists():
            state_owner = json.loads(self.state_marker.read_text(encoding="utf-8"))
            if state_owner.get("app") != APP_ID or Path(state_owner.get("dataRoot", "")).resolve() != self.data_root or Path(state_owner.get("privateStateRoot", "")).resolve() != self.private_root:
                raise RuntimeError("私有运行目录归属标记与配置不一致。")
        elif self.private_root.exists() and any(self.private_root.iterdir()):
            raise RuntimeError("运行目录已有内容却没有归属标记，启动器不会覆盖。")
        library_id = library_owner["libraryId"] if library_owner else state_owner["libraryId"] if state_owner else self.initial_library_id or str(uuid.uuid4())
        if state_owner and state_owner["libraryId"] != library_id:
            raise RuntimeError("资料与运行目录属于不同资料库。")
        library_owner = {"format": 1, "owner": "reference-library", "libraryId": library_id}
        state_owner = {"app": APP_ID, "libraryId": library_id, "dataRoot": str(self.data_root), "privateStateRoot": str(self.private_root), "version": 1}
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.private_root.mkdir(parents=True, exist_ok=True)
        if not self.library_marker.exists(): atomic_json(self.library_marker, library_owner)
        if not self.state_marker.exists(): atomic_json(self.state_marker, state_owner)
        private_acl(self.private_root)
        return library_id


class SingleInstance:
    def __init__(self, private_root, filename="application.lock"):
        self.path = Path(private_root) / filename
        self.file = None

    def acquire(self):
        self.file = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.file.write(b"0"); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.file.close(); self.file = None
            return False

    def release(self):
        if self.file:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close(); self.file = None


def available_loopback_port(preferred, excluded=()):
    for port in range(preferred, min(65536, preferred + 100)):
        if port in excluded:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            # Windows exclusive bind detects ports used on wildcard addresses too.
            if os.name == "nt": sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("本机端口被占用，未能找到可用的 loopback 端口。")


class RuntimeManager:
    def __init__(self, machine, binary_root=None, executable_suffix=None):
        self.machine = machine
        self.root = machine.private_root
        self.pgdata = machine.pgdata
        self.config_path = self.root / "postgresql.json"
        self.binary_root = Path(binary_root).resolve() if binary_root else machine.repo / "runtime" / "postgresql" / "windows-x64"
        self.executable_suffix = (".exe" if os.name == "nt" else "") if executable_suffix is None else executable_suffix
        self.bin = self.binary_root / "bin"
        self.logs = self.root / "logs"
        self.logs.mkdir(exist_ok=True)
        self.owner = {"app": APP_ID, "libraryId": machine.library_id, "pgdata": str(self.pgdata.resolve()), "dataRoot": str(machine.data_root)}
        self.config = None
        self.started_this_call = False

    def crypto_environment(self):
        values = {}
        config = self.binary_root / "share" / "openssl" / "openssl.cnf"
        modules = self.binary_root / "lib" / "ossl-modules"
        if config.is_file(): values["OPENSSL_CONF"] = str(config)
        if modules.is_dir(): values["OPENSSL_MODULES"] = str(modules)
        return values

    def _run(self, name, *args, timeout=60):
        env = dict(os.environ, PGCONNECT_TIMEOUT="5", PYTHONUTF8="1")
        for key in ("OPENSSL_CONF", "OPENSSL_MODULES"): env.pop(key, None)
        env.update(self.crypto_environment())
        if sys.platform == "darwin":
            env["DYLD_LIBRARY_PATH"] = str(self.binary_root / "lib")
        # Do not inherit an operator's other cluster settings or search path.
        for key in list(env):
            if key.startswith("PG") and key not in {"PGCONNECT_TIMEOUT"}: del env[key]
        command = [str(self.bin / (name + self.executable_suffix)), *map(str, args)]
        if name == "pg_ctl" and args and args[0] in ("start", "stop"):
            # pg_ctl's long-lived child must never inherit an orchestration pipe.
            with (self.logs / "pg_ctl.log").open("ab", buffering=0) as log:
                result = subprocess.run(command, cwd=self.bin, env=env, creationflags=HIDDEN, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, timeout=timeout)
            output = ""
        else:
            result = subprocess.run(command, cwd=self.bin, env=env, creationflags=HIDDEN, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", close_fds=True, timeout=timeout)
            output = result.stdout + result.stderr
        if result.returncode:
            (self.logs / (name + "-last-error.log")).write_text(output, encoding="utf-8")
            raise RuntimeError(name + " 执行失败；详情见私有运行日志。")
        return output.strip()

    def verify_binaries(self):
        manifest = json.loads((self.binary_root / "manifest.json").read_text(encoding="utf-8"))
        manifest_path = self.binary_root / "manifest.json"
        fingerprint = {"manifestSha256": hashlib.file_digest(manifest_path.open("rb"), "sha256").hexdigest(), "runtimePath": str(self.binary_root.resolve()), "files": []}
        for item in manifest["files"]:
            path = self.binary_root / item["path"]
            if not path.is_file() or path.stat().st_size != item["bytes"]:
                raise RuntimeError("PostgreSQL 随包文件缺失或大小不一致。")
            fingerprint["files"].append([item["path"], path.stat().st_size, path.stat().st_mtime_ns])
        cache = self.root / "native-validation.json"
        cached = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else None
        if cached != fingerprint:
            for item in manifest["files"]:
                path = self.binary_root / item["path"]
                if hashlib.file_digest(path.open("rb"), "sha256").hexdigest() != item["sha256"]:
                    raise RuntimeError("PostgreSQL 随包文件校验失败，请重新解压完整发布包。")
            atomic_json(cache, fingerprint)
        expected = "PostgreSQL) " + manifest["postgresqlVersion"]
        for name in ("postgres", "initdb", "pg_ctl", "pg_dump", "pg_restore"):
            if expected not in self._run(name, "--version"):
                raise RuntimeError("PostgreSQL 服务与工具版本不匹配。")

    def _load_config(self):
        if self.config_path.exists():
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
            if value.get("libraryId") != self.machine.library_id or value.get("host") != "127.0.0.1" or Path(value.get("pgdata", "")).resolve() != self.pgdata.resolve() or Path(value.get("dataRoot", "")).resolve() != self.machine.data_root:
                raise RuntimeError("PostgreSQL 私有配置归属不一致。")
            if not 1024 <= int(value.get("port", 0)) <= 65535:
                raise RuntimeError("PostgreSQL 私有端口无效。")
            self.config = value
            return
        if self.pgdata.exists() and any(self.pgdata.iterdir()):
            raise RuntimeError("发现未归属的现有 PGDATA，启动器不会初始化或覆盖。")
        value = {"host": "127.0.0.1", "port": available_loopback_port(self.machine.pg_port, [self.machine.app_port]), "dbname": "reference_library", "user": "library_app", "password": secrets.token_urlsafe(40), "adminUser": "library_owner", "adminPassword": secrets.token_urlsafe(48), "libraryId": self.machine.library_id, "pgdata": str(self.pgdata.resolve()), "dataRoot": str(self.machine.data_root), "runtimeConfigVersion": 1, "setupComplete": False}
        atomic_json(self.config_path, value)
        self.config = value

    def _validate_cluster(self):
        marker = self.pgdata / MARKER
        if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8")) != self.owner:
            raise RuntimeError("PGDATA 没有匹配的应用归属标记，已拒绝操作。")
        if (self.pgdata / "PG_VERSION").read_text().strip() != "18":
            raise RuntimeError("现有 PostgreSQL 主版本与发布包不一致，必须先执行显式升级。")

    def _connection(self, admin=False, database=None):
        import psycopg
        cfg = self.config
        return psycopg.connect(host="127.0.0.1", hostaddr="127.0.0.1", sslmode="disable", options="-c search_path=pg_catalog,public", port=cfg["port"], dbname=database or cfg["dbname"], user=cfg["adminUser"] if admin else cfg["user"], password=cfg["adminPassword"] if admin else cfg["password"], connect_timeout=5, autocommit=True)

    def _initialize(self):
        self.pgdata.mkdir(parents=True, exist_ok=True)
        private_acl(self.pgdata)
        if any(self.pgdata.iterdir()):
            raise RuntimeError("PGDATA 已有内容，绝不覆盖初始化。")
        password_file = self.root / (".initdb-password-" + secrets.token_hex(8))
        try:
            password_file.write_text(self.config["adminPassword"], encoding="utf-8")
            self._run("initdb", "-D", self.pgdata, "--username=" + self.config["adminUser"], "--pwfile=" + str(password_file), "--auth-host=scram-sha-256", "--auth-local=scram-sha-256", "--encoding=UTF8", "--locale=C", "--data-checksums", timeout=120)
        finally:
            password_file.unlink(missing_ok=True)
        atomic_json(self.pgdata / MARKER, self.owner)
        (self.pgdata / "pg_hba.conf").write_text("# Private application cluster: authenticated loopback only.\nlocal all all scram-sha-256\nhost all all 127.0.0.1/32 scram-sha-256\nhost all all ::1/128 reject\n", encoding="utf-8")
        self._write_server_config()
        self._start()
        self._provision_database()

    def _provision_database(self):
        # Resume an interrupted first setup without reinitializing PGDATA.
        from psycopg import sql
        with self._connection(admin=True, database="postgres") as connection:
            if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (self.config["user"],)).fetchone():
                connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION").format(sql.Identifier(self.config["user"]), sql.Literal(self.config["password"])))
            if not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (self.config["dbname"],)).fetchone():
                connection.execute(sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8' TEMPLATE template0").format(sql.Identifier(self.config["dbname"]), sql.Identifier(self.config["user"])))
        with self._connection(admin=True) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS public.reference_library_runtime_identity (singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), library_id text NOT NULL UNIQUE)")
            connection.execute("INSERT INTO public.reference_library_runtime_identity VALUES (true, %s) ON CONFLICT(singleton) DO NOTHING", (self.machine.library_id,))
            actual = connection.execute("SELECT library_id FROM public.reference_library_runtime_identity WHERE singleton").fetchone()
            if not actual or actual[0] != self.machine.library_id:
                raise RuntimeError("首次设置目标库身份不一致，已拒绝继续。")
            connection.execute(sql.SQL("GRANT SELECT ON public.reference_library_runtime_identity TO {}").format(sql.Identifier(self.config["user"])))
            connection.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            connection.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(sql.Identifier(self.config["user"])))
        self.config["setupComplete"] = True
        atomic_json(self.config_path, self.config)

    def _write_server_config(self):
        # A bundled dedicated config overrides only this managed cluster.
        config = self.pgdata / "reference-library.conf"
        config.write_text("listen_addresses = '127.0.0.1'\nport = " + str(self.config["port"]) + "\npassword_encryption = 'scram-sha-256'\nssl = off\nunix_socket_directories = ''\nmax_connections = 32\nshared_buffers = '64MB'\nwork_mem = '8MB'\nmaintenance_work_mem = '64MB'\nlogging_collector = off\nlog_statement = 'none'\nlog_min_error_statement = 'panic'\n", encoding="utf-8")
        pgconf = self.pgdata / "postgresql.conf"
        content = pgconf.read_text(encoding="utf-8")
        if "include = 'reference-library.conf'" not in content:
            with pgconf.open("a", encoding="utf-8") as file: file.write("\ninclude = 'reference-library.conf'\n")

    def _pidfile(self):
        path = self.pgdata / "postmaster.pid"
        if not path.exists(): return None
        parts = path.read_text().splitlines()
        if len(parts) < 6 or Path(parts[1]).resolve() != self.pgdata.resolve() or parts[5].strip() != "127.0.0.1":
            raise RuntimeError("PostgreSQL 进程标记与私有实例不一致。")
        return parts

    @staticmethod
    def _windows_parent_pid(pid):
        import ctypes
        from ctypes import wintypes
        class ProcessEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot in (None, ctypes.c_void_p(-1).value):
            raise RuntimeError("Cannot verify PostgreSQL backend process parent.")
        try:
            entry = ProcessEntry(); entry.dwSize = ctypes.sizeof(entry)
            found = kernel.Process32FirstW(ctypes.c_void_p(snapshot), ctypes.byref(entry))
            while found:
                if entry.th32ProcessID == pid: return int(entry.th32ParentProcessID)
                found = kernel.Process32NextW(ctypes.c_void_p(snapshot), ctypes.byref(entry))
            raise RuntimeError("PostgreSQL ownership probe backend no longer exists.")
        finally:
            kernel.CloseHandle(ctypes.c_void_p(snapshot))

    def _owned_alive(self):
        parts = self._pidfile()
        if parts is None: return False
        pid = int(parts[0])
        if os.name == "nt":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle: return False
            try:
                size = ctypes.c_ulong(32768)
                buffer = ctypes.create_unicode_buffer(size.value)
                if not kernel.QueryFullProcessImageNameW(ctypes.c_void_p(handle), 0, buffer, ctypes.byref(size)):
                    raise RuntimeError("无法核对 PostgreSQL 进程归属。")
                if Path(buffer.value).resolve() != (self.bin / ("postgres" + self.executable_suffix)).resolve():
                    raise RuntimeError("PID 对应其他程序，已拒绝操作。")
                from ctypes import wintypes
                created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
                if not kernel.GetProcessTimes(ctypes.c_void_p(handle), ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
                    raise RuntimeError("Cannot verify PostgreSQL process creation time.")
                created_ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
                created_unix_seconds = (created_ticks - 116444736000000000) // 10000000
                reported_start = int(parts[2])
                # PG records its start after Windows DLL loading. The narrow
                # startup interval is combined with an exact live backend-parent
                # and PGDATA check, so even same-second PID reuse is refused.
                if not 0 <= reported_start - created_unix_seconds <= 2:
                    raise RuntimeError("PostgreSQL PID was reused or its start time does not match.")
                with self._connection(admin=True, database="postgres") as connection:
                    actual_data, backend_pid, sql_started = connection.execute("SELECT current_setting('data_directory'), pg_backend_pid(), floor(extract(epoch FROM pg_postmaster_start_time()))::bigint").fetchone()
                    if Path(actual_data).resolve() != self.pgdata.resolve() or int(sql_started) < reported_start or self._windows_parent_pid(int(backend_pid)) != pid:
                        raise RuntimeError("PostgreSQL live process belongs to a different cluster.")
                return True
            finally: kernel.CloseHandle(ctypes.c_void_p(handle))
        try: os.kill(pid, 0); return True
        except ProcessLookupError: return False

    def _start(self):
        self._validate_cluster()
        if self._owned_alive(): return
        preferred = int(self.config["port"])
        port = available_loopback_port(preferred, [self.machine.app_port])
        if port != preferred:
            self.config["port"] = port
            atomic_json(self.config_path, self.config)
        self._write_server_config()
        self.started_this_call = True
        self._run("pg_ctl", "start", "-D", self.pgdata, "-l", self.logs / "postgresql.log", "-w", "-t", "300", timeout=330)

    def start(self):
        self.verify_binaries()
        # Process-local only, before libpq initializes crypto. Relocation never
        # falls back to this build machine's logical OpenSSL provider prefix.
        for key in ("OPENSSL_CONF", "OPENSSL_MODULES"): os.environ.pop(key, None)
        os.environ.update(self.crypto_environment())
        self._load_config()
        if not (self.pgdata / "PG_VERSION").exists():
            self._initialize()
        else:
            self._validate_cluster()
            self._start()
            if not self.config.get("setupComplete"):
                self._provision_database()
        with self._connection() as connection:
            actual = connection.execute("SELECT library_id FROM public.reference_library_runtime_identity WHERE singleton").fetchone()
            if not actual or actual[0] != self.machine.library_id:
                raise RuntimeError("数据库身份与资料目录不一致。")
            settings = dict(connection.execute("SELECT name, setting FROM pg_settings WHERE name IN ('listen_addresses','password_encryption')").fetchall())
            if settings != {"listen_addresses": "127.0.0.1", "password_encryption": "scram-sha-256"}:
                raise RuntimeError("PostgreSQL loopback/SCRAM 安全配置不一致。")
        return self.config_path

    def stop(self):
        if not self.config:
            self._load_config()
        self._validate_cluster()
        if self._owned_alive():
            self._run("pg_ctl", "stop", "-D", self.pgdata, "-m", "fast", "-w", "-t", "45", timeout=60)
