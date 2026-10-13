"""Portable logical maintenance for Reference Library.

No command changes an existing library, deploys an application, replays legacy
deletion journals, or copies live PGDATA. New destinations must be empty.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import socket
import stat
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit
import uuid
import zipfile

FORMAT = 2
LOCK_KEY = 774831012
OWNER_FILE = '.reference-library-owner.json'
PROJECTION_FILE = '.migration-projections.json'
PROJECTION_OWNER = 'reference-library-migration-projections'
SHA = re.compile(r'[a-f0-9]{64}\Z')
RID = re.compile(r'[a-f0-9]{24}-\d+\Z')
OBJECT = re.compile(r'(?:[a-f0-9]{32}|[a-f0-9]{64})\Z')


class MaintenanceError(RuntimeError):
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf8')


def json_read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def json_write(path, value):
    path = Path(path)
    plain(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('xb') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2).encode('utf8') + b'\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def plain(path):
    """Reject links/junctions on every existing ancestor, before resolve()."""
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise MaintenanceError('Linked or reparse-point paths are not managed destinations.')
    return path


def relative(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name or '\0' in name:
        raise MaintenanceError('Invalid portable member path.')
    parts = name.split('/')
    if any(p in ('', '.', '..') or p.endswith((' ', '.')) for p in parts):
        raise MaintenanceError('Invalid portable member path.')
    for part in parts:
        if any(ord(c) < 32 or c in '<>"|?*' for c in part):
            raise MaintenanceError('Invalid portable member path.')
        if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I):
            raise MaintenanceError('Windows device names cannot be managed members.')
    return name


def contained(root, name):
    root = plain(root)
    result = plain(root.joinpath(*PurePosixPath(relative(name)).parts))
    if not result.is_relative_to(root) or result == root:
        raise MaintenanceError('Managed path escaped its ownership root.')
    return result


def separate(*paths):
    values = [plain(p) for p in paths if p is not None]
    for i, first in enumerate(values):
        for second in values[i + 1:]:
            if first == second or first.is_relative_to(second) or second.is_relative_to(first):
                raise MaintenanceError('Program, source, library and backup roots must be separate, with no ancestor overlap.')


def safe_output(path, protected_roots=(), protected_files=()):
    path = plain(path)
    for root in protected_roots:
        if root:
            root = plain(root)
            if path == root or path.is_relative_to(root):
                raise MaintenanceError('Evidence output must remain outside source, application, library and backup roots.')
    if path in {plain(p) for p in protected_files if p}:
        raise MaintenanceError('Evidence output cannot overwrite its pinned source manifest or receipt.')
    return path


def tree_files(root):
    root = plain(root)
    if not root.is_dir():
        raise MaintenanceError('Expected a plain directory.')
    files = []
    keys = set()
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            target = plain(Path(directory) / name)
            rel = target.relative_to(root).as_posix()
            relative(rel)
            key = rel.casefold()
            if key in keys:
                raise MaintenanceError('Case-colliding managed paths are not portable.')
            keys.add(key)
        files.extend(Path(directory) / name for name in names)
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def digest(path):
    result = hashlib.sha256()
    with plain(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def inventory(root, exclude=()):
    root = plain(root)
    return [{'path': p.relative_to(root).as_posix(), 'size': p.stat().st_size, 'sha256': digest(p)}
            for p in tree_files(root) if p.relative_to(root).as_posix() not in exclude]


def check_inventory(root, entries, exact=True, exclude=()):
    names = set()
    for entry in entries:
        name = relative(entry['path'])
        if name.casefold() in names or not SHA.fullmatch(entry['sha256']):
            raise MaintenanceError('Invalid or duplicate inventory entry.')
        names.add(name.casefold())
        path = contained(root, name)
        if not path.is_file() or path.stat().st_size != entry.get('size', entry.get('bytes')) or digest(path) != entry['sha256']:
            raise MaintenanceError('File bytes do not match the pinned manifest: ' + name)
    if exact:
        actual = {e['path'].casefold() for e in inventory(root, exclude)}
        if names != actual:
            raise MaintenanceError('The pinned manifest does not match the complete file set.')


def copy_file(source, target):
    source, target = plain(source), plain(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as src, target.open('xb') as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    if digest(source) != digest(target):
        raise MaintenanceError('Copied bytes changed.')


def copy_tree(source, target, exclude=()):
    source, target = plain(source), plain(target)
    separate(source, target)
    target.mkdir(parents=True, exist_ok=False)
    for path in tree_files(source):
        rel = path.relative_to(source).as_posix()
        if rel not in exclude:
            copy_file(path, contained(target, rel))
    # Preserve empty staging/journal directories as well as files.
    for directory, dirs, _ in os.walk(source):
        for name in dirs:
            rel = (Path(directory) / name).relative_to(source).as_posix()
            contained(target, rel).mkdir(parents=True, exist_ok=True)


def copy_library(source, target):
    """Copy the owned application content set, never a PGDATA/cache/runtime tree."""
    source, target = plain(source), plain(target)
    separate(source, target)
    target.mkdir(parents=True, exist_ok=False)
    exact = {OWNER_FILE, PROJECTION_FILE, 'maintenance-receipt.json'}
    roots = {'originals', 'staging', 'journals', 'evidence'}
    for path in tree_files(source):
        name = path.relative_to(source).as_posix()
        if name in exact or name.split('/', 1)[0] in roots:
            copy_file(path, contained(target, name))
    for name in roots:
        if (source / name).is_dir():
            (target / name).mkdir(exist_ok=True)
            for directory, dirs, _ in os.walk(source / name):
                for child in dirs:
                    rel = (Path(directory) / child).relative_to(source).as_posix()
                    contained(target, rel).mkdir(parents=True, exist_ok=True)


def new_destination(path):
    path = plain(path)
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise MaintenanceError('A new destination must be empty; existing data is never overwritten.')
    path.mkdir(parents=True, exist_ok=True)
    return path


def owner(root, create=False):
    root = plain(root)
    marker = root / OWNER_FILE
    if create:
        json_write(marker, {'format': 1, 'owner': 'reference-library', 'libraryId': str(uuid.uuid4())})
    value = json_read(marker)
    if value.get('format') != 1 or value.get('owner') != 'reference-library' or not re.fullmatch(r'(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})', value.get('libraryId', '')):
        raise MaintenanceError('Library ownership marker is invalid.')
    return value


def canonical_members(files):
    result = hashlib.sha256()
    for item in sorted(files, key=lambda e: e['path']):
        result.update(item['path'].encode('utf8'))
        result.update(bytes.fromhex(item['sha256']))
    return result.hexdigest()


def check_zip(path, members):
    expected = {e['path']: e for e in members}
    seen = set()
    with zipfile.ZipFile(plain(path)) as archive:
        for info in archive.infolist():
            name = info.filename.rstrip('/')
            relative(name)
            if info.is_dir():
                continue
            if name.casefold() in seen or info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16):
                raise MaintenanceError('Unsafe ZIP member.')
            seen.add(name.casefold())
            entry = expected.get(name)
            if entry is None or info.file_size != entry['size']:
                raise MaintenanceError('ZIP member set differs from the current surviving originals.')
            file_digest = hashlib.sha256()
            size = 0
            with archive.open(info) as stream:
                while block := stream.read(1024 * 1024):
                    size += len(block)
                    if size > entry['size']:
                        raise MaintenanceError('ZIP member exceeds its pinned size.')
                    file_digest.update(block)
            if size != entry['size'] or file_digest.hexdigest() != entry['sha256']:
                raise MaintenanceError('ZIP member CRC or bytes differ from the original.')
    if seen != {name.casefold() for name in expected}:
        raise MaintenanceError('ZIP lacks a surviving original member.')


class HTMLStructure(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.headings = []
        self.tables = 0
        self.images = []
        self.links = []
        self.ids = []
        self.text = []
        self.skip = 0
        self.heading = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style'):
            self.skip += 1
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.heading = [tag, []]
        if tag == 'table':
            self.tables += 1
        if attrs.get('id'):
            self.ids.append(attrs['id'])
        if tag == 'img' and attrs.get('src'):
            self.images.append(attrs['src'])
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)
        if self.heading and tag == self.heading[0]:
            self.headings.append([tag, ''.join(self.heading[1]).strip()])
            self.heading = None

    def handle_data(self, data):
        if not self.skip:
            self.text.append(data)
            if self.heading:
                self.heading[1].append(data)


def resolve_ref(source, ref, names):
    ref = ref.strip()
    url = urlsplit(ref)
    if url.scheme.lower() == 'data':
        # Pin embedded images by hash, without retaining a second original in
        # active-library acceptance evidence after a later permanent deletion.
        return {'reference': 'data:' + ref.split(';', 1)[0][5:100], 'kind': 'embedded', 'resolved': True,
                'referenceSha256': hashlib.sha256(ref.encode('utf8')).hexdigest(), 'referenceLength': len(ref)}
    if url.scheme or url.netloc:
        return {'reference': ref, 'kind': 'external', 'resolved': url.scheme in ('http', 'https', 'mailto', 'data')}
    if not url.path:
        return {'reference': ref, 'kind': 'anchor', 'anchor': unquote(url.fragment), 'resolved': True}
    member = posixpath.normpath(posixpath.join(str(PurePosixPath(source).parent), unquote(url.path)))
    if member.startswith(('../', '/')) or '\\' in member or ':' in member:
        return {'reference': ref, 'kind': 'rejected', 'resolved': False}
    actual = names.get(member.casefold())
    return {'reference': ref, 'kind': 'member', 'member': actual or member, 'resolved': actual is not None,
            'anchor': unquote(url.fragment)}


def report_structure(root, report, members):
    names = {e['path'].casefold(): e['path'] for e in members}
    md = contained(root, report['md']).read_bytes().decode('utf-8-sig')
    html = contained(root, report['html']).read_bytes().decode('utf-8-sig')
    if not md.strip() or not html.strip():
        raise MaintenanceError('A report has no full text.')
    scan = HTMLStructure()
    scan.feed(html)
    md_images = re.findall(r'!\[[^\]]*\]\(\s*([^\s)]+)(?:\s+[^)]*)?\)', md)
    md_links = re.findall(r'(?<!!)\[[^\]]+\]\(\s*([^\s)]+)(?:\s+[^)]*)?\)', md)
    refs = [resolve_ref(report['md'], ref, names) for ref in md_images + md_links]
    refs += [resolve_ref(report['html'], ref, names) for ref in scan.images + scan.links]
    for ref in md_images:
        item = resolve_ref(report['md'], ref, names)
        if item['kind'] == 'member' and not item['resolved']:
            raise MaintenanceError('Markdown image resource is missing.')
    for ref in scan.images:
        item = resolve_ref(report['html'], ref, names)
        if item['kind'] == 'member' and not item['resolved']:
            raise MaintenanceError('HTML image resource is missing.')
    return {'id': report['id'], 'importedAt': report['importedAt'],
            'markdownSha256': digest(contained(root, report['md'])), 'htmlSha256': digest(contained(root, report['html'])),
            'markdownCharacters': len(md), 'markdownHeadings': [list(h) for h in re.findall(r'^(#{1,6})\s+(.+?)\s*#*$', md, re.M)],
            'markdownTableDividers': len(re.findall(r'^\s*\|?\s*:?-{3,}:?\s*\|.*$', md, re.M)),
            'htmlHeadings': scan.headings, 'htmlTableCount': scan.tables, 'htmlImageCount': len(scan.images),
            'htmlTextSha256': hashlib.sha256(''.join(scan.text).encode('utf8')).hexdigest(),
            'anchors': scan.ids, 'duplicateAnchors': sorted({v for v in scan.ids if scan.ids.count(v) > 1}),
            'resourcesAndLinks': refs, 'unresolvedReferences': [r for r in refs if not r['resolved']],
            'declaredAttachments': report.get('attachments', [])}


def validate_manifest(directory, manifest, package=None):
    package = package or manifest.get('package') or manifest['reports'][0]['package']
    if not SHA.fullmatch(package) or not manifest.get('reports'):
        raise MaintenanceError('Invalid package identity.')
    members = inventory(directory / 'files')
    source_hash = digest(directory / 'source.zip')
    expected_source = manifest.get('currentSha256', manifest.get('sha256'))
    if source_hash != expected_source:
        raise MaintenanceError('Original ZIP hash differs from its current manifest.')
    if canonical_members(members) != manifest.get('contentSha256'):
        raise MaintenanceError('Expanded member hash differs from its current manifest.')
    if manifest.get('fileCount') != len(members) or manifest.get('expandedBytes') != sum(m['size'] for m in members):
        raise MaintenanceError('Expanded member sizes/count differ from the current manifest.')
    if 'files' in manifest and members != sorted(manifest['files'], key=lambda e: e['path']):
        raise MaintenanceError('Catalog member hashes differ from the originals.')
    check_zip(directory / 'source.zip', members)
    structures = []
    ids = set()
    for report in manifest['reports']:
        rid = report.get('id', '')
        if not RID.fullmatch(rid) or rid in ids or not rid.startswith(package[:24] + '-') or report.get('package') != package:
            raise MaintenanceError('Invalid report ownership.')
        ids.add(rid)
        if not isinstance(report.get('importedAt'), str):
            raise MaintenanceError('Report import generation is missing.')
        for member in [report['md'], report['html'], *report.get('attachments', [])]:
            if not contained(directory / 'files', member).is_file():
                raise MaintenanceError('A report references a missing managed member.')
        structures.append(report_structure(directory / 'files', report, members))
    return members, structures


def audit_legacy(source, snapshot_manifest=None):
    source = plain(source)
    before = inventory(source)
    if snapshot_manifest:
        pinned = json_read(snapshot_manifest)
        entries = [{**e, 'path': e['path'].replace('\\', '/'), 'size': e['bytes']} for e in pinned['dataFiles']]
        check_inventory(source, entries)
        if not pinned.get('dataStableBeforeAfter'):
            raise MaintenanceError('Legacy snapshot was not stable before and after copy.')
    catalog = json_read(source / 'catalog.json')
    if not isinstance(catalog.get('reports'), list):
        raise MaintenanceError('Legacy catalog is invalid.')
    manifests, overlays, structures = [], {}, []
    base_records = {}
    package_root = source / 'packages'
    for directory in sorted(package_root.iterdir()):
        plain(directory)
        if not directory.is_dir() or not SHA.fullmatch(directory.name):
            raise MaintenanceError('Unexpected unmanaged legacy package.')
        manifest = json_read(directory / 'metadata.json')
        files, reports = validate_manifest(directory, manifest, directory.name)
        enriched = copy.deepcopy(manifest)
        enriched.update(package=directory.name, currentSha256=digest(directory / 'source.zip'), files=files)
        manifests.append(enriched)
        structures.extend(reports)
        for report in manifest['reports']:
            if report['id'] in base_records:
                raise MaintenanceError('Duplicate legacy report identity.')
            base_records[report['id']] = report
    overlay_root = source / 'overlays'
    if overlay_root.exists():
        for path in tree_files(overlay_root):
            parts = path.relative_to(overlay_root).parts
            if len(parts) != 2 or parts[1] != 'metadata.json' or not RID.fullmatch(parts[0]):
                raise MaintenanceError('Unsupported legacy overlay member; preserve source and investigate.')
            if parts[0] not in base_records:
                raise MaintenanceError('An overlay has no current report owner.')
            overlays[parts[0]] = json_read(path)
    journal = []
    deletions = source / 'deletions'
    if deletions.exists():
        for path in tree_files(deletions):
            value = json_read(path)
            if (value.get('version') != 1 or not RID.fullmatch(value.get('id', '')) or value.get('state') not in ('pending', 'complete')
                    or not SHA.fullmatch(value.get('package', '')) or not value['id'].startswith(value['package'][:24] + '-')
                    or path.parent != deletions or path.name != value['id'] + '.json'
                    or not isinstance(value.get('importedAt'), str) or type(value.get('wholePackage')) is not bool
                    or not isinstance(value.get('removeFiles'), list)):
                raise MaintenanceError('Invalid legacy deletion journal.')
            for name in value.get('removeFiles', []):
                relative(name)
            live = base_records.get(value['id'])
            matches = bool(live and live.get('importedAt') == value.get('importedAt'))
            if value['state'] == 'pending' or matches:
                raise MaintenanceError('A current or pending legacy deletion requires explicit generation-aware recovery before migration.')
            journal.append({'id': value['id'], 'importedAt': value['importedAt'], 'state': value['state'],
                            'policy': 'sealed-history-never-replay', 'sha256': digest(path)})
    stage = source / 'staging'
    if stage.exists() and any(stage.iterdir()):
        raise MaintenanceError('Legacy staging is nonempty; finish or recover the import before migration.')
    effective = []
    for rid, report in base_records.items():
        overlay = overlays.get(rid, {})
        effective.append({**report, 'originalTitle': report['title'], 'title': overlay.get('title', report['title']),
                          'category': overlay.get('category', ''), 'trashedAt': overlay.get('trashedAt'),
                          'metadataUpdatedAt': overlay.get('updatedAt')})
    if sorted(effective, key=lambda r: r['id']) != sorted(catalog['reports'], key=lambda r: r['id']):
        raise MaintenanceError('Legacy catalog and current manifest/overlay generation disagree.')
    if before != inventory(source):
        raise MaintenanceError('Legacy source changed while it was being audited.')
    return {'format': FORMAT, 'createdAt': utcnow(), 'snapshotManifestSha256': digest(snapshot_manifest) if snapshot_manifest else None,
            'sourceFiles': before, 'manifests': manifests,
            'reports': sorted(effective, key=lambda r: r['id']), 'overlays': overlays,
            'preferences': json_read(source / 'settings.json') if (source / 'settings.json').exists() else {},
            'structures': sorted(structures, key=lambda r: r['id']), 'legacyJournals': journal,
            'counts': {'packages': len(manifests), 'reports': len(effective), 'normal': sum(not r['trashedAt'] for r in effective),
                       'trash': sum(bool(r['trashedAt']) for r in effective),
                       'svg': sum(e['path'].lower().endswith('.svg') for m in manifests for e in m['files'])}}


def load_catalog(config, initialize=True):
    from pg_catalog import Catalog
    return Catalog(json_read(config) if config else None, initialize=initialize)


def database_dsn(config=None):
    from psycopg.conninfo import make_conninfo
    value = json_read(config) if config else next((os.environ[k] for k in
        ('REFERENCE_LIBRARY_PG_DSN', 'REFERENCE_LIBRARY_DSN', 'PKM_PG_DSN', 'PG_DSN') if os.environ.get(k)), None)
    if value is None and os.environ.get('REFERENCE_LIBRARY_PG_CONFIG'):
        value = json_read(os.environ['REFERENCE_LIBRARY_PG_CONFIG'])
    if isinstance(value, dict):
        value = make_conninfo(**{k: value[k] for k in ('host', 'port', 'dbname', 'user', 'password') if k in value})
    if not value:
        raise MaintenanceError('Private PostgreSQL connection configuration is required.')
    return value


def scalar(row):
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def state_value(export):
    value = export.get('state', {})
    if isinstance(value, list):
        value = value[0]
    return value


def state_manifests(export):
    if 'manifests' in export:
        return export['manifests']
    return [p['manifest'] for p in export['packages']]


def authority_equivalent(actual, expected):
    def normalized(value):
        value = copy.deepcopy(value)
        for key in ('packages', 'manifests'):
            for manifest in value.get(key, []):
                if 'files' in manifest:
                    manifest['files'] = sorted(manifest['files'], key=lambda member: member['path'])
                # Report objectId is a derived ownership projection added by a
                # newer export API. Its authority is the parent package mapping.
                for report in manifest.get('reports', []):
                    object_id = report.get('objectId')
                    if object_id is not None and object_id != manifest['objectId']:
                        raise MaintenanceError('Report original ownership differs from its authoritative package.')
                    report['objectId'] = manifest['objectId']
                manifest['reports'] = sorted(manifest.get('reports', []), key=lambda report: report['id'])
            value[key] = sorted(value.get(key, []), key=lambda manifest: manifest['package'])
        for key in ('reports', 'operations', 'jobs'):
            if key in value:
                value[key] = sorted(value[key], key=lambda record: record['id'])
        return value
    return canonical(normalized(actual)) == canonical(normalized(expected))


def recovery_ownership(library, export):
    operations = export.get('operations', [])
    allowed_objects = {m['objectId'] for m in state_manifests(export)}
    allowed_stages, operation_ids, pending = set(), set(), []
    jobs = {job['id'] for job in export.get('jobs', [])}
    for operation in operations:
        operation_id = operation.get('id', '')
        if not re.fullmatch(r'[a-f0-9-]{20,80}', operation_id):
            raise MaintenanceError('Invalid durable operation identity.')
        operation_ids.add(operation_id)
        payload = operation.get('payload', {})
        active = operation.get('state') in ('pending', 'running')
        if active:
            pending.append(operation)
            job_id = operation.get('jobId') or payload.get('jobId')
            if job_id and job_id not in jobs:
                raise MaintenanceError('Pending operation has no matching durable job.')
            for key in ('objectId', 'oldObjectId', 'newObjectId'):
                value = payload.get(key)
                if value:
                    if not OBJECT.fullmatch(value):
                        raise MaintenanceError('Invalid recovery object ownership.')
                    allowed_objects.add(value)
            if payload.get('stageId'):
                if not OBJECT.fullmatch(payload['stageId']):
                    raise MaintenanceError('Invalid recovery staging ownership.')
                allowed_stages.add(payload['stageId'])
    original_root, staging_root, journal_root = (library / name for name in ('originals', 'staging', 'journals'))
    actual_objects = {p.name for p in original_root.iterdir()} if original_root.exists() else set()
    if not actual_objects.issubset(allowed_objects):
        raise MaintenanceError('An original object has no catalog or pending-operation owner.')
    for path in staging_root.iterdir() if staging_root.exists() else ():
        if path.name not in allowed_stages or not path.is_dir():
            raise MaintenanceError('Staging state has no matching current durable operation owner.')
    for path in journal_root.iterdir() if journal_root.exists() else ():
        if not path.is_file() or path.suffix != '.json' or path.stem not in operation_ids:
            raise MaintenanceError('Filesystem journal has no database operation owner.')
        disk = json_read(path)
        if disk.get('id') != path.stem:
            raise MaintenanceError('Filesystem journal identity differs from its owner.')
        # The filesystem checkpoint may lag the authoritative database payload.
    for operation in pending:
        payload = operation.get('payload', {})
        if operation.get('kind') == 'import':
            object_id, stage_id = payload.get('objectId'), payload.get('stageId')
            stage = contained(staging_root, stage_id) if stage_id else None
            obj = contained(original_root, object_id) if object_id else None
            location = obj if obj and obj.is_dir() else stage
            if location is None or not location.is_dir() or not (location / 'source.zip').is_file():
                raise MaintenanceError('Pending import lacks its durable source bytes.')
            if (location / 'metadata.json').exists():
                manifest = json_read(location / 'metadata.json')
                if manifest.get('objectId') != object_id:
                    raise MaintenanceError('Pending import object belongs to another generation.')
                validate_manifest(location, manifest)
        for key in ('newObjectId',):
            object_id = payload.get(key)
            if object_id and object_id in actual_objects:
                location = contained(original_root, object_id)
                manifest = json_read(location / 'metadata.json')
                if manifest.get('objectId') != object_id:
                    raise MaintenanceError('Pending survivor object identity differs from its operation.')
                validate_manifest(location, manifest)
    return pending, actual_objects


@contextlib.contextmanager
def frozen(catalog):
    """Hold the authority writer lock and export a single repeatable-read snapshot."""
    with catalog.library_lock(shared=False) as connection:
        try:
            connection.execute('SET idle_in_transaction_session_timeout=0')
            connection.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
            yield connection
        finally:
            connection.execute('ROLLBACK')


def migrate(source, library, program_root, config, snapshot_manifest=None):
    source, library = plain(source), plain(library)
    separate(source, library, program_root)
    audit = audit_legacy(source, snapshot_manifest)
    # A post-commit validation failure leaves the new target isolated. Resume
    # only its exact pinned migration; never clear a committed destination.
    if library.exists() and any(library.iterdir()):
        previous = json_read(library / 'maintenance-receipt.json')
        pinned_inventory = previous.get('sourceInventorySha256')
        if pinned_inventory is None and (library / 'evidence' / 'legacy-audit.json').is_file():
            pinned_inventory = hashlib.sha256(canonical(json_read(library / 'evidence' / 'legacy-audit.json')['sourceFiles'])).hexdigest()
        if previous.get('operation') != 'migration' or previous.get('state') != 'preparing' or pinned_inventory != hashlib.sha256(canonical(audit['sourceFiles'])).hexdigest():
            raise MaintenanceError('Existing destination is not this incomplete pinned migration.')
        catalog = load_catalog(config, initialize=False)
        try:
            verification = verify(library, catalog=catalog, expected=audit)
            committed = previous.get('committedMutationGeneration')
            if committed is None or state_value(catalog.export_state()).get('mutation_generation') != committed:
                raise MaintenanceError('The incomplete migration received authority writes; preserve it and reconcile before retrying.')
            receipt = migration_receipt(audit, verification, {'resumed': True})
            json_write(library / 'maintenance-receipt.json', receipt)
            return receipt
        finally:
            catalog.close()
    library = new_destination(library)
    owner(library, create=True)
    json_write(library / 'maintenance-receipt.json', {'format': FORMAT, 'operation': 'migration', 'state': 'preparing',
                                                  'createdAt': utcnow(), 'readOnlyAcceptance': True,
                                                  'sourceInventorySha256': hashlib.sha256(canonical(audit['sourceFiles'])).hexdigest()})
    manifests = []
    for manifest in audit['manifests']:
        target_manifest = copy.deepcopy(manifest)
        target_manifest['objectId'] = uuid.uuid4().hex
        package = manifest['package']
        destination = contained(library, 'originals/' + target_manifest['objectId'])
        copy_tree(contained(source, 'packages/' + package), destination)
        json_write(destination / 'metadata.json', target_manifest)
        validate_manifest(destination, target_manifest)
        manifests.append(target_manifest)
    # Full audit and pinned snapshot remain in the operator's separate output.
    # The active library has no second metadata/body projection to retain after purge.
    for name in ('staging', 'journals'):
        (library / name).mkdir()
    documents = {r['id']: contained(source / 'packages' / r['package'] / 'files', r['md']).read_bytes().decode('utf-8-sig')
                 for r in audit['reports']}
    catalog = load_catalog(config)
    try:
        catalog.bind_library(owner(library)['libraryId'])
        result = catalog.migration_import(manifests, documents, audit['overlays'], audit['preferences'])
        preparing = json_read(library / 'maintenance-receipt.json')
        preparing['committedMutationGeneration'] = result.get('mutation_generation', result.get('mutationGeneration'))
        json_write(library / 'maintenance-receipt.json', preparing)
        verification = verify(library, catalog=catalog, expected=audit)
        receipt = migration_receipt(audit, verification, result)
        json_write(library / 'maintenance-receipt.json', receipt)
        return receipt
    finally:
        catalog.close()


def register_migration_projections(library, expected):
    """Adopt only exact pre-release migration copies proved by an external audit."""
    library = plain(library)
    own = owner(library)
    expected = json_read(expected) if isinstance(expected, (str, Path)) else expected
    audit_path = library / 'evidence' / 'legacy-audit.json'
    legacy = library / 'evidence' / 'legacy'
    snapshot = library / 'evidence' / 'snapshot-manifest.json'
    if not audit_path.exists() and not legacy.exists() and not snapshot.exists():
        return None
    if not isinstance(expected, dict) or not isinstance(expected.get('sourceFiles'), list):
        raise MaintenanceError('External pinned audit is required to register old migration projections.')
    entries = {relative(e['path']): e for e in expected['sourceFiles']}
    files = []
    if legacy.exists():
        for path in tree_files(legacy):
            name = path.relative_to(legacy).as_posix()
            recognized = name in ('catalog.json', 'settings.json') or name.startswith(('overlays/', 'deletions/')) or re.fullmatch(r'packages/[a-f0-9]{64}/metadata\.json', name)
            item = entries.get(name)
            if not recognized or not item or path.stat().st_size != item['size'] or digest(path) != item['sha256']:
                raise MaintenanceError('Unknown or changed migration projection; registration refused.')
            files.append({'path': path.relative_to(library).as_posix(), 'size': item['size'], 'sha256': item['sha256']})
    if audit_path.exists():
        old = json_read(audit_path)
        if any(k != 'createdAt' and old[k] != expected.get(k) for k in old):
            raise MaintenanceError('Old migration audit differs from the external pinned audit.')
        files.append({'path': 'evidence/legacy-audit.json', 'size': audit_path.stat().st_size, 'sha256': digest(audit_path)})
    if snapshot.exists():
        if expected.get('snapshotManifestSha256') != digest(snapshot):
            raise MaintenanceError('Old snapshot projection lacks its exact external pinned hash.')
        files.append({'path': 'evidence/snapshot-manifest.json', 'size': snapshot.stat().st_size, 'sha256': digest(snapshot)})
    ledger_path = library / PROJECTION_FILE
    if ledger_path.exists():
        ledger = json_read(ledger_path)
        if ledger.get('libraryId') != own['libraryId'] or ledger.get('owner') != PROJECTION_OWNER or ledger.get('files') != sorted(files, key=lambda f: f['path']):
            raise MaintenanceError('Existing projection ownership differs; registration refused.')
        return ledger
    ledger = {'format': 1, 'owner': PROJECTION_OWNER, 'libraryId': own['libraryId'],
              'projectionId': uuid.uuid4().hex, 'files': sorted(files, key=lambda f: f['path'])}
    json_write(ledger_path, ledger)
    return ledger


def migration_receipt(audit, verification, result):
    return {'format': FORMAT, 'operation': 'migration', 'state': 'verified-read-only', 'createdAt': utcnow(),
            'readOnlyAcceptance': True, 'catalogState': verification['catalogState'],
            'counts': audit['counts'], 'migrationResult': result,
            'sourceInventorySha256': hashlib.sha256(canonical(audit['sourceFiles'])).hexdigest()}


def preserves_settings(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and preserves_settings(actual[key], value) for key, value in expected.items())
    return actual == expected


def verify(library, config=None, catalog=None, expected=None, conn=None, allow_unbound=False):
    library = plain(library)
    own = owner(library)
    tree_files(library)
    close = catalog is None
    catalog = catalog or load_catalog(config, initialize=False)
    try:
        export = catalog.export_state(conn=conn)
        identity = state_value(export).get('library_id')
        if identity != own['libraryId'] and not (allow_unbound and identity is None):
            raise MaintenanceError('PostgreSQL identity does not match this owned library root.')
        manifests = state_manifests(export)
        pending, actual_objects = recovery_ownership(library, export)
        structures, objects = [], set()
        for manifest in manifests:
            object_id = manifest.get('objectId', '')
            if not OBJECT.fullmatch(object_id) or object_id in objects:
                raise MaintenanceError('Invalid or duplicate catalog object ownership.')
            objects.add(object_id)
            directory = contained(library, 'originals/' + object_id)
            if manifest.get('blocked'):
                # A committed purge intentionally hides its affected package;
                # old bytes can already be removed before the final DB commit.
                # Preserve exact pending state and survivor bytes without replay.
                owners = [op for op in pending if op.get('kind') == 'purge' and op.get('payload', {}).get('oldObjectId', op.get('payload', {}).get('oldManifest', {}).get('objectId')) == object_id]
                if not owners:
                    raise MaintenanceError('Blocked package lacks a generation-aware pending purge owner.')
                for operation in owners:
                    payload = operation['payload']
                    old = payload.get('oldManifest', {})
                    if old.get('package', old.get('reports', [{}])[0].get('package')) != manifest['package']:
                        raise MaintenanceError('Pending purge is attached to another package generation.')
                    old_reports = {report['id']: report for report in old.get('reports', [])}
                    current_reports = {report['id']: report for report in manifest['reports']}
                    if set(old_reports) != set(current_reports):
                        raise MaintenanceError('Pending purge report generation differs from the current package.')
                    for rid, old_report in old_reports.items():
                        if any(old_report.get(key) != current_reports[rid].get(key) for key in
                               ('package', 'importedAt', 'md', 'html', 'attachments', 'reportGeneration', 'trashedAt', 'trashToken')):
                            raise MaintenanceError('Pending purge metadata or import generation changed.')
                    target = current_reports.get(payload.get('reportId'))
                    if not target or any(payload.get(key) != target.get(key) for key in
                                         ('importedAt', 'reportGeneration', 'trashedAt', 'trashToken')):
                        raise MaintenanceError('Pending purge confirmation belongs to another report generation.')
                continue
            if object_id not in actual_objects:
                raise MaintenanceError('A live report original object is missing.')
            disk_manifest = json_read(directory / 'metadata.json')
            # Disk metadata is immutable import identity; PG may change mutable metadata.
            for key in ('objectId', 'files', 'currentSha256', 'contentSha256'):
                disk_value, pg_value = disk_manifest.get(key), manifest.get(key)
                if key == 'files':
                    disk_value = sorted(disk_value or [], key=lambda member: member['path'])
                    pg_value = sorted(pg_value or [], key=lambda member: member['path'])
                if disk_value != pg_value:
                    raise MaintenanceError('PG and immutable object mapping disagree.')
            _, items = validate_manifest(directory, manifest)
            structures.extend(items)
        reports = export.get('reports', [r for m in manifests for r in m['reports']])
        # export reports may wrap the JSON record in an SQL-shaped row.
        reports = [r.get('record', r) for r in reports]
        if expected:
            expected = json_read(expected) if isinstance(expected, (str, Path)) else expected
            if sorted(structures, key=lambda r: r['id']) != sorted(expected['structures'], key=lambda r: r['id']):
                raise MaintenanceError('Text, headings, tables, images, paths or links changed during maintenance.')
            expected_reports = expected.get('reports', [])
            fields = ('id', 'package', 'importedAt', 'title', 'category', 'trashedAt', 'md', 'html', 'attachments')
            project = lambda rows: sorted([{k: r.get(k) for k in fields} for r in rows], key=lambda r: r['id'])
            if project(reports) != project(expected_reports):
                raise MaintenanceError('Report metadata, category, trash or generation changed during maintenance.')
            actual_by_id = {report['id']: report for report in reports}
            for report in expected_reports:
                if not preserves_settings(actual_by_id[report['id']], report):
                    raise MaintenanceError('An existing report metadata field changed during maintenance.')
            settings_ok = preserves_settings(export.get('preferences'), expected.get('preferences')) if 'sourceFiles' in expected else export.get('preferences') == expected.get('preferences')
            if not settings_ok:
                raise MaintenanceError('Persistent reading preferences changed during maintenance.')
        return {'format': FORMAT, 'verifiedAt': utcnow(), 'libraryId': own['libraryId'],
                'catalogState': state_value(export), 'structures': sorted(structures, key=lambda r: r['id']),
                'reports': sorted(reports, key=lambda r: r['id']), 'preferences': export.get('preferences'),
                'pendingOperations': [{'id': o['id'], 'kind': o['kind'], 'state': o['state'],
                                       'payloadSha256': hashlib.sha256(canonical(o['payload'])).hexdigest()} for o in pending],
                'counts': {'packages': len(manifests), 'reports': len(reports)}}
    finally:
        if close:
            catalog.close()


def pg_tool(directory, name):
    path = plain(Path(directory) / (name + ('.exe' if os.name == 'nt' else '')))
    if not path.is_file():
        raise MaintenanceError('Matching PostgreSQL maintenance tools are unavailable.')
    result = subprocess.run([str(path), '--version'], capture_output=True, text=True, timeout=15)
    match = re.search(r'PostgreSQL\)\s+(\d+)(?:\.\d+)*', result.stdout)
    if result.returncode or not match:
        raise MaintenanceError('Cannot verify PostgreSQL tool version.')
    return path, int(match.group(1)), result.stdout.strip()


def connection_env(connection, dsn):
    """Pass credentials to child tools in env, never command text or output."""
    from psycopg.conninfo import conninfo_to_dict
    info = {**connection.info.get_parameters(), **conninfo_to_dict(dsn)}
    env = os.environ.copy()
    for key, variable in [('host', 'PGHOST'), ('hostaddr', 'PGHOSTADDR'), ('port', 'PGPORT'), ('dbname', 'PGDATABASE'),
                          ('user', 'PGUSER'), ('password', 'PGPASSWORD'), ('sslmode', 'PGSSLMODE')]:
        env.pop(variable, None)
        if info.get(key):
            env[variable] = str(info[key])
    env['PGCONNECT_TIMEOUT'] = '10'
    env.pop('PGOPTIONS', None)
    env['PGCLIENTENCODING'] = 'UTF8'
    return env


def run_tool(command, env):
    result = subprocess.run([str(c) for c in command], env=env, capture_output=True, timeout=3600)
    if result.returncode:
        # PostgreSQL diagnostics may contain DSNs, role names, or original paths.
        raise MaintenanceError('PostgreSQL maintenance command failed; destination remains isolated and incomplete.')
    return hashlib.sha256(result.stderr).hexdigest() if result.stderr else None


def ensure_offline(port):
    if port is None:
        raise MaintenanceError('An application port is required to confirm the application is stopped.')
    for host in ('127.0.0.1', '::1'):
        try:
            with socket.create_connection((host, port), timeout=0.5):
                raise MaintenanceError('Stop the candidate application before this maintenance operation.')
        except (ConnectionRefusedError, TimeoutError, OSError):
            pass


@contextlib.contextmanager
def offline_library(root, port):
    ensure_offline(port)
    path = plain(Path(root) / '.runtime.lock')
    with path.open('a+b') as lease:
        if path.stat().st_size == 0:
            lease.write(b'0')
            lease.flush()
        lease.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise MaintenanceError('The library runtime lease is occupied; stop the candidate before maintenance.') from error
        try:
            ensure_offline(port)
            yield
        finally:
            lease.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lease.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lease.fileno(), fcntl.LOCK_UN)


def backup(library, destination, config, pg_bin, program_root, app_port):
    library, destination = plain(library), plain(destination)
    separate(library, destination, program_root)
    ensure_offline(app_port)
    owner(library)
    destination = new_destination(destination)
    json_write(destination / 'INCOMPLETE.json', {'format': FORMAT, 'createdAt': utcnow(), 'operation': 'backup'})
    catalog = load_catalog(config, initialize=False)
    try:
        with offline_library(library, app_port), frozen(catalog) as connection:
            ensure_offline(app_port)
            export = catalog.export_state(conn=connection)
            if any(operation.get('kind') == 'purge' and operation.get('state') in ('pending', 'running') for operation in export.get('operations', [])):
                raise MaintenanceError('Finish the committed purge before backup so deleted originals are not copied into a new recovery archive.')
            validation = verify(library, catalog=catalog, conn=connection)
            server_version = int(scalar(connection.execute('SHOW server_version_num').fetchone()))
            server_major = server_version // 10000
            dump, dump_major, dump_version = pg_tool(pg_bin, 'pg_dump')
            restore, restore_major, restore_version = pg_tool(pg_bin, 'pg_restore')
            if dump_major != server_major or restore_major != server_major:
                raise MaintenanceError('Server, pg_dump and pg_restore major versions must match for this portable backup contract.')
            extensions = connection.execute('SELECT extname,extversion FROM pg_extension ORDER BY extname').fetchall()
            snapshot = scalar(connection.execute('SELECT pg_export_snapshot()').fetchone())
            # Copy/pin all owned bytes while authority mutations and purge are blocked.
            copy_library(library, destination / 'library')
            dump_path = destination / 'catalog.dump'
            warning_hash = run_tool([dump, '--format=custom', '--no-owner', '--no-acl', '--snapshot=' + snapshot,
                                     '--file=' + str(dump_path)], connection_env(connection, database_dsn(config)))
            json_write(destination / 'acceptance.json', validation)
            json_write(destination / 'authority.json', export)
            entries = inventory(destination, exclude=('INCOMPLETE.json',))
            receipt = {'format': FORMAT, 'operation': 'logical-backup', 'createdAt': utcnow(), 'state': 'complete',
                       'consistentSnapshot': True, 'writerLockKey': LOCK_KEY, 'catalogState': state_value(export),
                       'postgresMajor': server_major, 'serverVersionNum': server_version,
                       'pgDumpVersion': dump_version, 'pgRestoreVersion': restore_version,
                       'extensions': [list(row.values()) if isinstance(row, dict) else list(row) for row in extensions], 'files': entries, 'dumpWarningSha256': warning_hash,
                       'applicationSchemaVersion': state_value(export).get('schemaVersion', state_value(export).get('schema_version')),
                       'pythonVersion': sys.version.split()[0], 'platform': sys.platform,
                       'restorationPolicy': 'empty-isolated-database-and-library-read-only-acceptance'}
            json_write(destination / 'backup-manifest.json', receipt)
            (destination / 'INCOMPLETE.json').unlink()
            return receipt
    finally:
        catalog.close()


def validate_backup(path):
    path = plain(path)
    if (path / 'INCOMPLETE.json').exists():
        raise MaintenanceError('Incomplete backups cannot be restored.')
    manifest = json_read(path / 'backup-manifest.json')
    if manifest.get('format') != FORMAT or manifest.get('state') != 'complete' or not manifest.get('consistentSnapshot'):
        raise MaintenanceError('Unsupported or incomplete logical backup.')
    check_inventory(path, manifest['files'], exclude=('backup-manifest.json',))
    owner(path / 'library')
    return manifest


def restore(backup_root, library, config, pg_bin, program_root, app_port):
    backup_root, library = plain(backup_root), plain(library)
    separate(backup_root, library, program_root)
    ensure_offline(app_port)
    manifest = validate_backup(backup_root)
    library = new_destination(library)
    import psycopg
    from psycopg.rows import dict_row
    dsn = database_dsn(config)
    # Raw connection avoids creating or requiring the authority schema in an
    # empty target. Catalog is opened only after the logical restore succeeds.
    with offline_library(library, app_port), psycopg.connect(dsn, autocommit=True, connect_timeout=5, row_factory=dict_row) as connection:
        connection.execute('SELECT pg_advisory_lock(%s)', (LOCK_KEY,))
        try:
            count = scalar(connection.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r','p','v','m','S')").fetchone())
            if count:
                raise MaintenanceError('Restore target database must be empty; existing authorities are never dropped.')
            version = int(scalar(connection.execute('SHOW server_version_num').fetchone())) // 10000
            tool, major, _ = pg_tool(pg_bin, 'pg_restore')
            if major != version or version != manifest['postgresMajor']:
                raise MaintenanceError('Restore requires matching PostgreSQL major and tool versions; upgrade separately after validation.')
            env = connection_env(connection, dsn)
            for path in tree_files(backup_root / 'library'):
                rel = path.relative_to(backup_root / 'library').as_posix()
                if rel != '.runtime.lock':
                    copy_file(path, contained(library, rel))
            for directory, dirs, _ in os.walk(backup_root / 'library'):
                for name in dirs:
                    contained(library, (Path(directory) / name).relative_to(backup_root / 'library').as_posix()).mkdir(parents=True, exist_ok=True)
            json_write(library / 'maintenance-receipt.json', {'format': FORMAT, 'operation': 'restore', 'state': 'restoring',
                                                          'createdAt': utcnow(), 'readOnlyAcceptance': True})
            run_tool([tool, '--dbname=' + env['PGDATABASE'], '--no-owner', '--no-acl', '--exit-on-error', '--single-transaction',
                      str(backup_root / 'catalog.dump')], env)
            catalog = load_catalog(config, initialize=False)
            try:
                from pg_catalog import SCHEMA_VERSION
                old_schema = manifest.get('applicationSchemaVersion')
                if type(old_schema) is not int or old_schema < 1 or old_schema > SCHEMA_VERSION:
                    raise MaintenanceError('Backup schema requires a separate supported application migration.')
                catalog.initialize(conn=connection)
                verify(library, catalog=catalog, expected=backup_root / 'acceptance.json', conn=connection, allow_unbound=True)
                old_identity = catalog.get_library_id(conn=connection)
                if old_identity is None:
                    catalog.bind_library(owner(library)['libraryId'], validated_existing=True, conn=connection)
                verification = verify(library, catalog=catalog, expected=backup_root / 'acceptance.json')
                restored = catalog.export_state()
                expected_state = json_read(backup_root / 'authority.json')
                if old_schema < SCHEMA_VERSION or old_identity is None:
                    expected_state = copy.deepcopy(expected_state)
                    upgrade_writes = SCHEMA_VERSION - old_schema + int(old_identity is None)
                    expected_state['state']['schema_version'] = SCHEMA_VERSION
                    expected_state['state']['library_id'] = owner(library)['libraryId']
                    expected_state['state']['mutation_generation'] += upgrade_writes
                    expected_state['mutationGeneration'] += upgrade_writes
                if not authority_equivalent(restored, expected_state):
                    raise MaintenanceError('Restored authority, staging jobs, journals or generations differ from the pinned database snapshot.')
                receipt = {'format': FORMAT, 'operation': 'restore', 'state': 'verified-read-only', 'createdAt': utcnow(),
                           'readOnlyAcceptance': True, 'catalogState': verification['catalogState'], 'counts': verification['counts'],
                           'backupManifestSha256': digest(backup_root / 'backup-manifest.json')}
                json_write(library / 'maintenance-receipt.json', receipt)
                return receipt
            finally:
                catalog.close()
        finally:
            connection.execute('SELECT pg_advisory_unlock(%s)', (LOCK_KEY,))


def upgrade_identity(library, config, program_root, app_port, expected=None):
    library = plain(library)
    separate(library, program_root)
    own = owner(library)
    previous = json_read(library / 'maintenance-receipt.json') if (library / 'maintenance-receipt.json').exists() else {}
    catalog = load_catalog(config, initialize=False)
    try:
        with offline_library(library, app_port), catalog.library_lock(shared=False) as connection:
            from pg_catalog import SCHEMA_VERSION
            # Schema migration writes only the schema identity column/counter.
            # A library binding is added only after complete byte/structure QA.
            catalog.initialize(conn=connection)
            before_binding = verify(library, catalog=catalog, expected=expected, conn=connection, allow_unbound=True)
            if expected is not None: register_migration_projections(library, expected)
            current = before_binding['catalogState']
            old_state = previous.get('catalogState', {})
            old_version = old_state.get('schema_version')
            schema_delta = SCHEMA_VERSION - old_version if type(old_version) is int and old_version <= SCHEMA_VERSION else 0
            previously_pristine = (previous.get('readOnlyAcceptance') is True and previous.get('state') == 'verified-read-only'
                                    and old_state.get('mutation_generation') is not None
                                    and current['mutation_generation'] == old_state['mutation_generation'] + schema_delta)
            catalog.bind_library(own['libraryId'], validated_existing=True, conn=connection)
            result = verify(library, catalog=catalog, expected=expected, conn=connection)
            receipt = {'format': FORMAT, 'operation': 'identity-upgrade',
                       'state': 'verified-read-only' if previously_pristine else 'identity-verified-writes-present',
                       'createdAt': utcnow(), 'readOnlyAcceptance': previously_pristine,
                       'catalogState': result['catalogState'], 'counts': result['counts'], 'libraryId': own['libraryId'],
                       'previousReceiptSha256': digest(library / 'maintenance-receipt.json') if previous else None}
            json_write(library / 'maintenance-receipt.json', receipt)
            return receipt
    finally:
        catalog.close()


def rollback_check(receipt, config):
    receipt = json_read(receipt)
    if not receipt.get('readOnlyAcceptance') or receipt.get('state') != 'verified-read-only':
        raise MaintenanceError('Only a verified read-only acceptance receipt permits a direct return to the previous library.')
    catalog = load_catalog(config, initialize=False)
    try:
        current = state_value(catalog.export_state())
        expected = receipt['catalogState']
        mutation = lambda s: s.get('mutation_generation', s.get('mutationGeneration'))
        if mutation(expected) is None or mutation(current) != mutation(expected) or current.get('library_id') != expected.get('library_id') or current.get('schema_version') != expected.get('schema_version'):
            raise MaintenanceError('New-authority writes occurred; direct rollback would lose data. Export and reconcile an incremental snapshot before switching.')
        return {'format': FORMAT, 'directRollbackAllowed': True, 'readOnlyAcceptance': True,
                'catalogState': current, 'deploymentAction': 'operator-may-switch-verified-entrypoint-only'}
    finally:
        catalog.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Reference Library isolated migration and logical backup tools')
    sub = parser.add_subparsers(dest='command', required=True)
    audit = sub.add_parser('audit-legacy')
    audit.add_argument('--source', required=True)
    audit.add_argument('--snapshot-manifest')
    audit.add_argument('--output', required=True)
    migrate_parser = sub.add_parser('migrate')
    migrate_parser.add_argument('--source', required=True)
    migrate_parser.add_argument('--snapshot-manifest')
    for name in ('migrate', 'backup', 'restore', 'verify', 'upgrade-identity'):
        command = migrate_parser if name == 'migrate' else sub.add_parser(name)
        command.add_argument('--library-root', required=True)
        command.add_argument('--db-config')
        command.add_argument('--output')
        if name != 'verify':
            command.add_argument('--program-root', required=True)
        if name in ('backup', 'restore'):
            command.add_argument('--pg-bin', required=True)
        if name in ('backup', 'restore', 'upgrade-identity'):
            command.add_argument('--app-port', type=int, required=True)
        if name == 'backup':
            command.add_argument('--destination', required=True)
        if name == 'restore':
            command.add_argument('--backup', required=True)
        if name in ('verify', 'upgrade-identity'):
            command.add_argument('--expect')
    check = sub.add_parser('rollback-check')
    check.add_argument('--receipt', required=True)
    check.add_argument('--db-config')
    check.add_argument('--output')
    args = parser.parse_args(argv)
    try:
        if args.output:
            safe_output(args.output, [getattr(args, key, None) for key in ('source', 'library_root', 'program_root', 'backup', 'destination')],
                        [getattr(args, key, None) for key in ('snapshot_manifest', 'receipt', 'db_config', 'expect')])
        if args.command == 'audit-legacy':
            result = audit_legacy(args.source, args.snapshot_manifest)
        elif args.command == 'migrate':
            result = migrate(args.source, args.library_root, args.program_root, args.db_config, args.snapshot_manifest)
        elif args.command == 'verify':
            result = verify(args.library_root, args.db_config, expected=args.expect)
        elif args.command == 'backup':
            result = backup(args.library_root, args.destination, args.db_config, args.pg_bin, args.program_root, args.app_port)
        elif args.command == 'restore':
            result = restore(args.backup, args.library_root, args.db_config, args.pg_bin, args.program_root, args.app_port)
        elif args.command == 'upgrade-identity':
            result = upgrade_identity(args.library_root, args.db_config, args.program_root, args.app_port, args.expect)
        else:
            result = rollback_check(args.receipt, args.db_config)
        if args.output:
            json_write(args.output, result)
        print(json.dumps({'ok': True, 'operation': args.command, 'counts': result.get('counts'),
                          'state': result.get('state'), 'directRollbackAllowed': result.get('directRollbackAllowed')}, ensure_ascii=True))
        return 0
    except MaintenanceError as error:
        print(json.dumps({'ok': False, 'operation': args.command, 'error': str(error)}, ensure_ascii=True), file=sys.stderr)
        return 2
    except Exception as error:
        # A traceback can disclose credentials and user paths. Detailed private diagnosis
        # is deliberately handled by a caller, without making public CLI output unsafe.
        print(json.dumps({'ok': False, 'operation': args.command, 'error': type(error).__name__,
                          'state': 'isolated-incomplete-no-switch'}, ensure_ascii=True), file=sys.stderr)
        return 3

