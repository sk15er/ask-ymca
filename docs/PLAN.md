# Ask YMCA implementation plan

## Repository inspection at initial plan draft

The initial planning review found the project specification in `prompt.md` and the
HTML design draft was provided afterward. That HTML's demo data and mock answer
logic are not sources of university facts. The current implementation status is
recorded at the end of this plan.

## Architecture

- **API:** FastAPI under `/api/v1`, with Pydantic request/response and extracted-fact schemas. Public and authenticated admin routes will be separated.
- **Persistence:** PostgreSQL via SQLAlchemy and Alembic. Store source documents and immutable versions alongside extracted structured records, chunks, feedback, cache entries, ingestion logs, and audit events. Use PostgreSQL full-text search and pgvector; do not add a separate vector database, Redis, or Celery.
- **Local model providers:** Define provider interfaces for generation and embeddings, backed by Ollama adapters selected through environment configuration. Keep structured lookups independent of these adapters. Before choosing defaults, check Ollama’s model registry for the requested Qwen3.5-4B and EmbeddingGemma models; if unavailable, choose close local alternatives and document the choice. Thinking mode defaults off.
- **Ingestion:** A configured registry in `config/sources.yaml` drives a targeted crawler. It checks robots.txt, restricts requests to approved university domains, avoids login/ERP paths, rate-limits per domain, records failures without aborting a crawl, and tracks ETag, Last-Modified, content hashes, and immutable document versions.
- **Extraction and retrieval:** Extract HTML/PDF content with page, heading, and table context; chunk at the specified 400–600 tokens with overlap and source metadata. Structured facts remain unverified until explicitly approved. Rules/explanations use hybrid FTS and vector retrieval, metadata filters, and a future reranker hook.
- **Question routing:** Normalize English, Hindi, and Roman Hindi queries while retaining the original. Route verified FAQs/cache first, then structured SQL lookups, then grounded RAG. Refuse off-topic prompts without invoking a model. Low-confidence or unsupported questions return an explicit inability to verify and, when available, the relevant office.
- **Grounding and safety:** Treat retrieved material as untrusted data. The backend validates every generated citation ID against retrieved results and supplies source URLs itself; the model cannot generate URLs. Conflicting documents are surfaced rather than silently resolved by recency. Facts require approved evidence.
- **Web UI:** Serve an API-backed chat at `/`, and do not treat demo facts from the separate HTML draft as real data.
- **Operations:** Docker Compose runs the API and PostgreSQL/pgvector. The API connects to the locally installed Ollama model. Add health checks, bounded upload handling, CORS/rate limits, API documentation, and admin authentication without exposing admin operations publicly.

## Data flow

1. A scheduled or admin-triggered crawl reads enabled source definitions, checks robots policy and domain/path allowlists, and fetches listing pages with per-domain limits and retry bounds.
2. Listing entries are classified by relevance. Relevant attachments are fetched and parsed; irrelevant procurement/recruitment/tender entries retain only their listing metadata. Each fetched item is content-hashed and stored as a new immutable version when changed.
3. Extraction stores source text and metadata, identifies candidate structured facts, and indexes document chunks. Extracted facts are validated against Pydantic schemas and remain unverified pending admin approval.
4. An incoming question is normalized and routed. Structured datesheets, notices, papers, offices, calendars, and procedures use SQL and do not require the LLM. Rule/explanation questions retrieve a bounded set of candidates with metadata filters before local generation.
5. Before returning a RAG answer, the backend validates cited source IDs against the retrieved set and builds citations from stored metadata. Unsupported answers fall back honestly. Cache entries are keyed by normalized query, language, programme, semester, and knowledge version, and are invalidated when supporting documents change state or are superseded.
6. Feedback and unanswered questions are recorded for evaluation; admin actions and knowledge changes are audited.

## Phased delivery and gates

Each phase will be implemented and tested independently, with README examples updated and remaining gaps listed before requesting review.

