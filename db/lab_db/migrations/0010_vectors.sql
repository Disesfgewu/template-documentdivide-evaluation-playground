-- 0010 pgvector store (SPEC 12.4, R15, T53-T57).
-- Embedding spaces are immutable identities; a collection belongs to one
-- space, one project and one purpose; items are written only while the
-- collection is building and are published only after a manifest check;
-- every query is recorded append-only with the mode actually used.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE embedding_spaces (
    id                  uuid PRIMARY KEY,
    project_id          uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    provider            text NOT NULL CHECK (length(provider) BETWEEN 1 AND 128),
    model               text NOT NULL CHECK (length(model) BETWEEN 1 AND 256),
    model_revision      text NOT NULL CHECK (length(model_revision) BETWEEN 1 AND 128),
    dimension           integer NOT NULL CHECK (dimension BETWEEN 1 AND 2000),
    normalization       text NOT NULL CHECK (normalization IN ('none', 'l2')),
    document_task       text NOT NULL CHECK (length(document_task) BETWEEN 1 AND 64),
    query_task          text NOT NULL CHECK (length(query_task) BETWEEN 1 AND 64),
    preprocess_version  text NOT NULL CHECK (length(preprocess_version) BETWEEN 1 AND 64),
    metric              text NOT NULL CHECK (metric IN ('cosine', 'l2', 'inner_product')),
    config_hash         text NOT NULL CHECK (config_hash ~ '^[0-9a-f]{64}$'),
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, id),
    UNIQUE (project_id, config_hash)
);

CREATE FUNCTION embedding_spaces_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'embedding_spaces are immutable (id %); a new model or setting is a new space', OLD.id
        USING ERRCODE = 'integrity_constraint_violation';
END $$;
CREATE TRIGGER embedding_spaces_immutable BEFORE UPDATE OR DELETE ON embedding_spaces
    FOR EACH ROW EXECUTE FUNCTION embedding_spaces_immutable();

CREATE TABLE vector_collections (
    id                      uuid PRIMARY KEY,
    project_id              uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    purpose                 text NOT NULL CHECK (purpose IN ('benchmark_generation', 'method')),
    method_revision         text CHECK (method_revision IS NULL OR length(method_revision) BETWEEN 1 AND 128),
    embedding_space_id      uuid NOT NULL,
    document_hashes         text[] NOT NULL CHECK (cardinality(document_hashes) >= 1),
    status                  text NOT NULL DEFAULT 'building'
                                CHECK (status IN ('building', 'ready', 'frozen', 'failed')),
    manifest_hash           text CHECK (manifest_hash IS NULL OR manifest_hash ~ '^[0-9a-f]{64}$'),
    item_count              integer CHECK (item_count IS NULL OR item_count >= 0),
    created_by_operation_id uuid NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    ready_at                timestamptz,
    UNIQUE (project_id, id),
    FOREIGN KEY (project_id, embedding_space_id) REFERENCES embedding_spaces (project_id, id),
    FOREIGN KEY (project_id, created_by_operation_id) REFERENCES operations (project_id, id),
    -- A method collection names its method revision; the system's own
    -- benchmark-generation collection never does.
    CHECK ((purpose = 'method') = (method_revision IS NOT NULL)),
    CHECK ((status IN ('ready', 'frozen')) = (manifest_hash IS NOT NULL AND item_count IS NOT NULL AND ready_at IS NOT NULL)
           OR status = 'failed')
);

CREATE FUNCTION vector_collections_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'vector_collections are never deleted (id %)', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.id, NEW.project_id, NEW.purpose, NEW.method_revision, NEW.embedding_space_id,
        NEW.document_hashes, NEW.created_by_operation_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.project_id, OLD.purpose, OLD.method_revision, OLD.embedding_space_id,
        OLD.document_hashes, OLD.created_by_operation_id, OLD.created_at) THEN
        RAISE EXCEPTION 'vector collection % identity is immutable', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT ((OLD.status, NEW.status) IN (('building', 'ready'), ('building', 'failed'), ('ready', 'frozen'))) THEN
            RAISE EXCEPTION 'vector collection % cannot go from % to %', OLD.id, OLD.status, NEW.status
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSIF OLD.status <> 'building' AND (NEW.manifest_hash, NEW.item_count, NEW.ready_at)
          IS DISTINCT FROM (OLD.manifest_hash, OLD.item_count, OLD.ready_at) THEN
        RAISE EXCEPTION 'published vector collection % is immutable', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status = 'ready' AND NEW.status = 'frozen'
       AND (NEW.manifest_hash, NEW.item_count, NEW.ready_at) IS DISTINCT FROM (OLD.manifest_hash, OLD.item_count, OLD.ready_at) THEN
        RAISE EXCEPTION 'freezing vector collection % must not change its manifest', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER vector_collections_guard BEFORE UPDATE OR DELETE ON vector_collections
    FOR EACH ROW EXECUTE FUNCTION vector_collections_guard();

-- The vector column is dimension-free so collections of different spaces can
-- share the table; the trigger pins each row to its space's dimension and
-- per-collection ANN indexes cast to the fixed dimension.
CREATE TABLE vector_items (
    project_id              uuid NOT NULL,
    collection_id           uuid NOT NULL,
    item_id                 text NOT NULL CHECK (length(item_id) BETWEEN 1 AND 128),
    content_hash            text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    embedding               vector NOT NULL,
    document_sha256         text NOT NULL CHECK (document_sha256 ~ '^[0-9a-f]{64}$'),
    created_by_operation_id uuid NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (collection_id, item_id),
    FOREIGN KEY (project_id, collection_id) REFERENCES vector_collections (project_id, id),
    FOREIGN KEY (project_id, created_by_operation_id) REFERENCES operations (project_id, id)
);

