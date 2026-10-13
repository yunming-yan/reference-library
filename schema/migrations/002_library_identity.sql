-- Bind one authoritative PG catalog to one immutable library-root identity.
-- Binding is explicit; no existing populated catalog is assigned an ID by SQL.
ALTER TABLE rl_state ADD COLUMN IF NOT EXISTS library_id text;
UPDATE rl_state SET schema_version=2,mutation_generation=mutation_generation+1 WHERE singleton AND schema_version<2;
