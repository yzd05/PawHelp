"""FastAPI application factory and lazy ASGI entry point."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
import inspect
import logging
from pathlib import Path
import threading
import time

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import create_api_router
from app.api.evaluation import (
    PROJECT_ROOT,
    RAG_EVAL_CASES_PATH,
    RAG_EVAL_REPORT_DIR,
    RagEvalRunner,
    evaluation_router,
)
from app.api.operations import ScheduledOperations, operations_router
from app.api.pages import (
    CHAT_PAGE_PATH,
    KNOWLEDGE_PAGE_PATH,
    RAG_EVAL_PAGE_PATH,
    page_router,
)
from app.config import Settings, get_settings
from app.database.session import create_async_engine_from_settings, create_session_factory
from app.evaluation.pipeline import EvaluationPipeline
from app.evaluation.runtime import DEFAULT_GENERATION_STRATEGIES
from app.flywheel import Flywheel
from app.knowledge.consistency import KnowledgeConsistencyChecker
from app.knowledge.embedding import BgeM3Embedder
from app.knowledge.mining import ConversationKnowledgeExtractor
from app.knowledge.query_understanding import (
    IdentityQueryUnderstanding,
    QueryUnderstandingService,
)
from app.knowledge.reranking import BgeReranker
from app.knowledge.search import HybridKnowledgeSearch
from app.knowledge.vector_store import MilvusKnowledgeStore, milvus_call
from app.llm import create_chat_model
from app.services.knowledge_admin import KnowledgeAdminService
from app.services.tool_chat import ToolAwareChatService
from app.topics.api import topic_router
from app.workflow.context_logging import configure_context_logging
from app.workflow.graph import WorkflowChatService

KNOWLEDGE_DOCUMENTS_PATH = PROJECT_ROOT / "knowledge"

logger = logging.getLogger(__name__)


async def warm_knowledge_models(embedder: BgeM3Embedder, reranker: BgeReranker) -> None:
    """Load BGE weights in the background; a chat arriving earlier waits on the load lock."""

    started = time.perf_counter()
    try:
        await asyncio.gather(
            asyncio.to_thread(embedder.warm), asyncio.to_thread(reranker.warm)
        )
    except Exception:
        logger.exception("knowledge model warm-up failed; models load on first query")
        raise
    logger.info("knowledge models ready in %.1fs", time.perf_counter() - started)


async def _close_vector_store(vector_store: object) -> None:
    """Release any live Milvus client on shutdown without masking earlier errors."""

    try:
        close = getattr(vector_store, "close", None)
        if callable(close):
            if inspect.iscoroutinefunction(close):
                await close()
            else:
                await milvus_call(close)
            return
        if getattr(vector_store, "_client", None) is not None:

            def _release_client() -> None:
                client = getattr(vector_store, "_client", None)
                if client is not None:
                    client_close = getattr(client, "close", None)
                    if callable(client_close):
                        client_close()
                    setattr(vector_store, "_client", None)
                    setattr(vector_store, "_ensured", False)

            await milvus_call(_release_client)
    except Exception:
        logger.warning("failed to close vector store cleanly", exc_info=True)


def _evaluation_runner_kwargs(
    evaluator_fn: Callable[..., object],
    resources: dict[str, object],
) -> dict[str, object]:
    """Pass pre-initialized runtime resources only when the evaluation signature accepts them."""

    parameters = inspect.signature(evaluator_fn).parameters
    accepts_var_kw = any(
        param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()
    )
    return {
        name: value
        for name, value in resources.items()
        if accepts_var_kw or name in parameters
    }


def create_app(
    settings: Settings | None = None,
    model: object | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    tool_chat_service: ToolAwareChatService | None = None,
    knowledge_admin_service: KnowledgeAdminService | None = None,
    rag_eval_runner: RagEvalRunner | None = None,
    preload_models: bool = False,
    runtime_root: Path | None = None,
) -> FastAPI:
    """Create an initialized API application and its runtime dependencies.

    ``runtime_root`` receives the context log and conversation checkpoints.
    """

    active_settings = settings or get_settings()
    active_model = model or create_chat_model(active_settings)
    owned_engine = None
    if session_factory is None:
        owned_engine = create_async_engine_from_settings(active_settings)
        active_session_factory = create_session_factory(owned_engine)
    else:
        active_session_factory = session_factory
    embedder = BgeM3Embedder(
        active_settings.embedding_model,
        batch_size=active_settings.embedding_batch_size,
        max_length=active_settings.embedding_max_length,
    )
    vector_store = MilvusKnowledgeStore(
        active_settings.milvus_uri,
        active_settings.milvus_hybrid_collection,
    )
    reranker = BgeReranker(
        active_settings.reranker_model,
        batch_size=active_settings.reranker_batch_size,
        query_max_length=active_settings.reranker_query_max_length,
        passage_max_length=active_settings.reranker_passage_max_length,
    )
    query_planner = QueryUnderstandingService(
        active_model,  # type: ignore[arg-type]
        method=active_settings.structured_output_method,
    )
    knowledge_search = HybridKnowledgeSearch(
        active_session_factory,
        embedder,
        vector_store,
        reranker,
        query_planner,
        confidence_threshold=active_settings.knowledge_rerank_threshold,
        dense_limit=active_settings.dense_recall_limit,
        bm25_limit=active_settings.bm25_recall_limit,
        rerank_limit=active_settings.rerank_limit,
    )
    active_tool_chat = tool_chat_service or WorkflowChatService(
        settings=active_settings,
        model=active_model,  # type: ignore[arg-type]
        session_factory=active_session_factory,
        knowledge_search=knowledge_search,
    )
    # Write-time checks compare the entry as written: no model rewrite of the query.
    consistency_search = HybridKnowledgeSearch(
        active_session_factory,
        embedder,
        vector_store,
        reranker,
        IdentityQueryUnderstanding(),
        confidence_threshold=active_settings.knowledge_rerank_threshold,
        dense_limit=active_settings.dense_recall_limit,
        bm25_limit=active_settings.bm25_recall_limit,
        rerank_limit=active_settings.rerank_limit,
    )
    active_knowledge_admin = knowledge_admin_service or KnowledgeAdminService(
        active_session_factory,
        embedder,
        vector_store,
        knowledge_search,
        documents_root=KNOWLEDGE_DOCUMENTS_PATH,
        vectorize_batch_size=active_settings.vectorize_batch_size,
        conversation_extractor=ConversationKnowledgeExtractor(
            active_model,  # type: ignore[arg-type]
            method=active_settings.structured_output_method,
        ),
        conversation_batch_size=active_settings.conversation_mining_batch_size,
        calibration_path=Path(active_settings.evidence_calibration_path),
        consistency_checker=KnowledgeConsistencyChecker(
            consistency_search,
            active_model,
            method=active_settings.structured_output_method,
        ),
    )
    if rag_eval_runner is None:

        async def default_rag_eval_runner(full_generation: bool) -> dict[str, object]:
            from app.evaluation import runtime as evaluation_runtime

            evaluator_fn = evaluation_runtime.run_retrieval_evaluation
            reusable = _evaluation_runner_kwargs(
                evaluator_fn,
                {
                    "embedder": embedder,
                    "reranker": reranker,
                    "vector_store": vector_store,
                    "session_factory": active_session_factory,
                },
            )
            report, _ = await evaluator_fn(
                active_settings,
                RAG_EVAL_CASES_PATH,
                RAG_EVAL_REPORT_DIR,
                generation_strategies=(
                    None if full_generation else DEFAULT_GENERATION_STRATEGIES
                ),
                **reusable,
            )
            return report

        active_rag_eval_runner = default_rag_eval_runner
    else:
        active_rag_eval_runner = rag_eval_runner
    rag_eval_lock = asyncio.Lock()
    # Retry policy must be set before SDK clients are constructed, not mutated afterwards.
    flywheel_model = model or create_chat_model(active_settings, max_retries=0)
    flywheel = Flywheel(
        active_settings, active_session_factory, flywheel_model, active_knowledge_admin
    )
    pipeline_kwargs = _evaluation_runner_kwargs(
        EvaluationPipeline,
        {
            "embedder": embedder,
            "reranker": reranker,
            "vector_store": vector_store,
        },
    )
    pipeline = EvaluationPipeline(
        active_settings,
        active_session_factory,
        PROJECT_ROOT,
        rag_eval_lock,
        **pipeline_kwargs,
    )
    scheduled = ScheduledOperations(
        active_settings, active_session_factory, flywheel, pipeline
    )

    warmup: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        nonlocal warmup
        if preload_models:
            warmup = asyncio.create_task(warm_knowledge_models(embedder, reranker))
        try:
            scheduled.start()
            if isinstance(active_tool_chat, WorkflowChatService):
                async with AsyncExitStack() as runtime:
                    # Only the served app writes files; other apps (tests) keep checkpoints in memory.
                    if runtime_root is not None:
                        runtime.callback(configure_context_logging(runtime_root))
                        checkpoint_dir = runtime_root / ".runtime"
                        checkpoint_dir.mkdir(exist_ok=True)
                        checkpoints = AsyncSqliteSaver.from_conn_string(
                            str(checkpoint_dir / "conversation-checkpoints.sqlite3")
                        )
                        active_tool_chat.set_checkpointer(
                            await runtime.enter_async_context(checkpoints)
                        )
                    if hasattr(active_tool_chat, "startup_check"):
                        active_tool_chat.startup_check()  # type: ignore[attr-defined]
                    elif hasattr(active_tool_chat, "_memory"):
                        active_tool_chat._memory.startup_check()
                    try:
                        yield
                    finally:
                        await active_tool_chat.close()
            else:
                yield
        finally:
            if warmup is not None:
                if not warmup.done():
                    warmup.cancel()
                with suppress(asyncio.CancelledError):
                    try:
                        await warmup
                    except Exception:
                        logger.debug(
                            "knowledge model warm-up ended with error during shutdown",
                            exc_info=True,
                        )
            await scheduled.close()
            await _close_vector_store(vector_store)
            if owned_engine is not None:
                await owned_engine.dispose()

    application = FastAPI(title="PawHelp", version="1.0.0", lifespan=lifespan)
    application.mount(
        "/static", StaticFiles(directory=CHAT_PAGE_PATH.parent), name="static"
    )
    application.state.settings = active_settings
    application.state.model = active_model
    application.state.engine = owned_engine
    application.state.session_factory = active_session_factory
    application.state.embedder = embedder
    application.state.reranker = reranker
    application.state.vector_store = vector_store
    application.state.tool_chat_service = active_tool_chat
    application.state.knowledge_admin_service = active_knowledge_admin
    application.state.flywheel = flywheel
    application.state.evaluation_pipeline = pipeline
    application.include_router(
        operations_router(active_settings, active_session_factory, flywheel, pipeline)
    )
    topic_kwargs = (
        {"settings": active_settings}
        if "settings" in inspect.signature(topic_router).parameters
        else {}
    )
    application.include_router(
        topic_router(active_session_factory, PROJECT_ROOT, **topic_kwargs)
    )
    application.include_router(
        create_api_router(
            active_settings,
            active_model,
            active_session_factory,
            active_tool_chat,
            active_knowledge_admin,
        )
    )

    application.include_router(page_router(runtime_root=runtime_root))
    application.include_router(
        evaluation_router(
            active_session_factory,
            pipeline,
            rag_eval_lock,
            active_rag_eval_runner,
            settings=active_settings,
        )
    )

    @application.get("/health")
    async def health() -> dict[str, str]:
        if warmup is None:
            return {"status": "ok"}
        if not warmup.done():
            return {"status": "ok", "knowledge_models": "loading"}
        failed = warmup.cancelled() or warmup.exception() is not None
        return {"status": "ok", "knowledge_models": "failed" if failed else "ready"}

    return application


class LazyASGIApp:
    """Defer ``create_app`` until ASGI lifespan/request startup so importing ``app.main`` is side-effect free."""

    def __init__(self, factory: Callable[[], FastAPI]) -> None:
        self._factory = factory
        self._instance: FastAPI | None = None
        self._lock = threading.Lock()

    @property
    def is_initialized(self) -> bool:
        return self._instance is not None

    def _resolve(self) -> FastAPI:
        if self._instance is None:
            with self._lock:
                if self._instance is None:
                    self._instance = self._factory()
        return self._instance

    async def __call__(self, scope, receive, send) -> None:
        target = self._resolve()
        await target(scope, receive, send)

    def __getattr__(self, name: str) -> object:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._resolve(), name)


# Module-level ASGI entry point used by ``uvicorn app.main:app``; initialized lazily on first serve.
app = LazyASGIApp(lambda: create_app(preload_models=True, runtime_root=PROJECT_ROOT))
