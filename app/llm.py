"""LangChain model construction."""

from langchain_openai import ChatOpenAI

from app.config import Settings


def create_chat_model(
    settings: Settings,
    *,
    max_retries: int = 2,
    streaming: bool = True,
    model_name: str | None = None,
) -> ChatOpenAI:
    """Create an OpenAI-compatible chat model with explicit streaming behavior."""

    return ChatOpenAI(
        model=model_name or settings.openai_model,
        api_key=settings.openai_api_key.get_secret_value(),
        base_url=str(settings.chat_base_url).rstrip("/"),
        timeout=settings.model_timeout_seconds,
        max_tokens=settings.model_max_output_tokens,
        streaming=streaming,
        stream_usage=streaming,
        max_retries=max_retries,
    )


def create_fast_chat_model(
    settings: Settings,
    *,
    max_retries: int = 2,
) -> ChatOpenAI:
    """Create a non-streaming client for lightweight structured classification/rewrites."""

    return create_chat_model(
        settings,
        max_retries=max_retries,
        streaming=False,
        model_name=settings.fast_openai_model or settings.openai_model,
    )
