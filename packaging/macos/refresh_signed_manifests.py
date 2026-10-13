"""Refresh post-nested-signing bytes before sealing a macOS outer app.
Run on an existing build_app.py bundle. Verification is content-only; it does
not substitute for native codesign, notarization, or Gatekeeper verification.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat

MACHO_MAGIC = {bytes.fromhex(x) for x in ('feedface', 'cefaedfe', 'feedfacf', 'cffaedfe', 'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca')}
REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)


def check(condition, message):
    if not condition:
        raise ValueError(message)


def plain(path):
    for item in (path, *path.parents):
        if not item.exists() and not item.is_symlink():
            continue
        info = item.lstat()
        check(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & REPARSE,
              'Links and reparse points are forbidden')
        check(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode), 'Non-regular payload entry')


def relative(name):
    check(isinstance(name, str) and name and '\\' not in name and '\x00' not in name, 'Malformed manifest path')
    value = PurePosixPath(name)
    check(not value.is_absolute() and value.as_posix() == name and all(x not in {'', '.', '..'} for x in value.parts), 'Unsafe manifest path')
    return value


def digest(path):
    plain(path)
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def record(path, name, mode):
    return {'path': name, 'bytes': path.stat().st_size, 'sha256': digest(path), 'unixMode': mode}


def is_macho(path):
    with path.open('rb') as stream:
        return stream.read(4) in MACHO_MAGIC


def write_atomic(path, value):
    data = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    temporary = path.with_name(path.name + '.signed-refresh.tmp')
    check(not temporary.exists(), 'Preserve existing interrupted refresh and choose a fresh bundle')
    mode = stat.S_IMODE(path.stat().st_mode)
    with temporary.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)


def entries(root):
    result = {}
    seen = set()
    plain(root)
    for current, dirs, files in os.walk(root, followlinks=False):
        current = Path(current)
        for name in dirs:
            plain(current / name)
        for name in files:
            path = current / name
            plain(path)
            name = path.relative_to(root).as_posix()
            relative(name)
            check(name.casefold() not in seen, 'Duplicate case-insensitive payload name')
            seen.add(name.casefold())
            result[name] = path
    return result


def item_map(items):
    check(isinstance(items, list), 'Manifest files must be a list')
    result = {}
    for item in items:
        name = item.get('path')
        relative(name)
        check(name not in result, 'Duplicate manifest path')
        check(isinstance(item.get('bytes'), int) and item['bytes'] > 0, 'Invalid manifest size')
        check(isinstance(item.get('sha256'), str) and len(item['sha256']) == 64, 'Invalid manifest hash')
        result[name] = item
    return result


def context(app):
    app = Path(app).absolute()
    plain(app)
    check(app.name.endswith('.app') and app.is_dir(), 'An existing absolute app bundle is required')
    parent = app.parent
    program = app / 'Contents' / 'Resources' / 'Program'
    release_path = program / 'release-manifest.json'
    native = program / 'runtime' / 'postgresql' / 'macos-universal2'
    runtime_path = native / 'manifest.json'
    check(release_path.is_file() and runtime_path.is_file(), 'build_app.py release/runtime manifests are required')
    release = json.loads(release_path.read_text('utf-8'))
    runtime = json.loads(runtime_path.read_text('utf-8'))
    check(release.get('format') == 1 and 'sharedSourceSnapshot' in release, 'Original build source provenance is required')
    for name, expected in release['sharedSourceSnapshot'].items():
        relative(name)
        check(digest(program / name) == expected, 'Shared source changed since original build')
    own_manifest = release_path.relative_to(parent).as_posix()
    # Outer CodeResources seals this manifest: recording its own bytes would
    # introduce a hash cycle. Codesign/Gatekeeper verify these seal files.
    def excluded(name):
        parts = PurePosixPath(name).parts
        return name == own_manifest or any(parts[i].endswith('.app') and parts[i + 1:i + 3] == ('Contents', '_CodeSignature') for i in range(max(0, len(parts) - 2)))
    actual = {name: path for name, path in entries(parent).items() if not excluded(name)}
    expected = item_map(release['files'])
    expected = {name: item for name, item in expected.items() if not excluded(name)}
    check(set(actual) == set(expected), 'Release payload membership changed since build')
    return parent, program, native, runtime_path, runtime, release_path, release, actual, expected, own_manifest


def verify(app, require_refreshed=False):
    parent, program, native, runtime_path, runtime, release_path, release, actual, expected, own_manifest = context(app)
    if require_refreshed:
        check(release.get('signingInventory', {}).get('state') == 'refreshed-after-nested-signing', 'Post-nested-signing inventory is required')
    runtime_items = item_map(runtime['files'])
    check('manifest.json' not in runtime_items, 'Runtime manifest self-hash is forbidden')
    for name, item in runtime_items.items():
        path = native / name
        check(path.is_file() and path.stat().st_size == item['bytes'] and digest(path) == item['sha256'], 'Runtime post-signing hash mismatch')
    for name, item in expected.items():
        check(actual[name].stat().st_size == item['bytes'] and digest(actual[name]) == item['sha256'], 'Release post-signing hash mismatch')
    for name, item in release.get('signedPayloadSnapshot', {}).items():
        relative(name)
        check((program / name).stat().st_size == item['bytes'] and digest(program / name) == item['sha256'], 'Signed payload snapshot changed')
    return {'status': 'PASS', 'operation': 'content-verification', 'runtimeFiles': len(runtime_items), 'releaseFiles': len(expected), 'releaseManifestSha256': digest(release_path), 'nativeSignatureValidation': 'requires actual macOS codesign/Gatekeeper'}


def refresh(app):
    parent, program, native, runtime_path, runtime, release_path, release, actual, expected, own_manifest = context(app)
    runtime_items = item_map(runtime['files'])
    check('manifest.json' not in runtime_items, 'Runtime manifest self-hash is forbidden')
    changed = {}
    # Only native Mach-O changes may be blessed by this signing-specific refresh.
    # Source, wheels, licenses, configs and their provenance must remain identical.
    for name, old in expected.items():
        path = actual[name]
        if path.stat().st_size != old['bytes'] or digest(path) != old['sha256']:
            check(is_macho(path), 'Non-Mach-O payload changed before signed refresh')
            if path.is_relative_to(program):
                changed[path.relative_to(program).as_posix()] = {'bytes': path.stat().st_size, 'sha256': digest(path)}
    for name, old in runtime_items.items():
        path = native / name
        check(path.is_file(), 'Missing runtime payload')
        if path.stat().st_size != old['bytes'] or digest(path) != old['sha256']:
            check(is_macho(path), 'Non-Mach-O runtime payload changed before signed refresh')
        old['bytes'] = path.stat().st_size
        old['sha256'] = digest(path)
    write_atomic(runtime_path, runtime)
    runtime_relative = runtime_path.relative_to(program).as_posix()
    changed[runtime_relative] = {'bytes': runtime_path.stat().st_size, 'sha256': digest(runtime_path)}
    payload = []
    for name, path in sorted(actual.items()):
        mode = expected[name].get('unixMode', oct(stat.S_IMODE(path.stat().st_mode)))
        payload.append(record(path, name, mode))
    release['files'] = payload
    release['signedPayloadSnapshot'] = changed
    release['signingInventory'] = {'state': 'refreshed-after-nested-signing',
                                  'originalBuildSnapshotPreserved': True,
                                  'excludedFromPayloadInventory': ['this release-manifest.json', '*.app/Contents/_CodeSignature/**'],
                                  'reason': 'Avoid release-manifest/outer-resource-seal self-hash cycles; native codesign verifies outer seals.',
                                  'nativeSignatureValidation': 'Pending actual macOS outer codesign/notarization verification'}
    write_atomic(release_path, release)
    result = verify(app, require_refreshed=True)
    result['operation'] = 'post-nested-signing-refresh'
    result['changedProgramFiles'] = len(changed)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--require-refreshed', action='store_true')
    args = parser.parse_args()
    check(args.app.is_absolute(), 'App path must be absolute')
    print(json.dumps(verify(args.app, args.require_refreshed) if args.verify else refresh(args.app), ensure_ascii=False))


if __name__ == '__main__':
    main()
