# Reproduce the Windows native runtime

This is a developer build recipe for the locked 2026-10-13 Windows x64 release. End users use the offline application package and need neither these tools nor a compiler. Read `windows-native-build-lock.json` for exact upstream URLs, archive hashes, checksum provenance and tool versions. Download each archive as a stream, verify its hash before extraction, and retain the original license/notice files. GitHub source URLs in the lock are stable release asset URLs; temporary signed redirect URLs are deliberately excluded.

Use a short, new, plain physical `<BUILD_ROOT>` with `sources`, `tools`, `dependencies`, `build`, `stage`, and `candidate` children. Select this directory explicitly; never use an installed application, live PGDATA, or a library as the build directory. The historical build used a build-only junction for some SDK tools, but the final application rejects such roots. All actual compilation, staging and tests remain inside the selected build directory. The following logical prefixes describe metadata; DESTDIR redirects installation to staging, so no files are installed into those logical system-drive locations.

## Tool environment

Activate the existing MSVC x64 developer environment for BuildTools 2026. The validated compiler was 19.51.36243.0, toolset 14.51.36231, Windows SDK 10.0.26100.0. Building these unmodified third-party open-source dependencies uses the BuildTools license's Open Source Dependencies provision; do not infer a separate CRT public redistribution license from its installation.

Create an isolated standard CPython 3.14.8 build venv under `tools/python`. Install the locked Meson 1.12.1, Ninja 1.13.2, CMake 4.4.4, packaging 26.3 and colorama 0.4.6 wheels into it. Extract WinFlexBison 2.5.25 (Flex 2.6.4 / Bison 3.8.2), Strawberry portable Perl 5.42.3.1 (interpreter 5.42.3), NASM 3.02 and JOM 1.1.7 to `tools`; these tools are not shipped to users. Prepend their directories only to the build process PATH. GNU diff 3.12 is a developer regression-test dependency, not a runtime dependency.

Use process-local `LC_ALL=C`, `LC_CTYPE=C`, `LANG=C`, `VSLANG=1033`. On a Chinese Windows compiler host use `PYTHONUTF8=0` and `PYTHONIOENCODING=cp936` for Meson, because compiler discovery consumes localized native bytes. No global environment, Python installation, service or site-packages modifications are needed.

## Dependency SDKs

Extract the verified ICU 78.3 upstream x64 MSVC SDK as `dependencies/icu` (`include`, `lib64`, `bin64`); keep the exact matching official source archive and Unicode license. Do not replace an older major ICU DLL without rebuilding PostgreSQL against its headers/import libraries.

For OpenSSL, run Configure from a fresh out-of-tree build directory using the extracted source's Configure script:

```text
perl <BUILD_ROOT>/sources/openssl-4.0.3/Configure VC-WIN64A shared no-makedepend --prefix=C:/OpenSSL --openssldir=C:/OpenSSL/ssl --libdir=lib
```

Set compiler environment `CL=/FS /experimental:deterministic /pathmap:<BUILD_ROOT>=ReferenceLibraryBuild`. Use linker `/PDBALTPATH:openssl.pdb`. Keep debug symbols privately; do not ship PDBs. Build the following targets sequentially, each with `jom /J4` and `MAKE=<absolute-jom-path>`: `build_libs`, then `build_modules`, then `apps\openssl.exe`. Do not run recursive installation targets in parallel: they can collide on intermediate objects. Run serial `nmake install_sw install_ssldirs DESTDIR=<BUILD_ROOT>/stage/openssl`, then copy the resulting staged OpenSSL SDK to `dependencies/openssl`. The logical prefix is not an installation destination.

Build the upstream test programs, then run the unfiltered official `test/run_tests.pl` from the source with absolute `SRCTOP`/`BLDTOP` and `HARNESS_JOBS=4`. The release passed 368 main recipes (326 PASS / 42 upstream skips), 4,135 assertions, plus an upstream FIPS-preparation skip. Set absolute source/build fixture variables so the out-of-tree runner finds upstream test data. Retain failed infrastructure-attempt logs as well as the final PASS.

Configure the compression SDKs with Ninja, Release and dynamic MSVC CRT. The exact feature options are:

