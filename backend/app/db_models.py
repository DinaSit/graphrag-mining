from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class DocumentRow(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    document_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_label: Mapped[str | None] = mapped_column(String(512))
    access_level: Mapped[str] = mapped_column(String(64), default="uploaded")
    checksum: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    current_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    element_count: Mapped[int] = mapped_column(Integer, default=0)
    storage_bucket: Mapped[str | None] = mapped_column(String(256))
    storage_object: Mapped[str | None] = mapped_column(String(1024))
    storage_uri: Mapped[str | None] = mapped_column(String(1400))
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)

    versions: Mapped[list["DocumentVersionRow"]] = relationship(back_populates="document")


class DocumentVersionRow(Base):
    __tablename__ = "document_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), nullable=False)
    checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    parser: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)

    document: Mapped[DocumentRow] = relationship(back_populates="versions")


class SourceFragmentRow(Base):
    __tablename__ = "source_fragments"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), nullable=False)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), nullable=False)
    page: Mapped[int] = mapped_column(Integer, default=1)
    element_type: Mapped[str] = mapped_column(String(64), nullable=False)
    section: Mapped[str | None] = mapped_column(String(512))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    fragment_metadata: Mapped[dict] = mapped_column(JSON, default=dict)


class ExtractionCandidateRow(Base):
    __tablename__ = "extraction_candidates"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    source: Mapped[dict | None] = mapped_column(JSON)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    review_note: Mapped[str | None] = mapped_column(Text)


class FactRow(Base):
    __tablename__ = "facts"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("extraction_candidates.id"))
    material: Mapped[str] = mapped_column(String(256), nullable=False)
    material_id: Mapped[str] = mapped_column(String(256), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sample: Mapped[str] = mapped_column(String(128), nullable=False)
    process: Mapped[str] = mapped_column(String(256), nullable=False)
    temperature_c: Mapped[float | None] = mapped_column(Float)
    duration_h: Mapped[float | None] = mapped_column(Float)
    property: Mapped[str] = mapped_column(String(256), nullable=False)
    effect_direction: Mapped[str] = mapped_column(String(32), nullable=False)
    effect_value: Mapped[float | None] = mapped_column(Float)
    effect_unit: Mapped[str | None] = mapped_column(String(32))
    result_value: Mapped[float | None] = mapped_column(Float)
    result_unit: Mapped[str | None] = mapped_column(String(32))
    lab: Mapped[str] = mapped_column(String(256), nullable=False)
    team: Mapped[str] = mapped_column(String(256), nullable=False)
    equipment: Mapped[str | None] = mapped_column(String(256))
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_hypothesis: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[dict] = mapped_column(JSON, nullable=False)


class VectorRow(Base):
    __tablename__ = "fragment_vectors"

    fragment_id: Mapped[str] = mapped_column(ForeignKey("source_fragments.id"), primary_key=True)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(JSON, nullable=False)
    vector_metadata: Mapped[dict] = mapped_column(JSON, default=dict)


class OntologyVersionRow(Base):
    __tablename__ = "ontology_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    config: Mapped[dict] = mapped_column(JSON, nullable=False)


class JobRow(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
