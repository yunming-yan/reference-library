"""Focused platform directory contracts; mocks do not claim native macOS QA."""
from __future__ import annotations
from contextlib import ExitStack
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'app'))
import runtime_manager as rm


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class WindowsFolderAPI:
    def __init__(self, path, result=0, allocate=True):
        self.buffer = ctypes.create_unicode_buffer(str(path))
        self.result, self.allocate = result, allocate
        self.lookups, self.frees, self.dlls = [], [], []

    def lookup(self, folder, flags, token, output):
        self.lookups.append((ctypes.string_at(folder, 16), flags, token))
        if self.allocate:
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(self.buffer)
        return self.result

    def load(self, name, **kwargs):
        self.dlls.append((name, kwargs))
        if name == 'shell32':
            return SimpleNamespace(SHGetKnownFolderPath=Function(self.lookup))
        return SimpleNamespace(CoTaskMemFree=Function(lambda value: self.frees.append(value.value)))


class MacFolderAPI:
    """Only simulates call boundaries and ownership; it does not emulate dyld."""
    def __init__(self, support, cache, fail=None):
        self.values = {401: os.fsencode(support), 402: os.fsencode(cache)}
        self.fail = fail
        self.calls, self.signatures, self.loads = [], [], []

    def load(self, name):
        self.loads.append(name)
        if name.endswith('Foundation'):
            return object()
        return SimpleNamespace(
            objc_getClass=Function(lambda name: {b'NSAutoreleasePool': 1, b'NSFileManager': 2}.get(name, 0)),
            sel_registerName=Function(lambda name: name),
            objc_msgSend=object())

    def signature(self, result, *arguments):
        self.signatures.append((result, arguments))
        return lambda address: self.dispatch

    def dispatch(self, receiver, selector, *args):
        self.calls.append((receiver, selector, args))
        if selector == b'alloc':
            return 101
        if selector == b'init':
            return 101
        if selector == b'defaultManager':
            return 200
        if selector == b'URLsForDirectory:inDomains:':
            return 301 if args[0] == 14 else 302
        if selector == b'count':
            return 0 if self.fail == 'empty' else 1
        if selector == b'firstObject':
            return 401 if receiver == 301 else 402
        if selector == b'isFileURL':
            return self.fail != 'nonfile'
        if selector == b'fileSystemRepresentation':
            return None if self.fail == 'representation' else self.values[receiver]
        if selector == b'drain':
            return None
        raise AssertionError('Unexpected Foundation selector')


class PlatformDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.program = self.root / 'Program'
        self.program.mkdir()
        self.support, self.cache = self.root / 'SystemSupport', self.root / 'SystemCaches'
        self.addCleanup(patch.stopall)
        patch.object(rm, 'private_acl', lambda path: None).start()

    def profile(self, where=None, **changes):
        where = where or self.root / 'machine.json'
        data = {
            'dataRoot': str(self.root / 'CustomLibrary'),
            'privateStateRoot': str(self.root / 'CustomState'),
            'cacheRoot': str(self.root / 'CustomCache')}
        data.update(changes)
        where.write_text(json.dumps(data), encoding='utf-8')
        return where, data

    def windows(self):
        # Patch this module's OS adapter, not global os.name/pathlib's platform.
        return patch.object(rm, 'os', SimpleNamespace(name='nt', environ=dict(os.environ), fsdecode=os.fsdecode, replace=os.replace))

    def call_windows_api(self, fake):
        with patch('ctypes.WinDLL', fake.load, create=True):
            return rm._windows_local_appdata()

    def call_mac_api(self, fake):
        with patch('ctypes.CDLL', fake.load), patch('ctypes.cast', return_value=SimpleNamespace(value=123)), patch('ctypes.CFUNCTYPE', fake.signature):
            return rm._mac_user_directories()

    def test_windows_current_user_guid_zero_flags_and_free(self):
        fake = WindowsFolderAPI(self.support / 'Unicode-资料')
        self.assertEqual(self.call_windows_api(fake), self.support / 'Unicode-资料')
        self.assertEqual(fake.lookups, [(uuid.UUID('F1B32785-6FBA-4FCF-9D55-7B8E7F157091').bytes_le, 0, None)])
        self.assertEqual(fake.frees, [ctypes.addressof(fake.buffer)])
        self.assertEqual(fake.dlls, [('shell32', {'winmode': 0x800}), ('ole32', {'winmode': 0x800})])

    def test_windows_failure_frees_allocated_output(self):
        fake = WindowsFolderAPI(self.support, result=-2147467259)
        with self.assertRaises(OSError):
            self.call_windows_api(fake)
        self.assertEqual(fake.frees, [ctypes.addressof(fake.buffer)])

    def test_windows_null_output_fails_without_freeing_null(self):
        fake = WindowsFolderAPI(self.support, allocate=False)
        with self.assertRaises(OSError):
            self.call_windows_api(fake)
        self.assertEqual(fake.frees, [])

    def test_mac_user_domain_support_cache_and_pool_lifetime(self):
        fake = MacFolderAPI(self.support, self.cache)
        self.assertEqual(self.call_mac_api(fake), (self.support, self.cache))
        lookups = [(selector, args) for _, selector, args in fake.calls if selector == b'URLsForDirectory:inDomains:']
        self.assertEqual(lookups, [(b'URLsForDirectory:inDomains:', (14, 1)), (b'URLsForDirectory:inDomains:', (13, 1))])
        self.assertEqual(fake.calls[-1], (101, b'drain', ()))
        self.assertEqual(fake.loads, ['/System/Library/Frameworks/Foundation.framework/Foundation', '/usr/lib/libobjc.A.dylib'])
        self.assertIn((ctypes.c_void_p, (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t)), fake.signatures)

    def test_mac_empty_domain_fails_and_drains_pool(self):
        fake = MacFolderAPI(self.support, self.cache, fail='empty')
        with self.assertRaises(OSError):
            self.call_mac_api(fake)
        self.assertEqual(fake.calls[-1], (101, b'drain', ()))

    def test_mac_nonfile_url_fails_and_drains_pool(self):
        fake = MacFolderAPI(self.support, self.cache, fail='nonfile')
        with self.assertRaises(OSError):
            self.call_mac_api(fake)
        self.assertEqual(fake.calls[-1], (101, b'drain', ()))

    def test_mac_missing_representation_fails_and_drains_pool(self):
        fake = MacFolderAPI(self.support, self.cache, fail='representation')
        with self.assertRaises(OSError):
            self.call_mac_api(fake)
        self.assertEqual(fake.calls[-1], (101, b'drain', ()))

    def test_windows_native_result_ignores_environment_and_home(self):
        with self.windows(), patch.dict(rm.os.environ, {'LOCALAPPDATA': str(self.root / 'WrongEnvironment')}), patch.object(rm, '_windows_local_appdata', return_value=self.support), patch.object(Path, 'home', side_effect=AssertionError('Do not guess home')):
            self.assertEqual(rm._default_user_directories(), (self.support, self.support))

    def test_mac_native_result_ignores_home(self):
        with patch.object(rm, 'sys', SimpleNamespace(platform='darwin')), patch.object(rm, '_mac_user_directories', return_value=(self.support, self.cache)), patch.object(Path, 'home', side_effect=AssertionError('Do not guess home')):
            self.assertEqual(rm._default_user_directories(), (self.support, self.cache))

    def test_native_failure_does_not_create_guessed_library(self):
        with self.windows(), patch.dict(rm.os.environ, {'LOCALAPPDATA': str(self.root / 'WrongEnvironment')}, clear=True), patch.object(rm, '_windows_local_appdata', side_effect=OSError('API unavailable')), patch.object(rm.MachineConfig, '_bind_roots') as bind:
            with self.assertRaises(RuntimeError):
                rm.MachineConfig(self.program)
            bind.assert_not_called()
        self.assertFalse((self.root / 'WrongEnvironment').exists())
        self.assertFalse((self.program / 'machine.json').exists())

    def test_explicit_profile_wins_over_environment_and_native_failure(self):
        profile, data = self.profile()
        before = profile.read_bytes()
        with patch.dict(os.environ, {'REFERENCE_LIBRARY_MACHINE_CONFIG': str(self.root / 'AbsentEnvironment.json')}), patch.object(rm, '_default_user_directories', side_effect=AssertionError('Profile must win')) as native:
            machine = rm.MachineConfig(self.program, profile)
        native.assert_not_called()
        self.assertEqual(machine.data_root, Path(data['dataRoot']))
        self.assertEqual(machine.private_root, Path(data['privateStateRoot']))
        self.assertEqual(machine.cache_root, Path(data['cacheRoot']))
        self.assertEqual(profile.read_bytes(), before)

    def test_environment_profile_wins_over_native_failure(self):
        profile, data = self.profile()
        with patch.dict(os.environ, {'REFERENCE_LIBRARY_MACHINE_CONFIG': str(profile)}), patch.object(rm, '_default_user_directories', side_effect=AssertionError('Profile must win')) as native:
            machine = rm.MachineConfig(self.program)
        native.assert_not_called()
        self.assertEqual(machine.data_root, Path(data['dataRoot']))

    def test_repo_profile_wins_over_native_failure(self):
        profile, data = self.profile(self.program / 'machine.json')
        with patch.dict(os.environ, {}, clear=True), patch.object(rm, '_default_user_directories', side_effect=AssertionError('Profile must win')) as native:
            machine = rm.MachineConfig(self.program)
        native.assert_not_called()
        self.assertEqual(machine.data_root, Path(data['dataRoot']))

    def test_missing_explicit_profile_cannot_fall_back(self):
        with patch.object(rm, '_default_user_directories') as native, patch.object(rm.MachineConfig, '_bind_roots') as bind:
            with self.assertRaises(RuntimeError):
                rm.MachineConfig(self.program, self.root / 'Missing.json')
        native.assert_not_called()
        bind.assert_not_called()

    def test_windows_defaults_preserve_existing_layout(self):
        with self.windows(), patch.dict(rm.os.environ, {}, clear=True), patch.object(rm, '_default_user_directories', return_value=(self.support, self.support)):
            machine = rm.MachineConfig(self.program)
        base = self.support / 'ReferenceLibrary'
        self.assertEqual((machine.data_root, machine.private_root, machine.cache_root), (base / 'library', base / 'runtime', base / 'cache'))
        self.assertEqual(machine.pgdata, machine.data_root / 'catalog/pg18-main')
        self.assertEqual(machine.config_path, machine.private_root / 'machine-selected.json')
        self.assertFalse((self.program / 'machine.json').exists())

    def test_mac_defaults_preserve_uuid_and_persistent_pgdata(self):
        existing = self.support / 'ReferenceLibrary/Library'
        existing.mkdir(parents=True)
        identity = str(uuid.uuid4())
        (existing / rm.MARKER).write_text(json.dumps({'format': 1, 'owner': 'reference-library', 'libraryId': identity}), encoding='utf-8')
        with patch.dict(os.environ, {}, clear=True), patch.object(rm, 'sys', SimpleNamespace(platform='darwin')), patch.object(rm, '_default_user_directories', return_value=(self.support, self.cache)):
            machine = rm.MachineConfig(self.program)
        self.assertEqual(machine.library_id, identity)
        self.assertEqual(machine.data_root, existing)
        self.assertEqual(machine.private_root, self.support / 'ReferenceLibrary/Runtime' / identity)
        self.assertEqual(machine.cache_root, self.cache / 'ReferenceLibrary' / identity)
        self.assertEqual(machine.pgdata, existing / 'catalog/pg18-main')

    def test_native_relative_and_volume_root_results_are_rejected(self):
        for invalid in (Path('relative'), Path(self.root.anchor)):
            with self.subTest(value=str(invalid)), self.windows(), patch.object(rm, '_windows_local_appdata', return_value=invalid):
                with self.assertRaises(RuntimeError):
                    rm._default_user_directories()

    def test_native_file_result_is_rejected(self):
        bad = self.root / 'NotDirectory'
        bad.write_bytes(b'owned test file')
        with self.windows(), patch.object(rm, '_windows_local_appdata', return_value=bad):
            with self.assertRaises(RuntimeError):
                rm._default_user_directories()

    def test_custom_overlap_still_rejected_before_creating_roots(self):
        profile, _ = self.profile(dataRoot=str(self.program / 'Content'))
        with patch.object(rm, '_default_user_directories', side_effect=AssertionError('Profile must win')):
            with self.assertRaises(RuntimeError):
                rm.MachineConfig(self.program, profile)
        self.assertFalse((self.program / 'Content').exists())


if __name__ == '__main__':
    unittest.main()
