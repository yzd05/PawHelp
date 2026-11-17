"""RAG evaluation console delivery and report API contracts."""

import pytest
from fastapi.testclient import TestClient

from app.api.evaluation import RAG_EVAL_REPORT_PATH
from app.config import Settings
from app.main import RAG_EVAL_PAGE_PATH, create_app
from tests.fakes import FakeChatModel

DATABASE_URL = "mysql+asyncmy://pawhelp:pawhelp@127.0.0.1:3306/pawhelp"
LOCAL_ADMIN_HEADERS = {"X-PawHelp-Local-Admin": "1"}


def _settings() -> Settings:
    return Settings(
        chat_base_url="http://localhost:11434/v1",
        openai_model="qwen3",
        openai_api_key="ollama",
        database_url=DATABASE_URL,
    )


def _admin_client(app) -> TestClient:
    return TestClient(
        app,
        base_url="http://localhost:8000",
        client=("127.0.0.1", 50000),
        headers=LOCAL_ADMIN_HEADERS,
    )


def test_rag_eval_page_and_external_modules_are_served() -> None:
    with _admin_client(create_app(settings=_settings(), model=FakeChatModel())) as client:
        page = client.get("/rag-eval")
        stylesheet = client.get("/static/rag-eval.css")
        base_script = client.get("/static/operations-base.js")
        module_script = client.get("/static/rag-eval.js")

    assert page.status_code == 200
    assert page.content == RAG_EVAL_PAGE_PATH.read_bytes()
    assert "<style>" not in page.text
    assert "<script>" not in page.text
    assert '/static/rag-eval.css' in page.text
    assert '/static/operations-base.js' in page.text
    assert '/static/rag-eval.js' in page.text
    assert "RAG 质量评估" in page.text
    assert "重新运行评估" in page.text
    assert "完整数据" in page.text
    assert "需人工复核的问题（仅混合 + Rerank）" in page.text
    assert "逐题证据审计" in page.text
    assert 'id="fullGeneration" type="checkbox" checked' in page.text
    assert 'id="issuePagination"' in page.text
    assert "召回材料要点命中（字面）" in page.text
    assert "最终答案要点覆盖（模型）" in page.text
    assert "回答事实有据（模型）" in page.text
    assert 'id="retrievalMetricHelp"' in page.text
    assert 'id="generationMetricHelp"' in page.text
    assert 'id="productionBreakdown" hidden' in page.text
    assert 'id="refusalCard"' not in page.text

    assert stylesheet.status_code == 200
    assert base_script.status_code == 200
    assert module_script.status_code == 200
    assert "adminHeaders" in module_script.text
    assert "当前评测题已修改，页面显示的是旧题目的历史分数" in module_script.text
    assert "旧口径报告，不含答案语义覆盖度" in module_script.text
    assert "知识块回答" in module_script.text
    assert "评估步骤报错" in module_script.text
    assert "疑似编造回答" in module_script.text
    assert "模型调用失败" not in module_script.text
    assert "台账写入失败" not in module_script.text
    assert '["口语类 MRR 提升"' not in module_script.text
    assert '["答案语义覆盖"' not in module_script.text


# The full report is a local, gitignored run artifact; CI has no copy.
@pytest.mark.skipif(not RAG_EVAL_REPORT_PATH.exists(), reason="local retrieval report not generated")
def test_local_report_is_served() -> None:
    with _admin_client(create_app(settings=_settings(), model=FakeChatModel())) as client:
        report = client.get("/v1/rag-eval/report")

    assert report.status_code == 200
    payload = report.json()
    assert payload["dataset_alignment"]["status"] in {"current", "stale", "unverified"}
    assert set(payload["strategies"]) == {
        "dense",
        "bm25",
        "hybrid",
        "hybrid_rerank",
    }
    assert len(payload["cases"]) == (
        payload["dataset"]["total_cases"] * len(payload["strategies"])
    )
    assert any(
        isinstance(citation.get("answer"), str) and citation["answer"]
        for row in payload["cases"]
        for citation in row.get("citations", [])
    )


def test_rag_eval_rerun_requires_confirmation_and_returns_runner_report() -> None:
    calls = 0

    async def fake_runner(full_generation: bool) -> dict[str, object]:
        nonlocal calls
        assert full_generation is True
        calls += 1
        return {"strategies": {"dense": {"mrr": 1.0}}, "cases": []}

    with _admin_client(
        create_app(
            settings=_settings(),
            model=FakeChatModel(),
            rag_eval_runner=fake_runner,
        )
    ) as client:
        missing = client.post("/v1/rag-eval/run", json={})
        complete = client.post(
            "/v1/rag-eval/run",
            json={"confirm_external_model_usage": True},
        )

    assert missing.status_code == 422
    assert complete.status_code == 200
    assert complete.json()["strategies"]["dense"]["mrr"] == 1.0
    assert calls == 1


def test_rag_eval_can_request_four_strategy_answer_generation() -> None:
    requested: list[bool] = []

    async def fake_runner(full_generation: bool) -> dict[str, object]:
        requested.append(full_generation)
        return {"strategies": {}, "cases": []}

    with _admin_client(
        create_app(
            settings=_settings(),
            model=FakeChatModel(),
            rag_eval_runner=fake_runner,
        )
    ) as client:
        response = client.post(
            "/v1/rag-eval/run",
            json={"confirm_external_model_usage": True, "full_generation": True},
        )

    assert response.status_code == 200
    assert requested == [True]


def test_rag_eval_can_limit_generation_to_production_route() -> None:
    requested: list[bool] = []

    async def fake_runner(full_generation: bool) -> dict[str, object]:
        requested.append(full_generation)
        return {"strategies": {}, "cases": []}

    with _admin_client(
        create_app(settings=_settings(), model=FakeChatModel(), rag_eval_runner=fake_runner)
    ) as client:
        response = client.post(
            "/v1/rag-eval/run",
            json={"confirm_external_model_usage": True, "full_generation": False},
        )

    assert response.status_code == 200
    assert requested == [False]
