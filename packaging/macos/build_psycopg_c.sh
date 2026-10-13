#!/bin/sh
# Developer-only native compilation. This is never invoked by the user launcher.
set -eu
if [ "$(uname -s)" != Darwin ]; then echo 'A native Mac build host is required.' >&2; exit 1; fi
PROGRAM=${1:?Usage: build_psycopg_c.sh /absolute/program/root /absolute/build/venv/bin/python /absolute/source-tar.gz /absolute/output}
PYTHON=${2:?Provide an isolated developer build Python with pinned build tools already installed.}
SOURCE=${3:?Provide the hash-verified psycopg_c 3.3.6 source archive.}
OUTPUT=${4:?Provide an isolated output directory.}
case "$PROGRAM:$PYTHON:$SOURCE:$OUTPUT" in /*:/*:/*:/*) ;; *) echo 'Absolute paths are required.' >&2; exit 1 ;; esac
export PATH="$PROGRAM/runtime/postgresql/macos-universal2/bin:$PATH"
export DYLD_LIBRARY_PATH="$PROGRAM/runtime/postgresql/macos-universal2/lib"
export MACOSX_DEPLOYMENT_TARGET=13.0
export PIP_NO_INDEX=1
export PIP_CONFIG_FILE=/dev/null
"$PYTHON" -I -c 'import platform,sys,sysconfig; assert sys.version_info[:3] >= (3,14,8) and sys.version_info[:2] == (3,14) and not sysconfig.get_config_var("Py_GIL_DISABLED"); print(platform.machine())'
test "$(pg_config --version)" = 'PostgreSQL 18.6'
mkdir -p "$OUTPUT"
"$PYTHON" -I -m pip --isolated wheel --no-index --no-deps --no-build-isolation --wheel-dir "$OUTPUT" "$SOURCE"
echo 'Inspect Mach-O dependencies, pin the output wheel SHA-256, and rerun native tests and performance comparison before release.'
