-- Explicit extension. Install separately; never changes canonical data/history.
CREATE TABLE canonical_export_meta (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 version integer NOT NULL CHECK(version=1),
 ddl_sha256 text NOT NULL CHECK(ddl_sha256 ~ '^[a-f0-9]{64}$'),
 deployment_id uuid NOT NULL,
 installed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE canonical_export_jobs (
 job_id uuid PRIMARY KEY,
 principal_id text NOT NULL,
 api_key_ref text,
 authorization_sha256 text NOT NULL CHECK(authorization_sha256 ~ '^[a-f0-9]{64}$'),
 idempotency_sha256 text NOT NULL CHECK(idempotency_sha256 ~ '^[a-f0-9]{64}$'),
 request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[a-f0-9]{64}$'),
 request_json text NOT NULL CHECK(octet_length(request_json)<=2097152),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','preparing','completed','failed','cancelled','expired')),
 phase text NOT NULL DEFAULT 'validating' CHECK(phase IN ('validating','selecting','materializing','writing','verifying','publishing')),
 checkpoint bigint NOT NULL DEFAULT 0 CHECK(checkpoint>=0),
 cursor_json text NOT NULL DEFAULT 'null' CHECK(octet_length(cursor_json)<=65536),
 cut_json text NOT NULL DEFAULT 'null' CHECK(octet_length(cut_json)<=65536),
 processed bigint NOT NULL DEFAULT 0 CHECK(processed>=0),
 total bigint CHECK(total>=0),
 total_relation text CHECK(total_relation IN ('eq','gte')),
 error_code text CHECK(error_code ~ '^[A-Z][A-Z0-9_]{0,100}$'),
 lease_token uuid,
 lease_until timestamptz,
 lease_owner text,
 attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 completed_at timestamptz,
 expires_at timestamptz,
 CHECK((total IS NULL)=(total_relation IS NULL)),
 CHECK(total IS NULL OR total_relation<>'eq' OR processed<=total),
 CHECK((lease_token IS NULL)=(lease_until IS NULL)),
 CHECK(lease_token IS NULL OR status='preparing'),
 UNIQUE(principal_id,idempotency_sha256)
);
CREATE INDEX canonical_export_claim ON canonical_export_jobs(status,lease_until,created_at,job_id)
 WHERE status IN ('pending','preparing');
CREATE INDEX canonical_export_owner ON canonical_export_jobs(principal_id,created_at DESC,job_id);
CREATE TABLE canonical_export_events (
 event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 job_id uuid NOT NULL REFERENCES canonical_export_jobs(job_id),
 action text NOT NULL,
 principal_id text NOT NULL,
 checkpoint bigint NOT NULL,
 detail_json text NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE canonical_export_checkpoints (
 job_id uuid NOT NULL REFERENCES canonical_export_jobs(job_id),
 operation_id uuid NOT NULL,
 previous_checkpoint bigint NOT NULL,
 next_checkpoint bigint NOT NULL,
 body_sha256 text NOT NULL CHECK(body_sha256 ~ '^[a-f0-9]{64}$'),
 body_json text NOT NULL CHECK(octet_length(body_json)<=262144),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(job_id,operation_id),
 UNIQUE(job_id,next_checkpoint)
);
CREATE TABLE canonical_export_parts (
 job_id uuid NOT NULL REFERENCES canonical_export_jobs(job_id),
 part_id uuid NOT NULL,
 ordinal integer NOT NULL CHECK(ordinal>=0),
 file_name text NOT NULL CHECK(file_name ~ '^part-[0-9]{8}\.(xlsx|zip)$'),
 bytes bigint NOT NULL CHECK(bytes>0),
 sha256 text NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
 verification_sha256 text NOT NULL CHECK(verification_sha256 ~ '^[a-f0-9]{64}$'),
 rows_json text NOT NULL CHECK(octet_length(rows_json)<=65536),
 verified_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(job_id,part_id), UNIQUE(job_id,ordinal), UNIQUE(job_id,file_name)
);
CREATE FUNCTION canonical_export_identity_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.job_id,NEW.principal_id,NEW.api_key_ref,NEW.authorization_sha256,NEW.idempotency_sha256,NEW.request_sha256,NEW.request_json,NEW.created_at)
 IS DISTINCT FROM (OLD.job_id,OLD.principal_id,OLD.api_key_ref,OLD.authorization_sha256,OLD.idempotency_sha256,OLD.request_sha256,OLD.request_json,OLD.created_at)
 THEN RAISE EXCEPTION 'immutable export request' USING ERRCODE='55000'; END IF;
 IF OLD.cut_json<>'null' AND NEW.cut_json IS DISTINCT FROM OLD.cut_json
 THEN RAISE EXCEPTION 'immutable export cut' USING ERRCODE='55000'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER canonical_export_request_guard BEFORE UPDATE ON canonical_export_jobs
 FOR EACH ROW EXECUTE FUNCTION canonical_export_identity_immutable();
CREATE TRIGGER canonical_export_jobs_no_delete BEFORE DELETE OR TRUNCATE ON canonical_export_jobs
 FOR EACH STATEMENT EXECUTE FUNCTION forbid_history_mutation();
CREATE TRIGGER canonical_export_meta_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON canonical_export_meta
 FOR EACH STATEMENT EXECUTE FUNCTION forbid_history_mutation();
CREATE TRIGGER canonical_export_events_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON canonical_export_events
 FOR EACH STATEMENT EXECUTE FUNCTION forbid_history_mutation();
CREATE TRIGGER canonical_export_checkpoint_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON canonical_export_checkpoints
 FOR EACH STATEMENT EXECUTE FUNCTION forbid_history_mutation();
CREATE TRIGGER canonical_export_parts_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON canonical_export_parts
 FOR EACH STATEMENT EXECUTE FUNCTION forbid_history_mutation();
