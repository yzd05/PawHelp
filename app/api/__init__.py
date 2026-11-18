"""Public HTTP router assembly. Existing create_api_router imports remain valid."""

from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.chat import chat_router
from app.api.knowledge import knowledge_router
from app.config import Settings
from app.services.knowledge_admin import KnowledgeAdminService
from app.services.tool_chat import ToolAwareChatService


def create_api_router(
    settings: Settings,
    model: object,
    session_factory: async_sessionmaker[AsyncSession],
    tool_chat_service: ToolAwareChatService,
    knowledge_admin_service: KnowledgeAdminService,
) -> APIRouter:
    router = APIRouter()
    router.include_router(
        chat_router(
            settings,
            model,
            session_factory,
            tool_chat_service,
            knowledge_admin_service,
        )
    )
    router.include_router(
        knowledge_router(knowledge_admin_service, settings=settings)
    )
    return router