```text
cmake -S <BUILD_ROOT>/sources/zlib-1.3.2 -B <BUILD_ROOT>/build/zlib -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=<BUILD_ROOT>/dependencies/zlib -DZLIB_BUILD_SHARED=ON -DZLIB_BUILD_STATIC=ON -DZLIB_BUILD_TESTING=ON
cmake -S <BUILD_ROOT>/sources/lz4-1.10.0/build/cmake -B <BUILD_ROOT>/build/lz4 -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=<BUILD_ROOT>/dependencies/lz4 -DBUILD_SHARED_LIBS=ON -DBUILD_STATIC_LIBS=ON -DLZ4_BUILD_CLI=ON
cmake -S <BUILD_ROOT>/sources/zstd-1.5.7/build/cmake -B <BUILD_ROOT>/build/zstd -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=<BUILD_ROOT>/dependencies/zstd -DZSTD_BUILD_SHARED=ON -DZSTD_BUILD_STATIC=ON -DZSTD_BUILD_TESTS=ON -DZSTD_BUILD_PROGRAMS=ON -DZSTD_USE_STATIC_RUNTIME=OFF
```

Build each with `cmake --build ... --parallel 4`, then install. Run zlib CTest serially: some upstream gzip fixtures share files and race under parallel testing. All 14 passed serially. Build LZ4's official `tests/fuzzer.c` with its required `lib` and `programs` source support and run 1,000 deterministic iterations; run Zstandard's official fuzzer and streaming tests for 1,000 iterations each. No upstream source is functionally modified.

## PostgreSQL source adjustments

Extract PostgreSQL 18.6. Replace only `src/timezone/data/tzdata.zi` with the verified 2026e file from the tzdata 2026.5 wheel, as permitted by `src/timezone/README`; check the exact data SHA-256 in the lock. The adapted timezone engine remains unchanged. Do not edit SQL expected output to accommodate timezone or locale changes.

Prevent `pg_config` and `config_info` metadata from publishing physical include/library/compiler paths. Immediately before `pg_config_paths = configure_file(...)` in `src/include/meson.build`, insert this metadata-only block. It changes reported strings, not the actual compiler/linker arguments:

```meson
release_build_roots = [fs.parent(fs.parent(meson.project_source_root())), fs.parent(fs.parent(fs.parent(get_option('extra_include_dirs')[0])))]
foreach release_var : ['var_cc', 'var_cpp', 'var_cflags', 'var_cxxflags', 'var_cppflags', 'var_cflags_sl', 'var_ldflags', 'var_ldflags_sl', 'var_ldflags_ex', 'var_libs']
  release_value = get_variable(release_var).replace('\\', '/')
  foreach release_build_root : release_build_roots
    release_value = release_value.replace(release_build_root.replace('\\', '/'), 'ReferenceLibraryBuild')
  endforeach
  set_variable(release_var, release_value)
endforeach
```

The first extra include directory must be `<BUILD_ROOT>/dependencies/icu/include`, matching the ancestor calculation. If using different source/SDK root layouts, redact all their reported absolute prefixes explicitly; keep the actual configure invocation privately for audit.

## PostgreSQL configure, compile and install

Use Meson `setup <BUILD_ROOT>/build/pg <BUILD_ROOT>/sources/postgresql-18.6` with:

```text
--prefix=C:/ReferenceLibrary/PostgreSQL --libdir=lib --bindir=bin --datadir=share --includedir=include --buildtype=release
-Db_vscrt=md -Drpath=false -Dauto_features=disabled
-Dssl=openssl -Dicu=enabled -Dzlib=enabled -Dlz4=enabled -Dzstd=enabled
-Dnls=disabled -Dlibxml=disabled -Dlibxslt=disabled -Dlibcurl=disabled
-Dplperl=disabled -Dplpython=disabled -Dpltcl=disabled -Dllvm=disabled
-Dldap=disabled -Dgssapi=disabled -Dreadline=disabled -Ddocs=disabled -Ddocs_pdf=disabled -Dtap_tests=disabled
-Dc_args=['/experimental:deterministic','/pathmap:<BUILD_ROOT>=ReferenceLibraryBuild']
-Dc_link_args=['/PDBALTPATH:reference-library-native.pdb']
```

