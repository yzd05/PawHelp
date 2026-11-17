import pytest

from app.config import Settings
from tests.fakes import fake_settings


def test_settings_repr_hides_api_key_and_database_password() -> None:
    """Catch credentials being rendered in operational logs or errors."""

    settings = fake_settings(
        openai_api_key="super-secret",
        database_url="mysql+asyncmy://pawhelp:database-secret@127.0.0.1:3306/pawhelp",
    )

    assert "super-secret" not in repr(settings)
    assert "database-secret" not in repr(settings)


@pytest.mark.parametrize("canonical,expected", [
    ("https://canonical.example/v1", "https://canonical.example/v1"),
    (None, "https://legacy.example/v1"),
])
def test_chat_base_url_prefers_canonical_name_and_keeps_openai_alias(monkeypatch, canonical, expected) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://legacy.example/v1")
    if canonical:
        monkeypatch.setenv("CHAT_BASE_URL", canonical)
    else:
        monkeypatch.delenv("CHAT_BASE_URL", raising=False)

    settings = Settings(_env_file=None, OPENAI_MODEL="demo", OPENAI_API_KEY="secret",
                        DATABASE_URL="mysql+asyncmy://unused")

    assert str(settings.chat_base_url).rstrip("/") == expected


def test_deprecated_fields_removed_and_new_optional_fields_validated() -> None:
    assert "milvus_collection" not in Settings.model_fields
    assert "knowledge_similarity_threshold" not in Settings.model_fields

    default_cfg = fake_settings()
    assert default_cfg.fast_openai_model is None
    assert default_cfg.topic_service_url == "http://127.0.0.1:8110"
    assert default_cfg.mock_delay_seconds == 0.0

    blank_fast = fake_settings(fast_openai_model="   ")
    assert blank_fast.fast_openai_model is None

    custom_cfg = fake_settings(
        fast_openai_model=" gpt-4o-mini ",
        topic_service_url="http://topic-svc:8110",
        mock_delay_seconds=0.25,
    )
    assert custom_cfg.fast_openai_model == "gpt-4o-mini"
    assert custom_cfg.topic_service_url == "http://topic-svc:8110"
    assert custom_cfg.mock_delay_seconds == 0.25
