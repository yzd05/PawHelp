"""Browser chat page and modular static asset delivery tests."""

from pathlib import Path
import re

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import CHAT_PAGE_PATH, STRICT_CSP, create_app
from tests.fakes import FakeChatModel

DATABASE_URL = "mysql+asyncmy://pawhelp:pawhelp@127.0.0.1:3306/pawhelp"


def _settings() -> Settings:
    return Settings(
        chat_base_url="http://localhost:11434/v1",
        openai_model="qwen3",
        openai_api_key="ollama",
        database_url=DATABASE_URL,
    )


def test_chat_page_is_served_from_app_static_without_inline_scripts_or_styles() -> None:
    with TestClient(create_app(settings=_settings(), model=FakeChatModel())) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert CHAT_PAGE_PATH == Path(__file__).parents[1] / "app" / "static" / "index.html"
    assert response.content == CHAT_PAGE_PATH.read_bytes()
    assert "<style>" not in response.text
    assert "<script>" not in response.text
    assert re.search(r"\sstyle\s*=", response.text) is None
    assert re.search(r"\son[a-z]+\s*=", response.text, flags=re.IGNORECASE) is None
    assert 'maxlength="2400"' in response.text
    assert "/static/chat-app.js" in response.text
    for forbidden_copy in (
        "数据库按 UTC 保存",
        "请选择固定原因类目",
        "正在压缩会话上下文",
    ):
        assert forbidden_copy not in response.text
    assert "正在整理历史对话" in response.text
    assert "请选择退款原因" in response.text


def test_chat_markdown_and_app_assets_are_served_locally() -> None:
    with TestClient(create_app(settings=_settings(), model=FakeChatModel())) as client:
        for name in [
            "chat-app.js",
            "chat-markdown.js",
            "context-status.js",
            "storage-migration.js",
            "vendor/marked-18.1.0.umd.js",
            "vendor/purify-3.4.16.min.js",
        ]:
            response = client.get("/static/" + name)
            assert response.status_code == 200
            assert "javascript" in response.headers["content-type"]
            assert response.content == (CHAT_PAGE_PATH.parent / name).read_bytes()
            if name == "chat-app.js":
                assert "本轮未调用工具" not in response.text
                assert "isComposing" in response.text
        assert client.get("/static/missing-file.js").status_code == 404


def test_strict_csp_and_security_headers_enforce_on_pages_and_exempt_openapi_docs() -> None:
    app = create_app(settings=_settings(), model=FakeChatModel())
    assert app.version == "0.1.0"

    with TestClient(app) as client:
        for path in ("/", "/kb", "/rag-eval", "/review", "/observability", "/topics", "/static/chat-app.js", "/health"):
            resp = client.get(path)
            assert resp.headers["content-security-policy"] == STRICT_CSP, path
            assert "unsafe-inline" not in resp.headers["content-security-policy"]
            assert "unsafe-eval" not in resp.headers["content-security-policy"]
            assert resp.headers["x-content-type-options"] == "nosniff"
            assert resp.headers["x-frame-options"] == "DENY"
            assert resp.headers["referrer-policy"] == "same-origin"

        for doc_path in ("/docs", "/redoc", "/openapi.json"):
            doc_resp = client.get(doc_path)
            assert doc_resp.status_code == 200
            assert "content-security-policy" not in doc_resp.headers
            assert doc_resp.headers["x-content-type-options"] == "nosniff"
            assert doc_resp.headers["x-frame-options"] == "DENY"
            assert doc_resp.headers["referrer-policy"] == "same-origin"
            if doc_path == "/openapi.json":
                assert doc_resp.json()["info"]["version"] == "0.1.0"
