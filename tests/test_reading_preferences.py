"""Public contract tests for reading settings accepted by HTTP and PostgreSQL.

Load the small validator functions without opening a database or importing native
packages. End-to-end persistence is covered separately by the backend HTTP suite.
"""
import ast
from http.client import HTTPConnection
import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, unquote
from copy import deepcopy
import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / 'app'
sys.path.insert(0, str(APP))
from reading_preferences import FONT_ASSET_PINS, FONT_FAMILIES, valid_font_size
from original_store import OriginalStore, OriginalError


def validators():
    implementations = []
    for name, selected, validate, normalize in (
        ('server.py', {'default_reading', 'validate_reading', 'normalized_preferences'}, 'validate_reading', 'normalized_preferences'),
        ('pg_catalog.py', {'_default_preferences', '_reading', '_preferences'}, '_reading', '_preferences'),
    ):
        tree = ast.parse((APP / name).read_text(encoding='utf-8'))
        body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in selected]
        namespace = {'FONT_FAMILIES': FONT_FAMILIES, 'valid_font_size': valid_font_size,
                     'deepcopy': deepcopy, 'ArchiveError': ValueError, 'CatalogError': ValueError}
        exec(compile(ast.Module(body=body, type_ignores=[]), name, 'exec'), namespace)
        implementations.append((name, namespace[validate], namespace[normalize]))
    return implementations


class ReadingPreferenceContractTests(unittest.TestCase):
    def test_each_half_pixel_and_legacy_sizes_round_trip_both_layers(self):
        for name, validate, normalize in validators():
            for half in range(24, 65):
                value = half / 2
                with self.subTest(layer=name, size=value):
                    self.assertEqual(validate({'fontSize': value}), {'fontSize': value})
                    self.assertEqual(normalize({'reading': {'fontSize': value}})['reading']['fontSize'], value)

    def test_invalid_numeric_forms_rejected_without_overflow(self):
        invalid = [True, False, None, '16', [], {}, float('nan'), float('inf'), -float('inf'),
                   11.5, 32.5, 12.25, 16.0000000001, 10**1000, -(10**1000)]
        for name, validate, _ in validators():
            for value in invalid:
                with self.subTest(layer=name, kind=type(value).__name__):
                    with self.assertRaises(ValueError):
                        validate({'fontSize': value})

    def test_family_allowlist_and_hostile_css_values(self):
        for name, validate, normalize in validators():
            for family in FONT_FAMILIES:
                with self.subTest(layer=name, family=family):
                    self.assertEqual(validate({'fontFamily': family}), {'fontFamily': family})
                    self.assertEqual(normalize({'reading': {'fontFamily': family}})['reading']['fontFamily'], family)
            for family in ['', 'Arial', '__proto__', 'url(https://invalid.test/font)', 'system; color:red', [], None, 12]:
                with self.assertRaises(ValueError, msg=name):
                    validate({'fontFamily': family})

    def test_legacy_missing_family_preserves_other_preferences(self):
        old = {'theme': 'dark', 'reading': {'fontSize': 16, 'lineHeight': 1.8,
               'width': 'balanced', 'library': False, 'toc': True}}
        for name, _, normalize in validators():
            result = normalize(old)
            self.assertEqual(result['theme'], 'dark', name)
            self.assertEqual(result['reading']['fontFamily'], 'system', name)
            for key, value in old['reading'].items():
                self.assertEqual(result['reading'][key], value, name)
            self.assertNotIn('fontFamily', old['reading'])

    def test_invalid_saved_fields_fall_back_independently(self):
        for name, _, normalize in validators():
            result = normalize({'reading': {'fontFamily': 'external', 'fontSize': 17.5,
                               'width': 'wide', 'focus': True}})['reading']
            self.assertEqual(result['fontFamily'], 'system', name)
            self.assertEqual(result['fontSize'], 17.5, name)
            self.assertEqual(result['width'], 'wide', name)
            self.assertTrue(result['focus'], name)


class FontCacheContractTests(unittest.TestCase):
    def test_only_exact_reviewed_font_version_has_one_immutable_header(self):
        tree = ast.parse((APP / 'server.py').read_text(encoding='utf-8'))
        factory = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'make_handler')
        constants = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in {'MIME', 'APP_CSP'}:
                        constants[target.id] = ast.literal_eval(node.value)
        # Synthetic bytes only, using the real static HTTP handler and path guard.
        with tempfile.TemporaryDirectory(prefix='font-cache-', dir=os.environ.get('BACKEND_TEST_ROOT')) as directory:
            root = Path(directory)
            path, pin = next(iter(FONT_ASSET_PINS.items()))
            target = root / 'web' / path.lstrip('/')
            target.parent.mkdir(parents=True)
            target.write_bytes(b'synthetic reviewed-font fixture')
            (root / 'web' / 'reader.js').write_text('/* fixture */', encoding='utf-8')
            namespace = {'BaseHTTPRequestHandler': BaseHTTPRequestHandler, 'ROOT': root,
                         'OriginalStore': OriginalStore, 'ArchiveError': OriginalError,
                         'FONT_ASSET_PINS': FONT_ASSET_PINS, 'CHUNK': 1024,
                         'urlsplit': urlsplit, 'parse_qs': parse_qs, 'unquote': unquote,
                         'json': json, 'os': os, **constants}
            exec(compile(ast.Module(body=[factory], type_ignores=[]), 'server.py', 'exec'), namespace)
            class StoreStub:
                def health(self): return {'syntheticOnly': True}
                def error_payload(self, error): return {'status': getattr(error, 'status', 500), 'error': str(error)}
            http = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
            port = http.server_address[1]
            http.RequestHandlerClass = namespace['make_handler'](StoreStub(), port)
            worker = threading.Thread(target=http.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
            worker.start()
            try:
                immutable = 'private, max-age=31536000, immutable'
                cases = [(path + '?v=' + pin, immutable), (path, 'no-store'),
                         (path + '?v=wrong', 'no-store'),
                         (path + '?v=' + pin + '&v=' + pin, 'no-store'),
                         (path + '?v=' + pin + '&other=1', 'no-store'),
                         ('/reader.js?v=' + pin, 'no-store'), ('/api/health?v=' + pin, 'no-store')]
                for url, expected in cases:
                    with self.subTest(url=url):
                        connection = HTTPConnection('127.0.0.1', port, timeout=5)
                        try:
                            connection.request('GET', url)
                            response = connection.getresponse()
                            headers = response.getheaders()
                            self.assertEqual(response.status, 200)
                            self.assertEqual([v for k, v in headers if k.lower() == 'cache-control'], [expected])
                            if url.startswith(path):
                                self.assertEqual(response.getheader('Content-Type'), 'font/woff2')
                            if not url.startswith('/api/'):
                                self.assertIn("font-src 'self'", response.getheader('Content-Security-Policy', ''))
                            response.read()
                        finally: connection.close()
            finally:
                http.shutdown(); http.server_close(); worker.join(timeout=3)
                self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
