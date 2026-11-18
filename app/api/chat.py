"""HTTP routes for the conversational service."""

from collections.abc import AsyncGenerator
from datetime import timezone
import json
from random import Random
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import PositiveInt
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.auth import authorize, bind_admin_settings
from app.config import Settings
from app.context import ContextBudgetExceeded
from app.database.models import Message
from app.extraction import AfterSalesExtractor, StructuredOutputError
from app.repositories.conversations import ConversationNotFound, ConversationRepository
from app.repositories.messages import MessageRepository
from app.repositories.orders import OrderNotFound, OrderRepository
from app.repositories.refunds import RefundRequestConflict, RefundRequestRepository
from app.schemas import (
    AfterSalesExtraction,
    ChatRequest,
    ExtractionRequest,
    OrderSelectionRequest,
    PublicMessage,
    RefundRequestAction,
    TicketActionRequest,
    TicketConfirmationRequest,
)
from app.services.history import to_public_messages
from app.services.knowledge_admin import KnowledgeAdminService
from app.services.orders import OrderData
from app.services.tool_chat import ToolAwareChatService
from app.sse import SSE_HEADERS, encode_sse
from app.tools.business import ToolRuntime, build_business_tools
from app.tools.executor import ToolExecutionFailed, ToolExecutor
from app.tools.registry import ToolRegistry
from app.workflow.graph import PendingWorkflowAction, WorkflowChatService


SAFE_DB_ERROR = {
    "code": "persistence_unavailable",
    "message": "消息服务暂时不可用，请稍后重试",
}
SAFE_NOT_FOUND = {
    "code": "conversation_not_found",
    "message": "会话不存在",
}


