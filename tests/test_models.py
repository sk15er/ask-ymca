from ask_ymca.models import Base


def test_schema_includes_planned_phase_one_tables():
    assert {
        "documents",
        "document_versions",
        "document_chunks",
        "notices",
        "deadlines",
        "exams",
        "papers",
        "rules",
        "procedures",
        "offices",
        "quick_questions",
        "answer_cache",
        "feedback",
        "ingestion_logs",
        "audit_log",
    } <= set(Base.metadata.tables)


def test_document_status_is_constrained_to_supported_values():
    document_table = Base.metadata.tables["documents"]
    status_check = next(
        constraint
        for constraint in document_table.constraints
        if constraint.name == "ck_documents_status"
    )

    assert "NEEDS_REVIEW" in str(status_check.sqltext)
    assert "WITHDRAWN" in str(status_check.sqltext)
