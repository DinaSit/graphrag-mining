from __future__ import annotations

import json
import logging
import os
from typing import Any

from app.pipeline.normalization import JUNK_VALUES, slug
from app.schemas import (
    ONTOLOGY_LABELS,
    CandidateStatus,
    DocumentRecord,
    DocumentVersion,
    ExtractionCandidate,
    Fact,
    OntologyCandidate,
    SourceFragment,
    SourceRef,
)

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

try:
    from neo4j import GraphDatabase
except ImportError:  # pragma: no cover
    GraphDatabase = None

log = logging.getLogger(__name__)

# Маркер строк-кандидатов онтологии в таблице ontology_versions: она хранит
# и версии конфигурации, и решения по кандидатам новых типов
ONTOLOGY_CANDIDATE_KIND = "ontology-candidate"


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
              element_count, storage_bucket, storage_object, storage_uri, created_at, hidden, is_scientific, origin,
              year, preview_object
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
              storage_uri = EXCLUDED.storage_uri,
              hidden = EXCLUDED.hidden,
              is_scientific = EXCLUDED.is_scientific,
              origin = EXCLUDED.origin,
              year = EXCLUDED.year,
              preview_object = EXCLUDED.preview_object
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
                document.hidden,
                document.is_scientific,
                document.origin,
                document.year,
                document.preview_object,
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
                          v.id, v.document_id, v.checksum, v.version_number, v.status, v.parser, v.created_at,
                          d.hidden, d.is_scientific, d.origin, d.year, d.preview_object
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
                hidden=bool(row[20]),
                is_scientific=row[21],
                origin=row[22],
                year=row[23],
                preview_object=row[24],
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
            log.error("PostgreSQL: поиск документа по checksum не выполнен: %s", exc)
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
              confidence, status, is_hypothesis, conflicts_with, source
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            ON CONFLICT (id) DO UPDATE SET
              confidence = EXCLUDED.confidence,
              status = EXCLUDED.status,
              conflicts_with = EXCLUDED.conflicts_with,
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
                json.dumps(fact.conflicts_with, ensure_ascii=False),
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
        # Размерность векторов задаёт модель эмбеддингов (EMBEDDING_DIM тот же,
        # что читает провайдер); дефолт 64 — детерминированный hash-эмбеддер
        dim = int(os.environ.get("EMBEDDING_DIM", "64"))
        statements = [
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS storage_bucket VARCHAR(256)",
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS storage_object VARCHAR(1024)",
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS storage_uri VARCHAR(1400)",
            # Скрытие из ответов и эвристические признаки документа
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS hidden BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS is_scientific BOOLEAN",
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS origin TEXT",
            # Год издания из текста документа; NULL — ещё не вычислен/не найден
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS year INTEGER",
            # Ключ PDF-превью DOCX/PPTX в MinIO (<storage_object>.preview.pdf);
            # NULL — превью нет. 1024 (storage_object) + суффикс ".preview.pdf"
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS preview_object VARCHAR(1040)",
            "ALTER TABLE facts ADD COLUMN IF NOT EXISTS conflicts_with JSONB NOT NULL DEFAULT '[]'::jsonb",
            # Индекс векторного поиска: близость считает PostgreSQL, а не backend
            "CREATE INDEX IF NOT EXISTS fragment_vectors_embedding_idx ON fragment_vectors USING hnsw (embedding vector_cosine_ops)",
            # Смена модели эмбеддингов меняет размерность: несовместимые векторы
            # вычищаются, колонка приводится к текущей EMBEDDING_DIM; переиндексацию
            # выполняет hydrate при старте. При совпадении типа блок ничего не
            # делает — существующие векторы сохраняются.
            f"""
            DO $$
            BEGIN
                IF (SELECT format_type(atttypid, atttypmod) FROM pg_attribute
                    WHERE attrelid = 'fragment_vectors'::regclass
                      AND attname = 'embedding') <> 'vector({dim})' THEN
                    TRUNCATE fragment_vectors;
                    ALTER TABLE fragment_vectors ALTER COLUMN embedding TYPE vector({dim});
                END IF;
            END $$;
            """,
        ]
        for statement in statements:
            self._execute(statement, ())

    def delete_document_data(self, document_id: str) -> None:
        """Каскадное удаление всего, что связано с документом (порядок — по FK)."""
        if not self.enabled:
            return
        statements = [
            "DELETE FROM fragment_vectors WHERE fragment_id IN (SELECT id FROM source_fragments WHERE document_id = %s)",
            # facts ссылаются на extraction_candidates по FK — удаляются первыми
            "DELETE FROM facts WHERE source->>'document_id' = %s",
            "DELETE FROM extraction_candidates WHERE source->>'document_id' = %s",
            "DELETE FROM source_fragments WHERE document_id = %s",
            "DELETE FROM document_versions WHERE document_id = %s",
            "DELETE FROM documents WHERE id = %s",
        ]
        for statement in statements:
            self._execute(statement, (document_id,))

    def upsert_job(self, job_id: str, job_type: str, status: str, payload: dict, error: str | None = None) -> None:
        if not self.enabled:
            return
        self._execute(
            """
            INSERT INTO jobs (id, job_type, status, payload, error)
            VALUES (%s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, error = EXCLUDED.error
            """,
            (job_id, job_type, status, json.dumps(payload, ensure_ascii=False), error),
        )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            with psycopg.connect(self.database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT id, job_type, status, payload, error FROM jobs WHERE id = %s", (job_id,))
                    row = cursor.fetchone()
            if row is None:
                return None
            return {"job_id": row[0], "job_type": row[1], "status": row[2], "payload": row[3], "error": row[4]}
        except Exception as exc:  # pragma: no cover - integration-only path
            self.last_error = str(exc)
            log.error("PostgreSQL: чтение статуса job %s не выполнено: %s", job_id, exc)
            return None

    def search_vectors(self, query_vector: list[float], top_k: int) -> list[tuple[str, float]]:
        """Векторный поиск на стороне PostgreSQL: (fragment_id, косинусная близость)."""
        if not self.enabled:
            return []
        try:
            with psycopg.connect(self.database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT fragment_id, 1 - (embedding <=> %s::vector) AS score
                        FROM fragment_vectors
                        WHERE embedding IS NOT NULL
                        ORDER BY embedding <=> %s::vector
                        LIMIT %s
                        """,
                        (str(query_vector), str(query_vector), top_k),
                    )
                    return [(row[0], float(row[1])) for row in cursor.fetchall()]
        except Exception as exc:  # pragma: no cover - integration-only path
            self.last_error = str(exc)
            log.error("PostgreSQL: векторный поиск не выполнен: %s", exc)
            return []

    def load_state(self) -> dict[str, dict[str, Any]]:
        """Загружает сохранённое состояние для восстановления ApplicationStore после рестарта.

        Ошибка загрузки пробрасывается: частичное состояние опаснее падения —
        оно выглядит как рабочая система с молча потерянными фактами.
        """
        state: dict[str, dict[str, Any]] = {
            "documents": {},
            "versions": {},
            "fragments": {},
            "candidates": {},
            "facts": {},
            "vectors": {},
            "ontology_candidates": {},
        }
        if not self.enabled:
            return state
        try:
            with psycopg.connect(self.database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT id, filename, document_type, source_label, access_level, checksum,
                               current_version_id, status, element_count, storage_bucket, storage_object,
                               storage_uri, created_at, hidden, is_scientific, origin, year, preview_object
                        FROM documents
                        """
                    )
                    for row in cursor.fetchall():
                        state["documents"][row[0]] = DocumentRecord(
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
                            hidden=bool(row[13]),
                            is_scientific=row[14],
                            origin=row[15],
                            year=row[16],
                            preview_object=row[17],
                        )

                    cursor.execute(
                        "SELECT id, document_id, checksum, version_number, status, parser, created_at FROM document_versions"
                    )
                    for row in cursor.fetchall():
                        state["versions"][row[0]] = DocumentVersion(
                            id=row[0],
                            document_id=row[1],
                            checksum=row[2],
                            version_number=row[3],
                            status=row[4],
                            parser=row[5],
                            created_at=row[6],
                        )

                    cursor.execute(
                        """
                        SELECT id, document_id, version_id, page, element_type, section, text,
                               normalized_text, fragment_metadata
                        FROM source_fragments
                        """
                    )
                    for row in cursor.fetchall():
                        state["fragments"][row[0]] = SourceFragment(
                            id=row[0],
                            document_id=row[1],
                            version_id=row[2],
                            page=row[3],
                            element_type=row[4],
                            section=row[5],
                            text=row[6],
                            normalized_text=row[7],
                            metadata=row[8] or {},
                        )

                    cursor.execute(
                        "SELECT id, type, payload, source, confidence, status, review_note FROM extraction_candidates"
                    )
                    for row in cursor.fetchall():
                        state["candidates"][row[0]] = ExtractionCandidate(
                            id=row[0],
                            type=row[1],
                            payload=row[2] or {},
                            source=SourceRef(**row[3]) if row[3] else None,
                            confidence=row[4],
                            status=row[5],
                            review_note=row[6],
                        )

                    cursor.execute(
                        """
                        SELECT id, candidate_id, material, material_id, experiment_id, sample, process,
                               temperature_c, duration_h, property, effect_direction, effect_value, effect_unit,
                               result_value, result_unit, lab, team, equipment, confidence, status, is_hypothesis,
                               conflicts_with, source
                        FROM facts
                        """
                    )
                    for row in cursor.fetchall():
                        if not row[22]:
                            continue
                        state["facts"][row[0]] = Fact(
                            id=row[0],
                            candidate_id=row[1],
                            material=row[2],
                            material_id=row[3],
                            experiment_id=row[4],
                            sample=row[5],
                            process=row[6],
                            temperature_c=row[7],
                            duration_h=row[8],
                            property=row[9],
                            effect_direction=row[10],
                            effect_value=row[11],
                            effect_unit=row[12],
                            result_value=row[13],
                            result_unit=row[14],
                            lab=row[15],
                            team=row[16],
                            equipment=row[17],
                            confidence=row[18],
                            status=row[19],
                            is_hypothesis=row[20],
                            conflicts_with=row[21] or [],
                            source=SourceRef(**row[22]),
                        )

                    cursor.execute("SELECT fragment_id, embedding FROM fragment_vectors WHERE embedding IS NOT NULL")
                    for row in cursor.fetchall():
                        state["vectors"][row[0]] = json.loads(str(row[1]))

                    cursor.execute(
                        "SELECT id, config FROM ontology_versions WHERE version = %s",
                        (ONTOLOGY_CANDIDATE_KIND,),
                    )
                    for row in cursor.fetchall():
                        state["ontology_candidates"][row[0]] = OntologyCandidate(**(row[1] or {}))
        except Exception as exc:
            self.last_error = str(exc)
            log.error("PostgreSQL: восстановление состояния не выполнено: %s", exc)
            raise
        return state

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
            self.last_error = None
        except Exception as exc:
            # Потеря записи недопустима: вызывающий слой (ingest, delete)
            # обязан пометить операцию как failed, а не «успешно» без данных
            self.last_error = str(exc)
            log.error("PostgreSQL: запись не выполнена: %s", exc)
            raise


class Neo4jSink:
    def __init__(self, uri: str | None, user: str | None, password: str | None):
        self.uri = uri
        self.user = user
        self.password = password
        self.last_error: str | None = None
        self._driver = None
        self._constraints_applied = False
        if self.enabled:
            # Недоступность Neo4j на старте не валит backend: constraints
            # доприменятся перед первой успешной записью
            try:
                self._ensure_constraints()
            except Exception as exc:
                log.error("Neo4j: constraints при старте не применены: %s", exc)
            # Исторический мусор ('не указано', 'unknown'…) из прежних загрузок
            # вычищается один раз при старте — после constraints
            try:
                self._cleanup_junk_nodes()
            except Exception as exc:
                log.error("Neo4j: зачистка мусорных узлов при старте не выполнена: %s", exc)

    @property
    def enabled(self) -> bool:
        return bool(self.uri and self.user and self.password and GraphDatabase is not None)

    def _get_driver(self):
        # Один драйвер на процесс: драйвер держит пул соединений и потокобезопасен,
        # создание нового на каждый запрос — лавина TCP-сессий под нагрузкой
        if self._driver is None:
            self._driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
        return self._driver

    def _ensure_constraints(self) -> None:
        """Уникальность id для всех меток онтологии: без constraint конкурентные
        MERGE двух воркеров создают дубликаты узлов, а MATCH по id идёт сканом."""
        if self._constraints_applied:
            return
        labels = sorted(self.ONTOLOGY_LABELS | {"Claim", "SourceFragment", "Effect", "Laboratory"})
        with self._get_driver().session() as session:
            for label in labels:
                session.run(
                    f"CREATE CONSTRAINT {slug(label)}_id IF NOT EXISTS "
                    f"FOR (n:{label}) REQUIRE n.id IS UNIQUE"
                )
        self._constraints_applied = True

    def _cleanup_junk_nodes(self) -> None:
        """Идемпотентно вычищает исторический мусор: узлы онтологических меток,
        чьё name (после toLower/ё→е) входит в список мусорных подписей КГ.
        DETACH DELETE — вместе со связями. Ошибки логируются выше, не роняют старт."""
        # ё→е нормализуется на стороне Cypher (replace), список — единый JUNK_VALUES
        junk = [value for value in JUNK_VALUES if value]
        labels = sorted(self.ONTOLOGY_LABELS | {"Effect", "Laboratory"})
        with self._get_driver().session() as session:
            for label in labels:
                session.run(
                    f"MATCH (n:{label}) "
                    "WHERE replace(toLower(coalesce(n.name, '')), 'ё', 'е') IN $junk "
                    "DETACH DELETE n",
                    junk=junk,
                )

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
            "property_id": f"property-{slug(fact.property)}",
            "property": fact.property,
            "effect_id": f"effect-{fact.effect_direction}-{fact.experiment_id}",
            "effect_direction": fact.effect_direction,
            "effect_value": fact.effect_value,
            "effect_unit": fact.effect_unit,
            "lab_id": f"lab-{slug(fact.lab)}",
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

    # Типы узлов и связей доменной онтологии. Whitelist обязателен: label и тип
    # связи нельзя параметризовать в Cypher. Список меток — единый из schemas.
    ONTOLOGY_LABELS = frozenset(ONTOLOGY_LABELS)
    ONTOLOGY_RELATIONS = {
        "applies_to", "uses_material", "operates_at_condition", "measured_parameter",
        "produced_effect", "validated_by", "contradicts", "authored_by", "expert_in",
        "applied_in", "worked_on", "described_in", "researched", "validated",
    }

    def upsert_semantics(self, claim_id: str, entities: list[dict], relations: list[dict]) -> None:
        """Проецирует извлечённые сущности и связи онтологии в граф.

        Сущности привязываются к Claim (провенанс: Claim -> SourceFragment уже есть),
        связи строятся между сущностями по данным извлечения.
        """
        if not self.enabled:
            return
        entity_types = {e["name"]: e["type"] for e in entities if e.get("name") and e.get("type") in self.ONTOLOGY_LABELS}
        for name, label in entity_types.items():
            self._write(
                f"MERGE (n:{label} {{id: $id}}) SET n.name = $name "
                f"WITH n MATCH (c:Claim {{id: $claim_id}}) MERGE (c)-[:MENTIONS]->(n)",
                {"id": f"{label.lower()}-{slug(name)}", "name": name, "claim_id": claim_id},
            )
        for relation in relations:
            subject, predicate, obj = relation.get("subject"), relation.get("predicate"), relation.get("object")
            if predicate not in self.ONTOLOGY_RELATIONS:
                continue
            subject_label, object_label = entity_types.get(subject), entity_types.get(obj)
            if not subject_label or not object_label:
                continue  # связь ссылается на сущность, не прошедшую whitelist
            self._write(
                f"MATCH (a:{subject_label} {{id: $subject_id}}), (b:{object_label} {{id: $object_id}}) "
                f"MERGE (a)-[:{predicate}]->(b)",
                {
                    "subject_id": f"{subject_label.lower()}-{slug(subject)}",
                    "object_id": f"{object_label.lower()}-{slug(obj)}",
                },
            )

    def run_read(self, query: str, params: dict) -> list[dict]:
        """Чтение из графа произвольным Cypher (шаблоны query-слоя)."""
        if not self.enabled:
            return []
        rows: list[dict] = []
        try:
            with self._get_driver().session() as session:
                for record in session.run(query, **params):
                    rows.append(dict(record))
        except Exception as exc:  # pragma: no cover - integration-only path
            self.last_error = str(exc)
            log.error("Neo4j: чтение не выполнено: %s", exc)
        return rows

    def delete_document(self, document_id: str, claim_ids: list[str]) -> None:
        """Удаляет из графа узлы документа и осиротевшие вершины."""
        if not self.enabled:
            return
        self._write("MATCH (s:SourceFragment {document_id: $document_id}) DETACH DELETE s",
                    {"document_id": document_id})
        self._write("MATCH (c:Claim) WHERE c.id IN $claim_ids DETACH DELETE c",
                    {"claim_ids": claim_ids})
        # Эксперименты без единого утверждения и вершины без связей — сироты
        self._write("MATCH (e:Experiment) WHERE NOT (e)<-[:BASED_ON]-() DETACH DELETE e", {})
        self._write("MATCH (n) WHERE NOT (n)--() DELETE n", {})

    def _write(self, query: str, params: dict[str, Any]) -> None:
        try:
            self._ensure_constraints()
            with self._get_driver().session() as session:
                session.run(query, **params)
            self.last_error = None
        except Exception as exc:
            # Расхождение графа с PostgreSQL недопустимо: вызывающий слой
            # обязан пометить операцию failed, а не отчитаться об успехе
            self.last_error = str(exc)
            log.error("Neo4j: запись не выполнена: %s", exc)
            raise


def _normalize_database_url(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace("postgresql+psycopg://", "postgresql://")
