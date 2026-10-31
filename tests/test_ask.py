from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ask_ymca.api import _synonyms
from ask_ymca.assistant import AgentUnavailable
from ask_ymca.config import Settings
from ask_ymca.crawler import CrawlSummary
from ask_ymca.db import get_db
from ask_ymca.main import app
from ask_ymca.models import (
    AnswerCache,
    AuditLog,
    Base,
    Document,
    DocumentChunk,
    DocumentVersion,
)


@pytest.fixture
def api() -> Iterator[tuple[TestClient, Session]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield TestClient(app), session
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


def add_document(
    session: Session,
    *,
    title: str,
    text: str,
    canonical_url: str,
    supersedes: Document | None = None,
) -> Document:
    document = Document(
        source_name="University website",
        source_url=canonical_url,
        canonical_url=canonical_url,
        title=title,
        category="notice",
        status="FINAL",
        supersedes_document_id=supersedes.id if supersedes else None,
    )
    session.add(document)
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        version_number=1,
        sha256="a" * 64,
        extracted_text=text,
    )
    session.add(version)
    session.flush()
    document.current_version_id = version.id
    session.add(
        DocumentChunk(
            document_version_id=version.id,
            chunk_index=0,
            content=text,
            page_number=1,
        )
    )
    session.flush()
    return document


def test_root_serves_the_real_chat_page(api: tuple[TestClient, Session]):
    client, _ = api

    response = client.get("/")

    assert response.status_code == 200
    assert "Ask YMCA" in response.text
    assert "/api/v1/ask" in response.text


