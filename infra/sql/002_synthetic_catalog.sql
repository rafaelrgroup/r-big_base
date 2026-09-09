-- Explicit opt-in extension for the private synthetic PostgreSQL fixture.
CREATE TABLE field_catalog_meta (
  singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
  deployment_id uuid NOT NULL,
  ddl_sha256 text NOT NULL
);
CREATE TABLE field_catalog (
  field_id text PRIMARY KEY,
  version bigint NOT NULL CHECK(version > 0),
  definition_json text NOT NULL
);
CREATE TABLE field_catalog_versions (
  field_id text NOT NULL,
  version bigint NOT NULL CHECK(version > 0),
  definition_json text NOT NULL,
  definition_sha256 text NOT NULL,
  PRIMARY KEY(field_id,version)
);
CREATE TABLE field_catalog_operations (
  operation_id uuid PRIMARY KEY,
  request_sha256 text NOT NULL,
  actor_json text NOT NULL,
  action text NOT NULL CHECK(action IN ('create','update')),
  field_id text NOT NULL,
  version bigint NOT NULL,
  receipt_json text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY(field_id,version) REFERENCES field_catalog_versions(field_id,version)
);
DO $$
DECLARE relation_name text;
BEGIN
  FOREACH relation_name IN ARRAY ARRAY['field_catalog_meta','field_catalog_versions','field_catalog_operations'] LOOP
    EXECUTE format('CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION forbid_history_mutation()',relation_name);
    EXECUTE format('CREATE TRIGGER immutable_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION forbid_history_mutation()',relation_name);
  END LOOP;
END;
$$;
