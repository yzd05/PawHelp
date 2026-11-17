"""HTTP behavior for structured after-sales extraction."""

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.fakes import FakeCombinedModel

DATABASE_URL = "mysql+asyncmy://pawhelp:pawhelp@127.0.0.1:3306/pawhelp"
LOCAL_ADMIN_HEADERS = {"X-PawHelp-Local-Admin": "1"}


class NeverCalledSessionFactory:
    def __call__(self) -> object:
        raise AssertionError("extraction must not open a database session")


def make_settings() -> Settings:
    return Settings(
        chat_base_url="http://localhost:11434/v1",
        openai_model="qwen3",
        openai_api_key="ollama",
        database_url=DATABASE_URL,
    )


def make_app(model: object):
    return create_app(
        settings=make_settings(),
        model=model,
        session_factory=NeverCalledSessionFactory(),
    )


def admin_client(model: object) -> TestClient:
    return TestClient(
        make_app(model),
        base_url="http://localhost:8000",
        client=("127.0.0.1", 50000),
        headers=LOCAL_ADMIN_HEADERS,
    )


def test_extraction_endpoint_requires_admin_authorization() -> None:
    model = FakeCombinedModel()
    with TestClient(make_app(model)) as client:
        response = client.post(
            "/v1/after-sales/extract",
            json={"description": "买错了，想原路退款"},
        )

    assert response.status_code == 503
    assert model.structured_runnable is not None
    assert model.structured_runnable.seen_messages == ()


def test_extraction_endpoint_returns_the_exact_public_schema() -> None:
    model = FakeCombinedModel(
        structured_result={
            "order_id": None,
            "request_type": "refund",
            "expected_solution": "原路退款",
        }
    )

    with admin_client(model) as client:
        response = client.post(
            "/v1/after-sales/extract",
            json={"description": "买错了，想原路退款"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "order_id": None,
        "request_type": "refund",
        "expected_solution": "原路退款",
    }


def test_extraction_endpoint_hides_upstream_error_details() -> None:
    model = FakeCombinedModel(structured_error=RuntimeError("secret body"))

    with admin_client(model) as client:
        response = client.post(
            "/v1/after-sales/extract",
            json={"description": "退款"},
        )

    assert response.status_code == 502
    assert response.json() == {
        "detail": {
            "code": "structured_output_error",
            "message": "售后信息暂时无法解析，请稍后重试",
        }
    }
    assert "secret body" not in response.text


def test_extraction_endpoint_rejects_an_empty_description() -> None:
    with admin_client(FakeCombinedModel()) as client:
        response = client.post("/v1/after-sales/extract", json={"description": "  "})

    assert response.status_code == 422