Pass absolute `PERL`, `FLEX`, `BISON`, `PYTHON` tool paths. Pass `extra_include_dirs` for ICU/OpenSSL/zlib/LZ4/Zstandard headers, matching `extra_lib_dirs` (`icu/lib64`; other SDKs `lib`), and `cmake_prefix_path` for all five SDKs. Use properly quoted Meson list arguments or a structured subprocess argument list.

Use exactly one physical build-root mapping in `/pathmap` and pair it with `/experimental:deterministic`; `/pathmap` alone can be ignored, and comma-separated multiple mappings were not effective in the validated compiler. Verify a `__FILE__` probe and scan built PE files rather than trusting the flags. Use a fixed generic PDB alternate path; percent macros can interfere with PostgreSQL-generated C string metadata.

Run `meson compile -C ... -j4` and `meson install -C ... --destdir <BUILD_ROOT>/stage/pg`. Assert every resolved installation destination remains inside staging before executing the install. Copy only installed `bin`, `lib`, `share` runtime files to the candidate; exclude development headers, static/import libraries and PDBs. The native PostgreSQL layout includes `share/postgresql/*`, `lib/postgresql/pg_trgm.dll` and standard extension resources.

## Runtime closure and validation

Place ICU's `icuuc78.dll`, `icuin78.dll`, `icudt78.dll`; OpenSSL's `libcrypto-4-x64.dll`, `libssl-4-x64.dll`, `openssl.exe`; zlib's **zlib.dll**; `lz4.dll`, `zstd.dll`; and signed Microsoft `vcruntime140.dll`, `vcruntime140_1.dll`, `msvcp140.dll` beside PostgreSQL executables. VC Runtime 14.51.36247.0 was obtained through the official signed redist's layout/CAB extraction without a global install. Windows UCRT and system/API-set DLLs are supplied by Windows. Confirm all non-system PE imports resolve within the app-local dependency closure.

Ship OpenSSL's legacy provider at `lib/ossl-modules/legacy.dll` and official config at `share/openssl/openssl.cnf`. `RuntimeManager.crypto_environment()` derives `OPENSSL_CONF` and `OPENSSL_MODULES` from the actual application runtime root before PostgreSQL/libpq use; inherited values are removed in the owned process. Test relocated runtime paths with a system32-only initial PATH and deliberately invalid inherited SSL settings. Do not rely on the generic build-time prefix existing on the machine.

Preserve all original licenses and notices, including OpenSSL acknowledgements and local notice clearly identified as local. Generate a manifest containing every shipped relative path, byte size and SHA-256; exclude only the manifest itself. Scan every binary/text file for actual build roots, usernames and project tokens in ASCII and UTF-16 (both alignments). The final build has no such hits. Merely redacting metadata in a report is insufficient.

Run upstream setup plus `pg_trgm`, `pgcrypto`, `isolation`, and core regression suites with GNU diff available to the child test PATH. For the core test cache explicitly initialize `--locale=C --encoding=UTF8`, matching production initialization. Expected files remain the official unmodified files. Core 231/231, pg_trgm 4/4, pgcrypto 25/25 and isolation 122/122 passed. Preserve the earlier host-locale run with 229/231 and its ICU/cascade diff; PostgreSQL's Windows host locale is not an ICU locale identifier.

Validate only fresh owned isolated clusters: SCRAM/non-superuser app access, loopback-only listening, Unicode ICU equivalence, IANA 2026e Winnipeg 2027 permanent -05 offset, certificate-verified TLS1.3, C-driver libpq 18.6, and logical custom dump/restore with gzip/LZ4/Zstandard to three empty databases. The native feature check passed 10/10. Run offline VBS first start/restart/duplicate/controlled stop, exact library identity, ProgramRoot byte/mtime immutability and PID reuse/proxy defenses; the release evidence has 17 lifecycle and 10 focused ownership/proxy passes.

This recipe specifies functional reproduction; exact byte-for-byte identity also depends on compiler, Windows SDK and upstream SDK binary versions. Never swap DLLs in a running runtime. Validate the candidate first, then coordinate stopping only the owned application before replacing its program files. Private application validation does not itself grant public Microsoft CRT redistribution rights; satisfy the original Runtime License before publishing a binary package. Public source export contains this recipe and source lock, not runtime binaries, credentials, venvs, PGDATA or private build logs.