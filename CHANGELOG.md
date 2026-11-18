# Changelog

## 0.1.0 — 2026-11-17

Initial open-source preview release of **PawHelp**.

### Conversation & workflow

- LangGraph customer-support state machine combining deterministic pre-routing
  (`resolve_reference → classify_intent → plan_route`) with full-corpus knowledge
  retrieval or a controlled tool agent over incremental SSE delta streaming.
- Two-stage resumable graph interrupts (`select_order` for ambiguous order selection
  and `confirm_ticket` bound to `proposal_id` for ticket confirmation) persisted via
  the SQLite checkpointer across restarts; prefetched orders are injected directly as
  the opening `query_order` call/result pair (saving one model round and one duplicate
  lookup).
- Immutable three-layer session memory (`MODEL_CONTEXT_WINDOW=65536`,
  `MODEL_MAX_OUTPUT_TOKENS=1024`) with protocol-safe tool-pair trimming, intermediate
  tool-result compaction, asynchronous segmented summaries with Chinese-numeral
  equivalence guards and failure backoff, anti-fabrication guards on order-number
  rewrites, and one-shot background sidebar titles.
- Split lightweight structured nodes onto `create_fast_chat_model` (15 s timeout,
  `max_retries=1`) for reference resolution, intent classification, query
  understanding, question splitting, knowledge consistency, mining, titles, and
  summaries, paired with single-attempt transport fallback on `classify_intent` and a
  90 s whole-turn wall-clock timeout (`workflow_turn_timeout_seconds`).
- Multi-angle `_policy_queries` expansion on return and after-sales intents without
  category hard-filtering so product manuals (`manuals/after-sales.md`) and
  operator-approved flywheel FAQ entries are retrieved alongside store policies;
  refined intent rules separate account-security incidents (`人工`) from self-service
  account/login questions (`商品咨询`).

### Knowledge, flywheel & evaluation

- Structured Markdown ingestion (16 documents, 97 chunks with series-scoped manual
  titles and same-`section_path` `prev_chunk_id` / `next_chunk_id` adjacency links)
  indexed into `BAAI/bge-m3` dense + Milvus BM25 hybrid recall with RRF fusion and
  `bge-reranker-v2-m3` cross-encoder reranking.
- 500-question retrieval benchmark (410 answerable, 90 out-of-scope; 30-day invoice
  window aligned across corpus and benchmark) comparing four retrieval strategies by
  query type (`hybrid + rerank` Recall@5 `0.997`, colloquial MRR `0.956`), alongside a
  22-case structured routing benchmark and a 20-case consistency benchmark.
- Calibrated two-tier evidence gate with citations and `转人工` / `建工单` fallback
  actions on refusal:
  - First-tier relevance prefilter (`0.174`) calibrated on the served path (72
    training / 24 independent hold-out cases, `0/24` unsafe accepts, `0.950` recall)
    and guarded by a SHA-256 `scoring_fingerprint` covering corpus files, retrieval
    code, rewrite prompt, embedding/reranking models, and retrieval settings.
  - Second-tier sufficiency verification supporting optional TypeSafe Jev
    (`jev-1.14.0`, `EVIDENCE_JUDGE=shadow|jev`, default `llm`): calibrated thresholds
    (`P ≤ 0.05` refuse, `P ≥ 0.60` accept) decide `92.3%` of sufficiency checks
    (`93.5%` with `0` false accepts / `0` false refusals on the 77-case served
    hold-out split, `AUC 0.999`) while preserving `90/90` out-of-scope refusals;
    shadow mode cleanly awaits cancelled background tasks. Also evaluated and rejected
    Jev for single-turn intent review after a 199-question benchmark tied
    `confidence < 0.55`.
- Closed-loop knowledge flywheel and fail-closed `KnowledgeConsistencyChecker`:
  low-confidence and downvoted questions are normalized, pre-ranked by lexical overlap
  when exceeding `flywheel_candidate_limit` (100), deduplicated, reviewed on `/review`
  (rendering gate scores as readable sentences), checked against existing chunks
  (`409` on manual conflicts, `discarded` staging on mined QA with migration `0012`,
  non-destructive conflict warnings on `/kb` after Markdown rebuilds), and written
  back safely.
- Four-state MySQL-to-Milvus dual-write lifecycle (`pending / done / failed / deleting`):
  deletes and Markdown syncs mark transient `deleting` tombstones before Milvus vector
  removal (excluded from hydration and chunk totals), batch vectorization isolates
  poison-pill chunks as `failed` without blocking healthy peers, Milvus UTF-8
  `VARCHAR` byte limits are widened and validated, and orphan vectors are reconciled
  bidirectionally.

### Tools, security & web

- Controlled tool registry with JSON-schema validation, timeouts, read-only retries,
  confirmation-gated writes, cached MCP tool discovery, and an audit log; logistics
  and after-sales demo services run over Streamable HTTP MCP with an allow-list and
  HMAC-signed demo scenario headers.
- Unified admin route authentication under `app.api.auth.authorize` (loopback
  same-origin console header or constant-time `X-Admin-Token`, including
  `/v1/topics/*`), sanitized public citation views, and bounded `ChatRequest` /
  `ExtractionRequest` payload lengths.
- Modularized static assets across all 6 HTML consoles (zero inline scripts or styles)
  paired with DOMPurify allow-list sanitization, strict CSP3
  (`default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'`),
  `X-Content-Type-Options`, `X-Frame-Options`, and `Referrer-Policy` headers.
- Side-effect-free `LazyASGIApp` import wrapper, non-blocking async route handlers,
  explicit SQLAlchemy connection pool sizing (`db_pool_size`, `db_max_overflow`,
  `db_pool_recycle_seconds`), background retrieval model warm-up (first chat
  `37.5s → 13.2s`, text-splitter import `27s → 7s`, shared warmed models in
  `EvaluationPipeline`), and serving-only `log/app.log` / SQLite checkpointer
  isolation so test runs remain in memory.

### Operations, observability & packaging

- One-command `pawhelp-start` (`python -m scripts.start`, with `--bootstrap` first-run
  ingestion/vectorization and custom MCP port forwarding), keyless offline/online
  Alembic migrations (`0001`–`0013`, including `0013_hardening_schema_cleanup`),
  self-hosted Langfuse export with async shielded cost persistence, and an offline
  17-label `RoBERTa-wwm-ext` topic classifier for the question pool.
- Tracked synthetic calibration snapshots
  (`relevance-train-served-20261020.json`,
  `relevance-validation-served-20261020.json`) and a compact read-only evaluation
  baseline (`reports/retrieval-evaluation.baseline.json`) with automatic fallback in
  `GET /v1/rag-eval/report` so `/observability` and `/rag-eval` render out of the box
  on fresh clones.
- Cross-platform `pyproject.toml` and `uv.lock` (CUDA 13.0 PyTorch wheels on
  Windows/Linux with automatic PyPI wheel fallback on macOS), `SECURITY.md`,
  `THIRD_PARTY_NOTICES.md`, GitHub issue/PR templates, Architecture Decision Records
  (`docs/adr/0001..0004`), and GitHub Actions CI (`source-checks.yml`) running
  `uv run --locked ruff check`, headless browser Markdown/XSS/CSP assertions, keyless
  `alembic upgrade head` against `mysql:8.4` (`+00:00`), and the full `pytest` suite.
