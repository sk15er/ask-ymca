import hashlib
import logging
import re
import secrets
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import UUID

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, exists, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from ask_ymca.assistant import (
    AgentUnavailable,
    check_ollama,
    generate_answer,
    generate_embeddings,
)
from ask_ymca.config import get_settings
from ask_ymca.crawler import CrawlSummary, crawl_university
from ask_ymca.db import SessionLocal, get_db
from ask_ymca.models import (
    AnswerCache,
    AuditLog,
    Document,
    DocumentChunk,
    DocumentVersion,
    Feedback,
    IngestionLog,
    Office,
    QuickQuestion,
)

router = APIRouter(prefix="/api/v1")
logger = logging.getLogger(__name__)
_crawl_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="university-crawl")
_crawl_future: Future[CrawlSummary] | None = None
_crawl_future_lock = Lock()
STOP_WORDS = {
    "about",
    "after",
    "are",
    "can",
    "could",
    "does",
    "for",
    "from",
    "have",
    "how",
    "into",
    "please",
    "should",
    "that",
    "the",
    "their",
    "this",
    "what",
    "when",
    "where",
    "which",
    "who",
    "with",
    "would",
}
MAX_SOURCES = 5
OFF_TOPIC = re.compile(
    r"\b(cricket|football|movie|movies|joke|poem|essay|weather|recipe|"
    r"write me|write a|code for|stock price)\b",
    re.I,
)


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    programme: str | None = Field(default=None, max_length=160)
    semester: str | None = Field(default=None, max_length=40)


class AnswerSource(BaseModel):
    id: UUID
    title: str
    url: str
    page: int | None
    excerpt: str
    date: date | None
    status: str


class AskResponse(BaseModel):
    answer: str
    answer_type: Literal["grounded", "not_found", "model_unavailable", "off_topic"]
    language: Literal["en", "hi", "hinglish"]
    confidence: float
    cache_hit: bool
    agent_status: Literal["ready", "unavailable", "no_evidence"]
    sources: list[AnswerSource]
    suggested_questions: list[str]


class DocumentStatusUpdate(BaseModel):
    status: Literal[
        "FINAL", "REVISED", "TENTATIVE", "ARCHIVED", "WITHDRAWN", "NEEDS_REVIEW"
    ]


class FeedbackRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    answer: str | None = Field(default=None, max_length=5000)
    rating: int | None = Field(default=None, ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)


def _query_terms(question: str) -> list[str]:
    terms = re.findall(r"[\w]{3,}", question.casefold())
    terms = [term for term in terms if term not in STOP_WORDS]
    normalized = f" {question.casefold()} "
    for canonical, aliases in _synonyms().items():
        if any(f" {alias.casefold()} " in normalized for alias in aliases):
            terms.extend(re.findall(r"[\w]{3,}", canonical.casefold()))
    return list(dict.fromkeys(terms))


@lru_cache
def _synonyms() -> dict[str, list[str]]:
    config_path = Path(__file__).parents[2] / "config" / "synonyms.yaml"
    if not config_path.is_file():
        config_path = Path.cwd() / "config" / "synonyms.yaml"
    with config_path.open(encoding="utf-8") as synonyms_file:
        config = yaml.safe_load(synonyms_file)
    return config["synonyms"]


def _language(question: str) -> Literal["en", "hi", "hinglish"]:
    if re.search(r"[\u0900-\u097f]", question):
        return "hi"
    if re.search(r"\b(kya|kaise|kab|kahan|hai|hain|mujhe|chahiye|kitna|kitni)\b", question, re.I):
        return "hinglish"
    return "en"


