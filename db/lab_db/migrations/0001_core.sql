-- 0001 core: projects, actors, roles, versioned resources, lineage,
-- operations, artifacts, idempotency, audit (append-only) and outbox.
-- SPEC 10.1-10.3, 16.4, 16.7. All timestamps are timestamptz (UTC).

CREATE TYPE lab_role AS ENUM ('owner', 'maintainer', 'reviewer', 'participant', 'observer');
CREATE TYPE lineage_relation AS ENUM (
    'extracted_from', 'derived_from', 'generated_from', 'validated_against',
    'retrieved_from', 'answered_using', 'evaluated_against', 'supersedes');
CREATE TYPE artifact_state AS ENUM ('staged', 'committed', 'tombstoned');

CREATE TABLE projects (
    id          uuid PRIMARY KEY,
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE actors (
    id          uuid PRIMARY KEY,
    kind        text NOT NULL CHECK (kind IN ('user', 'service')),
    display     text NOT NULL CHECK (length(display) BETWEEN 1 AND 200),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE project_members (
    project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    actor_id    uuid NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    role        lab_role NOT NULL,
    PRIMARY KEY (project_id, actor_id)
);

-- Every versioned resource is registered here; typed tables reference it.
CREATE TABLE resource_versions (
    id              uuid PRIMARY KEY,
    project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    resource_type   text NOT NULL CHECK (resource_type ~ '^[a-z][a-z0-9_]{0,63}$'),
    logical_id      text NOT NULL CHECK (length(logical_id) BETWEEN 1 AND 128),
    revision        integer NOT NULL CHECK (revision >= 1),
    content_hash    text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    hash_algorithm  text NOT NULL DEFAULT 'sha256' CHECK (hash_algorithm = 'sha256'),
    c14n_version    text NOT NULL CHECK (length(c14n_version) BETWEEN 1 AND 32),
    frozen          boolean NOT NULL DEFAULT false,
    created_by      uuid NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, resource_type, logical_id, revision),
    UNIQUE (project_id, id)   -- target for project-consistent composite FKs
);

-- Frozen versions are immutable; content never changes in place (10.2).
CREATE FUNCTION resource_versions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'resource_versions rows are never deleted (id %)', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.frozen THEN
        RAISE EXCEPTION 'resource version % is frozen', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.id, NEW.project_id, NEW.resource_type, NEW.logical_id, NEW.revision, NEW.content_hash,
        NEW.hash_algorithm, NEW.c14n_version, NEW.created_by, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.project_id, OLD.resource_type, OLD.logical_id, OLD.revision, OLD.content_hash,
        OLD.hash_algorithm, OLD.c14n_version, OLD.created_by, OLD.created_at) THEN
        RAISE EXCEPTION 'only the frozen flag of a resource version may change'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER resource_versions_guard BEFORE UPDATE OR DELETE ON resource_versions
    FOR EACH ROW EXECUTE FUNCTION resource_versions_guard();

CREATE TABLE operations (
    id              uuid PRIMARY KEY,
    project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    kind            text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_.]{0,63}$'),
    actor_id        uuid NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    trace_id        text CHECK (trace_id ~ '^[0-9a-f]{32}$'),
    status          text NOT NULL CHECK (status IN ('intent', 'running', 'succeeded', 'failed', 'outcome_unknown')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz CHECK (finished_at IS NULL OR finished_at >= created_at),
    UNIQUE (project_id, id)
);

CREATE TABLE lineage_edges (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id      uuid NOT NULL,
    upstream_id     uuid NOT NULL,
    downstream_id   uuid NOT NULL,
    relation        lineage_relation NOT NULL,
    operation_id    uuid NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (upstream_id <> downstream_id),
    UNIQUE (upstream_id, downstream_id, relation, operation_id),
    FOREIGN KEY (project_id, upstream_id) REFERENCES resource_versions(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, downstream_id) REFERENCES resource_versions(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, operation_id) REFERENCES operations(project_id, id) ON DELETE RESTRICT
);
CREATE INDEX lineage_upstream_idx ON lineage_edges (upstream_id);
CREATE INDEX lineage_downstream_idx ON lineage_edges (downstream_id);