def chat_router(
    settings: Settings,
    model: object,
    session_factory: async_sessionmaker[AsyncSession],
    tool_chat_service: ToolAwareChatService,
    knowledge_admin_service: KnowledgeAdminService,
) -> APIRouter:
    """Create routes bound to explicit application-owned dependencies."""

    router = APIRouter(tags=["conversations"])
    extractor = AfterSalesExtractor(
        model,  # type: ignore[arg-type]
        method=settings.structured_output_method,
    )

    @router.post(
        "/v1/after-sales/extract",
        response_model=AfterSalesExtraction,
        dependencies=[Depends(bind_admin_settings(settings)), Depends(authorize)],
    )
    async def extract_after_sales(request: ExtractionRequest) -> AfterSalesExtraction:
        try:
            return await extractor.extract(request.description)
        except StructuredOutputError as error:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "structured_output_error",
                    "message": "售后信息暂时无法解析，请稍后重试",
                },
            ) from error

    @router.post("/v1/chat/stream")
    async def stream_chat(request: ChatRequest) -> StreamingResponse:
        try:
            turn = await tool_chat_service.prepare_turn(request)
        except ConversationNotFound as error:
            raise HTTPException(status_code=404, detail=SAFE_NOT_FOUND) from error
        except PendingWorkflowAction as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ticket_confirmation_pending",
                    "message": "请先确认或取消当前工单预览",
                },
            ) from error
        except ContextBudgetExceeded as error:
            raise HTTPException(
                status_code=422,
                detail={"code": "context_budget_exceeded"},
            ) from error
        except SQLAlchemyError as error:
            raise HTTPException(status_code=503, detail=SAFE_DB_ERROR) from error

        async def event_bytes() -> AsyncGenerator[bytes, None]:
            async for event in tool_chat_service.stream_turn(turn):
                yield encode_sse(event.name, event.data)

        return StreamingResponse(
            event_bytes(),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    @router.get("/api/conversations")
    async def list_conversations(
        user_id: Annotated[UUID, Header(alias="X-Demo-User-Id")],
    ) -> list[dict[str, object]]:
        try:
            async with session_factory() as session:
                return await ConversationRepository(session).list_owned(str(user_id))
        except SQLAlchemyError as error:
            raise HTTPException(status_code=503, detail=SAFE_DB_ERROR) from error

    @router.get("/api/conversations/{conversation_id}/context-status")
    async def conversation_context_status(
        conversation_id: PositiveInt,
        user_id: Annotated[UUID, Header(alias="X-Demo-User-Id")],
        response: Response,
    ) -> dict[str, str]:
        try:
            async with session_factory() as session:
                await ConversationRepository(session).get_owned(
                    conversation_id, str(user_id)
                )
        except ConversationNotFound as error:
            raise HTTPException(status_code=404, detail=SAFE_NOT_FOUND) from error
        except SQLAlchemyError as error:
            raise HTTPException(status_code=503, detail=SAFE_DB_ERROR) from error
        response.headers["Cache-Control"] = "no-store"
        running = isinstance(
            tool_chat_service, WorkflowChatService
        ) and tool_chat_service.is_summarizing(conversation_id)
        return {"status": "summarizing" if running else "idle"}

    @router.get(
        "/api/conversations/{conversation_id}/messages",
        response_model=list[PublicMessage],
    )
    @router.get(
        "/v1/conversations/{conversation_id}/messages",
        response_model=list[PublicMessage],
    )
    async def conversation_messages(
        conversation_id: PositiveInt,
        user_id: Annotated[UUID, Header(alias="X-Demo-User-Id")],
    ) -> list[PublicMessage]:
        try:
            async with session_factory() as session:
                await ConversationRepository(session).get_owned(
                    conversation_id, str(user_id)
                )
                rows = await MessageRepository(session).list_ordered(conversation_id)
        except ConversationNotFound as error:
            raise HTTPException(status_code=404, detail=SAFE_NOT_FOUND) from error
        except SQLAlchemyError as error:
            raise HTTPException(status_code=503, detail=SAFE_DB_ERROR) from error

        runtime = ToolRuntime(conversation_id, session_factory, Random())
        registry = ToolRegistry(build_business_tools(runtime), runtime=runtime)
        safe_labels = {name: registry.safe_label(name) for name in registry.names}
        return to_public_messages(rows, safe_labels)

    @router.get("/v1/orders")
    async def owned_orders(
        user_id: Annotated[UUID, Header(alias="X-Demo-User-Id")],
    ) -> list[dict[str, object]]:
        try:
            return await OrderData(session_factory).list_orders(str(user_id))
        except SQLAlchemyError as error:
            raise HTTPException(status_code=503, detail=SAFE_DB_ERROR) from error

    @router.get("/v1/conversations/{conversation_id}/pending-order")
    async def pending_order_selection(
        conversation_id: PositiveInt,
        user_id: Annotated[UUID, Header(alias="X-Demo-User-Id")],
    ) -> dict[str, object]:
        try:
            async with session_factory() as session:
                await ConversationRepository(session).get_owned(
                    conversation_id, str(user_id)
                )
        except ConversationNotFound as error:
            raise HTTPException(status_code=404, detail=SAFE_NOT_FOUND) from error
        if not isinstance(tool_chat_service, WorkflowChatService):
            return {"pending": False}
        payload = await tool_chat_service.pending_order(str(user_id), conversation_id)
        return {"pending": payload is not None, "selection": payload}

    @router.post("/v1/conversations/{conversation_id}/select-order")
    async def select_order(
        conversation_id: PositiveInt,
        request: OrderSelectionRequest,
    ) -> StreamingResponse:
        try:
            async with session_factory() as session:
                await ConversationRepository(session).get_owned(
                    conversation_id, str(request.user_id)
                )
        except ConversationNotFound as error:
            raise HTTPException(status_code=404, detail=SAFE_NOT_FOUND) from error
        if not isinstance(tool_chat_service, WorkflowChatService):
            raise HTTPException(
                status_code=409, detail={"code": "order_selection_unavailable"}
            )
        pending = await tool_chat_service.pending_order(
            str(request.user_id), conversation_id
        )
        allowed_orders = {
            str(item["order_id"])
            for item in (pending.get("orders", []) if pending else [])
            if isinstance(item, dict) and "order_id" in item
        }
        if (
            pending is None
            or pending.get("message_id") != request.message_id
            or request.order_id not in allowed_orders
        ):
            raise HTTPException(
                status_code=409, detail={"code": "order_selection_not_pending"}
            )

        async def event_bytes() -> AsyncGenerator[bytes, None]:
            try:
                async for event in tool_chat_service.resume_order(
                    str(request.user_id),
                    conversation_id,
                    request.order_id,
                    request.message_id,
                ):
                    yield encode_sse(event.name, event.data)
            except OrderNotFound:
                yield encode_sse(
                    "error",
                    {
                        "message": "没有找到您的这笔订单，请刷新重试。",
                        "code": "order_not_found",
                    },
                )
            except ValueError:
                yield encode_sse(
                    "error",
                    {
                        "message": "该订单选择已处理或已失效，请刷新。",
                        "code": "order_selection_not_pending",
                    },
                )

        return StreamingResponse(
            event_bytes(), media_type="text/event-stream", headers=SSE_HEADERS
        )

    @router.post("/v1/conversations/{conversation_id}/tickets")
    async def create_explicit_ticket(
        conversation_id: PositiveInt, request: TicketActionRequest
    ) -> dict[str, str]:
        """Execute the existing ticket tool only after an owned assistant suggestion is clicked."""

        try:
            async with session_factory() as session:
                await ConversationRepository(session).get_owned(
                    conversation_id, str(request.user_id)
                )
                suggestion = await session.scalar(
                    select(Message).where(
                        Message.id == request.message_id,
                        Message.conversation_id == conversation_id,
                        Message.role == "assistant",
                    )
                )
                if suggestion is None or "建工单" not in (
                    suggestion.suggested_actions or []
                ):
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "ticket_action_not_offered",
                            "message": "当前消息未提供创建工单入口",
                        },
                    )
        except ConversationNotFound as error:
            raise HTTPException(status_code=404, detail=SAFE_NOT_FOUND) from error
        except SQLAlchemyError as error:
            raise HTTPException(status_code=503, detail=SAFE_DB_ERROR) from error

        runtime = ToolRuntime(
            conversation_id,
            session_factory,
            Random(),
            user_id=str(request.user_id),
            ticket_key=f"ticket-click-{request.message_id}",
        )
        registry = ToolRegistry(build_business_tools(runtime), runtime=runtime)
        executor = ToolExecutor(
            registry,
            timeout_seconds=settings.tool_timeout_seconds,
            max_attempts=settings.tool_max_attempts,
        )
        try:
            result = await executor.execute(
                {
                    "id": f"ticket-click-{request.message_id}",
                    "name": "create_ticket",
                    "args": {
                        "description": request.description,
                        "ticket_type": request.ticket_type,
                    },
                },
                write_requested=True,
                confirmed=True,
            )
            payload = json.loads(result.content)
            if not result.ok:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "ticket_creation_failed",
                        "message": "工单暂时无法创建，请稍后重试",
                    },
                )
            ticket_no = str(payload["ticket_no"])
            ticket_status = str(payload["status"])
            created_at = str(payload["created_at"])
            confirmation_prefix = f"工单已创建：{ticket_no}"
            async with session_factory() as session, session.begin():
                existing = await session.scalar(
                    select(Message.id)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.role == "assistant",
                        Message.content.like(f"{confirmation_prefix}%"),
                    )
                    .limit(1)
                )
                if existing is None:
                    await MessageRepository(session).add_assistant(
                        conversation_id,
                        f"工单已创建：{ticket_no}（{ticket_status}），反馈类别：{request.ticket_type}。",
                    )
            return {
                "ticket_no": ticket_no,
                "status": ticket_status,
                "created_at": created_at,
            }
        except ToolExecutionFailed as error:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ticket_creation_failed",
                    "message": "工单暂时无法创建，请稍后重试",
                },
            ) from error
        except SQLAlchemyError as error:
            raise HTTPException(status_code=503, detail=SAFE_DB_ERROR) from error

    @router.get("/v1/conversations/{conversation_id}/pending-ticket")
    async def pending_ticket(
        conversation_id: PositiveInt,
        user_id: Annotated[UUID, Header(alias="X-Demo-User-Id")],
    ) -> dict[str, Any]:
        try:
            async with session_factory() as session:
                await ConversationRepository(session).get_owned(
                    conversation_id, str(user_id)
                )
        except ConversationNotFound as error:
            raise HTTPException(status_code=404, detail=SAFE_NOT_FOUND) from error
        payload = (
            await tool_chat_service.pending_ticket(str(user_id), conversation_id)
            if isinstance(tool_chat_service, WorkflowChatService)
            else None
        )
        return {"pending": payload is not None, "confirmation": payload}

    @router.post("/v1/conversations/{conversation_id}/confirm-ticket")
    async def confirm_ticket(
        conversation_id: PositiveInt, request: TicketConfirmationRequest
    ) -> StreamingResponse:
        try:
            async with session_factory() as session:
                await ConversationRepository(session).get_owned(
                    conversation_id, str(request.user_id)
                )
        except ConversationNotFound as error:
            raise HTTPException(status_code=404, detail=SAFE_NOT_FOUND) from error
        if not isinstance(tool_chat_service, WorkflowChatService):
            raise HTTPException(
                status_code=409, detail={"code": "ticket_confirmation_unavailable"}
            )
        pending = await tool_chat_service.pending_ticket(
            str(request.user_id), conversation_id
        )
        if (
            pending is None
            or pending.get("proposal_id") != request.proposal_id
            or pending.get("message_id") != request.message_id
        ):
            raise HTTPException(
                status_code=409, detail={"code": "ticket_confirmation_not_pending"}
            )

        async def event_bytes() -> AsyncGenerator[bytes, None]:
            try:
                async for event in tool_chat_service.resume_ticket(
                    str(request.user_id),
                    conversation_id,
                    request.proposal_id,
                    request.message_id,
                    request.confirmed,
                ):
                    yield encode_sse(event.name, event.data)
            except ValueError:
                yield encode_sse(
                    "error",
                    {
                        "message": "该工单预览已处理或已失效，请刷新。",
                        "code": "ticket_confirmation_not_pending",
                    },
                )

        return StreamingResponse(
            event_bytes(), media_type="text/event-stream", headers=SSE_HEADERS
        )

    @router.post("/v1/conversations/{conversation_id}/refund-requests")
    async def submit_refund_request(
        conversation_id: PositiveInt,
        request: RefundRequestAction,
    ) -> dict[str, str]:
        """Store an explicit application; no payment refund is executed."""

        try:
            async with session_factory() as session, session.begin():
                await ConversationRepository(session).get_owned(
                    conversation_id, str(request.user_id)
                )
                offered = await session.scalar(
                    select(Message).where(
                        Message.id == request.message_id,
                        Message.conversation_id == conversation_id,
                        Message.role == "assistant",
                    )
                )
                if (
                    offered is None
                    or "申请退款" not in (offered.suggested_actions or [])
                    or offered.related_order_id != request.order_id
                ):
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "refund_action_not_offered",
                            "message": "当前消息未绑定该订单的退款申请入口",
                        },
                    )
                order = await OrderRepository(session).get_owned(
                    str(request.user_id), request.order_id
                )
                if order["status"] == "pending_payment":
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "order_not_paid",
                            "message": "订单尚未付款，无需申请退款",
                        },
                    )
                row = await RefundRequestRepository(session).create_or_get(
                    conversation_id=conversation_id,
                    source_message_id=request.message_id,
                    order_id=request.order_id,
                    order_user_id=str(request.user_id),
                    reason=request.reason,
                    description=request.description,
                )
                confirmation_prefix = f"退款申请已提交：{row.request_no}"
                existing = await session.scalar(
                    select(Message.id)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.role == "assistant",
                        Message.content.like(f"{confirmation_prefix}%"),
                    )
                    .limit(1)
                )
                if existing is None:
                    await MessageRepository(session).add_assistant(
                        conversation_id,
                        f"退款申请已提交：{row.request_no}（{row.status}），关联订单 {request.order_id}，退款原因：{request.reason}。",
                        related_order_id=request.order_id,
                    )
                created_at = (
                    row.created_at.replace(tzinfo=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
                return {
                    "request_no": row.request_no,
                    "status": row.status,
                    "created_at": created_at,
                }
        except ConversationNotFound as error:
            raise HTTPException(status_code=404, detail=SAFE_NOT_FOUND) from error
        except RefundRequestConflict as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "refund_request_already_submitted",
                    "message": "该消息对应的退款申请已提交，请勿修改原因或说明重复提交",
                },
            ) from error
        except OrderNotFound as error:
            raise HTTPException(
                status_code=404,
                detail={"code": "order_not_found", "message": "没有找到您的这笔订单"},
            ) from error
        except SQLAlchemyError as error:
            raise HTTPException(status_code=503, detail=SAFE_DB_ERROR) from error

    return router
