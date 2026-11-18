# Changelog

## 1.1.0 — 2026-11-17

### Conversation & workflow

- Removed the legacy `content_types=("policy",)` hard filter in `TurnNodes.retrieve`
  on return and after-sales intents while preserving multi-angle `_policy_queries`
  expansion, so product manuals (`manuals/after-sales.md`) and operator-approved
  flywheel FAQ entries are retrieved on the live path.
- Split structured lightweight nodes onto `create_fast_chat_model` (15 s timeout,
  `max_retries=1`) for reference resolution, intent classification, query
  understanding, question splitting, knowledge consistency, mining, titles and
  summaries; removed double-retry amplification on `classify_intent` timeouts and
  added a 90 s whole-turn wall-clock timeout (`workflow_turn_timeout_seconds`).
- Incremental SSE delta streaming, cached MCP tool discovery, explicit SQLAlchemy
  connection pool sizing (`db_pool_size`, `db_max_overflow`,
  `db_pool_recycle_seconds`), non-blocking async observability persistence, and
  aligned `.env.example` context window defaults (`MODEL_CONTEXT_WINDOW=65536`,
  `MODEL_MAX_OUTPUT_TOKENS=1024`).

### Knowledge, flywheel & evaluation

- Hardened MySQL-to-Milvus dual writes with a four-state lifecycle
  (`pending / done / failed / deleting`): deletes and Markdown syncs mark
  transient `deleting` tombstones before Milvus vector removal (excluded from
  hydration and chunk totals), batch vectorization isolates poison-pill chunks as
  `failed` without blocking healthy peers, Milvus UTF-8 `VARCHAR` byte limits are
  widened and validated, and orphan vectors are reconciled bidirectionally.
- Scoped `prev_chunk_id` / `next_chunk_id` adjacency links strictly to chunks
  sharing the same `section_path`; pre-ranked `ReviewQueue` candidates by lexical
  overlap when exceeding `flywheel_candidate_limit` (100) instead of raising a
  hard error; pinned explicit JSON-mode keys in
  `CONVERSATION_KNOWLEDGE_EXTRACTION_SYSTEM_TEMPLATE`; safely awaited cancelled
  `jev_task` in shadow mode; shared warmed models in `EvaluationPipeline`.
- Tracked synthetic calibration snapshots
  (`relevance-train-served-20261020.json`,
  `relevance-validation-served-20261020.json`) and a compact read-only evaluation
  baseline (`reports/retrieval-evaluation.baseline.json`) with automatic fallback
  in `GET /v1/rag-eval/report`, so `/observability` and `/rag-eval` render full
  charts out of the box on fresh clones without paid model runs.

### Security & web

- Unified admin route authentication under `app.api.auth.authorize` (including
  `/v1/topics/*`), moved all scripts and styles out of the 6 HTML pages into
  external static modules, enforced strict CSP3 (`default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'`)
  plus `X-Content-Type-Options`, `X-Frame-Options` and `Referrer-Policy` headers
  alongside DOMPurify sanitization, and bounded `ChatRequest` / `ExtractionRequest`
  payload lengths.
- Introduced `LazyASGIApp` for side-effect-free `app.main` imports and non-blocking
  route handlers.

### Packaging, documentation & CI

- Cross-platform `pyproject.toml` and `uv.lock` (CUDA 13.0 PyTorch wheels on
  Windows/Linux with automatic PyPI wheel fallback on macOS), `pawhelp-start --bootstrap`
  one-command onboarding with custom MCP port forwarding, keyless Alembic
  migrations (including `0013_hardening_schema_cleanup`), `SECURITY.md`,
  `THIRD_PARTY_NOTICES.md`, GitHub issue/PR templates, and 4 architecture decision
  records under `docs/adr/0001..0004`.
- GitHub Actions CI (`source-checks.yml`) runs `uv run --locked ruff check`,
  headless browser Markdown/XSS assertions, keyless `alembic upgrade head` against
  `mysql:8.4` (`+00:00`), and the full `pytest` suite including live-MySQL
  integration tests.

## 1.0.0 — 2026-10-22

### Conversation

- LangGraph workflow: reference resolution → intent classification →
  deterministic routing → knowledge retrieval or tool agent → SSE stream.
- Order selection and ticket confirmation as graph interrupts, resumable after a
  restart through the SQLite checkpointer.
- Layered session context with a token budget and asynchronous summaries.
- Guards against rewrites that invent or replace an explicit order number.
- Sidebar titles generated once in the background; greetings are skipped and
  short questions are used as-is, falling back to the first question on failure.

### Knowledge and retrieval

- Markdown ingestion into structured chunks; BGE-M3 dense + Milvus BM25 hybrid
  recall, RRF fusion, BGE reranking and a calibrated evidence gate with citations.
- 500-question retrieval benchmark (410 answerable, 90 out-of-scope) comparing
  four strategies by query type.
- Knowledge flywheel: low-confidence questions are normalized, deduplicated,
  reviewed by an operator and written back to the knowledge base.
