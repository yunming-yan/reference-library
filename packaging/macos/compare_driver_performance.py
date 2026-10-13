"""Native-only synthetic microcomparison; does not claim application scale SLOs."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "app"))
from bootstrap_macos import check_python, child_environment
from runtime_manager import available_loopback_port

BENCH = r'''
import ctypes.util,json,os,statistics,time,psycopg
assert psycopg.pq.version()==180006
with psycopg.connect(host="127.0.0.1",port=int(os.environ["BENCH_PORT"]),dbname="postgres",user=os.environ["BENCH_USER"],autocommit=True) as conn:
    conn.execute("CREATE TEMP TABLE benchmark_rows AS SELECT g AS id,repeat('reference-library-',8) AS body FROM generate_series(1,10000) g")
    for _ in range(10): conn.execute("SELECT id,body FROM benchmark_rows WHERE id BETWEEN 1 AND 1000").fetchall()
    measurements=[]
    for _ in range(100):
        before=time.perf_counter(); result=conn.execute("SELECT id,body FROM benchmark_rows WHERE id BETWEEN 1 AND 1000").fetchall(); assert len(result)==1000 and result[0][0]==1; measurements.append((time.perf_counter()-before)*1000)
    print(json.dumps({"implementation":psycopg.pq.__impl__,"libpq":psycopg.pq.version(),"medianMs":statistics.median(measurements),"p95Ms":sorted(measurements)[94],"samplesMs":measurements}))
'''


def main():
    arch = check_python()
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True, type=Path, help="Isolated native developer venv containing both psycopg and its C extension")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    runtime = REPO / "runtime" / "postgresql" / "macos-universal2"
    env = child_environment(REPO)
    results = []
    with tempfile.TemporaryDirectory(prefix="reference-library-driver-benchmark-") as temporary:
        root = Path(temporary)
        data = root / "pgdata"
        subprocess.run([str(runtime / "bin" / "initdb"), "-D", str(data), "--auth=trust", "--encoding=UTF8", "--locale=C"], env=env, check=True, capture_output=True)
        port = available_loopback_port(28762)
        user = subprocess.check_output(["/usr/bin/id", "-un"], text=True).strip()
        started = False
        try:
            with (root / "pg_ctl.log").open("ab", buffering=0) as log:
                subprocess.run([str(runtime / "bin" / "pg_ctl"), "start", "-D", str(data), "-l", str(root / "postgresql.log"), "-o", f"-p {port} -h 127.0.0.1 -c unix_socket_directories=''", "-w"], env=env, check=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, timeout=60)
            started = True
            env.update(BENCH_PORT=str(port), BENCH_USER=user)
            # Bind pure-Python libpq to the exact private 18.6 lib.
            code = "import ctypes.util; _old=ctypes.util.find_library; ctypes.util.find_library=lambda n: " + repr(str(runtime / "lib" / "libpq.5.dylib")) + " if n in ('libpq.dylib','pq','libpq') else _old(n)\n" + BENCH
            for implementation in ("python", "c", "c", "python"):
                env["PSYCOPG_IMPL"] = implementation
                result = subprocess.run([str(args.python.resolve()), "-I", "-c", code], env=env, capture_output=True, text=True, timeout=180)
                if result.returncode:
                    raise RuntimeError("Native driver comparison failed for " + implementation + ": " + result.stderr)
                results.append(json.loads(result.stdout))
        finally:
            if started:
                with (root / "pg_ctl.log").open("ab", buffering=0) as log:
                    subprocess.run([str(runtime / "bin" / "pg_ctl"), "stop", "-D", str(data), "-m", "fast", "-w"], env=env, check=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, timeout=60)
    report = {"nativeMac": True, "architecture": arch, "platform": platform.platform(), "python": platform.python_version(), "fixture": "10000 generated temporary rows; 1000 rows fetched per request; 10 warmups + 100 measured queries per run", "runOrder": [r["implementation"] for r in results], "results": results, "limitations": "Synthetic driver/network microcomparison; not full application import/search/reader scale performance or Safari latency"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    print(str(args.output))


if __name__ == "__main__":
    main()
