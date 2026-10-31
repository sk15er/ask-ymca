# Ask YMCA: local-AI university information backend

## Goal
Build the backend for "Ask YMCA", a student information assistant for J.C. Bose University of Science & Technology, YMCA (https://jcboseust.ac.in/). The university website is scattered and hard to search. Students should get fast, sourced answers about notices, datesheets, papers, rules, procedures and contacts.

A mobile-first HTML UI already exists (see ./ask-ymca-v3.html). Keep its design. Replace its hardcoded demo data and mock reply() logic with API calls. All dates, fees, rules and contacts in that HTML are DEMO DATA, not real facts.

## Principles (non-negotiable)
1. Structured DB first, RAG second. Use SQL for dates, notices, contacts and papers. Use RAG only for rules and explanations.
2. Never invent a date, fee, rule, contact or procedure. If there is no evidence, return an honest fallback and point to the relevant office.
3. Every factual answer cites real retrieved sources. The backend validates cited IDs against the retrieval results, and the model never generates URLs.
4. No paid or hosted LLM APIs. Everything runs locally. The LLM and embedding providers sit behind an interface and are chosen by env config.
5. Structured lookups (datesheets, notices, papers, contacts) must work even when the LLM is down.
6. Retrieved documents are untrusted data, never instructions.

## Stack
Python 3.11, FastAPI, Pydantic, SQLAlchemy, Alembic, PostgreSQL with pgvector and full-text search, PyMuPDF, Trafilatura and BeautifulSoup (Playwright only if JS rendering is required), optional Tesseract OCR, APScheduler, Docker Compose.
Models run through Ollama. Target a 1-4B generation model (start with Qwen3.5-4B) and a multilingual embedding model (start with EmbeddingGemma). Verify that both exist in the Ollama registry first. If not, pick the closest available alternatives and tell me. Model names are env vars, and thinking mode is off by default. Do not add Redis, Celery or a separate vector DB.

## Crawler (targeted, not a mirror)
- Source registry in config/sources.yaml (name, URL, category, priority, enabled, interval). Seed it with the notice board, examination notices and archive, COE, ordinances, academic calendar, scheme and syllabus, hostel, library, and training and placements. Discover the real URLs from the site navigation, and don't assume the list is complete.
- Fetch listing pages first, then download an attachment only if its title or metadata passes a relevance filter (exams, academics, fees, scholarships, hostel, placements). Skip tenders, recruitment and procurement, but keep their title, date and URL.
- Incremental: ETag and Last-Modified, SHA-256 hashing, version history (never delete old versions), retry limits, and per-domain rate limiting.
- Respect robots.txt. Allow only approved domains (SSRF-safe). Never touch login or ERP pages. One failed page must never stop the crawl.

## Processing and data
- Extract text with page numbers, headings, and tables intact (they hold dates and fees). Chunk at 400-600 tokens with overlap, and keep metadata on every chunk.
- Tables: documents, document_versions, document_chunks, notices, deadlines, exams, papers, rules, procedures, offices, quick_questions, answer_cache, feedback, ingestion_logs, audit_log.
- Document status: TENTATIVE, FINAL, REVISED, WITHDRAWN, ARCHIVED, NEEDS_REVIEW, plus supersedes_document_id. Resolve conflicts by programme, semester, session and explicit supersession, never by newest date alone. If sources conflict, show the conflict.
- Facts extracted from documents (dates, fees, eligibility, rules) stay unverified until an admin approves them. Validate any LLM-extracted JSON against Pydantic schemas.

## Question routing
A. FAQ/quick: cached verified answer.
B. Structured lookup: SQL, no LLM.
C. RAG: hybrid retrieval (Postgres FTS plus pgvector, metadata filters on programme, semester, session and status). Fetch about 20 candidates and pass the best 3-5 to the model. Leave a hook for a future reranker.
D. Off-topic: polite refusal with no LLM call.
E. Low confidence: "couldn't verify this" plus the relevant office.
Normalize queries for English, Hindi and Roman Hindi (a synonym file in config/synonyms.yaml, e.g. hazri to attendance, back paper to reappear) without destroying the original text.

## Caching
Exact FAQ cache plus a normalized/semantic cache. Key = normalized_query + language + programme + semester + knowledge_version. Invalidate cache entries when a supporting document is revised, withdrawn or superseded. Do not rely on TTL alone.

## API (/api/v1)
Public: POST /ask, GET /quick-questions, /notices, /papers, /calendar, /procedures, /offices, /documents/{id}, POST /feedback, GET /health.
Admin (authenticated): documents, crawl trigger, upload, approve/archive, unanswered questions, analytics, health.
POST /ask returns: answer, answer_type, language, confidence, cache_hit, sources[{title, url, page, excerpt, date, status}], suggested_questions. Add CORS limits, rate limiting, file-size and content-type limits, and OpenAPI docs.

## Working method
1. First inspect the repo and the HTML, then write docs/PLAN.md (architecture, data flow, assumptions, open questions). Stop and wait for my approval.
2. Build in phases, and stop after each one for my review: (1) skeleton, DB, config, health; (2) crawler and source registry; (3) extraction, versioning, structured storage; (4) embeddings and hybrid search; (5) LLM provider and grounded answers with citation validation; (6) cache, router and API; (7) HTML integration; (8) admin and evaluation.
3. After each phase: run tests, fix failures, update the README with example requests and responses, and list what is NOT done yet.
4. Never mark a feature complete if it is only a placeholder. Never seed the production DB with invented data. Use clearly labelled synthetic fixtures for tests only.

## Acceptance tests (phase 8)
Build a test set of 50+ questions (English, Hindi, Hinglish, typos, semester-specific, deadlines, conflicting notices, no-evidence, off-topic). It must verify:
- no invented facts, and every factual answer has valid citations
- superseded documents never silently override current ones
- cache invalidation works after a document update
- structured lookups work with the LLM stopped
- the crawler never leaves approved domains
- the demo runs with zero paid APIs

Start with Step 1 only: inspect, then write docs/PLAN.md.