def _find_evidence(
    db: Session, request: AskRequest
) -> list[tuple[Document, DocumentVersion, DocumentChunk]]:
    terms = _query_terms(request.question)
    if not terms:
        return []

    predicates = [
        DocumentChunk.content.ilike(f"%{term}%") | Document.title.ilike(f"%{term}%")
        for term in terms
    ]
    superseding_document = aliased(Document)
    base_statement = (
        select(Document, DocumentVersion, DocumentChunk)
        .join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
        .join(DocumentChunk, DocumentChunk.document_version_id == DocumentVersion.id)
        .where(
            Document.status.in_(("FINAL", "REVISED", "TENTATIVE")),
            Document.current_version_id.is_not(None),
            ~exists(
                select(1)
                .select_from(superseding_document)
                .where(
                    superseding_document.supersedes_document_id == Document.id,
                    superseding_document.status.in_(("FINAL", "REVISED")),
                )
            ),
        )
    )
    if request.programme:
        base_statement = base_statement.where(
            (Document.programme.is_(None)) | (Document.programme == request.programme)
        )
    if request.semester:
        base_statement = base_statement.where(
            (Document.semester.is_(None)) | (Document.semester == request.semester)
        )

    lexical_matches = db.execute(
        base_statement.where(or_(*predicates)).limit(100)
    ).all()
    matches_by_chunk = {
        row[2].id: (row, 0)
        for row in lexical_matches
    }

    if db.get_bind().dialect.name == "postgresql":
        try:
            query_embedding = generate_embeddings([request.question], get_settings())[0]
        except AgentUnavailable as exc:
            logger.warning("Semantic retrieval is unavailable; using text matches: %s", exc)
        else:
            semantic_matches = db.execute(
                base_statement.where(DocumentChunk.embedding.is_not(None))
                .order_by(DocumentChunk.embedding.cosine_distance(query_embedding))
                .limit(20)
            ).all()
            for rank, row in enumerate(semantic_matches):
                previous = matches_by_chunk.get(row[2].id)
                semantic_score = 5 - rank * 0.1
                matches_by_chunk[row[2].id] = (
                    row,
                    (previous[1] if previous else 0) + semantic_score,
                )

    def lexical_score(row: tuple[Document, DocumentVersion, DocumentChunk]) -> int:
        document, _, chunk = row
        title = document.title.casefold()
        content = chunk.content.casefold()
        return sum(
            (3 if term in title else 0) + content.count(term)
            for term in terms
        ) + (5 if request.question.casefold() in content else 0)

    ranked_matches = sorted(
        matches_by_chunk.values(),
        key=lambda match: (lexical_score(match[0]) + match[1]),
        reverse=True,
    )
    selected: list[tuple[Document, DocumentVersion, DocumentChunk]] = []
    seen_documents: set[UUID] = set()
    for row, _ in ranked_matches:
        if row[0].id in seen_documents:
            continue
        selected.append(row)
        seen_documents.add(row[0].id)
        if len(selected) == MAX_SOURCES:
            break
    return selected


def _source(document: Document, chunk: DocumentChunk) -> AnswerSource:
    return AnswerSource(
        id=document.id,
        title=document.title,
        url=document.source_url,
        page=chunk.page_number,
        excerpt=chunk.content[:500],
        date=document.published_date,
        status=document.status,
    )


def _require_admin(authorization: str | None) -> str:
    token = get_settings().admin_token
    if not token:
        raise HTTPException(
            status_code=503,
            detail="Admin API is disabled until ASK_YMCA_ADMIN_TOKEN is configured.",
        )
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.casefold() != "bearer" or not secrets.compare_digest(supplied, token):
        raise HTTPException(status_code=401, detail="Invalid or missing admin token.")
    return "admin"


def _document_payload(document: Document, excerpt: str | None = None) -> dict:
    return {
        "id": str(document.id),
        "title": document.title,
        "category": document.category,
        "url": document.source_url,
        "date": document.published_date,
        "status": document.status,
        "excerpt": excerpt,
        "programme": document.programme,
        "semester": document.semester,
    }


@router.get("/health", tags=["health"])
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"status": "unavailable", "database": "unavailable"},
        ) from exc
    return {"status": "ok", "database": "ok"}


@router.get("/agent/status", tags=["assistant"])
def agent_status(db: Session = Depends(get_db)) -> dict[str, str | bool | int]:
    status = check_ollama(get_settings())
    superseding_document = aliased(Document)
    indexed_documents = db.scalar(
        select(func.count(Document.id)).where(
            Document.status.in_(("FINAL", "REVISED", "TENTATIVE")),
            Document.current_version_id.is_not(None),
            exists(
                select(1).where(
                    DocumentChunk.document_version_id == Document.current_version_id
                )
            ),
            ~exists(
                select(1)
                .select_from(superseding_document)
                .where(
                    superseding_document.supersedes_document_id == Document.id,
                    superseding_document.status.in_(("FINAL", "REVISED")),
                )
            ),
        )
    ) or 0
    status["indexed_documents"] = indexed_documents
    status["knowledge_ready"] = indexed_documents > 0
    return status


def _run_crawl() -> CrawlSummary:
    with SessionLocal() as db:
        return crawl_university(db)


