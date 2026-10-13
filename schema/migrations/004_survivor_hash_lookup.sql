-- Current survivor hashes are lookup values, not package identities.
-- Partial purges may legitimately leave different original packages with identical bytes.
-- Normal imports retain serialized ANY-existing duplicate checks under rl_state FOR UPDATE.
DROP INDEX IF EXISTS rl_packages_source_uq;
DROP INDEX IF EXISTS rl_packages_content_uq;
CREATE INDEX IF NOT EXISTS rl_packages_source_lookup ON rl_packages(source_sha);
CREATE INDEX IF NOT EXISTS rl_packages_content_lookup ON rl_packages(content_sha) WHERE content_sha IS NOT NULL;
CREATE INDEX IF NOT EXISTS rl_operations_recovery_order ON rl_operations(created_at,operation_id) WHERE state IN ('pending','running');
UPDATE rl_state SET schema_version=4,mutation_generation=mutation_generation+1 WHERE singleton;
