"""RAG evaluation and human review endpoints; separate from application assembly."""

import asyncio
from collections.abc import Awaitable, Callable
import inspect
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.auth import authorize, bind_admin_settings
from app.database.models import FaithCase
from app.evaluation.pipeline import EvaluationPipeline
from app.evaluation.runtime import DEFAULT_EVAL_CASES_PATH, report_dataset_alignment
from app.repositories.quality import (
    FaithCaseConflict,
    FaithCaseNotFound,
    QualityRepository,
)
from app.schemas import FaithCaseReviewRequest, RagEvalRunRequest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAG_EVAL_CASES_PATH = PROJECT_ROOT / DEFAULT_EVAL_CASES_PATH
RAG_EVAL_REPORT_DIR = PROJECT_ROOT / "reports"
RAG_EVAL_REPORT_PATH = RAG_EVAL_REPORT_DIR / "retrieval-evaluation.json"
RAG_EVAL_BASELINE_REPORT_PATH = (
    RAG_EVAL_REPORT_DIR / "retrieval-evaluation.baseline.json"
)
RagEvalRunner = Callable[[bool], Awaitable[dict[str, object]]]


def _read_rag_eval_report(runtime_root: Path | None = None) -> dict[str, object]:
    if runtime_root is not None:
        report_dir = runtime_root / "reports"
        report_path = report_dir / "retrieval-evaluation.json"
        baseline_path = report_dir / "retrieval-evaluation.baseline.json"
    else:
        report_path = RAG_EVAL_REPORT_PATH
        baseline_path = RAG_EVAL_BASELINE_REPORT_PATH

    is_baseline = False
    if report_path.is_file():
        target_path = report_path
    elif baseline_path.is_file():
        target_path = baseline_path
        is_baseline = True
    else:
        raise HTTPException(
            status_code=404, detail={"code": "rag_eval_report_not_found"}
        )

    try:
        payload = json.loads(target_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("report must be an object")
        if is_baseline:
            return {**payload, "is_baseline_snapshot": True}
        return payload
    except (OSError, ValueError) as error:
        raise HTTPException(
            status_code=503, detail={"code": "rag_eval_report_unavailable"}
        ) from error


def _call_read_rag_eval_report(runtime_root: Path | None = None) -> dict[str, object]:
    if runtime_root is None or not inspect.signature(_read_rag_eval_report).parameters:
        return _read_rag_eval_report()
    return _read_rag_eval_report(runtime_root)


def _load_aligned_rag_eval_report(runtime_root: Path | None = None) -> dict[str, object]:
    payload = dict(_call_read_rag_eval_report(runtime_root))
    cases_path = (
        runtime_root / DEFAULT_EVAL_CASES_PATH
        if runtime_root is not None and (runtime_root / DEFAULT_EVAL_CASES_PATH).is_file()
        else RAG_EVAL_CASES_PATH
    )
    try:
        payload["dataset_alignment"] = report_dataset_alignment(payload, cases_path)
    except (OSError, ValueError):
        payload["dataset_alignment"] = {
            "status": "unverified",
            "changed_cases": 0,
        }
    return payload


def _flagged_faith_rows(report: dict[str, object]) -> dict[str, dict[str, object]]:
    cases = report.get("cases")
    if not isinstance(cases, list):
        return {}
    return {
        str(row["case_id"]): row
        for row in cases
        if isinstance(row, dict)
        and row.get("strategy") == "hybrid_rerank"
        and row.get("faithful") is False
        and isinstance(row.get("case_id"), str)
    }


def _faith_review_state(
    row: FaithCase | None, snapshot: dict[str, object]
) -> dict[str, object]:
    if row is None:
        return {"available": False, "message": "这道题没有保存人工复核记录"}
    if (
        row.answer != str(snapshot.get("answer") or "")
        or row.reason != str(snapshot.get("faithfulness_reason") or "")
    ):
        return {"available": False, "message": "记录与当前报告不一致，请刷新后复核"}
    return {
        "available": True,
        "status": row.status,
        "resolution": row.resolution,
        "seen_count": row.seen_count,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
    }


def evaluation_router(
    active_session_factory: async_sessionmaker[AsyncSession],
    pipeline: EvaluationPipeline,
    rag_eval_lock: asyncio.Lock,
    active_rag_eval_runner: RagEvalRunner,
    settings: object | None = None,
    runtime_root: Path | None = None,
) -> APIRouter:
    router = APIRouter(
        tags=["evaluation"],
        dependencies=[Depends(bind_admin_settings(settings)), Depends(authorize)],
    )

    @router.get("/v1/rag-eval/report", response_class=JSONResponse)
    async def rag_eval_report() -> JSONResponse:
        payload = await asyncio.to_thread(_load_aligned_rag_eval_report, runtime_root)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @router.get("/v1/rag-eval/faith-cases", response_class=JSONResponse)
    async def rag_eval_faith_cases() -> JSONResponse:
        report = await asyncio.to_thread(_call_read_rag_eval_report, runtime_root)
        flagged = _flagged_faith_rows(report)
        try:
            async with active_session_factory() as session:
                rows = await QualityRepository(session).list_faith_cases(list(flagged))
        except Exception as error:
            raise HTTPException(
                status_code=503, detail={"code": "faith_case_ledger_unavailable"}
            ) from error
        by_id = {row.eval_id: row for row in rows}
        cases = {
            eval_id: _faith_review_state(by_id.get(eval_id), snapshot)
            for eval_id, snapshot in flagged.items()
        }
        return JSONResponse({"cases": cases}, headers={"Cache-Control": "no-store"})

    @router.post("/v1/rag-eval/faith-cases/{eval_id}/status", response_class=JSONResponse)
    async def review_rag_eval_faith_case(
        eval_id: str, request: FaithCaseReviewRequest
    ) -> JSONResponse:
        if request.status != "未解决" and not request.resolution:
            raise HTTPException(
                status_code=400, detail={"code": "review_reason_required"}
            )
        if rag_eval_lock.locked():
            raise HTTPException(status_code=409, detail={"code": "rag_eval_running"})
        async with rag_eval_lock:
            report = await asyncio.to_thread(_call_read_rag_eval_report, runtime_root)
            snapshot = _flagged_faith_rows(report).get(eval_id)
            if snapshot is None:
                raise HTTPException(
                    status_code=404, detail={"code": "faith_case_not_in_report"}
                )
            try:
                async with active_session_factory() as session, session.begin():
                    row = await QualityRepository(session).review_faith_case(
                        eval_id=eval_id,
                        answer=str(snapshot.get("answer") or ""),
                        judge_reason=str(snapshot.get("faithfulness_reason") or ""),
                        expected_seen_count=request.expected_seen_count,
                        expected_status=request.expected_status,
                        expected_resolution=request.expected_resolution,
                        status=request.status,
                        resolution=(
                            request.resolution if request.status != "未解决" else None
                        ),
                    )
                    state = _faith_review_state(row, snapshot)
            except FaithCaseNotFound as error:
                raise HTTPException(
                    status_code=404, detail={"code": "faith_case_ledger_missing"}
                ) from error
            except FaithCaseConflict as error:
                raise HTTPException(
                    status_code=409, detail={"code": "faith_case_changed"}
                ) from error
            except Exception as error:
                raise HTTPException(
                    status_code=503, detail={"code": "faith_case_save_failed"}
                ) from error
        return JSONResponse(state, headers={"Cache-Control": "no-store"})

    @router.post("/v1/rag-eval/run", response_class=JSONResponse)
    async def rerun_rag_eval(request: RagEvalRunRequest) -> JSONResponse:
        if rag_eval_lock.locked():
            raise HTTPException(
                status_code=409,
                detail={"code": "rag_eval_already_running"},
            )
        async with rag_eval_lock:
            try:
                payload = await active_rag_eval_runner(request.full_generation)
                if payload.get("dataset"):
                    await pipeline.record(payload)
            except Exception as error:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "rag_eval_failed"},
                ) from error
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    return router