CREATE TABLE artifacts (
    id              uuid PRIMARY KEY,
    project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    object_key      text NOT NULL CHECK (length(object_key) BETWEEN 1 AND 1024),
    object_version  text NOT NULL CHECK (length(object_version) BETWEEN 1 AND 256),
    sha256          text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes      bigint NOT NULL CHECK (size_bytes >= 0),
    mime            text NOT NULL CHECK (mime ~ '^[a-z]+/[a-z0-9.+-]+$'),
    state           artifact_state NOT NULL DEFAULT 'staged',
    retention       text NOT NULL CHECK (retention IN ('standard', 'restricted', 'legal_hold')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    committed_at    timestamptz,
    tombstoned_at   timestamptz,
    tombstone_reason text,
    UNIQUE (project_id, id),
    UNIQUE (object_key, object_version),
    CHECK (state <> 'staged' OR (committed_at IS NULL AND tombstoned_at IS NULL)),
    CHECK (state = 'staged' OR committed_at IS NOT NULL),
    CHECK ((state = 'tombstoned') = (tombstoned_at IS NOT NULL)),
    CHECK (state <> 'tombstoned' OR tombstone_reason IS NOT NULL)
);

-- staged -> committed -> tombstoned only; committed identity never changes.
CREATE FUNCTION artifacts_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.state <> 'staged' THEN
            RAISE EXCEPTION 'artifact % is %; tombstone it instead of deleting', OLD.id, OLD.state
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN OLD;
    END IF;
    IF (NEW.id, NEW.project_id, NEW.object_key, NEW.object_version, NEW.sha256, NEW.size_bytes, NEW.mime, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.project_id, OLD.object_key, OLD.object_version, OLD.sha256, OLD.size_bytes, OLD.mime, OLD.created_at)
       AND OLD.state <> 'staged' THEN
        RAISE EXCEPTION 'committed artifact % is immutable', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT ((OLD.state = NEW.state)
            OR (OLD.state = 'staged' AND NEW.state = 'committed')
            OR (OLD.state = 'committed' AND NEW.state = 'tombstoned')) THEN
        RAISE EXCEPTION 'illegal artifact transition % -> %', OLD.state, NEW.state
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.state = 'tombstoned' THEN
        RAISE EXCEPTION 'tombstoned artifact % is final', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER artifacts_guard BEFORE UPDATE OR DELETE ON artifacts
    FOR EACH ROW EXECUTE FUNCTION artifacts_guard();

CREATE TABLE idempotency_keys (
    project_id        uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    idempotency_scope text NOT NULL CHECK (idempotency_scope ~ '^[a-z][a-z0-9_.]{0,63}$'),
    idempotency_key   text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 256),
    request_hash      text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    operation_id      uuid NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, idempotency_scope, idempotency_key),
    FOREIGN KEY (project_id, operation_id) REFERENCES operations(project_id, id) ON DELETE RESTRICT
);

CREATE TABLE audit_events (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id         uuid NOT NULL UNIQUE,
    project_id       uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    actor_id         uuid NOT NULL REFERENCES actors(id) ON DELETE RESTRICT,
    action           text NOT NULL CHECK (action ~ '^[a-z][a-z0-9_.]{0,63}$'),
    resource_type    text NOT NULL,
    resource_id      uuid,
    before_revision  integer,
    after_revision   integer,
    reason           text,
    outcome          text NOT NULL CHECK (outcome IN ('succeeded', 'denied', 'failed')),
    operation_id     uuid NOT NULL,
    trace_id         text CHECK (trace_id ~ '^[0-9a-f]{32}$'),
    occurred_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, operation_id) REFERENCES operations(project_id, id) ON DELETE RESTRICT
);
CREATE INDEX audit_project_resource_time_idx ON audit_events (project_id, resource_id, occurred_at);
CREATE INDEX audit_actor_time_idx ON audit_events (actor_id, occurred_at);

-- Append-only for everyone; corrections are new events (10.3).
CREATE FUNCTION append_only_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
        USING ERRCODE = 'integrity_constraint_violation';
END $$;
CREATE TRIGGER audit_events_append_only BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION append_only_guard();
CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION append_only_guard();

CREATE TABLE outbox_events (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id      uuid NOT NULL UNIQUE,
    project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    topic         text NOT NULL CHECK (topic ~ '^[a-z][a-z0-9_.]{0,63}$'),
    payload       jsonb NOT NULL,
    operation_id  uuid NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    delivered_at  timestamptz,
    FOREIGN KEY (project_id, operation_id) REFERENCES operations(project_id, id) ON DELETE RESTRICT
);
CREATE INDEX outbox_undelivered_idx ON outbox_events (id) WHERE delivered_at IS NULL;