@router.post(
    "/admin/crawl",
    tags=["admin"],
    status_code=status.HTTP_202_ACCEPTED,
)
def start_crawl(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict:
    global _crawl_future
    _require_admin(authorization)
    with _crawl_future_lock:
        if _crawl_future is not None and not _crawl_future.done():
            raise HTTPException(
                status_code=409,
                detail="A university source crawl is already running.",
            )
        db.execute(
            update(IngestionLog)
            .where(IngestionLog.status == "RUNNING")
            .values(
                status="FAILED",
                completed_at=datetime.now(UTC),
                error_message="Interrupted by API restart before this crawl completed.",
            )
        )
        db.commit()
        try:
            _crawl_future = _crawl_executor.submit(_run_crawl)
        except RuntimeError as exc:
            logger.exception("Unable to start the university source crawl")
            raise HTTPException(status_code=503, detail="The crawl worker is unavailable.") from exc
    return {"status": "started"}


@router.get("/admin/crawl/status", tags=["admin"])
def crawl_status(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict:
    _require_admin(authorization)
    with _crawl_future_lock:
        future = _crawl_future
        if future is None:
            crawl_state = "idle"
            summary = None
            error = None
        elif not future.done():
            crawl_state = "running"
            summary = None
            error = None
        else:
            error = future.exception()
            if error is not None:
                crawl_state = "failed"
                summary = None
                error = str(error)
            else:
                result = future.result()
                crawl_state = "completed"
                summary = {
                    "sources": result.sources,
                    "documents_seen": result.seen,
                    "documents_changed": result.changed,
                    "documents_skipped": result.skipped,
                    "errors": result.errors,
                }
                error = None
    if future is None or future.done():
        interrupted = db.execute(
            update(IngestionLog)
            .where(IngestionLog.status == "RUNNING")
            .values(
                status="FAILED",
                completed_at=datetime.now(UTC),
                error_message="Crawl interrupted before this source completed.",
            )
        ).rowcount
        if interrupted:
            db.commit()
    logs = db.scalars(
        select(IngestionLog).order_by(IngestionLog.started_at.desc()).limit(10)
    ).all()
    return {
        "status": crawl_state,
        "summary": summary,
        "error": error,
        "sources": [
            {
                "name": log.source_name,
                "status": log.status,
                "items_seen": log.items_seen,
                "items_changed": log.items_changed,
                "error": log.error_message,
            }
            for log in logs
        ],
    }


@router.get("/admin/documents", tags=["admin"])
def review_documents(
    status: Literal[
        "NEEDS_REVIEW", "FINAL", "REVISED", "TENTATIVE", "WITHDRAWN", "ARCHIVED"
    ] = "NEEDS_REVIEW",
    limit: int = 100,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> list[dict]:
    _require_admin(authorization)
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    documents = db.scalars(
        select(Document)
        .where(Document.status == status)
        .order_by(Document.published_date.desc().nullslast(), Document.title)
        .limit(limit)
    ).all()
    current_versions = {
        document.current_version_id for document in documents if document.current_version_id
    }
    excerpts: dict[UUID, str] = {}
    if current_versions:
        rows = db.execute(
            select(DocumentChunk.document_version_id, DocumentChunk.content)
            .where(DocumentChunk.document_version_id.in_(current_versions))
            .order_by(DocumentChunk.chunk_index)
        ).all()
        for version_id, content in rows:
            excerpts.setdefault(version_id, content[:350])
    return [
        _document_payload(
            document,
            excerpts.get(document.current_version_id),
        )
        for document in documents
    ]


@router.post("/admin/documents/{document_id}/status", tags=["admin"])
def set_document_status(
    document_id: UUID,
    update: DocumentStatusUpdate,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict:
    actor = _require_admin(authorization)
    document = db.get(Document, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found.")
    old_status = document.status
    document.status = update.status
    db.add(
        AuditLog(
            actor=actor,
            action="document_status_updated",
            entity_type="document",
            entity_id=str(document.id),
            details={"from": old_status, "to": update.status},
        )
    )
    db.execute(delete(AnswerCache))
    db.commit()
    return _document_payload(document)


@router.get("/admin/ingestion", tags=["admin"])
def ingestion_history(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> list[dict]:
    _require_admin(authorization)
    logs = db.scalars(
        select(IngestionLog).order_by(IngestionLog.started_at.desc()).limit(50)
    ).all()
    return [
        {
            "source": log.source_name,
            "status": log.status,
            "started_at": log.started_at,
            "completed_at": log.completed_at,
            "items_seen": log.items_seen,
            "items_changed": log.items_changed,
            "error": log.error_message,
        }
        for log in logs
    ]


@router.get("/documents/{document_id}", tags=["documents"])
def get_document(document_id: UUID, db: Session = Depends(get_db)) -> dict:
    document = db.get(Document, document_id)
    if not document or document.status not in ("FINAL", "REVISED", "TENTATIVE"):
        raise HTTPException(status_code=404, detail="Approved document not found.")
    chunks = db.scalars(
        select(DocumentChunk)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .where(DocumentVersion.id == document.current_version_id)
        .order_by(DocumentChunk.chunk_index)
    ).all()
    result = _document_payload(document)
    result["chunks"] = [
        {"page": chunk.page_number, "text": chunk.content} for chunk in chunks
    ]
    return result


@router.get("/quick-questions", tags=["knowledge"])
def quick_questions(db: Session = Depends(get_db)) -> list[dict]:
    questions = db.scalars(
        select(QuickQuestion).where(QuickQuestion.verified.is_(True)).limit(100)
    ).all()
    return [{"question": item.question, "answer": item.answer} for item in questions]


@router.get("/notices", tags=["knowledge"])
def notices(db: Session = Depends(get_db)) -> list[dict]:
    documents = db.scalars(
        select(Document)
        .where(
            Document.status.in_(("FINAL", "REVISED", "TENTATIVE")),
            Document.category.in_(("notices", "examinations")),
        )
        .order_by(Document.published_date.desc().nullslast())
        .limit(100)
    ).all()
    return [_document_payload(document) for document in documents]


@router.get("/calendar", tags=["knowledge"])
def calendar(db: Session = Depends(get_db)) -> list[dict]:
    documents = db.scalars(
        select(Document)
        .where(
            Document.status.in_(("FINAL", "REVISED", "TENTATIVE")),
            Document.category == "calendar",
        )
        .order_by(Document.published_date.desc().nullslast())
        .limit(100)
    ).all()
    return [_document_payload(document) for document in documents]


@router.get("/papers", tags=["knowledge"])
def papers(db: Session = Depends(get_db)) -> list[dict]:
    documents = db.scalars(
        select(Document)
        .where(
            Document.status.in_(("FINAL", "REVISED", "TENTATIVE")),
            Document.category.in_(("papers", "academics")),
        )
        .order_by(Document.title)
        .limit(100)
    ).all()
    return [_document_payload(document) for document in documents]


@router.get("/procedures", tags=["knowledge"])
def procedures(db: Session = Depends(get_db)) -> list[dict]:
    documents = db.scalars(
        select(Document)
        .where(
            Document.status.in_(("FINAL", "REVISED", "TENTATIVE")),
            Document.category == "procedures",
        )
        .order_by(Document.title)
        .limit(100)
    ).all()
    return [_document_payload(document) for document in documents]


@router.get("/offices", tags=["knowledge"])
def offices(db: Session = Depends(get_db)) -> list[dict]:
    records = db.scalars(
        select(Office)
        .where(Office.verification_status == "VERIFIED")
        .order_by(Office.name)
    ).all()
    return [
        {
            "name": office.name,
            "phone": office.phone,
            "email": office.email,
            "address": office.address,
        }
        for office in records
    ]


@router.post("/feedback", tags=["feedback"])
def submit_feedback(
    feedback: FeedbackRequest, db: Session = Depends(get_db)
) -> dict[str, str]:
    record = Feedback(**feedback.model_dump())
    db.add(record)
    db.commit()
    return {"status": "received"}


@router.post("/ask", response_model=AskResponse, tags=["assistant"])
def ask(request: AskRequest, db: Session = Depends(get_db)) -> AskResponse:
    language = _language(request.question)
    if OFF_TOPIC.search(request.question):
        return AskResponse(
            answer=(
                "I can help with J.C. Bose YMCA university information, such as notices, "
                "examinations, academics, rules, and campus services."
            ),
            answer_type="off_topic",
            language=language,
            confidence=1,
            cache_hit=False,
            agent_status="no_evidence",
            sources=[],
            suggested_questions=[],
        )
    matches = _find_evidence(db, request)
    if not matches:
        return AskResponse(
            answer=(
                "I couldn't verify this from the university sources available to Ask YMCA. "
                "Please check the official university website or contact the relevant office."
            ),
            answer_type="not_found",
            language=language,
            confidence=0,
            cache_hit=False,
            agent_status="no_evidence",
            sources=[],
            suggested_questions=[],
        )

    normalized_query = " ".join(request.question.casefold().split())
    knowledge_version = hashlib.sha256(
        "\n".join(
            sorted(
                f"{document.id}:{version.sha256}:{document.status}"
                for document, version, _ in matches
            )
        ).encode()
    ).hexdigest()
    cache_filters = {
        "normalized_query": normalized_query,
        "language": language,
        "programme": request.programme or "",
        "semester": request.semester or "",
        "knowledge_version": knowledge_version,
    }
    cached = db.scalar(select(AnswerCache).filter_by(**cache_filters))
    documents_by_id = {str(document.id): (document, chunk) for document, _, chunk in matches}
    if cached:
        cached_payload = cached.answer_payload
        cached_answer = cached_payload.get("answer") if isinstance(cached_payload, dict) else None
        cited_ids = cached_payload.get("source_ids") if isinstance(cached_payload, dict) else None
        if (
            isinstance(cached_answer, str)
            and cached_answer.strip()
            and isinstance(cited_ids, list)
            and cited_ids
            and all(isinstance(source_id, str) for source_id in cited_ids)
            and all(source_id in documents_by_id for source_id in cited_ids)
        ):
            return AskResponse(
                answer=cached_answer,
                answer_type="grounded",
                language=language,
                confidence=0.75,
                cache_hit=True,
                agent_status="ready",
                sources=[
                    _source(*documents_by_id[source_id])
                    for source_id in dict.fromkeys(cited_ids)
                ],
                suggested_questions=[],
            )
        logger.warning("Discarding an invalid cached answer")
        db.delete(cached)
        db.commit()

    evidence = [
        {
            "id": str(document.id),
            "title": document.title,
            "status": document.status,
            "text": chunk.content[:2000],
        }
        for document, _, chunk in matches
    ]
    try:
        result = generate_answer(request.question, evidence, get_settings())
    except AgentUnavailable as exc:
        logger.warning("Local answer model unavailable: %s", exc)
        return AskResponse(
            answer=(
                "I found relevant university documents, but the local answer model is "
                "unavailable. Open the sources below to verify the details."
            ),
            answer_type="model_unavailable",
            language=language,
            confidence=0,
            cache_hit=False,
            agent_status="unavailable",
            sources=[_source(document, chunk) for document, _, chunk in matches],
            suggested_questions=[],
        )

    cited_ids = result["source_ids"]
    if (
        not result["answer"].strip()
        or not cited_ids
        or any(source_id not in documents_by_id for source_id in cited_ids)
        or re.search(r"https?://|www\.", result["answer"], re.I)
    ):
        logger.warning("Rejected a model answer with missing or invalid citations")
        return AskResponse(
            answer=(
                "I couldn't verify a reliable answer in the retrieved sources. "
                "Please open the documents below or contact the relevant office."
            ),
            answer_type="not_found",
            language=language,
            confidence=0,
            cache_hit=False,
            agent_status="ready",
            sources=[_source(document, chunk) for document, _, chunk in matches],
            suggested_questions=[],
        )

    cited_sources = [
        _source(*documents_by_id[source_id])
        for source_id in dict.fromkeys(cited_ids)
    ]
    answer_text = result["answer"].strip()
    if any(source.status == "TENTATIVE" for source in cited_sources):
        disclaimer = {
            "en": "Tentative information (not final): ",
            "hi": "अस्थायी जानकारी (अंतिम नहीं): ",
            "hinglish": "Yeh jaankari tentative hai, final nahi: ",
        }[language]
        answer_text = f"{disclaimer}{answer_text}"

    response = AskResponse(
        answer=answer_text,
        answer_type="grounded",
        language=language,
        confidence=0.75,
        cache_hit=False,
        agent_status="ready",
        sources=cited_sources,
        suggested_questions=[],
    )
    db.add(
        AnswerCache(
            **cache_filters,
            answer_payload={
                "answer": response.answer,
                "source_ids": [str(source.id) for source in response.sources],
            },
            supporting_document_ids=[str(source.id) for source in response.sources],
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.scalar(select(AnswerCache).filter_by(**cache_filters)) is None:
            raise
    return response
