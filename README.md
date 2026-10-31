# Ask YMCA

Local-first university information assistant for J.C. Bose University of Science
and Technology, YMCA, Faridabad.

## Run in Codespaces or Linux

Start Ollama in one terminal. The Compose API uses host networking so it can reach
the host's loopback Ollama listener (`127.0.0.1:11434`) directly:

```sh
ollama serve
ollama pull qwen2.5:1.5b
ollama pull embeddinggemma
```

In another terminal:

```sh
docker compose up --build -d
```

Open forwarded port **8000** in Codespaces (or `http://localhost:8000` locally).
The interactive API docs are at `/docs`. Compose runs database migrations before
starting the API. The locally installed models used here are `qwen2.5:1.5b` for
answers and `embeddinggemma` (768 dimensions) for vector retrieval.

Check service and knowledge readiness:

```sh
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/agent/status
```

## Load official university sources

The source registry in `config/sources.yaml` uses pages verified from the live
university navigation: notices and examination archives, COE, ordinances,
academic calendar, schemes and syllabi, hostel, library, and Training & Placement.
The crawler respects the approved-host allowlist and robots.txt, skips login/ERP
paths, follows only relevant same-university PDFs, extracts PDF page numbers, and
stores immutable content versions and local embeddings.

On the chat page, open **Manage university sources**, enter the admin token, and
select **Scan sources**. The first local Compose default token is
`local-development-only`; set `ASK_YMCA_ADMIN_TOKEN` in `.env` to a private value
before exposing the app or using it outside local development. The review queue
shows extracted titles and excerpts. Approve only documents you have checked;
approval makes a source eligible for factual answers. Use status `TENTATIVE` for
official tentative notices rather than approving them as final.

The crawler is incremental: unchanged documents are skipped, changed documents
get new versions, and changed content returns to review before it is used. A crawl
can also be triggered through `POST /api/v1/admin/crawl`; admin endpoints require
`Authorization: Bearer <ASK_YMCA_ADMIN_TOKEN>`.

## Ask a question

```sh
curl -sS http://localhost:8000/api/v1/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"What does the latest examination notice say?"}'
```

The response includes the answer type, language, confidence, cache indicator,
agent status, citations (title, official URL, page, excerpt, date, and document
status), and suggested questions. Search combines PostgreSQL lexical matches with
pgvector cosine similarity from the local embedding model, then sends the best
evidence to Ollama. A citation is returned only if its ID was among the retrieved
documents. Off-topic questions do not call Ollama; missing evidence and model
outages produce explicit fallbacks.

Useful public endpoints include `GET /api/v1/notices`, `/calendar`, `/papers`,
`/procedures`, `/offices`, `/quick-questions`, and `/documents/{id}`. Feedback is
accepted at `POST /api/v1/feedback`.

## Tests and configuration

```sh
python -m pip install -e '.[dev]'
pytest
ruff check src/ask_ymca tests alembic
```

See `.env.example` for model, database, CORS, and admin settings. The default
Compose PostgreSQL credentials and admin token are for local development only.
The demo data in `ask-ymca-v3.html` is intentionally not used as university
knowledge.

## Remaining work

The admin crawl request returns immediately; the browser polls
`GET /api/v1/admin/crawl/status` and displays recent source progress. Each listing
is capped at six relevant PDFs per run. These are in-memory, single-process jobs,
so an API container restart interrupts an active crawl. Large PDFs retain all
extracted text for keyword search while vector embeddings are sampled across up
to 128 chunks per document to bound local CPU work.

Retrieval uses lexical plus dense-vector search and local generation.
This is a working local prototype, not yet a production-ready professional
university service: the official corpus still needs an administrator to review
and approve documents, and coverage/answer accuracy has not yet been validated
against a university-wide test set. A recurring scheduler, bounded document
upload, automated structured-fact extraction/approval, production-grade identity
provider/rate limiting, and the 50-question multilingual evaluation corpus are
not implemented yet. Compose's `network_mode: host` setup is intended for Linux
Codespaces/Linux Docker; other platforms may need a host-gateway networking
adjustment.