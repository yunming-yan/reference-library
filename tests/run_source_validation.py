"""Source-only CI using synthetic bytes; never starts PostgreSQL or a native app.

Node is used only for development JavaScript syntax checks. Native runtime,
browser behavior, real PostgreSQL integration and production report acceptance
are separate checks; this runner must not be presented as those checks.
"""
from __future__ import annotations

import ast
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PURE_TESTS = ("test_backend_api.py", "test_cache_sqlite.py", "test_platform_directories.py")


def main() -> int:
    if os.environ.get("REFERENCE_LIBRARY_TEST_PG_CONFIG"):
        raise RuntimeError("Source CI refuses inherited PostgreSQL test configuration")
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node is required for this development check, not app runtime")
    python_files = sorted(
        p for p in ROOT.rglob("*.py")
        if not {".git", ".venv", "__pycache__"}.intersection(p.relative_to(ROOT).parts)
    )
    for path in python_files:
        ast.parse(path.read_text(encoding="utf-8-sig"), filename=path.relative_to(ROOT).as_posix())
    js_files = sorted((ROOT / "app" / "web").glob("*.js"))
    for path in js_files:
        subprocess.run([node, "--check", str(path)], cwd=ROOT, check=True)
    versions = {}
    for name in ("psycopg", "psycopg-pool", "apsw", "Markdown"):
        versions[name] = importlib.metadata.version(name)
    print(json.dumps({
        "scope": "source syntax and synthetic standalone tests only",
        "python": sys.version.split()[0],
        "node": subprocess.check_output([node, "--version"], text=True).strip(),
        "dependencies": versions,
        "pythonFilesParsed": len(python_files),
        "javascriptFilesChecked": len(js_files),
        "selectedTestModules": list(PURE_TESTS),
        "postgresqlServerStarted": False,
        "nativePlatformAcceptance": False,
    }, ensure_ascii=False), flush=True)
    temp_parent = Path(os.environ.get("REFERENCE_LIBRARY_CI_ROOT", tempfile.gettempdir()))
    temp_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reference-library-source-ci-", dir=temp_parent) as temp:
        saved = {key: os.environ.get(key) for key in (
            "BACKEND_TEST_ROOT", "REFERENCE_LIBRARY_TEST_ROOT"
        )}
        os.environ["BACKEND_TEST_ROOT"] = temp
        os.environ["REFERENCE_LIBRARY_TEST_ROOT"] = temp
        try:
            suite = unittest.TestSuite()
            for name in PURE_TESTS:
                suite.addTests(unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=name))
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            print(json.dumps({
                "testsRun": result.testsRun,
                "failures": len(result.failures),
                "errors": len(result.errors),
                "skips": len(result.skipped),
                "status": "PASS" if result.wasSuccessful() else "FAIL",
                "scope": "this source-only runner; no PostgreSQL/native/browser performance claim",
            }), flush=True)
            return 0 if result.wasSuccessful() else 1
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    raise SystemExit(main())
