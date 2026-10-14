"""Host-executable contract checks; never claims native macOS execution."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "app"))
import runtime_manager
import launch_macos


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    work = args.work.resolve()
    if work.exists(): raise RuntimeError("Use a fresh isolated contract directory")
    work.mkdir(parents=True)
    program, home = work / "Program", work / "synthetic-home"
    program.mkdir(); home.mkdir()
    checks = []
    def passed(name, **details):
        checks.append({"check": name, "passed": True, **details})
    def rejected(call, name):
        try: call()
        except RuntimeError:
            passed(name)
        else: raise AssertionError(name + " was not rejected")
    with patch.object(runtime_manager, "sys", SimpleNamespace(platform="darwin")), patch.object(runtime_manager, "private_acl", lambda p: None), patch.object(runtime_manager, "_mac_user_directories", return_value=(home / "Library" / "Application Support", home / "Library" / "Caches")), patch.object(launch_macos, "REPO", program), patch.dict("os.environ", {}, clear=True):
        first = launch_macos.select_machine()
        expected_data = home / "Library" / "Application Support" / "ReferenceLibrary" / "Library"
        expected_state = home / "Library" / "Application Support" / "ReferenceLibrary" / "Runtime" / first.library_id
        expected_cache = home / "Library" / "Caches" / "ReferenceLibrary" / first.library_id
        assert first.data_root == expected_data and first.private_root == expected_state and first.cache_root == expected_cache
        passed("Mocked NSFileManager boundary selects user-domain Application Support and Caches")
        assert first.pgdata == first.data_root / "catalog" / "pg18-main"
        assert not runtime_manager.overlaps(first.pgdata, first.private_root) and not runtime_manager.overlaps(first.pgdata, first.cache_root)
        passed("PGDATA remains persistent Library content, disjoint from State and Cache")
        roots = (program, first.data_root, first.private_root, first.cache_root)
        assert not any(runtime_manager.overlaps(a,b) for i,a in enumerate(roots) for b in roots[i+1:])
        passed("Program/Library/State/Cache have no collisions")
        assert first.config_path == first.private_root / "machine-selected.json" and first.config_path.is_file()
        assert not (program / "machine.json").exists()
        passed("First-use machine profile exists in private state before worker launch")
        second = launch_macos.select_machine()
        child = launch_macos.select_machine(first.config_path)
        for item in (second, child):
            assert (item.library_id, item.data_root, item.private_root, item.cache_root, item.pgdata) == (first.library_id, first.data_root, first.private_root, first.cache_root, first.pgdata)
        passed("Implicit reopen and explicit persisted worker profile share the same UUID and roots")
        marker_snapshot = {str(p):p.read_bytes() for p in (first.library_marker, first.state_marker)}
        missing = work / "missing-profile.json"
        rejected(lambda: launch_macos.select_machine(missing), "Explicit missing --machine profile is rejected")
        assert not missing.exists() and marker_snapshot == {str(p):p.read_bytes() for p in (first.library_marker, first.state_marker)}
        passed("Missing custom profile does not create roots or rewrite default markers")
        with patch.dict("os.environ", {"REFERENCE_LIBRARY_MACHINE_CONFIG": str(missing)}):
            rejected(lambda: launch_macos.select_machine(), "Explicit missing environment profile is rejected")
        bad = work / "overlap-profile.json"
        bad.write_text(json.dumps({"dataRoot": str(work / "collision"), "privateStateRoot":str(work / "collision"), "cacheRoot":str(work / "cache-uncreated")}), "utf-8")
        rejected(lambda: launch_macos.select_machine(bad), "Overlapping custom roots are rejected before creating them")
        assert not (work / "collision").exists() and not (work / "cache-uncreated").exists()
        passed("Rejected overlap leaves declared directories absent")
        with patch.object(Path, "is_symlink", lambda p: p == home / "Library"):
            rejected(lambda: launch_macos.select_machine(first.config_path), "Mocked symbolic-link ancestor is rejected")
        with patch.object(Path, "is_junction", lambda p: p == home / "Library"):
            rejected(lambda: launch_macos.select_machine(first.config_path), "Mocked junction ancestor is rejected")
        class FakeLock:
            def __init__(self,*args): pass
            def acquire(self): return True
            def release(self): pass
        calls = []
        def fake_run(command, **kwargs):
            calls.append((command,kwargs))
            return SimpleNamespace(returncode=0)
        fake_python = first.private_root / "python" / "test-only-python"
        with patch.object(launch_macos, "check_python", lambda: "arm64"), patch.object(launch_macos, "SingleInstance", FakeLock), patch.object(launch_macos, "ensure_environment", lambda repo,state: fake_python), patch.object(launch_macos.subprocess, "run", fake_run), patch.object(sys, "argv", [str(HERE / "launch_macos.py"), "start", "--no-browser"]):
            launch_macos.main()
        assert len(calls)==1
        command, kwargs = calls[0]
        profile = Path(command[command.index("--machine")+1])
        assert profile.is_file() and profile == first.config_path and "runtime" in command and "-I" in command and "--no-browser" in command
        assert kwargs["env"]["PSYCOPG_IMPL"] == "python"
        passed("Parent worker argv passes an existing persisted private profile; invocation is mocked")
        assert "D:" not in (HERE / "launch_macos.py").read_text("utf-8") and "D:" not in (HERE / "bootstrap_macos.py").read_text("utf-8")
        passed("Daily Mac launcher/bootstrap contain no D-drive-specific path")
    report = {"format":1,"executionHost":"Windows CPython 3.14.8","nativeMacExecution":False,"nativeMacDirectoryApiExecution":False,"directoryBoundary":"Mocked NSFileManager user-domain return values","scope":"Pure configuration, mocked native-directory boundary and mocked parent-worker contract; no PG, Safari, Finder, dyld or libproc execution","checkedAtUTC":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"checks":checks,"hashes":{"launch_macos.py":digest(HERE/"launch_macos.py"),"bootstrap_macos.py":digest(HERE/"bootstrap_macos.py"),"runtime_manager.py":digest(REPO/"app/runtime_manager.py")}}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n","utf-8")
    print(json.dumps({"contractChecks":len(checks),"passed":True,"nativeMacPassClaimed":False,"output":str(args.output)}))


if __name__=="__main__": main()
