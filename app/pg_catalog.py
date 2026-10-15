"""PostgreSQL authority and exact Unicode casefold substring catalog.

Original bytes are owned by the immutable filesystem store, not this derived
search index. A query never stats or opens originals. Chunk overlap covers every
allowed 200-codepoint query (600 after casefold). Literal 1/2-folded-codepoint
postings are exact; longer queries use bytea position() as the final predicate,
including NUL, punctuation and non-normalized Unicode.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import time
import threading
import uuid

from reading_preferences import FONT_FAMILIES, valid_font_size

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

SCHEMA_VERSION = 4
MAINTENANCE_LOCK_KEY = 774831012
CHUNK_CORE = 8192
MAX_QUERY = 200
CHUNK_OVERLAP = MAX_QUERY * 3
MAX_LEGACY_OFFSET = 5000
SEARCH_VERSION = 'casefold-bytea-chunks-1'


class CatalogError(Exception):
    def __init__(self, message, status=400, existing=None):
        super().__init__(message)
        self.status = status
        self.existing = existing


class CatalogConflict(CatalogError):
    def __init__(self, message='Catalog generation changed; refresh and retry.', existing=None):
        super().__init__(message, 409, existing)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _pack(value):
    # bytea, rather than jsonb, preserves strings containing U+0000 exactly.
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _unpack(value):
    return json.loads(bytes(value).decode('utf-8'))


def _pg_text(value):
    return str(value).replace('\x00', '\ufffd')


def _default_preferences():
    return {'theme': 'light', 'reading': {'library': True, 'toc': True,
        'fontSize': 18, 'fontFamily': 'system', 'lineHeight': 1.8, 'width': 'balanced', 'focus': False,
        'focusRestore': {'library': True, 'toc': True}}}


def _reading(changes):
    defaults = _default_preferences()['reading']
    if not isinstance(changes, dict) or not set(changes).issubset(defaults):
        raise CatalogError('Reading settings contain unsupported fields.')
    result = deepcopy(changes)
    for key, value in result.items():
        if key in ('library', 'toc', 'focus') and type(value) is not bool:
            raise CatalogError('Reading panel and focus settings must be boolean.')
        if key == 'fontSize' and not valid_font_size(value):
            raise CatalogError('Invalid reading font size.')
        if key == 'fontFamily' and (type(value) is not str or value not in FONT_FAMILIES):
            raise CatalogError('Invalid reading font family.')
        if key == 'lineHeight' and (type(value) not in (int, float) or value not in (1.6, 1.8, 2.0)):
            raise CatalogError('Invalid reading line height.')
        if key == 'width' and value not in ('narrow', 'balanced', 'wide'):
            raise CatalogError('Invalid reading width.')
        if key == 'focusRestore' and (not isinstance(value, dict)
                or not set(value).issubset({'library', 'toc'})
                or any(type(v) is not bool for v in value.values())):
            raise CatalogError('Invalid focus restore settings.')
    return result


def _preferences(saved):
    result = _default_preferences()
    if not isinstance(saved, dict):
        return result
    if saved.get('theme') in ('light', 'dark'):
        result['theme'] = saved['theme']
    if isinstance(saved.get('reading'), dict):
        for key, value in saved['reading'].items():
            try:
                item = _reading({key: value})
            except CatalogError:
                continue
            if key == 'focusRestore':
                result['reading'][key].update(item[key])
            else:
                result['reading'].update(item)
    return result


def _payload_package(payload):
    if payload.get('package'):
        return payload['package']
    manifest = payload.get('manifest', {})
    reports = manifest.get('reports', []) if isinstance(manifest, dict) else []
    return (manifest.get('package') or (reports[0].get('package') if reports else None)) if isinstance(manifest, dict) else None


def _identifier(value, label='identifier'):
    if not isinstance(value, str) or not value or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise CatalogError('Invalid ' + label + '.')
    return value


def _member_path(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise CatalogError('Invalid member path.')
    p = PurePosixPath(value)
    if p.is_absolute() or any(part in ('', '.', '..') for part in value.split('/')):
        raise CatalogError('Invalid member path.')
    return value


class Catalog:
    def __init__(self, dsn=None, min_size=1, max_size=6, initialize=True):
        if dsn is None:
            dsn = next((os.environ[k] for k in ('REFERENCE_LIBRARY_PG_DSN',
                'REFERENCE_LIBRARY_DSN', 'PKM_PG_DSN', 'PG_DSN') if os.environ.get(k)), None)
            if dsn is None and os.environ.get('REFERENCE_LIBRARY_PG_CONFIG'):
                dsn = json.loads(Path(os.environ['REFERENCE_LIBRARY_PG_CONFIG']).read_text(encoding='utf-8'))
        if isinstance(dsn, dict):
            dsn = make_conninfo(**{k: dsn[k] for k in ('host', 'port', 'dbname', 'user', 'password') if k in dsn})
        if not dsn:
            raise CatalogError('PostgreSQL connection configuration is required.', 503)
        self._bulk_state = threading.local()
        self._list_cache_lock = threading.Lock()
        self._list_generation = -1
        self._list_summary = None
        self._list_counts = OrderedDict()
        self._instance_id = os.environ.get('REFERENCE_LIBRARY_INSTANCE_ID') or str(uuid.uuid4())
        self._dsn = dsn
        self.pool = ConnectionPool(conninfo=dsn, min_size=min_size, max_size=max_size,
            max_waiting=32, timeout=10, open=False,
            kwargs={'row_factory': dict_row, 'connect_timeout': 5,
                'options': '-c statement_timeout=30000 -c idle_in_transaction_session_timeout=60000'})
        self.pool.open()
        self.pool.wait(timeout=10)
        if initialize:
            self.initialize()
        with self.pool.connection() as conn:
            row = conn.execute('SELECT cursor_secret FROM rl_state WHERE singleton').fetchone()
            if not row:
                raise CatalogError('PostgreSQL catalog is not initialized.', 503)
            self._cursor_secret = bytes(row['cursor_secret'])

    def close(self):
        self.pool.close()

    def initialize(self, conn=None):
        if conn is None:
            with self.pool.connection() as owned:
                return self.initialize(owned)
        folder = Path(__file__).resolve().parent.parent / 'schema' / 'migrations'
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute('SELECT pg_advisory_xact_lock(%s)', (MAINTENANCE_LOCK_KEY,))
                cur.execute('CREATE TABLE IF NOT EXISTS rl_schema_migrations(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())')
                applied = {r['version'] for r in cur.execute('SELECT version FROM rl_schema_migrations')}
                needs_path_backfill = False
                for path in sorted(folder.glob('*.sql')):
                    version = int(path.name.split('_', 1)[0])
                    if version not in applied:
                        needs_path_backfill = needs_path_backfill or version == 3
                        cur.execute(path.read_text(encoding='utf-8-sig'), prepare=False)
                        cur.execute('INSERT INTO rl_schema_migrations(version) VALUES(%s)', (version,))
                if needs_path_backfill:
                    with conn.cursor(name='rl_md_path_backfill_' + uuid.uuid4().hex, row_factory=dict_row) as backfill:
                        backfill.execute('SELECT report_id,record_data FROM rl_reports WHERE md_path IS NULL')
                        while batch := backfill.fetchmany(1000):
                            rows = [(_member_path(_unpack(row['record_data'])['md']), row['report_id']) for row in batch]
                            cur.executemany('UPDATE rl_reports SET md_path=%s WHERE report_id=%s', rows)
                cur.execute('INSERT INTO rl_state(singleton,cursor_secret,schema_version) VALUES(true,%s,%s) ON CONFLICT DO NOTHING', (secrets.token_bytes(32), SCHEMA_VERSION))
                cur.execute('INSERT INTO rl_preferences(singleton,settings) VALUES(true,%s) ON CONFLICT DO NOTHING', (Jsonb(_default_preferences()),))
                self._cursor_secret = bytes(cur.execute('SELECT cursor_secret FROM rl_state WHERE singleton').fetchone()['cursor_secret'])

    @contextmanager
    def _write(self, conn=None):
        if conn is None:
            with self.pool.connection() as owned:
                with self._write(owned) as value:
                    yield value
            return
        previous_factory = conn.row_factory
        conn.row_factory = dict_row
        try:
            with conn.transaction():
                conn.execute('SELECT pg_advisory_xact_lock_shared(%s)', (MAINTENANCE_LOCK_KEY,))
                state = conn.execute('SELECT * FROM rl_state WHERE singleton FOR UPDATE').fetchone()
                if state['maintenance_owner']:
                    raise CatalogError('Library maintenance is in progress.', 503)
                yield conn, state
        finally:
            conn.row_factory = previous_factory

    def _bump(self, conn, catalog=False):
        return conn.execute('UPDATE rl_state SET mutation_generation=mutation_generation+1, generation=generation+%s WHERE singleton RETURNING generation,mutation_generation', (int(catalog),)).fetchone()

    def _guard(self, state, generation):
        if generation is not None and int(generation) != state['generation']:
            raise CatalogConflict()

    def health(self):
        with self.pool.connection() as conn:
            state = conn.execute('SELECT generation,mutation_generation,schema_version,maintenance_owner,library_id FROM rl_state WHERE singleton').fetchone()
            state.update(database='postgresql', searchVersion=SEARCH_VERSION,
                serverVersion=conn.execute('SHOW server_version').fetchone()['server_version'],
                pendingOperations=conn.execute("SELECT count(*) AS n FROM rl_operations WHERE state IN ('pending','running')").fetchone()['n'])
            state['schemaVersion'] = state['schema_version']
            state['mutationGeneration'] = state['mutation_generation']
            state['libraryId'] = state['library_id']
            state['instanceId'] = self._instance_id
            return state

    def get_library_id(self, conn=None):
        if conn is None:
            with self.pool.connection() as owned:
                return self.get_library_id(owned)
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute('SELECT * FROM rl_state WHERE singleton').fetchone()
            return row.get('library_id') if row else None

    def bind_library(self, library_id, initialize_if_empty=True, read_only=False, *, validated_existing=False, conn=None):
        _identifier(library_id, 'library id')
        try:
            uuid.UUID(library_id)
        except (ValueError, TypeError, AttributeError):
            raise CatalogError('Library identity must be a UUID.') from None
        current = self.get_library_id(conn)
        if current is not None:
            if current != library_id:
                raise CatalogConflict('PostgreSQL belongs to a different library.')
            return current
        if read_only or (not initialize_if_empty and not validated_existing):
            raise CatalogConflict('PostgreSQL library identity is missing; validated migration is required.')
        with self._write(conn) as (writer, state):
            if state.get('library_id') is not None:
                if state['library_id'] != library_id:
                    raise CatalogConflict('PostgreSQL belongs to a different library.')
                return state['library_id']
            populated = writer.execute('SELECT (EXISTS(SELECT 1 FROM rl_packages) OR EXISTS(SELECT 1 FROM rl_operations) OR EXISTS(SELECT 1 FROM rl_jobs)) AS present').fetchone()['present']
            if populated and not validated_existing:
                raise CatalogConflict('Populated PostgreSQL requires explicit validated migration binding.')
            writer.execute('UPDATE rl_state SET library_id=%s WHERE singleton', (library_id,))
            self._bump(writer)
            return library_id

    def _report(self, row):
        if not row:
            return None
        record = _unpack(row['record_data'])
        record.update(title=record.get('title', row['title']),
            originalTitle=record.get('originalTitle', row['original_title']),
            category=record.get('category', row['category']), trashedAt=row['trashed_at'],
            trashToken=row['trash_token'], reportGeneration=row['report_generation'],
            metadataUpdatedAt=row['metadata_updated_at'], purgePending=row['purge_pending'])
        if 'generation' in row:
            record['generation'] = row['generation']
        if 'blocked' in row:
            record['blocked'] = row['blocked']
        if 'object_id' in row:
            record['objectId'] = row['object_id']
        return record

    def get_report(self, rid):
        with self.pool.connection() as conn:
            row = conn.execute('SELECT r.*,p.blocked,p.object_id,s.generation FROM rl_reports r JOIN rl_packages p USING(package_id) CROSS JOIN rl_state s WHERE s.singleton AND r.report_id=%s AND NOT r.purge_pending', (rid,)).fetchone()
            return self._report(row)

    def _package(self, conn, package):
        row = conn.execute('SELECT p.*,s.generation FROM rl_packages p CROSS JOIN rl_state s WHERE s.singleton AND package_id=%s', (package,)).fetchone()
        if not row:
            return None
        manifest = _unpack(row['manifest_data'])
        manifest.update(package=package, objectId=row['object_id'], blocked=row['blocked'], generation=row['generation'])
        manifest['reports'] = [self._report(r) for r in conn.execute('SELECT r.*,%s::text AS object_id FROM rl_reports r WHERE package_id=%s ORDER BY report_id', (row['object_id'], package))]
        manifest['files'] = [{'path': r['member_path'], 'sha256': r['sha256'], 'size': r['byte_size']}
            for r in conn.execute('SELECT * FROM rl_members WHERE package_id=%s ORDER BY member_path', (package,))]
        return manifest

    def get_package(self, package):
        with self.pool.connection() as conn:
            return self._package(conn, package)

    def get_package_ref(self, package):
        with self.pool.connection() as conn:
            row = conn.execute('SELECT package_id,object_id,blocked,generation FROM rl_packages CROSS JOIN rl_state WHERE singleton AND package_id=%s', (package,)).fetchone()
            return {'package': row['package_id'], 'objectId': row['object_id'], 'blocked': row['blocked'], 'generation': row['generation']} if row else None

    def get_member(self, package, path):
        _member_path(path)
        with self.pool.connection() as conn:
            row = conn.execute('SELECT member_path,sha256,byte_size FROM rl_members WHERE package_id=%s AND member_path=%s', (package, path)).fetchone()
            return {'path': row['member_path'], 'sha256': row['sha256'], 'size': row['byte_size']} if row else None

    def package_members(self, package):
        manifest = self.get_package(package)
        return manifest['files'] if manifest else []

    def preferences(self):
        with self.pool.connection() as conn:
            return _preferences(conn.execute('SELECT settings FROM rl_preferences WHERE singleton').fetchone()['settings'])

    def update_preferences(self, changes):
        if not isinstance(changes, dict) or not changes or not set(changes).issubset({'theme', 'reading'}):
            raise CatalogError('Settings must contain theme or reading only.')
        if 'theme' in changes and changes['theme'] not in ('light', 'dark'):
            raise CatalogError('Invalid theme.')
        reading = _reading(changes['reading']) if 'reading' in changes else None
        with self._write() as (conn, state):
            result = _preferences(conn.execute('SELECT settings FROM rl_preferences WHERE singleton FOR UPDATE').fetchone()['settings'])
            if 'theme' in changes:
                result['theme'] = changes['theme']
            if reading is not None:
                reading = deepcopy(reading)
                if 'focusRestore' in reading:
                    result['reading']['focusRestore'].update(reading.pop('focusRestore'))
                result['reading'].update(reading)
            conn.execute('UPDATE rl_preferences SET settings=%s WHERE singleton', (Jsonb(result),))
            self._bump(conn)
            return result

    def _chunks(self, text, kind):
        # Split raw codepoints, then casefold. Overlap bounds *folded* query size.
        for ordinal, start in enumerate(range(0, max(1, len(text)), CHUNK_CORE)):
            raw = text[start:start + CHUNK_CORE + CHUNK_OVERLAP]
            yield kind, ordinal, start, raw, raw.casefold()

    def _index_text(self, conn, rid, text, kind, replace=True):
        if not isinstance(text, str):
            raise CatalogError('Search document must be Unicode text.')
        batch = getattr(self._bulk_state, 'batch', None)
        if batch is not None:
            for _, ordinal, start, raw, folded in self._chunks(text, kind):
                grams = {c.encode('utf-8') for c in folded}
                grams.update(folded[i:i + 2].encode('utf-8') for i in range(len(folded) - 1))
                gram_count = self._bulk_state.gram_count
                if batch and (len(batch) >= 32 or gram_count + len(grams) > 25000):
                    self._flush_index_batch(conn, batch)
                    batch.clear()
                    self._bulk_state.gram_count = 0
                batch.append((rid, kind, ordinal, start, raw.encode('utf-8'), folded.encode('utf-8'), _pg_text(folded), '\x00' in folded, grams))
                self._bulk_state.gram_count += len(grams)
            return
        if replace:
            conn.execute('DELETE FROM rl_search_chunks WHERE report_id=%s AND kind=%s', (rid, kind))
        for _, ordinal, start, raw, folded in self._chunks(text, kind):
            row = conn.execute('INSERT INTO rl_search_chunks(report_id,kind,ordinal,original_start,raw_text,folded_text,index_text,has_nul) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING chunk_id',
                (rid, kind, ordinal, start, raw.encode('utf-8'), folded.encode('utf-8'), _pg_text(folded), '\x00' in folded)).fetchone()
            grams = {c.encode('utf-8') for c in folded}
            grams.update(folded[i:i + 2].encode('utf-8') for i in range(len(folded) - 1))
            with conn.cursor().copy('COPY rl_short_postings(gram,chunk_id) FROM STDIN') as copy:
                for gram in sorted(grams):
                    copy.write_row((gram, row['chunk_id']))

    def _flush_index_batch(self, conn, batch):
        # Only new reports use this path. Parent rows already exist in this same
        # atomic transaction; sequence gaps after rollback are harmless.
        ids = [r['chunk_id'] for r in conn.execute("SELECT nextval(pg_get_serial_sequence('rl_search_chunks','chunk_id')) AS chunk_id FROM generate_series(1,%s)", (len(batch),))]
        with conn.cursor().copy('COPY rl_search_chunks(chunk_id,report_id,kind,ordinal,original_start,raw_text,folded_text,index_text,has_nul) FROM STDIN') as copy:
            for chunk_id, chunk in zip(ids, batch):
                copy.write_row((chunk_id, *chunk[:8]))
        # Sorting is bounded by 32 chunks/25k grams, plus at most one full chunk.
        # This groups identical prefix keys and improves B-tree page locality.
        postings = sorted((gram, chunk_id) for chunk_id, chunk in zip(ids, batch) for gram in chunk[8])
        with conn.cursor().copy('COPY rl_short_postings(gram,chunk_id) FROM STDIN') as copy:
            for posting in postings:
                copy.write_row(posting)

    def _metadata_text(self, record):
        return ' '.join(str(record.get(k, '')) for k in ('title', 'originalTitle', 'category', 'date', 'issue'))

    def _insert_report(self, conn, original, overlays, documents, preserve_versions=False):
        record = deepcopy(original)
        rid = _identifier(record.get('id'), 'report id')
        if not re.fullmatch(r'[a-f0-9]{24}-[0-9]+', rid) or not rid.startswith(record.get('package', '')[:24] + '-'):
            raise CatalogError('Report id does not match its package.')
        if not isinstance(record.get('importedAt'), str) or not record['importedAt'] or '\x00' in record['importedAt']:
            raise CatalogError('Invalid report import incarnation.')
        original_title = record.get('originalTitle', record.get('title', ''))
        overlay = overlays.get(rid, {}) if overlays else {}
        record.update(originalTitle=original_title, title=overlay.get('title', record.get('title', original_title)),
            category=overlay.get('category', record.get('category', '')),
            trashedAt=overlay.get('trashedAt', record.get('trashedAt')),
            metadataUpdatedAt=overlay.get('updatedAt', record.get('metadataUpdatedAt')))
        if not all(isinstance(record[k], str) for k in ('title', 'originalTitle', 'category')):
            raise CatalogError('Report title and category must be text.')
        try:
            generation = int(record.get('reportGeneration', record.get('generation', 1)))
        except (TypeError, ValueError):
            raise CatalogError('Invalid report generation.') from None
        if generation < 0:
            raise CatalogError('Invalid report generation.')
        md_path = _member_path(record.get('md'))
        if not preserve_versions:
            record['sameTitleVersion'] = conn.execute('SELECT EXISTS(SELECT 1 FROM rl_reports WHERE md_path=%s AND NOT purge_pending) AS present', (md_path,)).fetchone()['present']
        trash_token = record.get('trashToken') or (uuid.uuid4().hex if record['trashedAt'] else None)
        conn.execute('INSERT INTO rl_reports(report_id,package_id,record_data,title,original_title,category,category_fold,report_date,issue,imported_at,report_generation,trashed_at,trash_token,metadata_updated_at,md_path) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (rid, record['package'], _pack(record), _pg_text(record['title']), _pg_text(original_title), _pg_text(record['category']), record['category'].casefold().encode('utf-8'), str(record.get('date', '')), str(record.get('issue', '')), str(record['importedAt']), generation, record['trashedAt'], trash_token, record['metadataUpdatedAt'], md_path))
        if rid not in documents:
            raise CatalogError('Missing search document for report ' + rid + '.')
        self._index_text(conn, rid, documents[rid], 'body')
        self._index_text(conn, rid, self._metadata_text(record), 'metadata')

    def _members(self, conn, package, members):
        seen = set()
        for member in members or []:
            if isinstance(member, str):
                member = {'path': member}
            path = _member_path(member.get('path', member.get('name')))
            if path.casefold() in seen:
                raise CatalogError('Duplicate member path.')
            seen.add(path.casefold())
            sha = member.get('sha256')
            if sha is not None and not re.fullmatch('[a-f0-9]{64}', sha):
                raise CatalogError('Invalid member hash.')
            size = member.get('size', member.get('byteSize'))
            if size is not None and (type(size) is not int or size < 0):
                raise CatalogError('Invalid member size.')
            conn.execute('INSERT INTO rl_members(package_id,member_path,sha256,byte_size) VALUES(%s,%s,%s,%s)', (package, path, sha, size))

    def _insert_package(self, conn, manifest, documents, members=None, overlays=None, preserve_versions=False):
        previous = getattr(self._bulk_state, 'batch', None)
        previous_count = getattr(self._bulk_state, 'gram_count', 0)
        self._bulk_state.batch = []
        self._bulk_state.gram_count = 0
        try:
            package = self._insert_package_rows(conn, manifest, documents, members, overlays, preserve_versions)
            if self._bulk_state.batch:
                self._flush_index_batch(conn, self._bulk_state.batch)
            return package
        finally:
            self._bulk_state.batch = previous
            self._bulk_state.gram_count = previous_count

    def _insert_package_rows(self, conn, manifest, documents, members=None, overlays=None, preserve_versions=False):
        manifest = deepcopy(manifest)
        reports = manifest.get('reports')
        if not isinstance(reports, list) or not reports:
            raise CatalogError('Package contains no reports.')
        package = manifest.get('package', reports[0].get('package', manifest.get('sha256')))
        if not isinstance(package, str) or not re.fullmatch('[a-f0-9]{64}', package):
            raise CatalogError('Invalid package digest.')
        source = manifest.get('sha256', package)
        content = manifest.get('contentSha256') or None
        if not re.fullmatch('[a-f0-9]{64}', source) or (content and not re.fullmatch('[a-f0-9]{64}', content)):
            raise CatalogError('Invalid package hash.')
        object_id = _identifier(manifest.get('objectId', package), 'object id')
        if not re.fullmatch(r'(?:[a-f0-9]{32}|[a-f0-9]{64})', object_id):
            raise CatalogError('Invalid immutable object id.')
        if preserve_versions:
            # An audited authority migration may contain separate surviving packages
            # with identical current bytes. Their original package identities remain distinct.
            existing = conn.execute('SELECT p.package_id,r.report_id FROM rl_packages p LEFT JOIN rl_reports r USING(package_id) WHERE p.package_id=%s LIMIT 1', (package,)).fetchone()
        else:
            # All normal imports hold the rl_state writer lock. This indexed check
            # serializes duplicate decisions even though survivors may share current hashes.
            existing = conn.execute('SELECT p.package_id,p.blocked,r.report_id,r.purge_pending FROM rl_packages p LEFT JOIN rl_reports r USING(package_id) WHERE p.source_sha=%s OR p.package_id=%s OR (%s::text IS NOT NULL AND p.content_sha=%s) ORDER BY COALESCE(p.blocked OR r.purge_pending,true),r.report_id IS NULL,r.imported_at DESC,r.report_id LIMIT 1', (source, package, content, content)).fetchone()
        if existing:
            if not preserve_versions and (existing['blocked'] or existing['purge_pending'] or existing['report_id'] is None):
                error = CatalogConflict('Matching content has pending permanent cleanup; retry after cleanup completes.')
                error.code = 'duplicate_cleanup_pending'
                raise error
            raise CatalogConflict('This package or its content is already imported.', existing['report_id'])
        if any(r.get('package') != package for r in reports):
            raise CatalogError('Report belongs to a different package.')
        conn.execute('INSERT INTO rl_packages(package_id,source_sha,content_sha,object_id,manifest_data) VALUES(%s,%s,%s,%s,%s)', (package, source, content, object_id, _pack(manifest)))
        self._members(conn, package, members if members is not None else manifest.get('files', []))
        for record in reports:
            self._insert_report(conn, record, overlays, documents, preserve_versions=preserve_versions)
        return package

    def resolve_import_identity(self, manifest):
        # A package id identifies its original admission. A partial purge changes
        # current bytes, so importing the complete original creates a distinct revision.
        candidate = deepcopy(manifest)
        source, content = candidate.get('sha256'), candidate.get('contentSha256')
        package = candidate.get('package')
        if candidate.get('revisionOf') or package != source:
            return candidate
        with self.pool.connection() as conn:
            current = conn.execute('SELECT content_sha FROM rl_packages WHERE package_id=%s', (package,)).fetchone()
        if not current or current['content_sha'] == content:
            return candidate
        object_id = _identifier(candidate.get('objectId'), 'object id')
        revision = hashlib.sha256(b'reference-library-revision-v1\0'+source.encode('ascii')+b'\0'+object_id.encode('ascii')).hexdigest()
        candidate['package'] = revision
        candidate['revisionOf'] = package
        for report in candidate['reports']:
            if report.get('package') != package or not report['id'].startswith(package[:24]+'-'):
                raise CatalogConflict('Import revision report identity differs from its package.')
            report['package'] = revision
            report['id'] = revision[:24]+report['id'][24:]
        return candidate

    def import_package(self, manifest, search_documents, operation_id=None, object_members=None, job_id=None):
        with self._write() as (conn, state):
            if operation_id:
                op = conn.execute('SELECT * FROM rl_operations WHERE operation_id=%s FOR UPDATE', (operation_id,)).fetchone()
                if not op or op['kind'] != 'import':
                    raise CatalogError('Import operation is missing.', 409)
                if op['state'] == 'complete':
                    payload = _unpack(op['payload_data'])
                    package = self._package(conn, payload['package'])
                    return {'reports': package['reports'] if package else [], 'generation': state['generation'], 'idempotent': True}
                if op['state'] not in ('pending', 'running'):
                    raise CatalogConflict('Import operation is not pending.')
                intent = _unpack(op['payload_data'])
                if intent.get('cancelCleanup'):
                    raise CatalogConflict('Import cancellation cleanup must finish before further work.')
                intended = intent.get('manifest')
                if intended and (intended.get('sha256') != manifest.get('sha256') or intended.get('objectId') != manifest.get('objectId')):
                    raise CatalogConflict('Import operation object identity changed.')
            package = self._insert_package(conn, manifest, search_documents, object_members)
            if operation_id:
                payload = {'package': package, 'objectId': manifest.get('objectId'), 'jobId': op['job_id']}
                conn.execute('UPDATE rl_jobs SET package_id=%s WHERE job_id=%s', (package, op['job_id']))
                conn.execute("UPDATE rl_operations SET state='complete',payload_data=%s,package_id=%s,updated_at=now() WHERE operation_id=%s", (_pack(payload), package, operation_id))
            result = self._bump(conn, True)
            result.update(reports=self._package(conn, package)['reports'], message='Reports imported.')
            return result

    def migration_import(self, manifests, search_documents, overlays=None, preferences=None, *, operation_records=None, jobs=None, generation=None, mutation_generation=None):
        with self._write() as (conn, state):
            if conn.execute('SELECT EXISTS(SELECT 1 FROM rl_packages) AS present').fetchone()['present']:
                raise CatalogConflict('Migration target must be empty.')
            for manifest in manifests:
                self._insert_package(conn, manifest, search_documents, overlays=overlays, preserve_versions=True)
            if preferences is not None:
                conn.execute('UPDATE rl_preferences SET settings=%s WHERE singleton', (Jsonb(_preferences(preferences)),))
            for op in operation_records or []:
                self._put_operation(conn, op.get('id', op.get('operation_id')), op['kind'], op.get('payload', {}), op.get('jobId'), op.get('state', 'pending'))
            for job in jobs or []:
                self._put_job(conn, job.get('id', job.get('job_id')), job['kind'], job.get('payload', {}), job)
            result = self._bump(conn, True)
            if generation is not None or mutation_generation is not None:
                conn.execute('UPDATE rl_state SET generation=%s,mutation_generation=%s WHERE singleton', (int(generation) if generation is not None else result['generation'], int(mutation_generation) if mutation_generation is not None else result['mutation_generation']))
                result = conn.execute('SELECT generation,mutation_generation FROM rl_state WHERE singleton').fetchone()
            result.update(packages=len(manifests), reports=len(search_documents))
            return result

    def _locked_report(self, conn, rid):
        row = conn.execute('SELECT r.*,p.blocked,p.object_id FROM rl_reports r JOIN rl_packages p USING(package_id) WHERE r.report_id=%s FOR UPDATE OF r,p', (rid,)).fetchone()
        if not row or row['purge_pending']:
            raise CatalogError('Report does not exist.', 404)
        if row['blocked']:
            raise CatalogError('Package cleanup is pending.', 503)
        return row

    def update_metadata(self, rid, changes, generation=None):
        if not isinstance(changes, dict) or not changes or not set(changes).issubset({'title', 'category'}):
            raise CatalogError('Only report title and category may be changed.')
        cleaned = {}
        for key, limit in (('title', 200), ('category', 80)):
            if key not in changes:
                continue
            value = changes[key]
            if not isinstance(value, str):
                raise CatalogError('Title and category must be text.')
            value = value.strip()
            if (key == 'title' and not value) or len(value) > limit or any(ord(c) < 32 for c in value):
                raise CatalogError('Title or category is empty, too long or contains control characters.')
            cleaned[key] = value
        with self._write() as (conn, state):
            self._guard(state, generation)
            row = self._locked_report(conn, rid)
            record = self._report(row)
            record.update(cleaned)
            record['metadataUpdatedAt'] = _now()
            conn.execute('UPDATE rl_reports SET record_data=%s,title=%s,category=%s,category_fold=%s,metadata_updated_at=%s,report_generation=report_generation+1 WHERE report_id=%s',
                (_pack(record), _pg_text(record['title']), _pg_text(record['category']), record['category'].casefold().encode('utf-8'), record['metadataUpdatedAt'], rid))
            self._index_text(conn, rid, self._metadata_text(record), 'metadata')
            result = self._bump(conn, True)
            result['report'] = self._report(conn.execute('SELECT * FROM rl_reports WHERE report_id=%s', (rid,)).fetchone())
            return result

    def set_trash(self, rid, restore=False, generation=None):
        if type(restore) is not bool:
            raise CatalogError('Restore must be boolean.')
        with self._write() as (conn, state):
            self._guard(state, generation)
            row = self._locked_report(conn, rid)
            trashed_at = None if restore else (row['trashed_at'] or _now())
            token = None if restore else (row['trash_token'] or uuid.uuid4().hex)
            if trashed_at == row['trashed_at'] and token == row['trash_token']:
                return {'report': self._report(row), 'generation': state['generation'], 'idempotent': True}
            conn.execute('UPDATE rl_reports SET trashed_at=%s,trash_token=%s,metadata_updated_at=%s,report_generation=report_generation+1 WHERE report_id=%s', (trashed_at, token, _now(), rid))
            result = self._bump(conn, True)
            result.update(report=self._report(conn.execute('SELECT * FROM rl_reports WHERE report_id=%s', (rid,)).fetchone()), message='Report restored.' if restore else 'Report moved to trash.')
            return result

    def _operation(self, row):
        if not row:
            return None
        return {'id': row['operation_id'], 'opId': row['operation_id'], 'operationId': row['operation_id'],
            'kind': row['kind'], 'state': row['state'], 'payload': _unpack(row['payload_data']),
            'jobId': row['job_id'], 'createdAt': row['created_at'].isoformat(), 'updatedAt': row['updated_at'].isoformat()}

    def _put_operation(self, conn, operation_id, kind, payload, job_id=None, state='pending'):
        operation_id = _identifier(operation_id or uuid.uuid4().hex, 'operation id')
        if state not in ('pending', 'running', 'complete', 'failed', 'cancelled'):
            raise CatalogError('Invalid operation state.')
        row = conn.execute('INSERT INTO rl_operations(operation_id,kind,payload_data,job_id,state,report_id,package_id,imported_at,trash_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *', (operation_id, _identifier(kind, 'operation kind'), _pack(payload), job_id, state, payload.get('reportId'), _payload_package(payload), payload.get('importedAt'), payload.get('trashedAt'))).fetchone()
        if job_id and _payload_package(payload):
            conn.execute('UPDATE rl_jobs SET package_id=%s WHERE job_id=%s', (_payload_package(payload), job_id))
        return self._operation(row)

    def begin_operation(self, kind, payload, job_id=None):
        if not isinstance(payload, dict):
            raise CatalogError('Operation payload must be an object.')
        with self._write() as (conn, state):
            if job_id:
                existing = conn.execute('SELECT * FROM rl_operations WHERE job_id=%s AND kind=%s ORDER BY created_at LIMIT 1', (job_id, kind)).fetchone()
                if existing:
                    return self._operation(existing)
            result = self._put_operation(conn, None, kind, payload, job_id)
            self._bump(conn)
            return result

    def begin_import(self, job_id, payload):
        return self.begin_operation('import', payload, job_id)

    def get_import_operation(self, job_id):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM rl_operations WHERE job_id=%s AND kind='import' ORDER BY created_at,operation_id LIMIT 1", (job_id,)).fetchone()
            return self._operation(row)

    def _locked_import_pair(self, conn, job_id, operation_id, expected_identity=None):
        # The global authority lock and both row locks make worker/cancel claims
        # one compare-and-set. A caller never receives another job's stage.
        job = conn.execute('SELECT * FROM rl_jobs WHERE job_id=%s FOR UPDATE', (job_id,)).fetchone()
        op = conn.execute('SELECT * FROM rl_operations WHERE operation_id=%s FOR UPDATE', (operation_id,)).fetchone()
        if not job or not op:
            raise CatalogError('Import job or operation does not exist.', 404)
        if job['kind'] != 'import' or op['kind'] != 'import' or op['job_id'] != job_id:
            raise CatalogConflict('Import job and operation identity do not match.')
        payload, job_payload = _unpack(op['payload_data']), _unpack(job['payload_data'])
        for source in (payload, job_payload):
            if source.get('jobId', job_id) != job_id:
                raise CatalogConflict('Import intent job identity changed.')
        for key in ('stageId', 'objectId'):
            if key in job_payload and key in payload and job_payload[key] != payload[key]:
                raise CatalogConflict('Import stage or object identity changed.')
        if op['state'] in ('pending', 'running'):
            for key in ('stageId', 'objectId'):
                _identifier(payload.get(key), 'import ' + key)
                if job_payload.get(key) != payload[key]:
                    raise CatalogConflict('Import job is missing its stage or object identity.')
            manifest = payload.get('manifest')
            if isinstance(manifest, dict) and manifest.get('objectId', payload['objectId']) != payload['objectId']:
                raise CatalogConflict('Import manifest object identity changed.')
        if expected_identity is not None:
            if not isinstance(expected_identity, dict) or not set(expected_identity).issubset({'stageId', 'objectId'}):
                raise CatalogError('Invalid expected import identity.')
            if any(payload.get(key) != value for key, value in expected_identity.items()):
                raise CatalogConflict('Expected import stage or object identity changed.')
        return job, op, payload

    def _import_claim_result(self, job, op, claimed):
        return {'job': self._job(job), 'operation': self._operation(op), 'claimed': claimed}

    def claim_import(self, job_id, operation_id, *, recover=False, expected_identity=None):
        if type(recover) is not bool:
            raise CatalogError('Import recovery flag must be boolean.')
        with self._write() as (conn, state):
            job, op, payload = self._locked_import_pair(conn, job_id, operation_id, expected_identity)
            initial = job['state'] == 'queued' and job['phase'] == 'queued' and op['state'] == 'pending'
            # recover=True is only for the startup owner of the exclusive app
            # lease; normal workers cannot claim an already running import.
            recovering = recover and job['state'] in ('running', 'failed') and op['state'] in ('pending', 'running')
            if job['cancel_requested'] or payload.get('cancelCleanup') or not (initial or recovering):
                return self._import_claim_result(job, op, False)
            job = conn.execute("UPDATE rl_jobs SET state='running',phase='validating',error=NULL,error_data=NULL,updated_at=now() WHERE job_id=%s RETURNING *", (job_id,)).fetchone()
            op = conn.execute("UPDATE rl_operations SET state='running',updated_at=now() WHERE operation_id=%s RETURNING *", (operation_id,)).fetchone()
            self._bump(conn)
            return self._import_claim_result(job, op, True)

    def cancel_queued_import(self, job_id, operation_id, *, expected_identity=None):
        with self._write() as (conn, state):
            job, op, payload = self._locked_import_pair(conn, job_id, operation_id, expected_identity)
            if job['phase'] == 'committing' and job['state'] not in ('complete', 'completed', 'succeeded', 'failed', 'cancelled'):
                raise CatalogConflict('Import catalog commit has started; wait for completion.')
            if job['state'] != 'queued' or job['phase'] != 'queued' or op['state'] != 'pending' or payload.get('cancelCleanup'):
                return self._import_claim_result(job, op, False)
            # This is a durable cleanup claim, not terminal cancellation. The
            # filesystem owner must finish its deletion barrier before finish.
            payload['cancelCleanup'] = True
            job = conn.execute("UPDATE rl_jobs SET state='running',phase='cancellation_cleanup',cancel_requested=true,error=NULL,error_data=NULL,updated_at=now() WHERE job_id=%s RETURNING *", (job_id,)).fetchone()
            op = conn.execute("UPDATE rl_operations SET state='running',payload_data=%s,updated_at=now() WHERE operation_id=%s RETURNING *", (_pack(payload), operation_id)).fetchone()
            self._bump(conn)
            return self._import_claim_result(job, op, True)

    def finish_cancelled_import(self, job_id, operation_id, *, expected_identity=None):
        with self._write() as (conn, state):
            job, op, payload = self._locked_import_pair(conn, job_id, operation_id, expected_identity)
            if job['state'] == 'cancelled' and op['state'] == 'cancelled':
                return self._import_claim_result(job, op, False)
            cleaning = job['state'] == 'running' and job['phase'] == 'cancellation_cleanup'
            retrying = job['state'] == 'failed' and job['phase'] == 'recovery_pending'
            if op['state'] != 'running' or payload.get('cancelCleanup') is not True or not (cleaning or retrying):
                raise CatalogConflict('Import cancellation cleanup intent is missing or changed.')
            result = {'cancelled': True, 'cleanupPending': False}
            # Preserve the stage/object identity for safe idempotent retries;
            # discarded report metadata does not belong in a cancelled journal.
            minimal = {key: payload[key] for key in ('stageId', 'objectId', 'jobId', 'package') if key in payload}
            minimal.update(cancelCleanup=True, cleanupComplete=True)
            job = conn.execute("UPDATE rl_jobs SET state='cancelled',phase='cancelled',cancel_requested=true,message='Import cancelled.',error=NULL,error_data=NULL,result_data=%s,updated_at=now() WHERE job_id=%s RETURNING *", (_pack(result), job_id)).fetchone()
            op = conn.execute("UPDATE rl_operations SET state='cancelled',payload_data=%s,updated_at=now() WHERE operation_id=%s RETURNING *", (_pack(minimal), operation_id)).fetchone()
            self._bump(conn)
            return self._import_claim_result(job, op, True)

    def finalize_import(self, job_id, manifest, search_documents, object_members=None):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT operation_id FROM rl_operations WHERE job_id=%s AND kind='import' ORDER BY created_at LIMIT 1", (job_id,)).fetchone()
        if not row:
            raise CatalogError('Import operation is missing.', 409)
        return self.import_package(manifest, search_documents, row['operation_id'], object_members)

    def update_operation(self, operation_id, payload=None, state=None):
        if payload is not None and not isinstance(payload, dict):
            raise CatalogError('Operation checkpoint must be an object.')
        if state is not None and state not in ('pending', 'running', 'complete', 'failed', 'cancelled'):
            raise CatalogError('Invalid operation state.')
        with self._write() as (conn, catalog_state):
            row = conn.execute('SELECT * FROM rl_operations WHERE operation_id=%s FOR UPDATE', (operation_id,)).fetchone()
            if not row:
                raise CatalogError('Operation does not exist.', 404)
            old = _unpack(row['payload_data'])
            for key in ('reportId', 'package', 'importedAt', 'reportGeneration', 'trashedAt', 'oldObjectId', 'stageId', 'objectId'):
                if payload and key in old and key in payload and payload[key] != old[key]:
                    raise CatalogConflict('Operation identity cannot be changed.')
            if row['kind'] == 'import' and old.get('cancelCleanup') and payload and 'cancelCleanup' in payload and payload['cancelCleanup'] is not True:
                raise CatalogConflict('Import cancellation cleanup intent cannot be removed.')
            old.update(payload or {})
            # A committed purge cannot be cancelled: original cleanup must resume.
            if row['kind'] == 'purge' and state in ('cancelled', 'failed', 'complete'):
                raise CatalogConflict('Committed purge must complete through complete_purge().')
            if row['state'] in ('complete', 'failed', 'cancelled') and state not in (None, row['state']):
                raise CatalogConflict('A terminal operation cannot be resumed.')
            package_id = _payload_package(old) or row['package_id']
            if package_id and row['job_id']:
                conn.execute('UPDATE rl_jobs SET package_id=%s WHERE job_id=%s', (package_id,row['job_id']))
            if row['kind'] == 'import' and (state or row['state']) in ('failed','cancelled'):
                # Terminal abandoned imports own no originals. Do not retain a
                # duplicate report metadata projection after their cleanup.
                old = {k:old[k] for k in ('stageId','objectId','jobId','package','cancelCleanup') if k in old}
                if package_id: old['package'] = package_id
            updated = conn.execute('UPDATE rl_operations SET payload_data=%s,state=%s,package_id=%s,updated_at=now() WHERE operation_id=%s RETURNING *', (_pack(old), state or row['state'], package_id, operation_id)).fetchone()
            self._bump(conn)
            return self._operation(updated)

    def complete_operation(self, operation_id):
        return self.update_operation(operation_id, state='complete')

    def pending_operations(self, kind=None, *, limit=None, after=None, through=None, reverse=False):
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 32):
            raise CatalogError('Pending operation page must be between 1 and 32.')
        if type(reverse) is not bool:
            raise CatalogError('Invalid operation ordering.')
        clauses=["state IN ('pending','running')", "(%s::text IS NULL OR kind=%s)"]
        params=[kind,kind]
        for marker,operator in ((after,">"),(through,"<=")):
            if marker is not None:
                if not isinstance(marker,(tuple,list)) or len(marker)!=2 or not isinstance(marker[0],str) or len(marker[0])>64:
                    raise CatalogError('Invalid recovery operation boundary.')
                operation_id=_identifier(marker[1],'operation id')
                clauses.append("(created_at,operation_id) "+operator+" (%s::timestamptz,%s)")
                params.extend((marker[0],operation_id))
        direction=" DESC" if reverse else " ASC"
        query="SELECT * FROM rl_operations WHERE "+" AND ".join(clauses)+" ORDER BY created_at"+direction+",operation_id"+direction
        if limit is not None:
            query+=" LIMIT %s";params.append(limit)
        with self.pool.connection() as conn:
            return [self._operation(r) for r in conn.execute(query,params)]

    def pending_purge(self, rid, trashed_at):
        with self.pool.connection() as conn:
            row=conn.execute("SELECT * FROM rl_operations WHERE kind='purge' AND report_id=%s AND trash_at=%s AND state IN ('pending','running') ORDER BY created_at DESC,operation_id DESC LIMIT 1",(rid,trashed_at)).fetchone()
            return self._operation(row)

    def begin_purge(self, rid, changes, payload=None, generation=None):
        if not isinstance(changes, dict) or changes.get('confirm') != rid or not isinstance(changes.get('trashedAt'), str) or not changes['trashedAt']:
            raise CatalogError('Permanent deletion requires the report id and trash timestamp.')
        with self._write() as (conn, state):
            self._guard(state, generation)
            # Retry only this exact imported incarnation/trash token, never a new one.
            for existing in conn.execute("SELECT * FROM rl_operations WHERE kind='purge' AND report_id=%s AND trash_at=%s ORDER BY created_at DESC", (rid, changes['trashedAt'])):
                identity = _unpack(existing['payload_data'])
                if identity.get('reportId') == rid and identity.get('trashedAt') == changes['trashedAt']:
                    current = conn.execute('SELECT imported_at,trash_token FROM rl_reports WHERE report_id=%s', (rid,)).fetchone()
                    if current is None or current['imported_at'] == identity['importedAt']:
                        if changes.get('trashToken') is not None and changes['trashToken'] != identity.get('trashToken'):
                            raise CatalogConflict('Permanent deletion confirmation expired.')
                        return self._operation(existing)
            row = self._locked_report(conn, rid)
            if not row['trashed_at'] or row['trashed_at'] != changes['trashedAt']:
                raise CatalogConflict('Report has been restored or the trash state changed.')
            if changes.get('trashToken') is not None and changes['trashToken'] != row['trash_token']:
                raise CatalogConflict('Permanent deletion confirmation expired.')
            if changes.get('reportGeneration') is not None and int(changes['reportGeneration']) != row['report_generation']:
                raise CatalogConflict('Report generation changed.')
            old_manifest = self._package(conn, row['package_id'])
            identity = dict(payload or {})
            identity.update(reportId=rid, package=row['package_id'], importedAt=row['imported_at'],
                reportGeneration=row['report_generation'], trashedAt=row['trashed_at'],
                trashToken=row['trash_token'], oldObjectId=row['object_id'], oldManifest=old_manifest)
            result = self._put_operation(conn, None, 'purge', identity, identity.get('jobId'))
            conn.execute('UPDATE rl_reports SET purge_pending=true WHERE report_id=%s', (rid,))
            conn.execute('UPDATE rl_packages SET blocked=true WHERE package_id=%s', (row['package_id'],))
            self._bump(conn, True)
            result.update(identity)
            return result

    def complete_purge(self, operation_id, surviving_manifest=None, object_members=None):
        with self._write() as (conn, state):
            op = conn.execute('SELECT * FROM rl_operations WHERE operation_id=%s FOR UPDATE', (operation_id,)).fetchone()
            if not op or op['kind'] != 'purge':
                raise CatalogError('Purge operation does not exist.', 404)
            identity = _unpack(op['payload_data'])
            if op['state'] == 'complete':
                return {'id': identity['reportId'], 'deleted': True, 'cleanupPending': False, 'generation': state['generation'], 'idempotent': True}
            if op['state'] not in ('pending', 'running'):
                raise CatalogConflict('Purge operation is not pending.')
            package = identity['package']
            row = conn.execute('SELECT * FROM rl_reports WHERE report_id=%s FOR UPDATE', (identity['reportId'],)).fetchone()
            if not row or row['imported_at'] != identity['importedAt'] or row['report_generation'] != identity['reportGeneration'] or not row['purge_pending']:
                raise CatalogConflict('Purge report incarnation changed.')
            current = conn.execute('SELECT * FROM rl_packages WHERE package_id=%s FOR UPDATE', (package,)).fetchone()
            if current['object_id'] != identity['oldObjectId']:
                raise CatalogConflict('Purge original object changed.')
            survivors = list(conn.execute('SELECT report_id,imported_at FROM rl_reports WHERE package_id=%s AND report_id<>%s', (package, identity['reportId'])))
            if survivors:
                if not surviving_manifest:
                    raise CatalogError('Surviving package manifest is required.', 409)
                supplied = surviving_manifest.get('reports', [])
                if {(r['id'], r['importedAt']) for r in supplied} != {(r['report_id'], r['imported_at']) for r in survivors}:
                    raise CatalogConflict('Surviving package reports changed.')
                if surviving_manifest.get('package', package) != package:
                    raise CatalogConflict('Surviving package identity changed.')
                object_id = _identifier(surviving_manifest.get('objectId'), 'object id')
                if not re.fullmatch(r'(?:[a-f0-9]{32}|[a-f0-9]{64})', object_id) or not re.fullmatch(r'[a-f0-9]{64}', surviving_manifest.get('sha256', '')):
                    raise CatalogError('Invalid surviving object or source digest.')
                if surviving_manifest.get('contentSha256') is not None and not re.fullmatch(r'[a-f0-9]{64}', surviving_manifest['contentSha256']):
                    raise CatalogError('Invalid surviving content digest.')
                old_reports = {r['id']: r for r in identity['oldManifest']['reports']}
                if any(any(r.get(k) != old_reports[r['id']].get(k) for k in ('md', 'html', 'attachments')) for r in supplied):
                    raise CatalogConflict('Surviving report member paths changed.')
                if object_id == identity['oldObjectId']:
                    raise CatalogConflict('Purge survivors must use a new object.')
                conn.execute('UPDATE rl_packages SET source_sha=%s,content_sha=%s,object_id=%s,manifest_data=%s,blocked=false WHERE package_id=%s',
                    (surviving_manifest['sha256'], surviving_manifest.get('contentSha256'), object_id, _pack(surviving_manifest), package))
                conn.execute('DELETE FROM rl_members WHERE package_id=%s', (package,))
                self._members(conn, package, object_members if object_members is not None else surviving_manifest.get('files', []))
                conn.execute('DELETE FROM rl_reports WHERE report_id=%s', (identity['reportId'],))
            else:
                if surviving_manifest is not None:
                    raise CatalogConflict('Whole-package purge must not supply surviving reports.')
                conn.execute('DELETE FROM rl_packages WHERE package_id=%s', (package,))
            # Remove metadata copies from historical import jobs and completed journals.
            for job in conn.execute('SELECT * FROM rl_jobs WHERE package_id=%s', (package,)):
                job_result = _unpack(job['result_data']) if job['result_data'] else {}
                if isinstance(job_result, dict):
                    ids = [r.get('id') for r in job_result.get('reports', []) if isinstance(r, dict) and r.get('id') != identity['reportId']]
                    job_result = {'reportIds': ids, 'importedCount': len(job_result.get('reports', [])), 'generation': job_result.get('generation')}
                else:
                    job_result = {}
                conn.execute('UPDATE rl_jobs SET payload_data=%s,result_data=%s WHERE job_id=%s', (_pack({'package': package}), _pack(job_result), job['job_id']))
            for previous in conn.execute("SELECT * FROM rl_operations WHERE package_id=%s AND operation_id<>%s AND state IN ('complete','failed','cancelled')", (package, operation_id)):
                minimal = {'package': package, 'jobId': previous['job_id']}
                if previous['report_id']:
                    minimal.update(reportId=previous['report_id'], importedAt=previous['imported_at'], trashedAt=previous['trash_at'])
                conn.execute('UPDATE rl_operations SET payload_data=%s WHERE operation_id=%s', (_pack(minimal), previous['operation_id']))
            # Sensitive deleted title/body/manifest must not survive in journal.
            completed = {k: identity[k] for k in ('reportId', 'package', 'importedAt', 'reportGeneration', 'trashedAt', 'trashToken', 'oldObjectId')}
            completed['completedAt'] = _now()
            conn.execute("UPDATE rl_operations SET state='complete',payload_data=%s,updated_at=now() WHERE operation_id=%s", (_pack(completed), operation_id))
            result = self._bump(conn, True)
            result.update(id=identity['reportId'], deleted=True, cleanupPending=False)
            return result

    def _job(self, row):
        if not row:
            return None
        return {'id': row['job_id'], 'jobId': row['job_id'], 'kind': row['kind'], 'state': row['state'],
            'phase': row['phase'], 'progress': row['progress'], 'message': row['message'],
            'error': _unpack(row['error_data']) if row['error_data'] else row['error'],
            'result': _unpack(row['result_data']) if row['result_data'] else None,
            'cancelRequested': row['cancel_requested'], 'package': row['package_id'], 'payload': _unpack(row['payload_data']),
            'createdAt': row['created_at'].isoformat(), 'updatedAt': row['updated_at'].isoformat()}

    def _put_job(self, conn, job_id, kind, payload, fields=None):
        fields = fields or {}
        job_id = _identifier(job_id or uuid.uuid4().hex, 'job id')
        row = conn.execute('INSERT INTO rl_jobs(job_id,kind,payload_data,state,phase,progress,message,cancel_requested,error_data,result_data,package_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',
            (job_id, kind, _pack(payload), fields.get('state', 'queued'), fields.get('phase', 'queued'),
                fields.get('progress', 0), fields.get('message', ''), fields.get('cancelRequested', False),
                _pack(fields['error']) if fields.get('error') is not None else None,
                _pack(fields['result']) if fields.get('result') is not None else None, fields.get('package') or _payload_package(payload))).fetchone()
        return self._job(row)

    def create_job(self, kind, payload=None, job_id=None):
        with self._write() as (conn, state):
            if job_id:
                old = conn.execute('SELECT * FROM rl_jobs WHERE job_id=%s', (job_id,)).fetchone()
                if old:
                    if old['kind'] != kind or _unpack(old['payload_data']) != (payload or {}):
                        raise CatalogConflict('Job id already belongs to another request.')
                    return self._job(old)
            result = self._put_job(conn, job_id, _identifier(kind, 'job kind'), payload or {})
            self._bump(conn)
            return result

    def job_start(self, job_id, kind, payload=None):
        job = self.create_job(kind, payload, job_id)
        return self.update_job(job['id'], state='running')

    def get_job(self, job_id):
        with self.pool.connection() as conn:
            return self._job(conn.execute('SELECT * FROM rl_jobs WHERE job_id=%s', (job_id,)).fetchone())

    def update_job(self, job_id, state=None, phase=None, progress=None, message=None, error=None, result=None):
        if progress is not None and (isinstance(progress, bool) or not 0 <= float(progress) <= 1):
            raise CatalogError('Job progress must be between zero and one.')
        with self._write() as (conn, catalog_state):
            row = conn.execute('SELECT * FROM rl_jobs WHERE job_id=%s FOR UPDATE', (job_id,)).fetchone()
            if not row:
                raise CatalogError('Job does not exist.', 404)
            if row['kind'] == 'import' and phase == 'committing' and row['cancel_requested'] and row['phase'] != 'committing':
                cancelled = CatalogConflict('Import was cancelled before its catalog commit started.')
                cancelled.code = 'cancelled'
                raise cancelled
            if row['state'] in ('complete', 'completed', 'succeeded', 'failed', 'cancelled') and state not in (None, row['state']):
                recovering = row['state'] == 'failed' and state == 'running' and conn.execute("SELECT EXISTS(SELECT 1 FROM rl_operations WHERE job_id=%s AND state IN ('pending','running')) AS present", (job_id,)).fetchone()['present']
                if not recovering:
                    raise CatalogConflict('A terminal job cannot be resumed.')
            row = conn.execute('UPDATE rl_jobs SET state=%s,phase=%s,progress=%s,message=%s,error_data=%s,result_data=%s,updated_at=now() WHERE job_id=%s RETURNING *',
                (state or row['state'], phase or row['phase'], float(progress) if progress is not None else row['progress'],
                    _pg_text(message) if message is not None else row['message'],
                    _pack(error) if error is not None else (None if state in ('running', 'succeeded', 'complete', 'completed') else row['error_data']),
                    _pack(result) if result is not None else row['result_data'], job_id)).fetchone()
            self._bump(conn)
            return self._job(row)

    def request_cancel(self, job_id):
        with self._write() as (conn, state):
            row = conn.execute('SELECT * FROM rl_jobs WHERE job_id=%s FOR UPDATE', (job_id,)).fetchone()
            if not row:
                raise CatalogError('Job does not exist.', 404)
            if row['kind'] == 'purge':
                raise CatalogConflict('Committed purge cannot be cancelled.')
            if row['state'] not in ('complete', 'completed', 'succeeded', 'failed', 'cancelled'):
                if row['phase'] == 'committing':
                    raise CatalogConflict('Import catalog commit has started; wait for completion.')
                if not row['cancel_requested']:
                    row = conn.execute('UPDATE rl_jobs SET cancel_requested=true,updated_at=now() WHERE job_id=%s RETURNING *', (job_id,)).fetchone()
                    self._bump(conn)
            return self._job(row)

    def _search_cte(self, query):
        folded = query.casefold()
        if not folded:
            return '', [], 'none'
        needle = folded.encode('utf-8')
        if len(folded) <= 2:
            # Every posting is an actual consecutive folded-codepoint gram in
            # one body/metadata chunk. It is already an exact substring proof.
            return ('WITH matched AS MATERIALIZED (SELECT DISTINCT c.report_id FROM rl_short_postings p JOIN rl_search_chunks c USING(chunk_id) WHERE p.gram=%s) ', [needle], 'short-postings-exact')
        # Guaranteed ASCII trigram extraction. Chinese and punctuation use exact
        # Unicode 1/2-codepoint postings, avoiding pg_trgm's no-trigram scan.
        if '\x00' not in folded and re.search(r'[a-z0-9]{3}', folded):
            pattern = '%' + folded.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            return ("WITH matched AS MATERIALIZED (SELECT DISTINCT c.report_id FROM rl_search_chunks c WHERE c.index_text LIKE %s ESCAPE E'\\\\' AND position(%s::bytea in c.folded_text)>0) ", [pattern, needle], 'pg_trgm+bytea')
        gram = folded[:min(2, len(folded))].encode('utf-8')
        return ('WITH matched AS MATERIALIZED (SELECT DISTINCT c.report_id FROM rl_short_postings p JOIN rl_search_chunks c USING(chunk_id) WHERE p.gram=%s AND position(%s::bytea in c.folded_text)>0) ', [gram, needle], 'short-postings+bytea')

    def _cursor(self, keys, query, category, trash, generation):
        data = _pack({'keys': keys, 'query': query, 'category': category, 'trash': trash, 'generation': generation})
        signature = hmac.digest(self._cursor_secret, data, 'sha256')
        return base64.urlsafe_b64encode(data + signature).rstrip(b'=').decode('ascii')

    def _read_cursor(self, cursor, query, category, trash, generation):
        try:
            if not isinstance(cursor, str) or len(cursor) > 8000:
                raise ValueError('invalid cursor')
            encoded = base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4))
            data, signature = encoded[:-32], encoded[-32:]
            if not hmac.compare_digest(signature, hmac.digest(self._cursor_secret, data, 'sha256')):
                raise ValueError('invalid cursor')
            value = _unpack(data)
            if any(value[k] != v for k, v in (('query', query), ('category', category), ('trash', trash))):
                raise ValueError('cursor filters changed')
            if value['generation'] != generation:
                raise CatalogConflict()
            if not isinstance(value['keys'], list) or len(value['keys']) != 4 or not all(isinstance(x, str) for x in value['keys']):
                raise ValueError('invalid cursor')
            return value['keys']
        except CatalogConflict:
            raise
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise CatalogError('Invalid pagination cursor.', 400) from None

    def _snippet(self, raw, query):
        text = bytes(raw).decode('utf-8')
        folded = text.casefold()
        pos = folded.find(query.casefold())
        if pos < 0:
            return '', 0
        # Casefold expansion changes offsets (e.g. sharp-s, dotted capital I).
        seen = 0
        original_pos = 0
        for i, char in enumerate(text):
            width = len(char.casefold())
            if seen + width > pos:
                original_pos = i
                break
            seen += width
        return re.sub(r'\s+', ' ', text[max(0, original_pos - 40):original_pos + 120]), original_pos

    def _listing_summary(self, conn, generation):
        # Every report/category/trash mutation advances catalog generation.
        # A summary is published only for that exact repeatable-read snapshot;
        # an older in-flight reader cannot replace a newer published generation.
        with self._list_cache_lock:
            if generation == self._list_generation and self._list_summary is not None:
                return self._list_summary
            if generation > self._list_generation:
                self._list_generation = generation
                self._list_summary = None
                self._list_counts.clear()
        groups = list(conn.execute(
            'SELECT category,category_fold,count(*) FILTER(WHERE trashed_at IS NULL) AS active_count,'
            'count(*) FILTER(WHERE trashed_at IS NOT NULL) AS trash_count '
            'FROM rl_reports WHERE NOT purge_pending GROUP BY category,category_fold'))
        total = trash_total = 0
        categories = set()
        category_counts = {}
        for row in groups:
            active, deleted = row['active_count'], row['trash_count']
            total += active
            trash_total += deleted
            folded = bytes(row['category_fold'])
            previous = category_counts.get(folded, (0, 0))
            category_counts[folded] = (previous[0] + active, previous[1] + deleted)
            if active and row['category']:
                categories.add(row['category'])
        summary = ({'total': total, 'trash_total': trash_total},
                   tuple(sorted(categories, key=lambda value: (value.casefold(), value))), category_counts)
        with self._list_cache_lock:
            if generation == self._list_generation:
                self._list_summary = summary
        return summary

    def _cached_count(self, generation, query, category, trash):
        key = (query.casefold(), category.casefold(), trash)
        with self._list_cache_lock:
            if generation != self._list_generation or key not in self._list_counts:
                return None
            value = self._list_counts[key]
            self._list_counts.move_to_end(key)
            return value

    def _remember_count(self, generation, query, category, trash, value):
        key = (query.casefold(), category.casefold(), trash)
        with self._list_cache_lock:
            if generation != self._list_generation:
                return
            self._list_counts[key] = value
            self._list_counts.move_to_end(key)
            while len(self._list_counts) > 128:
                self._list_counts.popitem(last=False)

    def _ordered_page_match(self, query):
        # For a broad result, test only ordered candidate reports. LIMIT inside
        # the correlated scalar subquery prevents an all-library materialization.
        # These are exactly the predicates used by _search_cte, over both kinds.
        folded = query.casefold()
        needle = folded.encode('utf-8')
        if len(folded) <= 2:
            return (
                '(SELECT true FROM rl_search_chunks c JOIN rl_short_postings sp USING(chunk_id) '
                'WHERE c.report_id=r.report_id AND sp.gram=%s LIMIT 1) IS TRUE', [needle])
        if '\x00' not in folded and re.search(r'[a-z0-9]{3}', folded):
            pattern = '%' + folded.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            return (
                "(SELECT true FROM rl_search_chunks c WHERE c.report_id=r.report_id "
                "AND c.index_text LIKE %s ESCAPE E'\\\\' AND position(%s::bytea in c.folded_text)>0 "
                "LIMIT 1) IS TRUE", [pattern, needle])
        gram = folded[:min(2, len(folded))].encode('utf-8')
        return (
            '(SELECT true FROM rl_search_chunks c JOIN rl_short_postings sp USING(chunk_id) '
            'WHERE c.report_id=r.report_id AND sp.gram=%s AND position(%s::bytea in c.folded_text)>0 '
            'LIMIT 1) IS TRUE', [gram, needle])

    def list_reports(self, query='', category='', trash=False, offset=0, limit=50, cursor=None, generation=None):
        started = time.perf_counter()
        if not isinstance(query, str) or len(query) > MAX_QUERY:
            raise CatalogError('Search query exceeds 200 characters.')
        if not isinstance(category, str) or len(category) > 80 or type(trash) is not bool:
            raise CatalogError('Invalid category or trash filter.')
        try:
            offset, limit = int(offset), int(limit)
        except (TypeError, ValueError):
            raise CatalogError('Invalid pagination size.') from None
        if not 1 <= limit <= 200 or not 0 <= offset <= MAX_LEGACY_OFFSET or (cursor and offset):
            raise CatalogError('Use a continuation cursor for deep pagination.')
        cte, search_params, route = self._search_cte(query)
        matched = ' JOIN matched m ON m.report_id=r.report_id ' if query else ' '
        where = ' WHERE NOT r.purge_pending AND (r.trashed_at IS NOT NULL)=%s'
        filter_params = [trash]
        if category:
            where += ' AND r.category_fold=%s'
            filter_params.append(category.casefold().encode('utf-8'))
        with self.pool.connection() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            state = conn.execute('SELECT generation FROM rl_state WHERE singleton').fetchone()
            self._guard(state, generation)
            keys = self._read_cursor(cursor, query, category, trash, state['generation']) if cursor else None
            counts, categories, category_counts = self._listing_summary(conn, state['generation'])
            scope_counts = category_counts.get(category.casefold().encode('utf-8'), (0, 0)) if category else (counts['total'], counts['trash_total'])
            scope_total = scope_counts[int(trash)]
            count_start = time.perf_counter()
            filtered_count = scope_total if not query else self._cached_count(state['generation'], query, category, trash)
            if filtered_count is None:
                filtered_count = conn.execute(cte + 'SELECT count(*) AS n FROM rl_reports r' + matched + where, search_params + filter_params, prepare=False).fetchone()['n']
                self._remember_count(state['generation'], query, category, trash, filtered_count)
            count_ms = (time.perf_counter() - count_start) * 1000
            page_where = where
            page_cte, page_matched = cte, matched
            page_params = search_params + filter_params
            page_route = 'materialized-matches' if query else 'ordered-index'
            if query and filtered_count >= max(200, (scope_total + 19) // 20):
                predicate, exact_params = self._ordered_page_match(query)
                page_where += ' AND ' + predicate
                page_cte, page_matched = '', ' '
                page_params = filter_params + exact_params
                page_route = 'ordered-exact-match'
            if keys:
                page_where += ' AND (r.report_date,r.issue,r.imported_at,r.report_id)<(%s,%s,%s,%s)'
                page_params += keys
            page_sql = page_cte + 'SELECT r.*,p.object_id,p.blocked FROM rl_reports r JOIN rl_packages p USING(package_id)' + page_matched + page_where + ' ORDER BY r.purge_pending,(r.trashed_at IS NOT NULL),r.report_date DESC,r.issue DESC,r.imported_at DESC,r.report_id DESC LIMIT %s'
            page_params.append(limit + 1)
            if offset:
                page_sql += ' OFFSET %s'
                page_params.append(offset)
            rows = list(conn.execute(page_sql, page_params, prepare=False))
            has_more = len(rows) > limit
            rows = rows[:limit]
            reports = [self._report(r) for r in rows]
            if query and reports:
                chunks = conn.execute("SELECT DISTINCT ON(report_id) report_id,ordinal,original_start,raw_text FROM rl_search_chunks WHERE report_id=ANY(%s) AND kind='body' AND position(%s::bytea in folded_text)>0 ORDER BY report_id,ordinal", ([r['id'] for r in reports], query.casefold().encode('utf-8')))
                snippets = {}
                for chunk in chunks:
                    snippet, pos = self._snippet(chunk['raw_text'], query)
                    snippets[chunk['report_id']] = (snippet, {'kind': 'body', 'chunk': chunk['ordinal'], 'characterOffset': chunk['original_start'] + pos})
                for record in reports:
                    record['snippet'], record['matchLocation'] = snippets.get(record['id'], ('', {'kind': 'metadata'}))
            else:
                for record in reports:
                    record['snippet'] = ''
            next_cursor = None
            if has_more and rows:
                last = rows[-1]
                next_cursor = self._cursor([last[k] for k in ('report_date', 'issue', 'imported_at', 'report_id')], query, category, trash, state['generation'])
            return {'reports': reports, 'total': counts['total'], 'trashTotal': counts['trash_total'],
                'filteredCount': filtered_count, 'categories': sorted(categories, key=str.casefold),
                'generation': state['generation'], 'nextCursor': next_cursor, 'hasMore': has_more,
                'offset': offset, 'limit': limit, 'countMode': 'exact', 'searchRoute': route, 'pageRoute': page_route,
                'countMs': round(count_ms, 3), 'elapsedMs': round((time.perf_counter() - started) * 1000, 3),
                'countCost': 'Exact counts belong to this catalog generation and may be reused. New queries verify all indexed candidates; ordered page summaries are bounded by limit.'}

    def explain_search(self, query, category='', trash=False):
        cte, params, route = self._search_cte(query)
        matched = ' JOIN matched m ON m.report_id=r.report_id ' if query else ' '
        where = ' WHERE NOT r.purge_pending AND (r.trashed_at IS NOT NULL)=%s'
        params.append(trash)
        if category:
            where += ' AND category_fold=%s'
            params.append(category.casefold().encode('utf-8'))
        with self.pool.connection() as conn:
            plan = conn.execute('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) ' + cte + 'SELECT count(*) FROM rl_reports r' + matched + where, params, prepare=False).fetchone()['QUERY PLAN']
            return {'route': route, 'plan': plan}

    @contextmanager
    def library_lock(self, shared=True):
        # A dedicated autocommit connection pins a filesystem operation without
        # holding an idle transaction or exhausting the bounded request pool.
        fn = 'pg_advisory_lock_shared' if shared else 'pg_advisory_lock'
        unlock = 'pg_advisory_unlock_shared' if shared else 'pg_advisory_unlock'
        with psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row, connect_timeout=5) as conn:
            conn.execute('SELECT ' + fn + '(%s)', (MAINTENANCE_LOCK_KEY,))
            try:
                yield conn
            finally:
                conn.execute('SELECT ' + unlock + '(%s)', (MAINTENANCE_LOCK_KEY,))

    def export_state(self, conn=None):
        if conn is None:
            with self.pool.connection() as snapshot:
                snapshot.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                return self.export_state(snapshot)
        state = conn.execute('SELECT generation,mutation_generation,schema_version,library_id FROM rl_state WHERE singleton').fetchone()
        manifests = [self._package(conn, r['package_id']) for r in conn.execute('SELECT package_id FROM rl_packages ORDER BY package_id')]
        return {'state': state, 'generation': state['generation'], 'mutationGeneration': state['mutation_generation'],
            'packages': manifests, 'manifests': manifests,
            'reports': [self._report(r) for r in conn.execute('SELECT * FROM rl_reports ORDER BY report_id')],
            'preferences': conn.execute('SELECT settings FROM rl_preferences WHERE singleton').fetchone()['settings'],
            'operations': [self._operation(r) for r in conn.execute('SELECT * FROM rl_operations ORDER BY created_at')],
            'jobs': [self._job(r) for r in conn.execute('SELECT * FROM rl_jobs ORDER BY created_at')],
            'searchVersion': SEARCH_VERSION}

    def rebuild_search(self, search_documents):
        # Explicit original reads happen in the caller; never in list/search.
        with self._write() as (conn, state):
            ids = {r['report_id'] for r in conn.execute('SELECT report_id FROM rl_reports WHERE NOT purge_pending')}
            if set(search_documents) != ids:
                raise CatalogConflict('Rebuild documents do not match the live report set.')
            for row in conn.execute('SELECT * FROM rl_reports WHERE NOT purge_pending'):
                record = self._report(row)
                self._index_text(conn, record['id'], search_documents[record['id']], 'body')
                self._index_text(conn, record['id'], self._metadata_text(record), 'metadata')
            result = self._bump(conn, True)
            result['reports'] = len(ids)
            return result

