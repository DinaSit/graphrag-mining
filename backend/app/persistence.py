from __future__ import annotations

import json
from typing import Any

from app.schemas import (
    CandidateStatus,
    DocumentRecord,
    DocumentVersion,
    ExtractionCandidate,
    Fact,
    GraphEdge,
    GraphNode,
    GraphPayload,
    SourceFragment,
)

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

try:
    from neo4j import GraphDatabase
except ImportError:  # pragma: no cover
    GraphDatabase = None


class PostgresSink:
    def __init__(self, database_url: str | None):
        self.database_url = _normalize_database_url(database_url)
        self.last_error: str | None = None
        self.ensure_schema()

    @property
    def enabled(self) -> bool:
        return bool(self.database_url and psycopg is not None)

    def upsert_document(self, document: DocumentRecord, version: DocumentVersion) -> None:
        if not self.enabled:
            return
        self._execute(
            """
            INSERT INTO documents (
              id, filename, document_type, source_label, access_level, checksum, current_version_id, status,
              element_count, storage_bucket, storage_object, storage_uri, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
              filename = EXCLUDED.filename,
              document_type = EXCLUDED.document_type,
              source_label = EXCLUDED.source_label,
              access_level = EXCLUDED.access_level,
              current_version_id = EXCLUDED.current_version_id,
              status = EXCLUDED.status,
              element_count = EXCLUDED.element_count,
              storage_bucket = EXCLUDED.storage_bucket,
              storage_object = EXCLUDED.storage_object,
              storage_uri = EXCLUDED.storage_uri
            """,
            (
                document.id,
                document.filename,
                document.document_type,
                document.source_label,
                document.access_level,
                document.checksum,
                document.current_version_id,
                document.status.value,
                document.element_count,
                document.storage_bucket,
                document.storage_object,
                document.storage_uri,
                document.created_at,
            ),
        )
        self._execute(
            """
            INSERT INTO document_versions (id, document_id, checksum, version_number, status, parser, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, parser = EXCLUDED.parser
            """,
            (
                version.id,
                version.document_id,
                version.checksum,
                version.version_number,
                version.status.value,
                version.parser,
                version.created_at,
            ),
        )

    def get_document_by_checksum(self, checksum: str) -> tuple[DocumentRecord, DocumentVersion] | None:
        if not self.enabled:
            return None
        try:
            with psycopg.connect(self.database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                          d.id, d.filename, d.document_type, d.source_label, d.access_level, d.checksum,
                          d.current_version_id, d.status, d.element_count, d.storage_bucket, d.storage_object,
                          d.storage_uri, d.created_at,
                          v.id, v.document_id, v.checksum, v.version_number, v.status, v.parser, v.created_at
                        FROM documents d
                        JOIN document_versions v ON v.id = d.current_version_id
                        WHERE d.checksum = %s
                        LIMIT 1
                        """,
                        (checksum,),
                    )
                    row = cursor.fetchone()
            if row is None:
                return None
            document = DocumentRecord(
                id=row[0],
                filename=row[1],
                document_type=row[2],
                source_label=row[3],
                access_level=row[4],
                checksum=row[5],
                current_version_id=row[6],
                status=row[7],
                element_count=row[8],
                storage_bucket=row[9],
                storage_object=row[10],
                storage_uri=row[11],
                created_at=row[12],
            )
            version = DocumentVersion(
                id=row[13],
                document_id=row[14],
                checksum=row[15],
                version_number=row[16],
                status=row[17],
                parser=row[18],
                created_at=row[19],
            )
            return document, version
        except Exception as exc:  # pragma: no cover - integration-only path
            self.last_error = str(exc)
            return None

    def upsert_fragments(self, fragments: list[SourceFragment]) -> None:
        if not self.enabled or not fragments:
            return
        rows = [
            (
                fragment.id,
                fragment.document_id,
                fragment.version_id,
                fragment.page,
                fragment.element_type,
                fragment.section,
                fragment.text,
                fragment.normalized_text,
                json.dumps(fragment.metadata, ensure_ascii=False),
            )
            for fragment in fragments
        ]
        self._executemany(
            """
            INSERT INTO source_fragments (id, document_id, version_id, page, element_type, section, text, normalized_text, fragment_metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (id) DO UPDATE SET
              text = EXCLUDED.text,
              normalized_text = EXCLUDED.normalized_text,
              fragment_metadata = EXCLUDED.fragment_metadata
            """,
            rows,
        )

    def upsert_candidate(self, candidate: ExtractionCandidate) -> None:
        if not self.enabled:
            return
        self._execute(
            """
            INSERT INTO extraction_candidates (id, type, payload, source, confidence, status, review_note)
            VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
              payload = EXCLUDED.payload,
              source = EXCLUDED.source,
              confidence = EXCLUDED.confidence,
              status = EXCLUDED.status,
              review_note = EXCLUDED.review_note
            """,
            (
                candidate.id,
                candidate.type,
                json.dumps(candidate.payload, ensure_ascii=False),
                json.dumps(candidate.source.model_dump(mode="json"), ensure_ascii=False) if candidate.source else None,
                candidate.confidence,
                candidate.status.value if isinstance(candidate.status, CandidateStatus) else candidate.status,
                candidate.review_note,
            ),
        )

    def upsert_fact(self, fact: Fact) -> None:
        if not self.enabled:
            return
        self._execute(
            """
            INSERT INTO facts (
              id, candidate_id, material, material_id, experiment_id, sample, process, temperature_c, duration_h,
              property, effect_direction, effect_value, effect_unit, result_value, result_unit, lab, team, equipment,
              confidence, status, is_hypothesis, source
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (id) DO UPDATE SET
              confidence = EXCLUDED.confidence,
              status = EXCLUDED.status,
              source = EXCLUDED.source
            """,
            (
                fact.id,
                fact.candidate_id,
                fact.material,
                fact.material_id,
                fact.experiment_id,
                fact.sample,
                fact.process,
                fact.temperature_c,
                fact.duration_h,
                fact.property,
                fact.effect_direction,
                fact.effect_value,
                fact.effect_unit,
                fact.result_value,
                fact.result_unit,
                fact.lab,
                fact.team,
                fact.equipment,
                fact.confidence,
                fact.status,
                fact.is_hypothesis,
                json.dumps(fact.source.model_dump(mode="json"), ensure_ascii=False),
            ),
        )

    def upsert_vectors(self, vectors: dict[str, list[float]], embedding_model: str) -> None:
        if not self.enabled or not vectors:
            return
        rows = [
            (
                fragment_id,
                embedding_model,
                "[" + ",".join(f"{value:.8f}" for value in vector) + "]",
                json.dumps({"dimensions": len(vector)}, ensure_ascii=False),
            )
            for fragment_id, vector in vectors.items()
        ]
        self._executemany(
            """
            INSERT INTO fragment_vectors (fragment_id, embedding_model, embedding, vector_metadata)
            VALUES (%s, %s, %s::vector, %s::jsonb)
            ON CONFLICT (fragment_id) DO UPDATE SET
              embedding_model = EXCLUDED.embedding_model,
              embedding = EXCLUDED.embedding,
              vector_metadata = EXCLUDED.vector_metadata
            """,
            rows,
        )

    def ensure_schema(self) -> None:
        if not self.enabled:
            return
        statements = [
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS storage_bucket VARCHAR(256)",
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS storage_object VARCHAR(1024)",
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS storage_uri VARCHAR(1400)",
            # Совместимость с базами, созданными до перехода на remote-эмбеддинги
            # (256 изм.): старые векторы deterministic-hash (64) вычищаются,
            # колонка расширяется. На vector(256) блок ничего не делает.
            """
            DO $$
            BEGIN
                IF (SELECT format_type(atttypid, atttypmod) FROM pg_attribute
                    WHERE attrelid = 'fragment_vectors'::regclass
                      AND attname = 'embedding') <> 'vector(256)' THEN
                    TRUNCATE fragment_vectors;
                    ALTER TABLE fragment_vectors ALTER COLUMN embedding TYPE vector(256);
                END IF;
            END $$;
            """,
        ]
        for statement in statements:
            self._execute(statement, ())

    def list_facts(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        try:
            with psycopg.connect(self.database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT id, material, property, status, confidence, source FROM facts ORDER BY id")
                    return [
                        {
                            "id": row[0],
                            "material": row[1],
                            "property": row[2],
                            "status": row[3],
                            "confidence": row[4],
                            "source": row[5],
                        }
                        for row in cursor.fetchall()
                    ]
        except Exception as exc:  # pragma: no cover - integration-only path
            self.last_error = str(exc)
            return []

    def _execute(self, query: str, params: tuple[Any, ...]) -> None:
        self._executemany(query, [params])

    def _executemany(self, query: str, rows: list[tuple[Any, ...]]) -> None:
        if not self.enabled:
            return
        try:
            with psycopg.connect(self.database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.executemany(query, rows)
                connection.commit()
        except Exception as exc:  # pragma: no cover - integration-only path
            self.last_error = str(exc)


class Neo4jSink:
    def __init__(self, uri: str | None, user: str | None, password: str | None):
        self.uri = uri
        self.user = user
        self.password = password
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.uri and self.user and self.password and GraphDatabase is not None)

    def upsert_fact(self, fact: Fact) -> None:
        if not self.enabled:
            return
        query = """
        MERGE (m:Material {id: $material_id})
          SET m.name = $material
        MERGE (e:Experiment {id: $experiment_id})
          SET e.temperature_c = $temperature_c, e.duration_h = $duration_h, e.process = $process
        MERGE (p:Property {id: $property_id})
          SET p.name = $property
        MERGE (eff:Effect {id: $effect_id})
          SET eff.direction = $effect_direction, eff.value = $effect_value, eff.unit = $effect_unit
        MERGE (lab:Laboratory {id: $lab_id})
          SET lab.name = $lab
        MERGE (src:SourceFragment {id: $source_id})
          SET src.document_id = $document_id, src.page = $page, src.quote = $quote
        MERGE (c:Claim {id: $claim_id})
          SET c.confidence = $confidence, c.status = $status, c.is_hypothesis = $is_hypothesis
        MERGE (c)-[:ABOUT]->(m)
        MERGE (c)-[:BASED_ON]->(e)
        MERGE (e)-[:MEASURES]->(p)
        MERGE (e)-[:PRODUCED]->(eff)
        MERGE (src)-[:SUPPORTS]->(c)
        MERGE (e)-[:CONDUCTED_BY]->(lab)
        """
        params = {
            "material_id": fact.material_id,
            "material": fact.material,
            "experiment_id": fact.experiment_id,
            "temperature_c": fact.temperature_c,
            "duration_h": fact.duration_h,
            "process": fact.process,
            "property_id": f"property-{_slug(fact.property)}",
            "property": fact.property,
            "effect_id": f"effect-{fact.effect_direction}-{fact.experiment_id}",
            "effect_direction": fact.effect_direction,
            "effect_value": fact.effect_value,
            "effect_unit": fact.effect_unit,
            "lab_id": f"lab-{_slug(fact.lab)}",
            "lab": fact.lab,
            "source_id": fact.source.fragment_id,
            "document_id": fact.source.document_id,
            "page": fact.source.page,
            "quote": fact.source.quote,
            "claim_id": fact.id,
            "confidence": fact.confidence,
            "status": fact.status,
            "is_hypothesis": fact.is_hypothesis,
        }
        self._write(query, params)

    def get_graph(self, limit: int = 80) -> GraphPayload:
        if not self.enabled:
            return GraphPayload()
        query = """
        MATCH (a)-[r]->(b)
        WHERE a:Claim OR a:Experiment OR a:SourceFragment OR b:Claim OR b:Experiment OR b:Material
        RETURN a, type(r) AS rel, b
        LIMIT $limit
        """
        nodes: dict[str, GraphNode] = {}
        edges: dict[str, GraphEdge] = {}
        try:
            with GraphDatabase.driver(self.uri, auth=(self.user, self.password)) as driver:
                with driver.session() as session:
                    for record in session.run(query, limit=limit):
                        left = record["a"]
                        right = record["b"]
                        rel = record["rel"]
                        left_id = left.get("id")
                        right_id = right.get("id")
                        if not left_id or not right_id:
                            continue
                        nodes[left_id] = _node_from_neo4j(left)
                        nodes[right_id] = _node_from_neo4j(right)
                        edge_id = f"{left_id}-{rel}-{right_id}"
                        edges[edge_id] = GraphEdge(id=edge_id, source=left_id, target=right_id, label=rel)
        except Exception as exc:  # pragma: no cover - integration-only path
            self.last_error = str(exc)
        return GraphPayload(nodes=list(nodes.values()), edges=list(edges.values()))

    def _write(self, query: str, params: dict[str, Any]) -> None:
        try:
            with GraphDatabase.driver(self.uri, auth=(self.user, self.password)) as driver:
                with driver.session() as session:
                    session.run(query, **params)
        except Exception as exc:  # pragma: no cover - integration-only path
            self.last_error = str(exc)


def _normalize_database_url(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace("postgresql+psycopg://", "postgresql://")


def _slug(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
    return "-".join(part for part in cleaned.split("-") if part)


def _node_from_neo4j(node: Any) -> GraphNode:
    labels = list(node.labels)
    node_type = labels[0] if labels else "Entity"
    node_id = node.get("id")
    label = node.get("name") or node.get("quote") or node_id
    return GraphNode(id=node_id, label=str(label)[:80], type=node_type, data=dict(node))
