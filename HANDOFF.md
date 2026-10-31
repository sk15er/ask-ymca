# Ask YMCA implementation handoff

## Goal and honest status

The user wants a reliable university-information website whose local Ollama
assistant answers from accurate, official J.C. Bose University sources. The
current code is a local prototype, not an official university service. Only the
university can authorize branding, content policy, hosting, and an official
launch.

Estimated progress (judgment call, not a tracked metric):

- Runnable local prototype and core backend plumbing: about **70%**.
- Ready to present as an accurate, professional, official university app: about
  **35%**. The critical gap is verified content: there are no approved/indexed
  documents, so real university questions correctly receive a `not_found`
  fallback rather than a grounded answer.

Do not claim the assistant has answered a real university question accurately.
It has only been verified to produce a grounded-shaped answer against synthetic
test evidence.

## Current verified state

- Docker Compose API and PostgreSQL are running. `/` and `/docs` return HTTP 200;
  `/api/v1/health` reports the database healthy.
- Ollama is reachable from the API container on host loopback. `qwen2.5:1.5b`
  generation and `embeddinggemma` embeddings report ready.
- `/api/v1/ask` no longer returns the former config-path 500. With zero approved
  evidence it returns HTTP 200 and an explicit `not_found` response.
- Latest test run: **24 passed**, with one existing Starlette/httpx deprecation
  warning. `ruff check src/ask_ymca tests alembic`, Compose config validation,
  JS syntax validation, and `git diff --check` passed.
- The database has **36 documents in `NEEDS_REVIEW`** and
  `indexed_documents: 0`. Keep the review/approval gate; do not approve all
  documents automatically. OCR excerpts in notices can be poor and some facts
  conflict or have later revisions.

## Active crawl — check this first

After the latest image rebuild, a retry crawl was started through
`POST /api/v1/admin/crawl`. The last observed state was `running`, currently
processing **Examination ordinances**. Its `GET /api/v1/admin/crawl/status`
endpoint exposes source-level progress and the final summary. Do not start a
second crawl until checking that endpoint.

The immediately preceding crawl finished all 10 configured sources and reported
13 pages/documents seen, 0 changed, 11 skipped, and intermittent university
PDF responses that returned redirects without a `Location` header. The latest
code now retries that specific malformed response up to three attempts; verify
whether the active retry crawl reduces those errors. Do not poll or depend on a
previous shell session; query the API.

## Implemented path

1. `src/ask_ymca/main.py` serves the chat page at `/`.
2. `src/ask_ymca/api.py` implements chat, readiness, admin source review and
   status changes, citations, caching, and an in-process background crawl.
   `POST /api/v1/admin/crawl` returns 202; poll
   `GET /api/v1/admin/crawl/status`.
3. `config/sources.yaml` lists ten official university navigation sources. The
   crawler restricts hosts/paths, checks robots policy, extracts HTML/PDF text,
   stores versioned content/chunks, and creates local embeddings.
4. `src/ask_ymca/crawler.py` currently limits each listing to six relevant PDFs,
   accepts up to 30 MB per response, and samples vector embeddings across at
   most 128 chunks per document. All extracted text/chunks are retained for
   lexical search. Crawls are single-process/in-memory jobs and stop on API
   restart.
5. Crawled documents start as `NEEDS_REVIEW`. Answers use only approved
   `FINAL`, `REVISED`, or `TENTATIVE` evidence, cite retrieved IDs, and decline
   when evidence is absent.
6. Compose uses Linux host networking so the API reaches Ollama listening on
   `127.0.0.1:11434`. This is not a portable Docker networking setup.

## Important files

- `src/ask_ymca/api.py` — ask path, evidence retrieval, crawl and admin endpoints.
- `src/ask_ymca/crawler.py` — source fetching, extraction, retries, chunking,
  progress, embedding limits.
- `src/ask_ymca/assistant.py` — Ollama readiness, answer generation, embeddings.
- `src/ask_ymca/static/index.html` — chat and document-review UI.
- `config/sources.yaml`, `config/synonyms.yaml` — official source registry and
  query normalization.
- `docker-compose.yml`, `Dockerfile` — local app, DB, and Ollama connectivity.
- `tests/test_ask.py`, `tests/test_crawler.py` — current API/crawler regression
  tests.
- `README.md`, `docs/PLAN.md` — setup and stated product limitations.

## Next actions (in order)

1. Check `GET /api/v1/admin/crawl/status` until the active crawl is complete.
   Record the final source counts/errors. If a source is still running, check API
   responsiveness and Ollama CPU use before changing code or restarting.
2. Verify whether retries fixed the `Location`-less PDF redirects. If not, add
   bounded diagnostics/retries without weakening the approved-host allowlist or
   silently treating failed documents as successful. The official exam PDF was
   about 25 MB; the current 30 MB bound is intentional.
3. Inspect the 36 review items and OCR quality. Human-review and approve only
   clearly current, legible official material; explicitly handle revisions and
   superseding calendar/exam notices. Do not bulk-approve to make the status
   indicator green.
4. Once a verified document is approved, run a live question whose answer is
   directly present in it. Confirm the returned answer and citation URL/page
   against that document. Until then, report `knowledge_ready: false`.
5. Run `pytest -q`, `ruff check src/ask_ymca tests alembic`, `docker compose
   config -q`, and a browser/JS check after edits. Rebuild the API after source
   changes before claiming the running container includes them.
6. Be candid about remaining launch work: complete university-wide coverage and
   a maintained crawl schedule, conflict/supersession handling, multilingual
   factual evaluation, production authentication/rate limits, polished accessible
   UI, deployment/monitoring, and university authorization.

## Operational cautions

- Compose's default admin token and database credentials are development-only.
  Never expose the app publicly with those values.
- Keep official-source failures visible in ingestion status; do not convert
  timeouts, parse failures, or model failures into success-shaped responses.
- A successful model readiness check does not imply the assistant has usable
  university knowledge.
- The first scan is deliberately bounded. Six PDFs per listing is not proof of
  complete archive coverage; repeatable pagination/scheduling remains unfinished.
