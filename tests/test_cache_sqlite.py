from __future__ import annotations
import hashlib, os, pathlib, sys, tempfile, threading, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))
from cache_sqlite import RenderedCache, CacheError

class RenderCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ["REFERENCE_LIBRARY_TEST_ROOT"])
        self.root = pathlib.Path(self.temp.name)
        self.cache = RenderedCache(self.root / "cache")
        self.key = ("synthetic-report", "generation-a", hashlib.sha256(b"source").hexdigest(), "parser-v2")
    def tearDown(self):
        self.cache.close()
        self.temp.cleanup()
    def test_roundtrip_reopen_and_version_isolation(self):
        html = "<h1>合成全文</h1><table><tr><td>完整\0字符ß</td></tr></table>"
        self.cache.put(*self.key, html)
        self.assertEqual(self.cache.get(*self.key), html)
        self.assertIsNone(self.cache.get(*self.key[:-1], "new-parser"))
        self.assertIsNone(self.cache.get(self.key[0], "generation-b", *self.key[2:]))
        self.cache.close()
        self.cache = RenderedCache(self.root / "cache")
        self.assertEqual(self.cache.get(*self.key), html)
        self.assertEqual(self.cache.diagnostics()["sqlite"], "3.54.0")
    def test_full_overflow_erasure_all_generations_and_survivor(self):
        needle = b"SYNTHETIC_ERASURE_SENTINEL_987654321"
        html = (needle.decode() + "x" * 8096) * 30
        self.cache.put(*self.key, html)
        self.cache.put(self.key[0], "generation-b", *self.key[2:], html)
        self.cache.put("survivor", *self.key[1:], "<p>preserve this derived view</p>")
        self.cache.invalidate_report(self.key[0])
        self.assertIsNone(self.cache.get(*self.key))
        self.assertEqual(self.cache.get("survivor", *self.key[1:]), "<p>preserve this derived view</p>")
        for p in self.cache.root.iterdir():
            if p.is_file():
                self.assertNotIn(needle, p.read_bytes())
    def test_corrupt_entry_does_not_return_partial_text(self):
        self.cache.put(*self.key, "<p>source</p>")
        self.cache._connection.execute("UPDATE renders SET payload=?", (b"tampered",))
        with self.assertRaises(CacheError):
            self.cache.get(*self.key)
    def test_parallel_put_get_preserves_complete_payloads(self):
        errors = []
        def worker(n):
            try:
                rid = f"worker-{n}"
                html = f"<p>{n}</p>" * 2000
                self.cache.put(rid, *self.key[1:], html)
                if self.cache.get(rid, *self.key[1:]) != html: errors.append(n)
            except BaseException as exc: errors.append(str(exc))
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertFalse(errors)
    def test_unowned_directory_and_symbolic_link_refused(self):
        wrong = self.root / "wrong" / "rendered-v2"
        wrong.mkdir(parents=True)
        (wrong / "unrelated").write_text("keep", encoding="utf-8")
        with self.assertRaises(CacheError): RenderedCache(wrong.parent)
        self.assertEqual((wrong / "unrelated").read_text(), "keep")
    def test_closed_cache_and_clear(self):
        self.cache.put(*self.key, "<p>temporary</p>")
        self.cache.clear()
        self.assertIsNone(self.cache.get(*self.key))
        self.cache.close()
        with self.assertRaises(CacheError): self.cache.get(*self.key)

if __name__ == "__main__":
    unittest.main()
