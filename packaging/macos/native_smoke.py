"""Run on each physical Mac with the final extracted bundle; no global installation."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "app"))
from bootstrap_macos import check_python, child_environment, ensure_environment


def main():
    arch = check_python()
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {"platform": platform.platform(), "architecture": arch, "python": platform.python_version(), "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "native": True, "checks": []}
    runtime = REPO / "runtime" / "postgresql" / "macos-universal2"
    env = child_environment(REPO)
    # This disposable synthetic cluster never points at the default library.
    with tempfile.TemporaryDirectory(prefix="reference-library-native-smoke-") as work:
        work = Path(work)
        state = work / "private"
        state.mkdir()
        (state / "logs").mkdir()
        python = ensure_environment(REPO, state)
        cluster = work / "pgdata"
        for program in ("postgres", "initdb", "pg_ctl", "pg_dump", "pg_restore", "psql"):
            result = subprocess.run([str(runtime / "bin" / program), "--version"], env=env, capture_output=True, text=True, check=True)
            assert "18.6" in result.stdout, result.stdout
            report["checks"].append({"program": program, "version": result.stdout.strip(), "passed": True})
        result = subprocess.run([str(python), "-I", "-c", "import psycopg,apsw,markdown,json; print(json.dumps({'psycopg':psycopg.__version__,'implementation':psycopg.pq.__impl__,'libpq':psycopg.pq.version(),'apsw':apsw.apswversion(),'sqlite':apsw.sqlitelibversion(),'markdown':markdown.__version__}))"], env=env, capture_output=True, text=True, check=True)
        report["dependencies"] = json.loads(result.stdout)
        subprocess.run([str(runtime / "bin" / "initdb"), "-D", str(cluster), "--auth=trust", "--encoding=UTF8", "--locale=C", "--data-checksums"], env=env, capture_output=True, check=True)
        # No listening socket or TCP: this checks a synthetic standalone backend.
        result = subprocess.run([str(runtime / "bin" / "postgres"), "--single", "-D", str(cluster), "postgres"], input="SELECT version(); CREATE EXTENSION pg_trgm; SELECT 'abc' % 'abcd';\n", env=env, capture_output=True, text=True, check=True)
        report["checks"].append({"program": "single-user backend + pg_trgm", "passed": True, "stdout": result.stdout, "stderr": result.stderr})
    report.update(finderDoubleClick="not covered by this CLI smoke", safari="not covered; run manual matrix in docs/macos.md", developerId="not covered", notarization="not covered", elapsedStatus="native smoke completed")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(str(args.output))


if __name__ == "__main__":
    main()
