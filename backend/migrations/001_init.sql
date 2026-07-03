CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id VARCHAR(64) PRIMARY KEY,
    filename VARCHAR(512) NOT NULL,
    document_type VARCHAR(64) NOT NULL,
    source_label VARCHAR(512),
    access_level VARCHAR(64) NOT NULL DEFAULT 'uploaded',
    checksum VARCHAR(128) NOT NULL UNIQUE,
    current_version_id VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    element_count INTEGER NOT NULL DEFAULT 0,
    storage_bucket VARCHAR(256),
    storage_object VARCHAR(1024),
    storage_uri VARCHAR(1400),
    created_at VARCHAR(64) NOT NULL
);

ALTER TABLE documents ADD COLUMN IF NOT EXISTS storage_bucket VARCHAR(256);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS storage_object VARCHAR(1024);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS storage_uri VARCHAR(1400);

CREATE TABLE IF NOT EXISTS document_versions (
    id VARCHAR(64) PRIMARY KEY,
    document_id VARCHAR(64) NOT NULL REFERENCES documents(id),
    checksum VARCHAR(128) NOT NULL,
    version_number INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL,
    parser VARCHAR(128) NOT NULL,
    created_at VARCHAR(64) NOT NULL
);

CREATE TABLE IF NOT EXISTS source_fragments (
    id VARCHAR(96) PRIMARY KEY,
    document_id VARCHAR(64) NOT NULL REFERENCES documents(id),
    version_id VARCHAR(64) NOT NULL REFERENCES document_versions(id),
    page INTEGER NOT NULL DEFAULT 1,
    element_type VARCHAR(64) NOT NULL,
    section VARCHAR(512),
    text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    fragment_metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS extraction_candidates (
    id VARCHAR(96) PRIMARY KEY,
    type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    source JSONB,
    confidence DOUBLE PRECISION NOT NULL,
    status VARCHAR(32) NOT NULL,
    review_note TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id VARCHAR(96) PRIMARY KEY,
    candidate_id VARCHAR(96) REFERENCES extraction_candidates(id),
    material VARCHAR(256) NOT NULL,
    material_id VARCHAR(256) NOT NULL,
    experiment_id VARCHAR(128) NOT NULL,
    sample VARCHAR(128) NOT NULL,
    process VARCHAR(256) NOT NULL,
    temperature_c DOUBLE PRECISION,
    duration_h DOUBLE PRECISION,
    property VARCHAR(256) NOT NULL,
    effect_direction VARCHAR(32) NOT NULL,
    effect_value DOUBLE PRECISION,
    effect_unit VARCHAR(32),
    result_value DOUBLE PRECISION,
    result_unit VARCHAR(32),
    lab VARCHAR(256) NOT NULL,
    team VARCHAR(256) NOT NULL,
    equipment VARCHAR(256),
    confidence DOUBLE PRECISION NOT NULL,
    status VARCHAR(32) NOT NULL,
    is_hypothesis BOOLEAN NOT NULL DEFAULT FALSE,
    source JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS fragment_vectors (
    fragment_id VARCHAR(96) PRIMARY KEY REFERENCES source_fragments(id),
    embedding_model VARCHAR(128) NOT NULL,
    embedding vector(64),
    vector_metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS ontology_versions (
    id VARCHAR(64) PRIMARY KEY,
    version VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    config JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id VARCHAR(96) PRIMARY KEY,
    job_type VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_facts_material_property_temp ON facts(material, property, temperature_c);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON extraction_candidates(status);
CREATE INDEX IF NOT EXISTS idx_fragments_document ON source_fragments(document_id, version_id);
