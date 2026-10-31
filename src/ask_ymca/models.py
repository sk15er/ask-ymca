from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Document(Timestamped, Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('TENTATIVE', 'FINAL', 'REVISED', 'WITHDRAWN', 'ARCHIVED', 'NEEDS_REVIEW')",
            name="ck_documents_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_name: Mapped[str] = mapped_column(String(160), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="NEEDS_REVIEW")
    published_date: Mapped[date | None] = mapped_column(Date)
    programme: Mapped[str | None] = mapped_column(String(160))
    semester: Mapped[str | None] = mapped_column(String(40))
    session_name: Mapped[str | None] = mapped_column(String(80))
    supersedes_document_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id")
    )
    current_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "document_versions.id",
            use_alter=True,
            name="fk_documents_current_version_id_document_versions",
        ),
    )
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )


class DocumentVersion(Timestamped, Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version_number"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    etag: Mapped[str | None] = mapped_column(String(512))
    last_modified: Mapped[str | None] = mapped_column(String(200))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    extracted_text: Mapped[str | None] = mapped_column(Text)
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )


class DocumentChunk(Timestamped, Base):
    __tablename__ = "document_chunks"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(String(500))
    token_count: Mapped[int | None] = mapped_column(Integer)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(768).with_variant(JSON(), "sqlite")
    )
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )


class Notice(Base):
    __tablename__ = "notices"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    published_date: Mapped[date | None] = mapped_column(Date)
    summary: Mapped[str | None] = mapped_column(Text)


class Deadline(Base):
    __tablename__ = "deadlines"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    verification_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="UNVERIFIED"
    )
    programme: Mapped[str | None] = mapped_column(String(160))
    semester: Mapped[str | None] = mapped_column(String(40))
    session_name: Mapped[str | None] = mapped_column(String(80))


class Exam(Base):
    __tablename__ = "exams"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    exam_date: Mapped[date | None] = mapped_column(Date)
    programme: Mapped[str | None] = mapped_column(String(160))
    semester: Mapped[str | None] = mapped_column(String(40))
    session_name: Mapped[str | None] = mapped_column(String(80))
    verification_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="UNVERIFIED"
    )


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    programme: Mapped[str | None] = mapped_column(String(160))
    semester: Mapped[str | None] = mapped_column(String(40))
    subject: Mapped[str | None] = mapped_column(String(200))
    session_name: Mapped[str | None] = mapped_column(String(80))
    verification_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="UNVERIFIED"
    )


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="UNVERIFIED"
    )


class Procedure(Base):
    __tablename__ = "procedures"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    office_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("offices.id"))
    verification_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="UNVERIFIED"
    )


class Office(Timestamped, Base):
    __tablename__ = "offices"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(320))
    address: Mapped[str | None] = mapped_column(Text)
    source_document_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id")
    )
    verification_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="UNVERIFIED"
    )


class QuickQuestion(Base):
    __tablename__ = "quick_questions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(20), nullable=False, default="en")
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_document_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id")
    )


class AnswerCache(Timestamped, Base):
    __tablename__ = "answer_cache"
    __table_args__ = (
        UniqueConstraint(
            "normalized_query", "language", "programme", "semester", "knowledge_version"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    normalized_query: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(20), nullable=False)
    programme: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    semester: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    knowledge_version: Mapped[str] = mapped_column(String(64), nullable=False)
    answer_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    supporting_document_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class Feedback(Timestamped, Base):
    __tablename__ = "feedback"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[int | None] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)


class IngestionLog(Base):
    __tablename__ = "ingestion_logs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    items_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_changed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
