-- Exact source-path lookup preserves the existing sameTitleVersion contract.
-- bytea JSON may contain NUL; Python safely backfills the derived md_path column.
ALTER TABLE rl_reports ADD COLUMN IF NOT EXISTS md_path text;
CREATE INDEX IF NOT EXISTS rl_reports_md_path ON rl_reports(md_path) WHERE NOT purge_pending;
UPDATE rl_state SET schema_version=3,mutation_generation=mutation_generation+1 WHERE singleton AND schema_version<3;
