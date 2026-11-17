"""Mine at most one bounded batch of historical conversations through the consistency gate."""

import argparse
import asyncio
from pathlib import Path

from app.config import Settings, get_settings
from app.database.session import create_async_engine_from_settings, create_session_factory
from app.knowledge.consistency import KnowledgeConsistencyChecker
from app.knowledge.embedding import BgeM3Embedder
from app.knowledge.mining import ConversationKnowledgeExtractor
from app.knowledge.query_understanding import IdentityQueryUnderstanding
from app.knowledge.reranking import BgeReranker
from app.knowledge.search import HybridKnowledgeSearch
from app.knowledge.vector_store import MilvusKnowledgeStore
from app.llm import create_chat_model
from app.schemas import KnowledgeMiningResult
from app.services.knowledge_admin import KnowledgeAdminService

ROOT = Path(__file__).resolve().parents[1]


def build_knowledge_admin_service(
    settings: Settings,
    session_factory,
    *,
    batch_size: int | None = None,
) -> KnowledgeAdminService:
    model = create_chat_model(settings)
    embedder = BgeM3Embedder(
        settings.embedding_model,
        batch_size=settings.embedding_batch_size,
        max_length=settings.embedding_max_length,
    )
    vector_store = MilvusKnowledgeStore(
        settings.milvus_uri,
        settings.milvus_hybrid_collection,
    )
    reranker = BgeReranker(
        settings.reranker_model,
        batch_size=settings.reranker_batch_size,
        query_max_length=settings.reranker_query_max_length,
        passage_max_length=settings.reranker_passage_max_length,
    )
    consistency_search = HybridKnowledgeSearch(
        session_factory,
        embedder,
        vector_store,
        reranker,
        IdentityQueryUnderstanding(),
        confidence_threshold=settings.knowledge_rerank_threshold,
        dense_limit=settings.dense_recall_limit,
        bm25_limit=settings.bm25_recall_limit,
        rerank_limit=settings.rerank_limit,
    )
    calibration = Path(settings.evidence_calibration_path)
    if not calibration.is_absolute():
        calibration = ROOT / calibration
    return KnowledgeAdminService(
        session_factory,
        embedder,
        vector_store,
        consistency_search,
        documents_root=ROOT / "knowledge",
        vectorize_batch_size=settings.vectorize_batch_size,
        conversation_extractor=ConversationKnowledgeExtractor(
            model,
            method=settings.structured_output_method,
        ),
        conversation_batch_size=batch_size or settings.conversation_mining_batch_size,
        calibration_path=calibration,
        consistency_checker=KnowledgeConsistencyChecker(
            consistency_search,
            model,
            method=settings.structured_output_method,
        ),
    )


async def run(
    batch_size: int | None = None,
    *,
    vectorize_after: bool = False,
) -> KnowledgeMiningResult:
    settings = get_settings()
    engine = create_async_engine_from_settings(settings)
    factory = create_session_factory(engine)
    try:
        service = build_knowledge_admin_service(
            settings,
            factory,
            batch_size=batch_size,
        )
        return await service.mine_conversations(vectorize_after=vectorize_after)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument(
        "--vectorize",
        action="store_true",
        help="Vectorize promoted conversation chunks immediately after mining",
    )
    args = parser.parse_args()
    if args.batch_size is not None and args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    result = asyncio.run(run(args.batch_size, vectorize_after=args.vectorize))
    print(
        f"conversations={result.conversations} extracted={result.extracted} "
        f"kept={result.kept} discarded={result.discarded} vectorized={result.vectorized}"
    )


if __name__ == "__main__":
    main()
