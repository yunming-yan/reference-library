"""Assemble a frozen, verified Windows offline Program and recheck every ZIP byte.
This private helper never runs application/PG processes, downloads, or publishes.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import zipfile

REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
PACKAGE = 'ReferenceLibrary-windows-x64-cp314'
ZIP_DATE = (2026, 10, 14, 0, 0, 0)
MANIFEST_PATH = 'release/windows-offline-manifest.json'
FORBIDDEN_PARTS = {'.git', '.codex', '.agents', '.aws', 'private', 'data', 'catalog',
                   'originals', 'staging', 'journals', 'state', 'cache', 'backups',
                   '__pycache__', '.venv', 'venv', 'logs', 'screenshots', 'evidence', 'qa'}
FORBIDDEN_NAMES = {'launcher.py', 'machine.json', 'postgresql.json', 'runtime.json',
                   'settings.json', 'catalog.json', 'pyvenv.cfg', '.ds_store', 'thumbs.db'}
FORBIDDEN_SUFFIXES = {'.pdb', '.pyc', '.pyo', '.log', '.prof', '.pem', '.key', '.dmp'}
SOURCE_REQUIRED = {'launch.py', 'README.md', 'LICENSE', 'app/bootstrap.py', 'app/runtime_manager.py',
                   'app/server.py', 'app/ingest.py', 'app/pg_catalog.py', 'app/original_store.py',
                   'app/web/app.js', 'app/web/reader.js', 'app/web/source-images.js',
                   'app/web/management.js', 'app/web/index.html'}
RUNTIME_REQUIRED = {'manifest.json', 'bin/postgres.exe', 'bin/initdb.exe', 'bin/pg_ctl.exe',
                    'bin/pg_dump.exe', 'bin/pg_restore.exe', 'bin/libpq.dll',
                    'bin/libssl-4-x64.dll', 'bin/libcrypto-4-x64.dll',
                    'share/postgresql/postgres.bki', 'share/postgresql/postgresql.conf.sample',
                    'licenses/PostgreSQL-COPYRIGHT', 'licenses/ICU-LICENSE',
                    'licenses/OpenSSL-Apache2-LICENSE.txt', 'licenses/LZ4-BSD2-LICENSE',
                    'licenses/Zstandard-BSD3-LICENSE', 'licenses/zlib-LICENSE',
                    'licenses/Microsoft-Runtime-LICENSE.txt', 'licenses/Microsoft-Redist.txt',
                    'licenses/Microsoft-ThirdPartyNotices.txt'}
SECRET = re.compile(rb'(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|x-amz-(?:credential|signature)=)', re.I)
USER_PATH = re.compile(rb'[a-z]:[\\/]+users[\\/]+[^\\/\x00\r\n]+[\\/]', re.I)
HEX = re.compile(r'^[0-9a-f]{64}$')
# Raw wide-string regexes avoid decoding arbitrary large native binaries.
WIDE_USER_PATH = (
    re.compile(rb'[a-z]\x00:\x00(?:\\\x00|/\x00)+u\x00s\x00e\x00r\x00s\x00(?:\\\x00|/\x00)', re.I),
    re.compile(rb'\x00[a-z]\x00:(?:\x00\\|\x00/)+\x00u\x00s\x00e\x00r\x00s(?:\x00\\|\x00/)', re.I),
)
WIDE_SECRETS = []
for _encoding in ('utf-16-le', 'utf-16-be'):
    # Assemble scanner signatures so this public build source passes its own scan.
    for _marker in (tuple('-----' + 'BEGIN ' + kind + 'PRIVATE KEY-----'
                          for kind in ('', 'RSA ', 'EC ', 'OPENSSH ')) +
                    tuple('x-amz-' + parameter + '=' for parameter in ('credential', 'signature'))):
        WIDE_SECRETS.append(re.compile(re.escape(_marker.encode(_encoding)), re.I))
    _atom = rb'[A-Za-z0-9]\x00' if _encoding.endswith('le') else rb'\x00[A-Za-z0-9]'
    _cap_atom = rb'[A-Z0-9]\x00' if _encoding.endswith('le') else rb'\x00[A-Z0-9]'
    _type_atom = rb'[pousr]\x00' if _encoding.endswith('le') else rb'\x00[pousr]'
    WIDE_SECRETS.append(re.compile(re.escape('gh'.encode(_encoding)) + _type_atom + re.escape('_'.encode(_encoding)) + b'(?:' + _atom + b'){20,}', re.I))
    WIDE_SECRETS.append(re.compile(re.escape('AKIA'.encode(_encoding)) + b'(?:' + _cap_atom + b'){16}', re.I))

class ContractError(ValueError):
    pass

def require(condition, message):
    if not condition:
        raise ContractError(message)

def sha_file(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def sha_bytes(value):
    return hashlib.sha256(value).hexdigest()

def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')

def plain(path, ancestors=True):
    for item in ((path, *path.parents) if ancestors else (path,)):
        if not item.exists() and not item.is_symlink():
            continue
        info = item.lstat()
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & REPARSE,
                'Link or reparse point is forbidden')
        require(stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode), 'Non-regular filesystem entry is forbidden')
        if stat.S_ISREG(info.st_mode):
            require(info.st_nlink == 1, 'Hard-linked file is forbidden')

def rel_path(name):
    require(isinstance(name, str) and name and '\\' not in name and '\x00' not in name,
            'Malformed relative path')
    p = PurePosixPath(name)
    require(not p.is_absolute() and p.as_posix() == name and all(x not in {'', '.', '..'} for x in p.parts),
            'Unsafe relative path')
    for part in p.parts:
        require(':' not in part and not part.endswith((' ', '.')), 'Unsafe Windows path component')
        require(not re.fullmatch(r'(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part),
                'Reserved Windows path component')
    return p

def screen_name(name):
    p = rel_path(name)
    require(not ({part.casefold() for part in p.parts} & FORBIDDEN_PARTS), 'Private/disposable directory is forbidden: ' + name)
    require(p.name.casefold() not in FORBIDDEN_NAMES, 'Private/legacy file is forbidden: ' + name)
    require(p.suffix.casefold() not in FORBIDDEN_SUFFIXES and not p.name.casefold().startswith('.env'),
            'Private/build file is forbidden: ' + name)
    require(not re.search(r'(?i)(?:lib(?:ssl|crypto)-3-x64\.dll|icu[a-z]*77\.dll)$', name),
            'Obsolete Windows native DLL is forbidden: ' + name)

def walk(root):
    root = Path(root).absolute()
    plain(root)
    require(root.is_dir(), 'Inventory root must be a directory')
    result = []
    folded = set()
    for current, dirs, files in os.walk(root, followlinks=False):
        current = Path(current)
        for name in dirs:
            plain(current / name, False)
        for name in files:
            path = current / name
            plain(path, False)
            relative = path.relative_to(root).as_posix()
            rel_path(relative)
            require(relative.casefold() not in folded, 'Case-insensitive duplicate path')
            folded.add(relative.casefold())
            result.append({'path': relative, 'bytes': path.stat().st_size, 'sha256': sha_file(path)})
    return sorted(result, key=lambda item: item['path'])

def tree_hash(records):
    value = hashlib.sha256()
    for item in sorted(records, key=lambda item: item['path']):
        rel_path(item['path'])
        require(HEX.fullmatch(item['sha256']) is not None, 'Malformed content hash')
        value.update(item['path'].encode('utf-8') + b'\x00' + bytes.fromhex(item['sha256']))
    return value.hexdigest()

def map_records(records, filename=False):
    require(isinstance(records, list), 'Inventory must be a list')
    result = {}
    seen = set()
    for item in records:
        require(isinstance(item, dict), 'Malformed inventory item')
        name = item.get('filename') if filename else item.get('path')
        rel_path(name)
        if filename:
            require('/' not in name and name.endswith('.whl'), 'Wheel filename must be a single wheel basename')
        require(name.casefold() not in seen, 'Duplicate manifest path')
        seen.add(name.casefold())
        require(isinstance(item.get('sha256'), str) and HEX.fullmatch(item['sha256']), 'Malformed manifest hash')
        require(isinstance(item.get('bytes'), int) and item['bytes'] > 0, 'Manifest size must be positive')
        result[name] = {'path': name, 'bytes': item['bytes'], 'sha256': item['sha256']}
    return result

def exact(actual, expected, label):
    actual = {item['path']: item for item in actual}
    require(set(actual) == set(expected), label + ' file list mismatch')
    for name, item in expected.items():
        require(actual[name] == item, label + ' content mismatch: ' + name)

def read_json(path):
    plain(path)
    require(path.is_file(), 'Manifest file is missing')
    return json.loads(path.read_text(encoding='utf-8-sig'))

def privacy_needles(roots, extra):
    tokens = [str(Path.home()), Path.home().name]
    for root in roots:
        tokens.append(str(root))
        tokens.extend(part for part in root.parts if re.fullmatch(r'(?:project|codex)--[A-Za-z0-9-]+', part))
    require(isinstance(extra, list) and all(isinstance(x, str) and len(x) >= 4 for x in extra), 'Malformed forbidden-token list')
    tokens.extend(extra)
    needles = set()
    for token in tokens:
        if len(token) < 4:
            continue
        for variant in {token, token.replace('\\', '/'), token.replace('/', '\\'), token.replace('\\', '\\\\')}:
            for encoding in ('utf-8', 'utf-16-le', 'utf-16-be'):
                needles.add(variant.encode(encoding).lower())
    return sorted(needles, key=len, reverse=True)

def privacy_chunk(data, needles, name):
    lower = data.lower()
    require(not any(needle in lower for needle in needles), 'Private machine/token material found: ' + name)
    require(not USER_PATH.search(data) and not SECRET.search(data), 'User path or credential material found: ' + name)
    require(not any(pattern.search(data) for pattern in WIDE_USER_PATH + tuple(WIDE_SECRETS)), 'UTF-16 user path or credential material found: ' + name)

def privacy_stream(stream, needles, name):
    overlap = max([8192] + [len(value) + 8 for value in needles])
    previous = b''
    while data := stream.read(1024 * 1024):
        scan = previous + data
        privacy_chunk(scan, needles, name)
        previous = scan[-overlap:]

def inspect_embedded_zip(path, needles):
    with zipfile.ZipFile(path) as archive:
        require(archive.testzip() is None, 'Wheel ZIP CRC failed')
        seen = set()
        expanded = 0
        for member in archive.infolist():
            name = member.filename.rstrip('/')
            if not name:
                continue
            screen_name(name)
            require(name.casefold() not in seen, 'Wheel ZIP duplicate entry')
            seen.add(name.casefold())
            mode = member.external_attr >> 16
            require(not stat.S_ISLNK(mode), 'Wheel ZIP symlink is forbidden')
            require(not (member.flag_bits & 1), 'Encrypted wheel is forbidden')
            expanded += member.file_size
            require(member.file_size <= 512 * 1024 * 1024 and expanded <= 1024 * 1024 * 1024, 'Wheel expansion exceeds limit')
            if not member.is_dir():
                with archive.open(member) as stream:
                    privacy_stream(stream, needles, 'wheel member ' + name)

def screen_tree(root, records, needles):
    for item in records:
        screen_name(item['path'])
        with (root / item['path']).open('rb') as stream:
            privacy_stream(stream, needles, item['path'])
        if item['path'].endswith('.whl'):
            inspect_embedded_zip(root / item['path'], needles)

def disjoint(roots):
    resolved = []
    for root in roots:
        plain(root)
        resolved.append(root.absolute().resolve())
    for i, left in enumerate(resolved):
        for right in resolved[i + 1:]:
            require(left != right and not left.is_relative_to(right) and not right.is_relative_to(left), 'Input/output roots must be disjoint')

def validate_inputs(source, export_manifest, runtime, wheels, freeze_path, output):
    disjoint((source, runtime, wheels, output))
    require(not output.exists() or (output.is_dir() and not any(output.iterdir())), 'Output must be new and empty; existing artifacts are preserved')
    freeze = read_json(freeze_path)
    require(freeze.get('format') == 1 and freeze.get('status') == 'READY' and freeze.get('scope') == 'windows-offline-release', 'Final root READY freeze is required')
    require(freeze.get('expectedSchemaVersion') == 4, 'Schema-4 freeze is required')
    require(freeze.get('runtimeValidated') is True, 'Validated final runtime acknowledgment is required')
    for key in ('sourceTreeSha256', 'exportManifestSha256', 'runtimeTreeSha256', 'wheelTreeSha256'):
        require(isinstance(freeze.get(key), str) and HEX.fullmatch(freeze[key]), 'Malformed freeze content hash')
    exported = read_json(export_manifest)
    require(exported.get('status') == 'PASS' and exported.get('kind') == 'source-only-preview', 'Clean source export must PASS')
    require(exported.get('published') is False and exported.get('gitInitialized') is False, 'Source must be the unpublished clean export')
    source_records = walk(source)
    exact(source_records, map_records(exported.get('files')), 'Source export')
    require(tree_hash(source_records) == exported.get('treeSha256') == freeze['sourceTreeSha256'], 'Source tree freeze mismatch')
    require(sha_file(export_manifest) == freeze['exportManifestSha256'], 'Export manifest freeze mismatch')
    source_paths = {item['path'] for item in source_records}
    require(SOURCE_REQUIRED <= source_paths, 'Required final shared source is missing')
    required_source = freeze.get('requiredSourcePaths', [])
    require(isinstance(required_source, list), 'Malformed required source list')
    for name in required_source:
        rel_path(name)
    require(set(required_source) <= source_paths, 'Root-required source is missing')
    require(any(re.match(r'^schema/migrations/004[^/]*\.sql$', name) for name in source_paths), 'Schema-4 migration is missing')
    vbs = [item for item in source_records if '/' not in item['path'] and item['path'].endswith('.vbs')]
    require(len(vbs) >= 2, 'Root start/stop VBS entries are missing')
    runtime_manifest = read_json(runtime / 'manifest.json')
    require(runtime_manifest.get('platform') == 'windows-x64' and runtime_manifest.get('postgresqlVersion') == '18.6', 'Final Windows PG18.6 runtime is required')
    runtime_records = walk(runtime)
    runtime_expected = map_records(runtime_manifest.get('files'))
    require('manifest.json' not in runtime_expected, 'Runtime manifest must not self-hash')
    runtime_expected['manifest.json'] = next(item for item in runtime_records if item['path'] == 'manifest.json')
    exact(runtime_records, runtime_expected, 'Runtime')
    require(tree_hash(runtime_records) == freeze['runtimeTreeSha256'], 'Runtime tree freeze mismatch')
    runtime_paths = {item['path'] for item in runtime_records}
    required_runtime = freeze.get('requiredRuntimePaths', [])
    require(isinstance(required_runtime, list), 'Malformed required runtime list')
    for name in required_runtime:
        rel_path(name)
    require(RUNTIME_REQUIRED | set(required_runtime) <= runtime_paths, 'Required latest native payload/license is missing')
    require(any(re.fullmatch(r'bin/icuuc78\.dll', name) for name in runtime_paths), 'Latest ICU78 runtime is missing')
    wheel_manifest = read_json(wheels / 'manifest.json')
    wheel_expected = map_records(wheel_manifest, filename=True)
    wheel_records = walk(wheels)
    for name in ('manifest.json', 'requirements.lock'):
        require(any(item['path'] == name for item in wheel_records), 'Wheel metadata/requirements missing')
        wheel_expected[name] = next(item for item in wheel_records if item['path'] == name)
    exact(wheel_records, wheel_expected, 'Wheel')
    require(tree_hash(wheel_records) == freeze['wheelTreeSha256'], 'Wheel tree freeze mismatch')
    lock = (wheels / 'requirements.lock').read_text('utf-8')
    declared = {item['sha256'] for name, item in wheel_expected.items() if name.endswith('.whl')}
    require(set(re.findall(r'--hash=sha256:([0-9a-f]{64})', lock)) == declared, 'Offline requirements/wheel hashes differ')
    require('psycopg-c==' in lock and 'apsw==' in lock, 'C driver and SQLite offline wheels are required')
    require(not re.search(r'(?im)^\s*(?:-e\b|--(?:index-url|extra-index-url|find-links)|https?://|file:)', lock), 'Network/path-based requirements are forbidden')
    needles = privacy_needles((source, runtime, wheels, output), freeze.get('forbiddenTokens', []))
    for root, records in ((source, source_records), (runtime, runtime_records), (wheels, wheel_records)):
        screen_tree(root, records, needles)
    plan = {}
    for root, prefix, records, group in ((source, '', source_records, 'clean-source'), (runtime, 'runtime/postgresql/windows-x64/', runtime_records, 'windows-native'), (wheels, 'wheels/windows-x64-cp314/', wheel_records, 'windows-wheel')):
        for item in records:
            name = prefix + item['path']
            require(name != MANIFEST_PATH, 'Generated release manifest collision')
            if name in plan:
                require(plan[name]['record'] == dict(item, path=name), 'Stale source/native overlay metadata mismatch: ' + name)
            plan[name] = {'source': root / item['path'], 'record': dict(item, path=name), 'group': group}
    folded = [name.casefold() for name in plan]
    require(len(folded) == len(set(folded)), 'Overlay creates case-insensitive duplicate paths')
    # Only source metadata for the other platforms is retained; no foreign native payload.
    for name in plan:
        if name.startswith(('runtime/postgresql/macos-', 'wheels/macos-')):
            require(PurePosixPath(name).suffix not in {'.dll', '.exe', '.dylib', '.so', '.whl', '.a', '.lib'}, 'Foreign binary/wheel in Windows package')
    hashes = {key: freeze[key] for key in ('sourceTreeSha256', 'exportManifestSha256', 'runtimeTreeSha256', 'wheelTreeSha256')}
    hashes['rootFreezeSha256'] = sha_file(freeze_path)
    hashes['runtimeManifestSha256'] = sha_file(runtime / 'manifest.json')
    hashes['wheelManifestSha256'] = sha_file(wheels / 'manifest.json')
    return plan, hashes, needles, (source_records, runtime_records, wheel_records)

def verify_archive(archive_path, payload_root, expected, needles):
    with zipfile.ZipFile(archive_path) as archive:
        require(archive.testzip() is None, 'Release ZIP CRC failure')
        expected_names = {PACKAGE + '/' + name for name in expected}
        require(len(archive.infolist()) == len(expected_names) and {item.filename for item in archive.infolist()} == expected_names, 'Release ZIP inventory mismatch')
        for info in archive.infolist():
            require(info.filename.startswith(PACKAGE + '/'), 'Release ZIP root mismatch')
            name = info.filename[len(PACKAGE) + 1:]
            screen_name(name)
            require(not stat.S_ISLNK(info.external_attr >> 16) and stat.S_ISREG(info.external_attr >> 16), 'Release ZIP non-regular entry')
            require(not (info.flag_bits & 1), 'Release ZIP encrypted entry')
            require(info.file_size == expected[name]['bytes'], 'Release ZIP size mismatch: ' + name)
            with archive.open(info) as stream:
                require(hashlib.file_digest(stream, 'sha256').hexdigest() == expected[name]['sha256'], 'Release ZIP SHA mismatch: ' + name)
            with archive.open(info) as stream:
                privacy_stream(stream, needles, name)
            require(sha_file(payload_root / name) == expected[name]['sha256'], 'Program changed during ZIP verification: ' + name)

def build(source, export_manifest, runtime, wheels, freeze_path, output):
    source, export_manifest, runtime, wheels, freeze_path, output = [Path(p).absolute() for p in (source, export_manifest, runtime, wheels, freeze_path, output)]
    plan, hashes, needles, inventories = validate_inputs(source, export_manifest, runtime, wheels, freeze_path, output)
    print(json.dumps({'stage': 'input-contract-passed', 'payloadFiles': len(plan)}), flush=True)
    output.mkdir(parents=True, exist_ok=True)
    program = output / PACKAGE
    program.mkdir()
    for name in sorted(plan):
        item = plan[name]
        target = program / name
        target.parent.mkdir(parents=True, exist_ok=True)
        require(sha_file(item['source']) == item['record']['sha256'], 'Frozen input changed before copy: ' + name)
        with item['source'].open('rb') as src, target.open('xb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        require(sha_file(target) == item['record']['sha256'], 'Copied payload hash mismatch: ' + name)
    # Re-inventory all inputs after assembly to reject additions/deletions/content races.
    for root, original in zip((source, runtime, wheels), inventories):
        require(walk(root) == original, 'Frozen input changed during assembly')
    require(sha_file(export_manifest) == hashes['exportManifestSha256'] and sha_file(freeze_path) == hashes['rootFreezeSha256'], 'Freeze/export manifest changed during assembly')
    payload = [dict(plan[name]['record'], origin=plan[name]['group']) for name in sorted(plan)]
    manifest = {'format': 1, 'application': 'ReferenceLibrary', 'target': 'windows-x64-cp314',
                'kind': 'offline-program-local-delivery', 'schemaVersion': 4, 'published': False,
                'externalPrerequisites': ['standard CPython 3.14.8+ within 3.14, Windows x64', 'local web browser'],
                'postgresql': '18.6', 'nativeRuntimeValidation': 'root-frozen validated input',
                'inputContentHashes': hashes, 'treeSha256': tree_hash(payload),
                'fileCount': len(payload), 'totalBytes': sum(item['bytes'] for item in payload),
                'files': payload,
                'privacyChecks': ['paths and filenames', 'ASCII/UTF-8/UTF-16 LE/BE private machine roots and tokens', 'binary and text payloads', 'expanded wheel members'],
                'macOS': 'Cross-platform source metadata retained; no Mac binary/wheel included; no Mac native/signing claim.'}
    manifest_data = json_bytes(manifest)
    privacy_chunk(manifest_data, needles, MANIFEST_PATH)
    (program / MANIFEST_PATH).parent.mkdir(parents=True, exist_ok=True)
    (program / MANIFEST_PATH).write_bytes(manifest_data)
    expected = {item['path']: {key: item[key] for key in ('path', 'bytes', 'sha256')} for item in payload}
    expected[MANIFEST_PATH] = {'path': MANIFEST_PATH, 'bytes': len(manifest_data), 'sha256': sha_bytes(manifest_data)}
    exact(walk(program), expected, 'Assembled Program')
    archive = output / (PACKAGE + '.zip')
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as destination:
        for name in sorted(expected):
            info = zipfile.ZipInfo(PACKAGE + '/' + name, ZIP_DATE)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            with (program / name).open('rb') as src, destination.open(info, 'w', force_zip64=True) as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
    verify_archive(archive, program, expected, needles)
    # A successful receipt exists only after CRC, exact ZIP inventory, every SHA and privacy check.
    receipt = {'format': 1, 'status': 'PASS', 'published': False, 'archive': archive.name,
               'archiveBytes': archive.stat().st_size, 'archiveSha256': sha_file(archive),
               'programFiles': len(expected), 'programTreeSha256': tree_hash(list(expected.values())),
               'inputContentHashes': hashes,
               'checks': {'freshEmptyOutput': True, 'inputManifestsAndFreeze': True, 'frozenInputsStable': True,
                          'noLinksOrReparseOrHardlinks': True, 'noForbiddenPrivateOrBuildFiles': True,
                          'latestWindowsRuntimeNames': True, 'offlineWheelClosure': True, 'privacyBinaryTextAndWheels': True,
                          'zipCRC': True, 'zipExactInventory': True, 'zipEveryPayloadSHA256': True}}
    receipt_data = json_bytes(receipt)
    privacy_chunk(receipt_data, needles, 'build-validation.json')
    (output / 'build-validation.json').write_bytes(receipt_data)
    (output / 'SHA256SUMS').write_text(receipt['archiveSha256'] + '  ' + archive.name + '\n', encoding='ascii')
    print(json.dumps(receipt, ensure_ascii=False), flush=True)
    return receipt

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory-root', type=Path, help='Read-only helper: output exact tree records/content hash')
    for name in ('source', 'export-manifest', 'runtime-root', 'wheel-root', 'freeze-manifest', 'output'):
        parser.add_argument('--' + name, type=Path)
    args = parser.parse_args()
    if args.inventory_root:
        records = walk(args.inventory_root)
        print(json.dumps({'format': 1, 'fileCount': len(records), 'totalBytes': sum(x['bytes'] for x in records), 'treeSha256': tree_hash(records), 'files': records}, ensure_ascii=False))
        return
    require(all(getattr(args, x) for x in ('source', 'export_manifest', 'runtime_root', 'wheel_root', 'freeze_manifest', 'output')), 'All build inputs and final root freeze are required')
    build(args.source, args.export_manifest, args.runtime_root, args.wheel_root, args.freeze_manifest, args.output)

if __name__ == '__main__':
    try:
        main()
    except (ContractError, OSError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        # CLI diagnostics avoid actual local paths/secret token strings.
        print(json.dumps({'status': 'BLOCKED', 'errorType': type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1)
