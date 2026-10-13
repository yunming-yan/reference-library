# Isolated maintenance

Run with the application Python runtime. These operator tools migrate only from
a fixed legacy snapshot into a new library and empty PostgreSQL database. They
do not deploy, stop an application, replay legacy deletions, drop a database,
overwrite a library, or copy a running PostgreSQL data directory.

An evidence `--output` must be outside the source, application, active library and
backup roots. It cannot overwrite a source snapshot manifest, receipt or private
database configuration.

```text
python app/maintenance.py audit-legacy --source SNAPSHOT/data --snapshot-manifest SNAPSHOT/snapshot-manifest.json --output PRIVATE/audit.json
python app/maintenance.py migrate --source SNAPSHOT/data --snapshot-manifest SNAPSHOT/snapshot-manifest.json --library-root NEW_LIBRARY --program-root PROGRAM --db-config PRIVATE/db.json --output PRIVATE/migration.json
python app/maintenance.py verify --library-root NEW_LIBRARY --db-config PRIVATE/db.json --expect NEW_LIBRARY/evidence/legacy-audit.json --output PRIVATE/verification.json
python app/maintenance.py backup --library-root NEW_LIBRARY --destination EMPTY_BACKUP --program-root PROGRAM --db-config PRIVATE/db.json --pg-bin POSTGRES/bin --app-port APPLICATION_PORT --output PRIVATE/backup.json
python app/maintenance.py restore --backup EMPTY_BACKUP --library-root EMPTY_RESTORE_LIBRARY --program-root PROGRAM --db-config PRIVATE/isolated-db.json --pg-bin POSTGRES/bin --app-port RESTORE_APPLICATION_PORT --output PRIVATE/restore.json
python app/maintenance.py rollback-check --receipt NEW_LIBRARY/maintenance-receipt.json --db-config PRIVATE/db.json
python app/maintenance.py upgrade-identity --library-root EXISTING_ISOLATED_LIBRARY --program-root PROGRAM --db-config PRIVATE/db.json --app-port APPLICATION_PORT --expect PRIVATE/acceptance.json
```

The private database JSON uses `pg_catalog.Catalog` configuration. Credentials
are passed to PostgreSQL utilities through their environment, never shell
command strings. Routine CLI output contains only a result summary. Keep
audit outputs and database configuration outside the public source repository.

Every path must be a plain directory without symlinks or Windows reparse points.
Program, source snapshot, active library, and backup destination must be distinct
and cannot be ancestors of one another. New destinations must be empty. Only
valid application object IDs and validated portable ZIP members are copied.

Legacy manifests, catalog, overlays, settings and completed deletion journals are
validated against the current import generation. Completed journals are sealed
history; the migrator never executes them. Pending legacy deletion/import state
is rejected with the source unchanged so it can be recovered explicitly. This
avoids accidentally reapplying deletion authorization to reimported reports.
If migration committed but its final validation failed, rerunning the same
command can finalize only that exact pinned target while its mutation generation
still matches its recorded post-commit generation. It never clears or overwrites a partial target. New preference
defaults may be added; every existing saved setting must retain its value.

Backup requires the candidate application to be stopped. The database authority
and filesystem workers use advisory key `774831012`; backup holds its exclusive
session lock, takes a repeatable-read snapshot, exports that snapshot to
`pg_dump`, and copies every owned object, staging member and journal while
writes and purge are blocked. The resulting logical dump, complete byte
inventory, authority export, application schema and PostgreSQL/tool/extension
versions belong to one snapshot. No raw PGDATA is copied. The first version
requires matching server, `pg_dump` and `pg_restore` major versions. Logical
archives can be transferred across supported OS/CPU combinations; each target
still needs native runtime and extension availability.
The file portion includes only owned originals, staging, journals, evidence,
the library UUID marker and maintenance receipt. Database directories, rebuildable
caches, logs, credentials and runtime leases are excluded from this file copy.

Restore accepts only a complete, hash-verified backup, an empty isolated database
and an empty library. It restores in one database transaction and verifies the
full authority export, jobs/operations/generations, member hashes, ZIP CRC and
contents, text, headings, tables, images, relative resource paths, links,
categories, trash and reading preferences. Failed results remain isolated and
are never switched into the live entry point. An operator must perform read-only
application acceptance before switching. A receipt alone does not enforce
application read-only mode; launch the candidate with read-only configuration.
Pending v2 imports retain their owned source ZIP, staging/promoted object,
database job and operation, and filesystem checkpoint. The first portable backup
tool refuses a pending committed purge until cleanup completes, so it cannot
create a fresh recovery copy of deleted report content. It never completes that
purge itself. During isolated validation, pending survivor objects are checked
against current operation and report-generation ownership. Database state remains the recovery
authority and a filesystem checkpoint may legitimately lag it. Logical row
order and a derived report object ID are normalized before authority comparison;
every actual authority field and generation is compared exactly.

PostgreSQL schema 2 binds its authority to the library marker UUID. Fresh legacy
migration binds the empty database before importing reports. Verification,
backup and normal application startup reject a different or missing UUID.
`upgrade-identity` explicitly checks an older isolated library's complete bytes
before binding a populated unbound catalog. Restoring an earlier development
backup applies each available schema migration and validates before binding, with exactly those schema
and binding writes reflected in the receipt counters. A previous read-only
receipt stays eligible only if no other authority writes occurred; an upgrade
cannot erase a dirty rollback history.
Schema 3 adds the exact Markdown path index used to preserve the report version
contract. Each schema upgrade increments mutation generation once; binding an
unbound authority increments it separately. List and report generations retain
their saved meanings.

`rollback-check` refuses direct rollback after any authority mutation generation
change. At that point preserve the new authority and reconcile an incremental
snapshot before switching; returning directly to an old database would lose
changes. The tools intentionally do not alter deployment entry points.

Existing unresolved anchors or external links are retained in structural
evidence; source image members must resolve. A successful comparison means the
source semantics and bytes were preserved, not that every original document's
authoring error was repaired or that browser visual acceptance passed.

PostgreSQL's [pg_dump documentation](https://www.postgresql.org/docs/18/app-pgdump.html)
and [pg_restore documentation](https://www.postgresql.org/docs/18/app-pgrestore.html)
describe snapshot export and portable logical archives. Large-library physical
backup/WAL tooling is a separate future contract; this tool does not claim that
a full logical dump is an efficient daily backup for terabyte-scale originals.
