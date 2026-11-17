# Third-party notices

The root [MIT License](LICENSE) applies to original PawHelp source code, SQL
migrations, configuration templates, and documentation for which the project
contributors hold the copyright. It does not replace the upstream licenses or
terms of third-party software packages, browser vendor files, pretrained model
weights, or external API services.

## Bundled browser dependencies

The chat interface vendors two unmodified browser distributions in
`app/static/vendor/` so local deployments require no runtime CDN or `npm` build:

- **Marked 18.1.0** — MIT License (`app/static/vendor/marked-LICENSE.txt`).
- **DOMPurify 3.4.16** — Dual-licensed under Apache-2.0 or MPL-2.0
  (`app/static/vendor/dompurify-LICENSE.txt` and
  `app/static/vendor/dompurify-LICENSE-MPL.txt`).
- Exact registry URLs, version numbers, and SHA-256 digests are recorded in
  `app/static/vendor/README.md`.

## Open-source Python and infrastructure dependencies

Python runtime and development dependencies declared in `pyproject.toml` and
locked in `uv.lock` (including FastAPI, Pydantic, SQLAlchemy, Alembic,
LangGraph, LangChain Core/OpenAI, MCP Python SDK, PyMilvus, FlagEmbedding,
PyTorch, Langfuse SDK, httpx, and uvicorn) are installed from upstream package
indexes and remain governed by their respective permissive open-source licenses
(MIT, Apache-2.0, BSD-3-Clause, and PSF-2.0). Containerized services referenced
in `compose.yaml` and `compose.langfuse.yaml` (MySQL, Milvus, etcd, MinIO,
Langfuse, PostgreSQL, ClickHouse, and Redis) are fetched by the operator at
runtime under their upstream image licenses.

## External models and API services

Pretrained model weights (`BAAI/bge-m3`, `BAAI/bge-reranker-v2-m3`, and
`hfl/chinese-roberta-wwm-ext`) and upstream inference APIs (OpenAI-compatible
chat providers and optional TypeSafe Jev decision endpoints) retain their
respective upstream model cards, licenses, and terms of service. Model weights,
private corpora, and local runtime caches are excluded from this source
repository; any separate redistribution of fine-tuned weights must satisfy the
base model's license terms.

## Project data and provenance

All source modules, database migrations, pet-supplies knowledge documents in
`knowledge/`, and benchmark fixtures in `evals/` shipped in this repository are
original to PawHelp and licensed under the root [MIT License](LICENSE). Product
names, SKU model numbers, order scenarios, and store policies in `knowledge/`
and `evals/` are fictional pet-supplies fixtures created for engineering
demonstration and evaluation; they contain no real customer records or
third-party proprietary datasets. Earlier local prototype directories and
external scratch corpora were audited and excluded prior to the `v1.0.0` open-source release.