CREATE FUNCTION vector_items_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    c   vector_collections%ROWTYPE;
    s   embedding_spaces%ROWTYPE;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'vector_items are append-only (collection %, item %); rebuild a new collection',
            OLD.collection_id, OLD.item_id USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT * INTO c FROM vector_collections WHERE id = NEW.collection_id FOR SHARE;
    IF c.status <> 'building' THEN
        RAISE EXCEPTION 'vector collection % is %; items are written only while building', c.id, c.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT (NEW.document_sha256 = ANY (c.document_hashes)) THEN
        RAISE EXCEPTION 'item % document is outside collection % document scope', NEW.item_id, c.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT * INTO s FROM embedding_spaces WHERE id = c.embedding_space_id;
    IF vector_dims(NEW.embedding) <> s.dimension THEN
        RAISE EXCEPTION 'item % has dimension %, space % requires %', NEW.item_id,
            vector_dims(NEW.embedding), s.id, s.dimension USING ERRCODE = 'check_violation';
    END IF;
    IF s.metric = 'cosine' AND vector_norm(NEW.embedding) = 0 THEN
        RAISE EXCEPTION 'item % is a zero vector; cosine distance is undefined', NEW.item_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER vector_items_guard BEFORE INSERT OR UPDATE OR DELETE ON vector_items
    FOR EACH ROW EXECUTE FUNCTION vector_items_guard();

-- One item may map to several, possibly cross-page, source regions.
CREATE TABLE vector_item_regions (
    collection_id   uuid NOT NULL,
    item_id         text NOT NULL,
    ordinal         integer NOT NULL CHECK (ordinal >= 0),
    region          jsonb NOT NULL CHECK (jsonb_typeof(region) = 'object'),
    PRIMARY KEY (collection_id, item_id, ordinal),
    FOREIGN KEY (collection_id, item_id) REFERENCES vector_items (collection_id, item_id)
);

CREATE FUNCTION vector_item_regions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    c   vector_collections%ROWTYPE;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'vector_item_regions are append-only' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT * INTO c FROM vector_collections WHERE id = NEW.collection_id FOR SHARE;
    IF c.status <> 'building' THEN
        RAISE EXCEPTION 'regions are written only while the collection is building'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER vector_item_regions_guard BEFORE INSERT OR UPDATE OR DELETE ON vector_item_regions
    FOR EACH ROW EXECUTE FUNCTION vector_item_regions_guard();

CREATE TABLE vector_index_revisions (
    id                  uuid PRIMARY KEY,
    collection_id       uuid NOT NULL REFERENCES vector_collections(id) ON DELETE RESTRICT,
    mode                text NOT NULL CHECK (mode IN ('exact', 'hnsw', 'ivfflat')),
    data_type           text NOT NULL CHECK (data_type IN ('vector')),
    build_params        jsonb NOT NULL CHECK (jsonb_typeof(build_params) = 'object'),
    query_params        jsonb NOT NULL CHECK (jsonb_typeof(query_params) = 'object'),
    extension_version   text NOT NULL CHECK (length(extension_version) BETWEEN 1 AND 32),
    corpus_manifest_hash text NOT NULL CHECK (corpus_manifest_hash ~ '^[0-9a-f]{64}$'),
    index_name          text CHECK (index_name IS NULL OR index_name ~ '^vi_[a-z0-9_]{1,60}$'),
    created_at          timestamptz NOT NULL DEFAULT now(),
    CHECK ((mode = 'exact') = (index_name IS NULL)),
    UNIQUE (collection_id, id)
);

CREATE TABLE vector_queries (
    id                  uuid PRIMARY KEY,
    project_id          uuid NOT NULL,
    collection_id       uuid NOT NULL,
    index_revision_id   uuid NOT NULL,
    query_hash          text NOT NULL CHECK (query_hash ~ '^[0-9a-f]{64}$'),
    query_space_hash    text NOT NULL CHECK (query_space_hash ~ '^[0-9a-f]{64}$'),
    k                   integer NOT NULL CHECK (k BETWEEN 1 AND 1000),
    filter              jsonb NOT NULL CHECK (jsonb_typeof(filter) = 'object'),
    metric              text NOT NULL CHECK (metric IN ('cosine', 'l2', 'inner_product')),
    mode_requested      text NOT NULL CHECK (mode_requested IN ('exact', 'hnsw', 'ivfflat')),
    mode_used           text NOT NULL CHECK (mode_used IN ('exact', 'hnsw', 'ivfflat')),
    fallback            text CHECK (fallback IS NULL OR fallback IN ('exact_after_shortfall')),
    shortfall           integer NOT NULL CHECK (shortfall >= 0),
    results             jsonb NOT NULL CHECK (jsonb_typeof(results) = 'array'),
    elapsed_ms          double precision NOT NULL CHECK (elapsed_ms >= 0),
    trace_id            text CHECK (trace_id IS NULL OR trace_id ~ '^[0-9a-f]{32}$'),
    created_at          timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (project_id, collection_id) REFERENCES vector_collections (project_id, id),
    FOREIGN KEY (collection_id, index_revision_id) REFERENCES vector_index_revisions (collection_id, id),
    CHECK ((fallback IS NULL) = (mode_used = mode_requested))
);

CREATE FUNCTION vector_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'integrity_constraint_violation';
END $$;
CREATE TRIGGER vector_queries_append_only BEFORE UPDATE OR DELETE ON vector_queries
    FOR EACH ROW EXECUTE FUNCTION vector_append_only();
CREATE TRIGGER vector_index_revisions_append_only BEFORE UPDATE OR DELETE ON vector_index_revisions
    FOR EACH ROW EXECUTE FUNCTION vector_append_only();
