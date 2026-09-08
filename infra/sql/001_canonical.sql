-- Applied explicitly in the caller's private search_path, in one transaction.
-- The application role in a real deployment must not own this schema/tables.
CREATE TABLE canonical_meta (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  deployment_id uuid NOT NULL,
  environment text NOT NULL CHECK (environment IN ('synthetic','staging','production')),
  schema_version integer NOT NULL CHECK (schema_version = 1),
  schema_sha256 text NOT NULL CHECK (length(schema_sha256) = 64),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE entities (
  owner_id uuid PRIMARY KEY,
  entity_type text NOT NULL CHECK (entity_type IN ('person','company')),
  version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL
);
CREATE TABLE identity_keys (
  key_hash text PRIMARY KEY CHECK (length(key_hash) = 64),
  kind text NOT NULL CHECK (kind IN ('document','source')),
  identity_json text NOT NULL,
  owner_id uuid NOT NULL REFERENCES entities(owner_id),
  created_at timestamptz NOT NULL
) PARTITION BY HASH (key_hash);
CREATE INDEX identity_owner_idx ON identity_keys(owner_id);

CREATE TABLE operations (
  operation_id uuid PRIMARY KEY,
  owner_id uuid NOT NULL REFERENCES entities(owner_id),
  entity_version bigint NOT NULL CHECK (entity_version > 0),
  source_id_json text NOT NULL,
  source_record_id_json text NOT NULL,
  source_version_json text NOT NULL,
  record_hash text NOT NULL CHECK (length(record_hash) = 64),
  adapter_version_json text NOT NULL,
  normalizer_version_json text NOT NULL,
  prepared_hash text NOT NULL CHECK (length(prepared_hash) = 64),
  actor_id_json text NOT NULL,
  received_at timestamptz NOT NULL,
  UNIQUE(owner_id,entity_version)
);
CREATE INDEX operation_owner_idx ON operations(owner_id, received_at, operation_id);
CREATE TABLE items (
  owner_id uuid NOT NULL REFERENCES entities(owner_id),
  item_id uuid NOT NULL,
  kind text NOT NULL,
  item_key_hash text NOT NULL,
  item_key_json text NOT NULL,
  created_version bigint NOT NULL CHECK (created_version > 0),
  version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (owner_id,item_id),
  UNIQUE(owner_id,kind,item_key_hash)
) PARTITION BY HASH (owner_id);

CREATE TABLE observations (
  owner_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  item_id uuid NOT NULL,
  operation_id uuid NOT NULL REFERENCES operations(operation_id),
  entity_version bigint NOT NULL CHECK (entity_version > 0),
  operation_sequence integer NOT NULL CHECK (operation_sequence > 0),
  item_version bigint NOT NULL CHECK (item_version > 0),
  source_fact_id_json text NOT NULL,
  source_path_json text NOT NULL,
  target_path_hash text NOT NULL,
  target_path_json text NOT NULL,
  dimension text NOT NULL,
  input_json text NOT NULL,
  input_type text NOT NULL,
  input_encoding text NOT NULL,
  normalized_json text NOT NULL,
  metadata_json text NOT NULL,
  source_id_json text NOT NULL,
  source_id_hash text NOT NULL,
  source_updated_at timestamptz,
  observed_at timestamptz,
  effective_at timestamptz,
  received_at timestamptz NOT NULL,
  actor_id_json text NOT NULL,
  status text NOT NULL,
  applied boolean NOT NULL,
  pending_reason text,
  previous_observation_id uuid,
  previous_value_json text,
  binding_hash text,
  PRIMARY KEY(owner_id,observation_id),
  UNIQUE(owner_id,entity_version,operation_sequence),
  FOREIGN KEY(owner_id,item_id) REFERENCES items(owner_id,item_id)
) PARTITION BY HASH (owner_id);
CREATE INDEX observation_history_idx ON observations(owner_id,item_id,received_at,observation_id);
CREATE INDEX observation_operation_idx ON observations(operation_id);
CREATE INDEX observation_item_page_idx ON observations(owner_id,item_id,entity_version,operation_sequence);
CREATE INDEX observation_field_page_idx ON observations(owner_id,target_path_hash,entity_version,operation_sequence);
CREATE INDEX observation_source_page_idx ON observations(owner_id,source_id_hash,entity_version,operation_sequence);
CREATE INDEX observation_projection_page_idx ON observations(owner_id,item_id,target_path_hash,dimension,entity_version DESC,operation_sequence DESC) WHERE applied;
CREATE INDEX item_kind_page_idx ON items(owner_id,kind,item_id);

CREATE TABLE field_state (
  owner_id uuid NOT NULL,
  item_id uuid NOT NULL,
  target_path_hash text NOT NULL,
  target_path_json text NOT NULL,
  dimension text NOT NULL,
  observation_id uuid NOT NULL,
  value_json text NOT NULL,
  metadata_json text NOT NULL,
  status text NOT NULL,
  effective_at timestamptz,
  received_at timestamptz NOT NULL,
  source_id_json text NOT NULL,
  binding_hash text,
  PRIMARY KEY(owner_id,item_id,target_path_hash,dimension),
  FOREIGN KEY(owner_id,item_id) REFERENCES items(owner_id,item_id),
  FOREIGN KEY(owner_id,observation_id) REFERENCES observations(owner_id,observation_id)
) PARTITION BY HASH (owner_id);

-- Structure only; these rows contain no copied source subtree or source document.
CREATE TABLE source_containers (
  operation_id uuid NOT NULL REFERENCES operations(operation_id),
  path_hash text NOT NULL,
  path_json text NOT NULL,
  container_type text NOT NULL CHECK(container_type IN ('object','array')),
  length bigint NOT NULL CHECK(length >= 0),
  metadata_json text NOT NULL,
  PRIMARY KEY(operation_id,path_hash)
);
CREATE TABLE outbox (
  event_id uuid PRIMARY KEY,
  operation_id uuid NOT NULL UNIQUE REFERENCES operations(operation_id),
  owner_id uuid NOT NULL REFERENCES entities(owner_id),
  entity_version bigint NOT NULL,
  event_type text NOT NULL DEFAULT 'entity.changed',
  created_at timestamptz NOT NULL,
  lease_token uuid,
  lease_until timestamptz,
  attempts integer NOT NULL DEFAULT 0,
  published_at timestamptz
);
CREATE INDEX outbox_pending_idx ON outbox(created_at,event_id) WHERE published_at IS NULL;

CREATE TABLE migration_jobs (
  id uuid PRIMARY KEY,
  job_key_json text NOT NULL,
  source_id_json text NOT NULL,
  metadata_json text NOT NULL,
  status text NOT NULL CHECK(status IN ('pending','processing','completed','failed','cancelled')),
  checkpoint bigint NOT NULL DEFAULT 0 CHECK(checkpoint >= 0),
  cursor_json text NOT NULL DEFAULT 'null',
  records_processed bigint NOT NULL DEFAULT 0 CHECK(records_processed >= 0),
  operations_created bigint NOT NULL DEFAULT 0 CHECK(operations_created >= 0),
  observations_created bigint NOT NULL DEFAULT 0 CHECK(observations_created >= 0),
  error_json text,
  verification_json text,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL
);
CREATE TABLE migration_batches (
  job_id uuid NOT NULL REFERENCES migration_jobs(id),
  expected_checkpoint bigint NOT NULL,
  next_checkpoint bigint NOT NULL,
  batch_hash text NOT NULL,
  receipt_json text NOT NULL,
  created_at timestamptz NOT NULL,
  PRIMARY KEY(job_id,expected_checkpoint),
  CHECK(next_checkpoint > expected_checkpoint)
);

-- Temporary read cursors contain private context. Only their random bearer
-- token's hash is stored; the token itself contains no IDs, filters or values.
CREATE TABLE read_cursors (
  token_hash text PRIMARY KEY CHECK(length(token_hash)=64),
  owner_id uuid NOT NULL REFERENCES entities(owner_id),
  kind text NOT NULL CHECK(kind IN ('history','items','fields')),
  filter_hash text NOT NULL,
  filters_json text NOT NULL,
  ordering text NOT NULL CHECK(ordering IN ('asc','desc')),
  cut_version bigint NOT NULL CHECK(cut_version >= 0),
  position_json text NOT NULL,
  started_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  deadline_at timestamptz NOT NULL,
  CHECK(started_at < expires_at AND expires_at <= deadline_at)
);
CREATE INDEX read_cursor_expiry_idx ON read_cursors(expires_at,token_hash);

CREATE FUNCTION forbid_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'append-only canonical history: % is forbidden', TG_OP
    USING ERRCODE = '55000';
END;
$$;
DO $$
DECLARE relation_name text; part integer;
BEGIN
  FOREACH relation_name IN ARRAY ARRAY['identity_keys','items','observations','field_state'] LOOP
    FOR part IN 0..63 LOOP
      EXECUTE format('CREATE TABLE %I PARTITION OF %I FOR VALUES WITH (MODULUS 64, REMAINDER %s)',
        relation_name || '_p' || lpad(part::text,2,'0'), relation_name, part);
    END LOOP;
  END LOOP;
  FOREACH relation_name IN ARRAY ARRAY['canonical_meta','identity_keys','operations','observations','source_containers','migration_batches'] LOOP
    EXECUTE format('CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION forbid_history_mutation()',relation_name);
    EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION forbid_history_mutation()',relation_name);
  END LOOP;
  -- Direct partition truncation must also fail for non-owner runtime roles.
  FOR part IN 0..63 LOOP
    EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION forbid_history_mutation()', 'observations_p' || lpad(part::text,2,'0'));
    EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION forbid_history_mutation()', 'identity_keys_p' || lpad(part::text,2,'0'));
  END LOOP;
END;
$$;
