"""Print calibrated context budgets. No database or inference requests."""
import asyncio
from dataclasses import asdict
import json

from app.config import get_settings
from app.database.session import create_async_engine_from_settings, create_session_factory
from app.llm import create_chat_model
from app.workflow.graph import WorkflowChatService


async def run() -> dict[str, object]:
    settings = get_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        service = WorkflowChatService(
            settings=settings,
            model=create_chat_model(settings),
            session_factory=create_session_factory(engine),
        )
        service._memory.startup_check()
        return asdict(service._memory.budget)
    finally:
        await engine.dispose()


def main() -> None:
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