def test_synonyms_load_from_the_container_working_directory(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    (config_directory / "synonyms.yaml").write_text(
        "synonyms:\n  attendance: [hazri]\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "ask_ymca.api.__file__",
        "/usr/local/lib/python3.11/site-packages/ask_ymca/api.py",
    )
    _synonyms.cache_clear()

    try:
        assert _synonyms() == {"attendance": ["hazri"]}
    finally:
        _synonyms.cache_clear()


def test_agent_status_distinguishes_model_and_knowledge_readiness(
    api: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
):
    client, _ = api
    monkeypatch.setattr(
        "ask_ymca.api.check_ollama",
        lambda settings: {
            "configured": True,
            "available": True,
            "model": "local-test-model",
            "reason": "ready",
        },
    )

    response = client.get("/api/v1/agent/status")

    assert response.status_code == 200
    assert response.json()["available"] is True
    assert response.json()["indexed_documents"] == 0
    assert response.json()["knowledge_ready"] is False


def test_admin_review_and_approval_require_token_and_record_audit(
    api: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
):
    client, session = api
    document = add_document(
        session,
        title="Official examination notice",
        text="Official exam announcement.",
        canonical_url="https://jcboseust.ac.in/official-exam-notice",
    )
    document.status = "NEEDS_REVIEW"
    session.flush()
    monkeypatch.setattr(
        "ask_ymca.api.get_settings",
        lambda: Settings(admin_token="test-admin-token"),
    )

    unauthorized = client.get("/api/v1/admin/documents")
    review = client.get(
        "/api/v1/admin/documents",
        headers={"Authorization": "Bearer test-admin-token"},
    )
    approved = client.post(
        f"/api/v1/admin/documents/{document.id}/status",
        headers={"Authorization": "Bearer test-admin-token"},
        json={"status": "FINAL"},
    )

    assert unauthorized.status_code == 401
    assert review.status_code == 200
    assert review.json()[0]["excerpt"] == "Official exam announcement."
    assert approved.status_code == 200
    assert approved.json()["status"] == "FINAL"
    assert session.query(AuditLog).count() == 1


def test_admin_crawl_returns_while_background_job_runs(
    api: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
):
    import ask_ymca.api as api_module

    client, _ = api
    monkeypatch.setattr(
        "ask_ymca.api.get_settings",
        lambda: Settings(admin_token="test-admin-token"),
    )
    monkeypatch.setattr(
        api_module,
        "_run_crawl",
        lambda: CrawlSummary(sources=3, seen=4, changed=2, skipped=2),
    )

    response = client.post(
        "/api/v1/admin/crawl",
        headers={"Authorization": "Bearer test-admin-token"},
    )

    assert response.status_code == 202
    assert response.json() == {"status": "started"}
    assert api_module._crawl_future is not None
    api_module._crawl_future.result(timeout=2)
    status_response = client.get(
        "/api/v1/admin/crawl/status",
        headers={"Authorization": "Bearer test-admin-token"},
    )

    assert status_response.status_code == 200
    assert status_response.json()["status"] == "completed"
    assert status_response.json()["summary"]["documents_changed"] == 2


def test_ask_returns_honest_fallback_when_no_sources_are_indexed(
    api: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
):
    client, _ = api

    def unexpected_call(*args, **kwargs):
        pytest.fail("the model must not be called without evidence")

    monkeypatch.setattr("ask_ymca.api.generate_answer", unexpected_call)
    response = client.post("/api/v1/ask", json={"question": "When is the exam deadline?"})

    assert response.status_code == 200
    assert response.json()["answer_type"] == "not_found"
    assert response.json()["agent_status"] == "no_evidence"
    assert response.json()["sources"] == []


def test_off_topic_questions_do_not_reach_retrieval_or_model(
    api: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
):
    client, _ = api
    monkeypatch.setattr(
        "ask_ymca.api._find_evidence",
        lambda *args: pytest.fail("off-topic question should be refused before retrieval"),
    )

    response = client.post("/api/v1/ask", json={"question": "Tell me a cricket joke"})

    assert response.status_code == 200
    assert response.json()["answer_type"] == "off_topic"


def test_ask_returns_only_validated_citations(
    api: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
):
    client, session = api
    document = add_document(
        session,
        title="Examination notice",
        text="The examination application deadline is 15 November.",
        canonical_url="https://jcboseust.ac.in/examination-notice",
    )
    monkeypatch.setattr(
        "ask_ymca.api.generate_answer",
        lambda question, evidence, settings: {
            "answer": "The deadline is 15 November.",
            "source_ids": [evidence[0]["id"]],
        },
    )

    response = client.post(
        "/api/v1/ask",
        json={"question": "What is the examination application deadline?"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["answer_type"] == "grounded"
    assert result["sources"][0]["id"] == str(document.id)
    assert result["sources"][0]["url"] == document.source_url
    assert result["sources"][0]["status"] == "FINAL"


def test_ask_sends_only_one_best_chunk_per_document(
    api: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
):
    client, session = api
    document = add_document(
        session,
        title="Examination notice",
        text="The examination notice contains the university timetable.",
        canonical_url="https://jcboseust.ac.in/examination-notice",
    )
    version = session.query(DocumentVersion).filter_by(document_id=document.id).one()
    session.add(
        DocumentChunk(
            document_version_id=version.id,
            chunk_index=1,
            content="The examination notice gives the revised examination timetable.",
            page_number=2,
        )
    )
    session.flush()
    captured_evidence = []

    def answer(question, evidence, settings):
        captured_evidence.extend(evidence)
        return {
            "answer": "The notice contains the examination timetable.",
            "source_ids": [evidence[0]["id"]],
        }

    monkeypatch.setattr("ask_ymca.api.generate_answer", answer)
    response = client.post(
        "/api/v1/ask",
        json={"question": "What does the examination notice say about the timetable?"},
    )

    assert response.status_code == 200
    assert len(captured_evidence) == 1
    assert captured_evidence[0]["id"] == str(document.id)


def test_ask_does_not_return_model_answers_with_unknown_citations(
    api: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
):
    client, session = api
    add_document(
        session,
        title="Attendance policy",
        text="Attendance requirements are described in the official policy.",
        canonical_url="https://jcboseust.ac.in/attendance-policy",
    )
    monkeypatch.setattr(
        "ask_ymca.api.generate_answer",
        lambda question, evidence, settings: {
            "answer": "The rule is 75%.",
            "source_ids": ["not-a-retrieved-source"],
        },
    )

    response = client.post("/api/v1/ask", json={"question": "What attendance policy?"})

    assert response.status_code == 200
    assert response.json()["answer_type"] == "not_found"
    assert response.json()["confidence"] == 0


def test_ask_caches_grounded_answers_until_source_version_changes(
    api: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
):
    client, session = api
    document = add_document(
        session,
        title="Examination notice",
        text="The examination application deadline is 15 November.",
        canonical_url="https://jcboseust.ac.in/examination-notice",
    )
    calls = 0

    def answer(question, evidence, settings):
        nonlocal calls
        calls += 1
        return {
            "answer": "The deadline is 15 November.",
            "source_ids": [evidence[0]["id"]],
        }

    monkeypatch.setattr("ask_ymca.api.generate_answer", answer)
    first = client.post(
        "/api/v1/ask",
        json={"question": "What is the examination application deadline?"},
    )
    second = client.post(
        "/api/v1/ask",
        json={"question": "What is the examination application deadline?"},
    )

    assert first.json()["cache_hit"] is False
    assert second.json()["cache_hit"] is True
    assert calls == 1
    assert session.query(AnswerCache).count() == 1

    version = session.query(DocumentVersion).filter_by(document_id=document.id).one()
    version.sha256 = "b" * 64
    session.flush()
    third = client.post(
        "/api/v1/ask",
        json={"question": "What is the examination application deadline?"},
    )
    assert third.json()["cache_hit"] is False
    assert calls == 2


def test_tentative_sources_are_retrievable_and_cited_as_tentative(
    api: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
):
    client, session = api
    document = add_document(
        session,
        title="Tentative examination notice",
        text="The tentative examination schedule is published for review.",
        canonical_url="https://jcboseust.ac.in/tentative-examination-notice",
    )
    document.status = "TENTATIVE"
    monkeypatch.setattr(
        "ask_ymca.api.generate_answer",
        lambda question, evidence, settings: {
            "answer": "The published schedule covers the stated dates.",
            "source_ids": [evidence[0]["id"]],
        },
    )

    response = client.post(
        "/api/v1/ask",
        json={"question": "What is the tentative examination schedule?"},
    )

    assert response.status_code == 200
    assert response.json()["answer_type"] == "grounded"
    assert response.json()["answer"].startswith("Tentative information (not final):")
    assert response.json()["sources"][0]["id"] == str(document.id)
    assert response.json()["sources"][0]["status"] == "TENTATIVE"


def test_ask_falls_back_to_sources_when_local_model_is_unavailable(
    api: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
):
    client, session = api
    add_document(
        session,
        title="Examination notice",
        text="The examination notice contains the official timetable.",
        canonical_url="https://jcboseust.ac.in/examination-timetable",
    )

    def unavailable(*args, **kwargs):
        raise AgentUnavailable("test model outage")

    monkeypatch.setattr("ask_ymca.api.generate_answer", unavailable)
    response = client.post(
        "/api/v1/ask",
        json={"question": "What is in the examination timetable?"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["answer_type"] == "model_unavailable"
    assert result["agent_status"] == "unavailable"
    assert result["sources"][0]["title"] == "Examination notice"


def test_superseded_documents_are_not_used_as_evidence(
    api: tuple[TestClient, Session], monkeypatch: pytest.MonkeyPatch
):
    client, session = api
    old_document = add_document(
        session,
        title="Old scholarship deadline",
        text="Scholarship submissions close on 1 October.",
        canonical_url="https://jcboseust.ac.in/scholarship-old",
    )
    new_document = add_document(
        session,
        title="Revised scholarship notice",
        text="The revised scholarship notice contains the current instructions.",
        canonical_url="https://jcboseust.ac.in/scholarship-revised",
        supersedes=old_document,
    )
    session.flush()
    monkeypatch.setattr(
        "ask_ymca.api.generate_answer",
        lambda question, evidence, settings: {
            "answer": "The revised notice has the instructions.",
            "source_ids": [evidence[0]["id"]],
        },
    )

    response = client.post(
        "/api/v1/ask",
        json={"question": "What is the scholarship deadline?"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["answer_type"] == "grounded"
    assert [source["id"] for source in result["sources"]] == [str(new_document.id)]
