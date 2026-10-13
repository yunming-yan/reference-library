"""Finder launcher: private offline environment, PostgreSQL and native Safari."""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import urllib.request
import uuid

LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(HERE))
from bootstrap_macos import check_python, child_environment, ensure_environment
from runtime_manager import MachineConfig, RuntimeManager, SingleInstance, available_loopback_port, atomic_json


class BsdInfo(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in ("flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "rfu1")] + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)] + [(n, ctypes.c_uint32) for n in ("nfiles", "pgid", "pjobc", "e_tdev", "e_tpgid")] + [("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]


class MacRuntimeManager(RuntimeManager):
    def __init__(self, machine):
        super().__init__(machine, binary_root=machine.repo / "runtime" / "postgresql" / "macos-universal2", executable_suffix="")

    def _owned_alive(self):
        parts = self._pidfile()
        if parts is None:
            return False
        pid = int(parts[0])
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        library.proc_pidpath.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        library.proc_pidpath.restype = ctypes.c_int
        path = ctypes.create_string_buffer(4096)
        if library.proc_pidpath(pid, path, len(path)) <= 0:
            if ctypes.get_errno() == errno.ESRCH:
                return False
            raise RuntimeError("无法核对 PostgreSQL 原生进程路径，已拒绝操作。")
        if Path(os.fsdecode(path.value)).resolve() != (self.bin / "postgres").resolve():
            raise RuntimeError("PID 对应其他程序，已拒绝操作。")
        library.proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
        library.proc_pidinfo.restype = ctypes.c_int
        info = BsdInfo()
        count = library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if count != ctypes.sizeof(info) or info.pid != pid or abs(info.start_sec - int(parts[2])) > 2:
            raise RuntimeError("PostgreSQL 进程启动时间与私有实例不一致，已拒绝操作。")
        return True


def get_health(url):
    with LOCAL_OPENER.open(url + "/api/health", timeout=2) as response:
        return json.load(response)


def open_safari(url):
    subprocess.run(["/usr/bin/open", "-a", "Safari", url], check=True, timeout=15)


def read_instance(machine):
    path = machine.private_root / "instance.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text("utf-8"))
    if value.get("libraryId") != machine.library_id or not 1024 <= int(value.get("port", 0)) <= 65535:
        raise RuntimeError("应用实例标记与资料库归属不一致。")
    url = "http://127.0.0.1:" + str(value["port"])
    try:
        health = get_health(url)
    except (OSError, ValueError):
        return None
    if health.get("instanceId") != value.get("instanceId"):
        raise RuntimeError("端口由其他进程使用，已拒绝操作。")
    return value, url


def request_stop(url):
    request = urllib.request.Request(url + "/api/shutdown", data=b"{}", method="POST", headers={"Origin": url, "Content-Type": "application/json", "X-Moon-Request": "1"})
    with LOCAL_OPENER.open(request, timeout=10) as response:
        response.read()


def stop_application(machine):
    existing = read_instance(machine)
    if not existing:
        return
    request_stop(existing[1])
    for _ in range(300):
        if not (machine.private_root / "instance.json").exists():
            return
        time.sleep(0.1)
    raise RuntimeError("应用仍在完成后台操作；请查看私有运行日志。")


def run_runtime(machine, no_browser=False):
    runtime = MacRuntimeManager(machine)
    server = None
    pg_ready = False
    instance_path = machine.private_root / "instance.json"
    try:
        pg_config = runtime.start()
        pg_ready = True
        port = available_loopback_port(machine.app_port, [runtime.config["port"]])
        instance_id = str(uuid.uuid4())
        env = child_environment(REPO)
        env.update(REFERENCE_LIBRARY_PG_CONFIG=str(pg_config), REFERENCE_LIBRARY_DATA=str(machine.data_root), REFERENCE_LIBRARY_INSTANCE_ID=instance_id, REFERENCE_LIBRARY_CACHE=str(machine.cache_root))
        for key in ("REFERENCE_LIBRARY_PG_DSN", "REFERENCE_LIBRARY_DSN", "PKM_PG_DSN"):
            env.pop(key, None)
        with (machine.private_root / "logs" / "application.log").open("ab", buffering=0) as log:
            server = subprocess.Popen([sys.executable, "-I", "-B", str(HERE / "server_entry.py"), "--port", str(port), "--data", str(machine.data_root)], cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True)
            url = "http://127.0.0.1:" + str(port)
            for _ in range(300):
                if server.poll() is not None:
                    raise RuntimeError("资料库服务未完成启动；详情见私有运行日志。")
                try:
                    if get_health(url).get("instanceId") == instance_id:
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("资料库服务未在预定时间内就绪。")
            atomic_json(instance_path, {"libraryId": machine.library_id, "instanceId": instance_id, "port": port, "pid": server.pid, "launcherPid": os.getpid()})
            if not no_browser:
                open_safari(url)
            server.wait()
            if server.returncode:
                raise RuntimeError("资料库服务退出异常；详情见私有运行日志。")
    finally:
        if server is not None and server.poll() is None:
            try:
                existing = read_instance(machine)
                if existing:
                    request_stop(existing[1])
                    server.wait(timeout=30)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            if server.poll() is None:
                server.terminate()
                server.wait(timeout=10)
        if pg_ready or getattr(runtime, "started_this_call", False):
            runtime.stop()
        if instance_path.exists():
            instance_path.unlink()


def select_machine(path=None):
    # Passing a missing path means an explicit user typo, never first-use defaults.
    selected = path or os.environ.get("REFERENCE_LIBRARY_MACHINE_CONFIG")
    machine = MachineConfig(REPO, selected) if selected else MachineConfig(REPO)
    if not machine.config_path.is_file():
        raise RuntimeError("The private machine profile was not persisted before child launch.")
    return machine


def main():
    check_python()
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="综合资料库 Mac 本机启动器")
    parser.add_argument("action", choices=("start", "stop", "runtime"), nargs="?", default="start")
    parser.add_argument("--machine", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    machine = select_machine(args.machine)
    if args.action == "stop":
        stop_application(machine)
        return
    library_lock = SingleInstance(machine.data_root, ".launcher.lock") if args.action == "start" else None
    if library_lock and not library_lock.acquire():
        for _ in range(300):
            existing = read_instance(machine)
            if existing:
                if not args.no_browser:
                    open_safari(existing[1])
                return
            time.sleep(0.1)
        raise RuntimeError("此资料库正在启动或完成关闭；请稍后再试。")
    lock = SingleInstance(machine.private_root, "runtime.lock" if args.action == "runtime" else "application.lock")
    if not lock.acquire():
        if library_lock:
            library_lock.release()
        if args.action == "start":
            for _ in range(300):
                existing = read_instance(machine)
                if existing:
                    if not args.no_browser:
                        open_safari(existing[1])
                    return
                time.sleep(0.1)
        raise RuntimeError("资料库正在启动或完成关闭；请稍后再试。")
    try:
        if args.action == "runtime":
            run_runtime(machine, args.no_browser)
        else:
            executable = ensure_environment(REPO, machine.private_root)
            command = [str(executable), "-I", "-B", str(HERE / "launch_macos.py"), "runtime", "--machine", str(machine.config_path)]
            if args.no_browser:
                command.append("--no-browser")
            if subprocess.run(command, env=child_environment(REPO), stdin=subprocess.DEVNULL, close_fds=True).returncode:
                raise RuntimeError("资料库未完成启动或关闭；详情见私有运行日志。")
    finally:
        lock.release()
        if library_lock:
            library_lock.release()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Finder's fixed-location private log receives this traceback.
        traceback.print_exc()
        print(str(error) if isinstance(error, RuntimeError) else "资料库未能启动，请查看私有日志。", file=sys.stderr)
        sys.exit(1)