- Review page shows evidence-gate refusals as a sentence with score and threshold,
  not the stored confidence JSON.
- Invoice requests are accepted within 30 days of confirmed receipt (was 90); the
  benchmark rows and the evidence-gate calibration were updated with it. The
  96 labelled cases were re-collected locally and re-reviewed; the threshold (0.127)
  and its independent validation result are unchanged.
- The four product manuals name the series they cover in their title, so every
  section chunk carries it; a model-specific question such as "MH-FD30 的录音呼唤
  最长能录多久" is no longer refused for evidence that does not name the model. The
  separate "本篇适用于" intro chunks were dropped (101 → 97 document chunks) and the
  calibration was re-collected; the threshold (0.127) still passes validation.
- Every write outside a Markdown rebuild is checked against existing knowledge
  first: a model compares the entry with its nearest matches and refuses a
  duplicate or a contradiction (manual entries and edits get 409, approved
  reviews stay pending, mined QA is staged as discarded with its reason, migration
  0012), naming the existing entry by its ID with a one-sentence reason in Chinese.
  The check fails closed: if it cannot run, nothing is written. Seven old
  manual and mined entries that contradicted or repeated the shipping, return and
  promotion documents were removed.
- A Markdown rebuild rechecks the manual, approved and mined entries it keeps
  against the rebuilt documents and lists any that are now duplicates or
  contradictions on /kb; it never deletes them itself.
- The evidence-gate calibration is collected on the served path (model rewrite,
  question split, whole knowledge base) instead of the raw query. Its fingerprint
  covers the documents, the chunking/retrieval code, the rewrite prompt, the models
  and the retrieval settings, so changing any of them leaves the gate uncalibrated;
  /kb warns when manual or mined entries changed after calibration. The 96 labels
  were re-reviewed on the new evidence and are unchanged; the threshold rose from
  0.127 to 0.174 and independent validation passes (0/24 unsafe, recall 0.95).
- Optional TypeSafe Jev (`jev-1.14.0`) evidence-sufficiency judge. `EVIDENCE_JUDGE=shadow`
  logs Jev's P(yes) next to the LLM verdict; `jev` lets Jev accept or refuse outside a
  calibrated band and hands the band, Jev errors and calibration mismatches to the LLM.
  The default stays `llm` and needs no key. On the archived 500-question run (498 scored)
  the thresholds chosen on training cases are P ≤ 0.05 refuse / P ≥ 0.60 accept; on the
  served validation path Jev matches the LLM (0 false refusals, 0 false accepts) and decides
  93.5% of cases itself, AUC 0.999. A full served run in `jev` mode had Jev decide 372 of the
  403 evidence checks (92.3%) with no false refusal or false accept; false refusals went
  18 → 17 of 410, out-of-scope refusals stayed 90/90, and the remaining over-strict refusals
  all came from the band handed to the LLM.
- Jev was also tried as the intent review signal on 199 new single-turn questions (44 user-reviewed
  labels, fixed validation split). It tied the current confidence < 0.55 rule on validation and
  was not adopted: the LLM's wrong intents are confident, so no threshold on either signal catches them.
- The intent prompt counts account login and password rules, points and coupons, and shop and company
  information as 商品咨询. Questions such as "怎么改登录密码" were classed 其他 with high
  confidence and the customer was asked to clarify instead of getting the account-security answer.
- Account-security incidents (a stolen account, unusual logins, a frozen account) are classed 人工,
  because they need a person to verify identity and freeze the account; questions about how to change
  a password or rebind a phone stay 商品咨询.
- A knowledge refusal offers the 转人工 and 建工单 buttons instead of ending the conversation.

### Tools

- Tool registry with JSON-schema validation, timeouts, read-only retries,
  confirmation-gated writes and an audit log.
- Logistics and after-sales demo services over MCP with an allow-list policy.
- The order prefetched for a turn reaches the agent as its `query_order` call and
  result, so a logistics turn no longer queries the same order twice (one model
  round and one tool call fewer).

### Operations

- `scripts.start` brings up containers, migrations, MCP servers, the topic classifier
  and the app in one command, and stops them together on Ctrl+C.
- Only the served app writes `log/app.log` and the SQLite conversation checkpoints;
  apps built in tests keep checkpoints in memory, so test runs no longer mix fake
  conversations into the real context log. The log handler is detached at shutdown.
- Faster cold start: retrieval models warm up in the background at startup
  (first chat 37.5s → 13.2s) and the text-splitter import no longer
  loads sentence-transformers (import 27s → 7s).
- Admin pages for knowledge, RAG evaluation, review, cost/observability and topics.
- Self-hosted Langfuse export; token usage and estimated cost per intent.
- Offline 17-label topic classifier (RoBERTa-wwm-ext) for the question pool.

### Quality

- Offline regression suite (fake models, in-memory checkpoints) in CI; live-MySQL
  integration tests run against an explicit disposable database.
