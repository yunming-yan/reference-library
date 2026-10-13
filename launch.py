"""One-click Windows entry point, with private offline bootstrap and PG ownership."""
from __future__ import annotations
import argparse
import ctypes
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parent
_LAST_MACHINE = None
sys.path.insert(0, str(REPO / "app"))
from bootstrap import ensure_environment
from runtime_manager import MachineConfig, RuntimeManager, SingleInstance, available_loopback_port, atomic_json

# These requests always address this application on loopback.
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def clean_pg_environment(environment):
    return {key: value for key, value in environment.items() if not key.startswith("PG")}


def get_health(url):
    with LOCAL_OPENER.open(url + "/api/health", timeout=2) as response:
        return json.load(response)


def open_browser(url):
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as key:
                    executable = Path(winreg.QueryValue(key, None))
                if executable.is_file():
                    subprocess.Popen([str(executable), url], creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
                    return
            except OSError:
                continue
        os.startfile(url)
    else:
        import webbrowser
        webbrowser.open(url)


def read_instance(machine):
    path = machine.private_root / "instance.json"
    if not path.exists(): return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("libraryId") != machine.library_id or not 1024 <= int(value.get("port", 0)) <= 65535:
        raise RuntimeError("应用运行标记归属不一致，已拒绝操作。")
    url = "http://127.0.0.1:" + str(value["port"])
    try:
        health = get_health(url)
    except (OSError, ValueError):
        return None
    if health.get("instanceId") != value.get("instanceId") or health.get("libraryId") != machine.library_id:
        raise RuntimeError("端口由其他进程使用，已拒绝操作。")
    return value, url


def stop_application(machine):
    existing = read_instance(machine)
    if not existing:
        return
    instance, url = existing
    request = urllib.request.Request(url + "/api/shutdown", data=b"{}", method="POST", headers={"Origin": url, "Content-Type": "application/json", "X-Moon-Request": "1"})
    with LOCAL_OPENER.open(request, timeout=10) as response:
        response.read()
    # The launcher owns PG and stops it only after the API has closed its workers.
    for _ in range(300):
        if not (machine.private_root / "instance.json").exists(): return
        time.sleep(0.1)
    raise RuntimeError("应用仍在完成后台操作；请查看私有运行日志。")


def run_runtime(machine, no_browser=False):
    runtime = RuntimeManager(machine)
    server = None
    pg_started = False
    instance_path = machine.private_root / "instance.json"
    try:
        pg_config = runtime.start()
        pg_started = True
        port = available_loopback_port(machine.app_port, [runtime.config["port"]])
        instance_id = str(uuid.uuid4())
        env = dict(clean_pg_environment(os.environ), REFERENCE_LIBRARY_PG_CONFIG=str(pg_config), REFERENCE_LIBRARY_DATA=str(machine.data_root), REFERENCE_LIBRARY_INSTANCE_ID=instance_id, REFERENCE_LIBRARY_CACHE_ROOT=str(machine.cache_root), REFERENCE_LIBRARY_CACHE=str(machine.cache_root), PYTHONNOUSERSITE="1", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", PSYCOPG_IMPL="c")
        for key in ("REFERENCE_LIBRARY_PG_DSN", "REFERENCE_LIBRARY_DSN", "PKM_PG_DSN"):
            env.pop(key, None)
        log_path = machine.private_root / "logs" / "application.log"
        with log_path.open("ab", buffering=0) as log:
            server = subprocess.Popen([sys.executable, str(REPO / "app" / "server.py"), "--port", str(port), "--data", str(machine.data_root)], cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            url = "http://127.0.0.1:" + str(port)
            for _ in range(300):
                if server.poll() is not None:
                    raise RuntimeError("资料库服务未完成启动，请查看私有状态目录的 logs/application.log。")
                try:
                    health = get_health(url)
                    if health.get("instanceId") == instance_id:
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("资料库服务未在预定时间内就绪。")
            atomic_json(instance_path, {"libraryId": machine.library_id, "instanceId": instance_id, "port": port, "pid": server.pid, "launcherPid": os.getpid()})
            if not no_browser:
                open_browser(url)
            server.wait()
            if server.returncode:
                raise RuntimeError("资料库服务退出异常；PostgreSQL 已受控关闭，详情见私有日志。")
    finally:
        if server is not None and server.poll() is None:
            # Only signal the exact child object this launcher created.
            try:
                existing = read_instance(machine)
                if existing:
                    instance, url = existing
                    request = urllib.request.Request(url + "/api/shutdown", data=b"{}", method="POST", headers={"Origin": url, "X-Moon-Request": "1", "Content-Type": "application/json"})
                    LOCAL_OPENER.open(request, timeout=5).close()
                    server.wait()
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            if server.poll() is None:
                server.terminate(); server.wait(timeout=10)
        if pg_started or runtime.started_this_call:
            runtime.stop()
        if instance_path.exists():
            instance_path.unlink()


def main():
    global _LAST_MACHINE
    parser = argparse.ArgumentParser(description="综合资料库 Windows 本机启动器")
    parser.add_argument("action", choices=("start", "stop", "runtime"), nargs="?", default="start")
    parser.add_argument("--machine", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    machine = MachineConfig(REPO, args.machine)
    _LAST_MACHINE = machine
    if args.action == "stop":
        stop_application(machine)
        return
    if args.action == "runtime":
        runtime_lock = SingleInstance(machine.private_root, "runtime.lock")
        if not runtime_lock.acquire():
            raise RuntimeError("该资料库的运行实例已经在启动或运行。")
        library_lock = SingleInstance(machine.data_root, ".launcher.lock")
        if not library_lock.acquire():
            runtime_lock.release()
            raise RuntimeError("同一资料库已由另一个启动实例打开。")
        try:
            run_runtime(machine, args.no_browser)
        finally:
            library_lock.release()
            runtime_lock.release()
        return
    singleton = SingleInstance(machine.private_root)
    if not singleton.acquire():
        for _ in range(300):
            existing = read_instance(machine)
            if existing:
                if not args.no_browser: open_browser(existing[1])
                return
            time.sleep(0.1)
        raise RuntimeError("资料库正在启动或完成关闭；请稍后再试。")
    try:
        executable = ensure_environment(REPO, machine.private_root)
        command = [str(executable), str(REPO / "launch.py"), "runtime", "--machine", str(machine.config_path)]
        if args.no_browser: command.append("--no-browser")
        result = subprocess.run(command, env=dict(clean_pg_environment(os.environ), PYTHONDONTWRITEBYTECODE="1"), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stdin=subprocess.DEVNULL)
        if result.returncode:
            raise RuntimeError("资料库启动或关闭未完成；详情见私有状态目录的运行日志。")
    finally:
        singleton.release()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Tracebacks may contain local paths; they remain in the private state root.
        try:
            # Never derive a writable log root from an invalid/forbidden config.
            if _LAST_MACHINE is not None:
                root = _LAST_MACHINE.private_root
                (root / "logs").mkdir(exist_ok=True)
                (root / "logs" / "launcher-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        message = str(error) if isinstance(error, RuntimeError) else "资料库未能启动。请检查本机配置与私有运行日志。"
        if os.name == "nt" and Path(sys.executable).name.lower() == "pythonw.exe" and os.environ.get("REFERENCE_LIBRARY_DIAGNOSTIC") != "1":
            ctypes.windll.user32.MessageBoxW(0, message, "综合资料库", 0x10)
        else:
            print(message, file=sys.stderr)
        sys.exit(1)
