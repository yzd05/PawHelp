"""Customer feedback and local-only, same-origin operations access."""

import asyncio
import logging
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text

from app.api.auth import (
    _local_page_request,
    authorize,
    bind_admin_settings,
    local_page_request,
)
from app.evaluation.dashboard import calibration_report, trend_report
from app.evaluation.pipeline import cost_by_intent
from app.repositories.conversations import ConversationNotFound
from app.schemas import ReviewRequest
from app.services.knowledge_admin import KnowledgeCheckUnavailable

logger = logging.getLogger(__name__)

__all__ = [
    "FeedbackRequest",
    "ReviewRequest",
    "ScheduledOperations",
    "_local_page_request",
    "local_page_request",
    "operations_router",
]


class FeedbackRequest(BaseModel):
    vote: Literal["up", "down"]


def operations_router(settings, sessions, flywheel, pipeline) -> APIRouter:
    router = APIRouter()
    admin = APIRouter(
        dependencies=[Depends(bind_admin_settings(settings)), Depends(authorize)]
    )

    @router.post("/v1/conversations/{conversation_id}/messages/{message_id}/feedback")
    async def feedback(
        conversation_id: int,
        message_id: int,
        request: FeedbackRequest,
        user_id: Annotated[UUID, Header(alias="X-Demo-User-Id")],
    ):
        try:
            return await flywheel.feedback(
                conversation_id, message_id, str(user_id), request.vote
            )
        except (ConversationNotFound, LookupError) as error:
            raise HTTPException(status_code=404, detail="回答不存在") from error

    @admin.get("/api/review")
    async def list_reviews(
        status: Literal["全部", "待审", "通过", "驳回"] = "待审",
        offset: int = 0,
        sort: Literal["frequency", "newest"] = "frequency",
    ):
        if offset < 0:
            raise HTTPException(status_code=422, detail="offset must be non-negative")
        return await flywheel.list(status, offset, sort)

    @admin.post("/api/review/process")
    async def process():
        try:
            return await flywheel.process()
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @admin.get("/api/review/{review_id}")
    async def detail(review_id: int):
        try:
            return await flywheel.detail(review_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @admin.post("/api/review/{review_id}/approve")
    async def approve(review_id: int, request: ReviewRequest):
        try:
            return await flywheel.review(review_id, request.approved_answer)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except KnowledgeCheckUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @admin.post("/api/review/{review_id}/reject")
    async def reject(review_id: int):
        try:
            return await flywheel.review(review_id, reject=True)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @admin.post("/api/review/{review_id}/publish")
    async def publish(review_id: int):
        try:
            return await flywheel.publish(review_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @admin.get("/api/observability")
    async def overview(days: Annotated[int, Query(ge=1, le=90)] = 7):
        history = await pipeline.history()
        calibration = await asyncio.to_thread(
            calibration_report,
            settings,
            getattr(pipeline, "root", Path(__file__).resolve().parents[2]),
        )
        return {
            "costs": await cost_by_intent(sessions, settings, days=days),
            "eval_runs": history,
            "trend": trend_report(history),
            "calibration": calibration,
            "last_evaluation_error": pipeline.last_error,
            "configuration": {
                "langfuse_url": settings.langfuse_base_url,
                "flywheel_interval_seconds": settings.flywheel_interval_seconds,
                "evaluation_interval_seconds": settings.evaluation_interval_seconds,
                "sample_per_bucket": settings.evaluation_sample_per_bucket,
                "max_model_calls": settings.evaluation_max_model_calls,
            },
        }

    @admin.post("/api/observability/calibration/analyze")
    async def analyze_calibration():
        return await asyncio.to_thread(
            calibration_report,
            settings,
            getattr(pipeline, "root", Path(__file__).resolve().parents[2]),
        )

    @admin.post("/api/observability/evaluate")
    async def evaluate():
        try:
            return await pipeline.run()
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(
                status_code=503,
                detail="评估未完成，查看 log/app.log 与模型余额；没有生成虚假成功轮次",
            ) from error

    router.include_router(admin)
    return router


class ScheduledOperations:
    def __init__(self, settings, sessions, flywheel, pipeline) -> None:
        self.settings = settings
        self.sessions = sessions
        self.flywheel = flywheel
        self.pipeline = pipeline
        self.tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        jobs = (
            ("pawhelp-flywheel", self.settings.flywheel_interval_seconds, self.flywheel.process),
            (
                "pawhelp-evaluation",
                self.settings.evaluation_interval_seconds,
                lambda: self.pipeline.run("定时"),
            ),
        )
        for name, interval, work in jobs:
            if interval:
                self.tasks.append(asyncio.create_task(self._loop(name, interval, work)))

    async def _loop(self, name: str, interval: int, work) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                # Connection-scoped lease prevents multiple Uvicorn workers running the same scheduled job.
                async with self.sessions() as lease:
                    acquired = await lease.scalar(
                        text("SELECT GET_LOCK(:name, 0)"), {"name": name}
                    )
                    if acquired != 1:
                        continue
                    try:
                        await work()
                    finally:
                        await lease.execute(
                            text("SELECT RELEASE_LOCK(:name)"), {"name": name}
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "scheduled operation failed job=%s; next interval may retry", name
                )

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
