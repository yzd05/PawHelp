"""Customer and operator page entry points, served by the same application."""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from app.api.auth import local_page_request

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
CHAT_PAGE_PATH = STATIC_DIR / "index.html"
KNOWLEDGE_PAGE_PATH = STATIC_DIR / "kb.html"
RAG_EVAL_PAGE_PATH = STATIC_DIR / "rag-eval.html"

ALLOWED_MIGRATION_TARGETS = frozenset(
    {"pawhelp.userId", "pawhelp.conversationId", "pawhelp.messageFeedback.server"}
)
ALLOWED_MIGRATION_KINDS = frozenset({"uuid", "positive_integer", "json_object"})
NO_MIGRATION_POLICY: dict[str, object] = {"needed": False, "id": None, "rules": []}


def _read_migration_policy(path: Path) -> dict[str, object]:
    """Read and validate local storage migration rules off the event loop."""

    if path.stat().st_size > 4096:
        raise ValueError("policy too large")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("id"), str)
        or not isinstance(data.get("rules"), list)
        or len(data["rules"]) > 3
        or any(
            not isinstance(row, dict)
            or row.get("target") not in ALLOWED_MIGRATION_TARGETS
            or not isinstance(row.get("source"), str)
            or row.get("kind") not in ALLOWED_MIGRATION_KINDS
            for row in data["rules"]
        )
    ):
        raise ValueError("invalid migration policy")
    return {
        "needed": bool(data["rules"]),
        "id": data["id"],
        "rules": data["rules"],
    }


def page_router(runtime_root: Path | None = None) -> APIRouter:
    router = APIRouter()
    base_root = runtime_root if runtime_root is not None else STATIC_DIR.parents[1]

    @router.get("/v1/client-storage-migration", include_in_schema=False)
    async def client_storage_migration(request: Request) -> JSONResponse:
        """One-release local migration policy, never browser values or credentials."""

        path = base_root / ".runtime" / "client-storage-migration.json"
        if not await asyncio.to_thread(path.exists):
            return JSONResponse(NO_MIGRATION_POLICY, headers={"Cache-Control": "no-store"})
        if not local_page_request(request):
            raise HTTPException(status_code=403, detail="本地会话迁移仅支持本机同源访问")
        try:
            data = await asyncio.to_thread(_read_migration_policy, path)
        except FileNotFoundError:
            data = NO_MIGRATION_POLICY
        except (OSError, UnicodeError, ValueError):
            raise HTTPException(
                status_code=503, detail="本地会话迁移配置无效，请联系维护人员"
            ) from None
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @router.get("/admin", include_in_schema=False)
    async def admin_home() -> RedirectResponse:
        return RedirectResponse("/review", status_code=307)

    @router.get("/", include_in_schema=False, response_class=FileResponse)
    async def chat_page() -> FileResponse:
        """Serve the browser chat client with bundled Markdown assets."""

        return FileResponse(
            CHAT_PAGE_PATH,
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/kb", include_in_schema=False, response_class=FileResponse)
    async def knowledge_page() -> FileResponse:
        """Serve the local knowledge console."""

        return FileResponse(
            KNOWLEDGE_PAGE_PATH,
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/rag-eval", include_in_schema=False, response_class=FileResponse)
    async def rag_eval_page() -> FileResponse:
        return FileResponse(
            RAG_EVAL_PAGE_PATH,
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/review", include_in_schema=False, response_class=FileResponse)
    async def review_page() -> FileResponse:
        return FileResponse(
            CHAT_PAGE_PATH.parent / "review.html",
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/observability", include_in_schema=False, response_class=FileResponse)
    async def observability_page() -> FileResponse:
        return FileResponse(
            CHAT_PAGE_PATH.parent / "observability.html",
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    return router
