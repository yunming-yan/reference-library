#!/bin/sh
# Developer release host only. This never runs in the daily Finder launcher.
set -eu
if [ "$(uname -s)" != Darwin ]; then echo 'Native Mac host required; Windows cannot satisfy this build.' >&2; exit 1; fi
ARCHIVES=${1:?Usage: build_native_pg.sh /absolute/source-archives /absolute/fresh-output /absolute/python3.14}
OUTPUT=${2:?Provide a fresh isolated absolute output directory.}
PYTHON=${3:?Provide the installed standard CPython 3.14.8+ interpreter.}
case "$ARCHIVES:$OUTPUT:$PYTHON" in /*:/*:/*) ;; *) echo 'All paths must be absolute.' >&2; exit 1 ;; esac
if [ -e "$OUTPUT" ]; then echo 'Output must not exist; existing builds are preserved.' >&2; exit 1; fi
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
LOCK="$HERE/native-pg-version-lock.json"
ARCH=$(uname -m)
case "$ARCH" in arm64) SSL_TARGET=darwin64-arm64-cc ;; x86_64) SSL_TARGET=darwin64-x86_64-cc ;; *) echo 'Unsupported Mac architecture.' >&2; exit 1 ;; esac
/usr/bin/xcrun --find clang >/dev/null
"$PYTHON" -I -c 'import sys,sysconfig; assert sys.platform=="darwin" and sys.version_info[:2]==(3,14) and sys.version_info[:3]>=(3,14,8) and not sysconfig.get_config_var("Py_GIL_DISABLED")'
# All hashes are checked before any source code is extracted or executed.
"$PYTHON" -I - "$LOCK" "$ARCHIVES" "$OUTPUT" <<'PY'
import hashlib,json,pathlib,sys,tarfile
lock,archives,output=map(pathlib.Path,sys.argv[1:])
value=json.loads(lock.read_text('utf-8-sig'))
items=[]
for item in value['components']:
    path=archives/item['archiveFilename']
    expected=item['sourceSha256']
    if not expected or len(expected)!=64: raise RuntimeError('Unverified source checksum')
    with path.open('rb') as stream: actual=hashlib.file_digest(stream,'sha256').hexdigest()
    if actual!=expected: raise RuntimeError('Source archive hash mismatch: '+path.name)
    items.append((path,item))
output.mkdir()
source=output/'source'; source.mkdir()
for path,item in items:
    with tarfile.open(path) as archive: archive.extractall(source,filter='data')
    if not (source/item['sourceDirectory']).is_dir(): raise RuntimeError('Unexpected source root')
(output/'native-source-verification.json').write_text(json.dumps(value,indent=2)+'\n','utf-8')
PY
PREFIX="$OUTPUT/prefix"
export MACOSX_DEPLOYMENT_TARGET=13.0
export CC="$(/usr/bin/xcrun --find clang)"
export CXX="$(/usr/bin/xcrun --find clang++)"
export SDKROOT="$(/usr/bin/xcrun --show-sdk-path)"
export CFLAGS="-O2 -arch $ARCH -mmacosx-version-min=13.0"
export CXXFLAGS="$CFLAGS"
export CPPFLAGS="-I$PREFIX/include"
export LDFLAGS="-arch $ARCH -L$PREFIX/lib -Wl,-rpath,$PREFIX/lib"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"
export ICU_CFLAGS="-I$PREFIX/include"
export ICU_LIBS="-L$PREFIX/lib -licui18n -licuuc -licudata"
JOBS=${BUILD_JOBS:-2}
mkdir -p "$PREFIX" "$OUTPUT/logs"
(
  cd "$OUTPUT/source/openssl-4.0.3"
  /usr/bin/perl ./Configure "$SSL_TARGET" shared --prefix="$PREFIX" --openssldir="$PREFIX/ssl"
  /usr/bin/make -j "$JOBS"
  /usr/bin/make test
  /usr/bin/make install_sw
) >"$OUTPUT/logs/openssl.log" 2>&1
(
  cd "$OUTPUT/source/icu/source"
  ./runConfigureICU MacOSX --prefix="$PREFIX" --disable-samples --disable-tests
  /usr/bin/make -j "$JOBS"
  /usr/bin/make install
) >"$OUTPUT/logs/icu.log" 2>&1
(
  cd "$OUTPUT/source/zlib-1.3.2"
  ./configure --prefix="$PREFIX"
  /usr/bin/make -j "$JOBS"
  /usr/bin/make test
  /usr/bin/make install
) >"$OUTPUT/logs/zlib.log" 2>&1
(
  cd "$OUTPUT/source/lz4-1.10.0"
  /usr/bin/make -C lib -j "$JOBS"
  /usr/bin/make -C lib install PREFIX="$PREFIX"
) >"$OUTPUT/logs/lz4.log" 2>&1
(
  cd "$OUTPUT/source/zstd-1.5.7"
  /usr/bin/make -C lib -j "$JOBS"
  /usr/bin/make -C lib install PREFIX="$PREFIX"
) >"$OUTPUT/logs/zstd.log" 2>&1
(
  cd "$OUTPUT/source/postgresql-18.6"
  ./configure --prefix="$PREFIX" --with-ssl=openssl --with-icu --without-readline --with-lz4 --with-zstd --without-libxml --without-libxslt --without-llvm
  /usr/bin/make -j "$JOBS"
  /usr/bin/make check
  /usr/bin/make install
) >"$OUTPUT/logs/postgresql.log" 2>&1
export DYLD_LIBRARY_PATH="$PREFIX/lib"
"$PREFIX/bin/openssl" version >"$OUTPUT/logs/versions.log"
"$PREFIX/bin/pg_config" --version >>"$OUTPUT/logs/versions.log"
"$PREFIX/bin/icuinfo" >>"$OUTPUT/logs/versions.log"
/usr/bin/otool -L "$PREFIX/bin/postgres" "$PREFIX/lib/libpq.5.dylib" >"$OUTPUT/logs/native-linkage.log"
"$PYTHON" -I - "$PREFIX" <<'PY'
import ctypes,pathlib,sys
root=pathlib.Path(sys.argv[1]); lib=ctypes.CDLL(str(root/'lib/libpq.5.dylib'))
lib.PQlibVersion.restype=ctypes.c_int
assert lib.PQlibVersion()==180006
PY
echo 'Native source build completed locally; output is unpromoted and may retain absolute prefix load paths.'
echo 'Verify relocation/closure, build matching psycopg-c, run both CPU app/Safari/performance checks, then sign/notarize before replacing the EDB candidate.'