1. **Skeleton, database, config, health:** establish the Python application, settings, DB models/migrations, health endpoint, container setup, and focused tests.
2. **Crawler and source registry:** discover real source URLs from the university site, define the registry and synonym configuration, then implement scoped, policy-compliant crawling and tests.
3. **Extraction, versioning, structured storage:** implement HTML/PDF extraction, immutable versions, chunk metadata, structured records, approval states, and schema validation.
4. **Embeddings and hybrid search:** add local embedding integration, pgvector/FTS indexing and retrieval, metadata filters, and retrieval tests.
5. **LLM provider and grounded answers:** implement Ollama generation, citation validation, untrusted-document handling, conflict and low-evidence behavior, and provider-independent tests.
6. **Cache, router, and API:** add cache invalidation, route selection, public endpoints, feedback, safeguards, and API tests.
7. **HTML integration:** obtain and inspect the existing HTML, preserve its design, and replace demo data/mock replies with API calls.
8. **Admin and evaluation:** add authenticated administration, ingestion/approval workflows, analytics and evaluation; build at least 50 multilingual and edge-case questions and run the acceptance checks.

## Assumptions

- Python 3.11 and the stack in `prompt.md` are the baseline; dependencies will be selected in Phase 1 and pinned appropriately.
- PostgreSQL with pgvector is available through the development Compose setup; production hosting and operational sizing are outside this initial plan.
- University pages and documents are public unless discovered otherwise. The crawler will not attempt authentication or bypass access controls.
- Only explicitly approved, relevant source domains and paths will be fetched. Discovered URLs must be verified from actual university navigation rather than inferred from names.
- No production facts will be seeded. Synthetic data, if needed, will be clearly marked and restricted to tests.
- Admin authentication mechanism and secrets are environment-configured; secrets are never committed.
- The UI is an integration target, not a source of truth for university facts.

## Open questions / blockers

1. What admin authentication should be used for deployment (for example, a single environment-configured admin credential or integration with an existing identity provider)? Until decided, Phase 1 can define a replaceable boundary but should not claim production-ready admin security.
2. Which deployment environment and domain should the eventual deployment target? This affects production CORS origins, rate limits, and operational configuration; local development can proceed without it.
3. The Ollama model names and local availability will be confirmed against the registry before provider defaults are finalized. The user reports that Ollama and models are installed locally.
4. University source URLs, robots policies, and page behavior will be confirmed during Phase 2 before being enabled. This plan does not assume or seed unverified URLs.

## Implementation update

The core local application and university-source RAG path are implemented:

- The root page serves an API-backed chat, with a source-management panel for
  authenticated crawling, document review, final approval, and tentative approval.
- The source registry contains university pages verified from the live university
  navigation. The crawler respects the host allowlist and robots.txt, extracts
  HTML/PDF text and PDF page numbers, tracks content versions, and creates chunks.
- The API stores 768-dimensional local Ollama embeddings in pgvector and combines
  vector similarity with lexical search. Answers are generated from retrieved
  official evidence, with citations validated and URLs supplied by the API.
- `FINAL`, `REVISED`, and `TENTATIVE` sources can be used. The model is instructed
  to explicitly label tentative information; superseded final/revised sources are
  excluded. Answers without evidence or during model outages are not fabricated.
- Answer caching is keyed by question, language, programme, semester, and the
  retrieved source-version fingerprint. Admin status changes clear the cache.
- Compose applies database migrations and connects to the locally installed
  `qwen2.5:1.5b` and `embeddinggemma` Ollama models in the current Linux setup.
- Tests cover the homepage, agent readiness, crawl extraction and URL controls,
  admin authorization, answer citations, caching, tentative evidence, no-evidence
  behavior, model outages, and superseded documents.

The initial official-source crawl and manual content review are operational steps:
newly discovered or changed documents remain `NEEDS_REVIEW` until an administrator
checks them. No source is silently treated as verified. A recurring crawler,
bounded uploads, automated structured-fact extraction, production identity and
rate-limiting, and a multilingual acceptance corpus remain future work.

The admin crawl endpoint returns immediately and reports progress through
`GET /api/v1/admin/crawl/status`. Each listing is capped at six relevant PDFs per
run; crawls are in-memory single-process jobs and are interrupted by an API restart.
