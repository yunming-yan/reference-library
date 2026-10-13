"""Disposable full Markdown render cache. PostgreSQL remains authoritative."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path

import apsw

CACHE_FORMAT = 2
MIN_SQLITE = (3, 54, 0)


class CacheError(RuntimeError):
    pass


def _not_link(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise CacheError("Cache path must not contain links or reparse points.")


class RenderedCache:
    """Owns only its rendered-v2 child. Entries are derived, never user data."""

    def __init__(self, root: str | Path):
        base = Path(root).absolute()
        if len(base.parts) < 3:
            raise CacheError("Refusing a broad cache root.")
        for ancestor in (base, *base.parents):
            _not_link(ancestor)
        self.root = base / "rendered-v2"
        _not_link(self.root)
        marker = self.root / ".reference-library-render-cache.json"
        _not_link(marker)
        if self.root.exists() and not marker.exists() and any(self.root.iterdir()):
            raise CacheError("Refusing an unowned nonempty cache directory.")
        self.root.mkdir(parents=True, exist_ok=True)
        if marker.exists():
            try:
                identity = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise CacheError("Invalid cache ownership marker.") from exc
            if identity != {"application": "reference-library", "kind": "rendered-cache", "format": CACHE_FORMAT}:
                raise CacheError("Cache ownership/format does not match.")
        else:
            payload = json.dumps({"application": "reference-library", "kind": "rendered-cache", "format": CACHE_FORMAT})
            with marker.open("x", encoding="utf-8") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        if tuple(map(int, apsw.sqlitelibversion().split("."))) < MIN_SQLITE:
            raise CacheError("The private APSW cache requires SQLite 3.54.0 or newer.")
        self.path = self.root / "rendered.sqlite3"
        for suffix in ("", "-wal", "-shm", "-journal"):
            _not_link(Path(str(self.path) + suffix))
        self._lock = threading.RLock()
        self._closed = False
        self._connection = apsw.Connection(str(self.path))
        self._connection.set_busy_timeout(6000)
        self._connection.execute("PRAGMA secure_delete=ON")
        self._connection.execute("PRAGMA journal_mode=WAL").fetchall()
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA temp_store=MEMORY")
        self._connection.execute("PRAGMA wal_autocheckpoint=1000")
        self._connection.execute("""CREATE TABLE IF NOT EXISTS renders(
            report_id TEXT NOT NULL, generation TEXT NOT NULL,
            content_hash TEXT NOT NULL, renderer_version TEXT NOT NULL,
            payload BLOB NOT NULL, payload_hash TEXT NOT NULL,
            PRIMARY KEY(report_id, generation, content_hash, renderer_version)
        ) WITHOUT ROWID""")

    def _key(self, report_id, generation, content_hash, renderer_version):
        key = tuple(map(str, (report_id, generation, content_hash, renderer_version)))
        if not key[0] or len(key[0]) > 200 or not key[1] or len(key[1]) > 400 or not re.fullmatch(r"[a-f0-9]{64}", key[2]) or not key[3] or len(key[3]) > 200:
            raise CacheError("Invalid render cache key.")
        return key

    def _open(self):
        if self._closed:
            raise CacheError("Cache is closed.")

    def get(self, report_id, generation, content_hash, renderer_version):
        key = self._key(report_id, generation, content_hash, renderer_version)
        with self._lock:
            self._open()
            row = self._connection.execute(
                "SELECT payload,payload_hash FROM renders WHERE report_id=? AND generation=? AND content_hash=? AND renderer_version=?", key
            ).fetchone()
        if row is None:
            return None
        payload, expected_hash = row
        if hashlib.sha256(payload).hexdigest() != expected_hash:
            raise CacheError("Corrupt render cache entry.")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CacheError("Corrupt UTF-8 cache entry.") from exc

    def put(self, report_id, generation, content_hash, renderer_version, html: str):
        key = self._key(report_id, generation, content_hash, renderer_version)
        payload = html.encode("utf-8")
        checksum = hashlib.sha256(payload).hexdigest()
        with self._lock:
            self._open()
            self._connection.execute(
                "INSERT OR REPLACE INTO renders VALUES(?,?,?,?,?,?)",
                (*key, payload, checksum),
            )

    def _finish_erasure(self):
        # Our cursors are exhausted/closed before checkpoints. A foreign open
        # snapshot makes erasure fail explicitly rather than claim completion.
        checkpoint = self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and checkpoint[0]:
            raise CacheError("Cache checkpoint is busy; retry permanent cleanup.")
        wal = Path(str(self.path) + "-wal")
        if wal.exists() and wal.stat().st_size:
            raise CacheError("Cache WAL retained pages; retry permanent cleanup.")

    def invalidate_report(self, report_id):
        if not str(report_id) or len(str(report_id)) > 200:
            raise CacheError("Invalid report identity.")
        with self._lock:
            self._open()
            self._connection.execute("DELETE FROM renders WHERE report_id=?", (str(report_id),))
            self._finish_erasure()

    def clear(self):
        with self._lock:
            self._open()
            self._connection.execute("DELETE FROM renders")
            self._finish_erasure()

    def diagnostics(self):
        with self._lock:
            self._open()
            return {"kind": "rebuildable-markdown-render", "format": CACHE_FORMAT,
                    "wrapper": apsw.apswversion(), "sqlite": apsw.sqlitelibversion(),
                    "journalMode": self._connection.execute("PRAGMA journal_mode").fetchone()[0],
                    "secureDelete": bool(self._connection.execute("PRAGMA secure_delete").fetchone()[0]),
                    "entries": self._connection.execute("SELECT count(*) FROM renders").fetchone()[0]}

    def close(self):
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True
