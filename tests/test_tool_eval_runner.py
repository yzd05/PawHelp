"""Deterministic and live-service contracts for labelled workflow tool evaluation."""

from random import Random

from langchain_core.messages import AIMessageChunk
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import Conversation, Message, Ticket
from app.workflow.graph import WorkflowChatService
import scripts.run_tool_evals as runner
from scripts.run_tool_evals import EvalCase, run_cases, score_case
from tests.order_fakes import FakeOrderData, TestToolCatalog
from tests.workflow_fakes import LabelledModel, LabelledSearch, tool_call

DATABASE_URL = "mysql+asyncmy://unused"


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        chat_base_url="http://127.0.0.1:9/v1",
        openai_model="eval-test",
        openai_api_key="local-nonsecret-key",
        database_url=DATABASE_URL,
        shop_name="测试店",
        model_context_window=65536,
        tool_timeout_seconds=1,
        tool_max_attempts=1,
        mcp_enabled=False,
        observability_enabled=False,
    )


def logistics_call_chunk() -> AIMessageChunk:
    return tool_call("query_logistics", '{"order_id":"1001"}', "eval-call")


def logistics_case() -> EvalCase:
    return EvalCase(
        case_id="logistics",
        message="private prompt 订单 1001 的物流到哪了",
        expected_tool="query_logistics",
        expected_outcome="answer",
    )


async def table_counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int, int]:
    async with session_factory() as session:
        return (
            await session.scalar(select(func.count()).select_from(Conversation)) or 0,
            await session.scalar(select(func.count()).select_from(Message)) or 0,
            await session.scalar(select(func.count()).select_from(Ticket)) or 0,
        )


def test_logistics_case_requires_done_succeeded_and_grounded_answer() -> None:
    """An error after grounded text must not satisfy a labelled answer case."""

    result = score_case(
        expected_tool="query_logistics",
        expected_outcome="answer",
        observed_tool="query_logistics",
        answer="订单 1001 正在运输，包裹位于配送中心",
        tool_result='{"order_id":"1001","status":"in_transit","location":"配送中心"}',
        terminal_event="error",
        tool_succeeded=True,
    )

    assert result.passed is False
    assert result.classification == "terminal_error"


def test_logistics_accepts_chinese_status_paraphrase_but_rejects_refund_claim() -> None:
    """The fixed logistics facts must be complete and free of contradictory claims."""

    common = {
        "expected_tool": "query_logistics",
        "expected_outcome": "answer",
        "observed_tool": "query_logistics",
        "tool_result": '{"order_id":"1001","status":"accepted","location":"配送中心"}',
        "terminal_event": "done",
        "tool_succeeded": True,
    }

    faithful = score_case(answer="订单 1001 已揽收，目前在配送中心", **common)
    invented = score_case(answer="订单 1001 已退款，记录位于配送中心", **common)

    assert faithful.passed is True
    assert invented == type(invented)(False, "ungrounded_answer")


def test_logistics_rejects_negated_status_and_invented_eta_but_allows_location_paraphrase() -> None:
    """Fixed logistics rules must preserve polarity and reject ETA claims absent from data."""

    common = {
        "expected_tool": "query_logistics",
        "expected_outcome": "answer",
        "observed_tool": "query_logistics",
        "terminal_event": "done",
        "tool_succeeded": True,
    }
    negated = score_case(
        answer="订单 1001 尚未派送，目前在配送中心",
        tool_result='{"order_id":"1001","status":"out_for_delivery","location":"配送中心"}',
        **common,
    )
    invented_eta = score_case(
        answer="订单 1001 正在运输，目前在配送中心，预计两小时后送达",
        tool_result='{"order_id":"1001","status":"in_transit","location":"配送中心"}',
        **common,
    )
    paraphrased_location = score_case(
        answer="订单 1001 正在运输，目前还在路上",
        tool_result='{"order_id":"1001","status":"in_transit","location":"运输途中"}',
        **common,
    )

    assert negated.passed is False
    assert invented_eta.passed is False
    assert paraphrased_location.passed is True


def test_logistics_fixed_case_handles_colloquial_negation_and_unknown_eta() -> None:
    common = {
        "expected_tool": "query_logistics",
        "expected_outcome": "answer",
        "observed_tool": "query_logistics",
        "tool_result": '{"order_id":"1001","status":"out_for_delivery","location":"配送中心"}',
        "terminal_event": "done",
        "tool_succeeded": True,
    }

    assert score_case(answer="订单 1001 还没派送，目前在配送中心", **common).passed is False
    assert score_case(answer="订单 1001 正在派送，大概两天能送到", **common).passed is False
    assert score_case(
        answer="订单 1001 正在派送，目前在配送中心，但无法预计送达时间", **common
    ).passed is True


def test_return_policy_accepts_paraphrase_but_rejects_added_refund_fact() -> None:
    """Knowledge evaluation must validate policy facts rather than exact answer copying."""

    common = {
        "expected_tool": "knowledge_retrieval",
        "expected_outcome": "answer",
        "observed_tool": "knowledge_retrieval",
        "tool_result": (
            '{"matches":[{"question":"退货政策是什么？",'
            '"answer":"商品在符合条件时可申请退货，具体以售后页面说明为准。",'
            '"category":"退货政策"}]}'
        ),
        "terminal_event": "done",
        "tool_succeeded": True,
    }

    faithful = score_case(answer="符合条件的商品可以申请退货，具体规则以售后页面为准。", **common)
    invented = score_case(
        answer="商品在符合条件时可申请退货，具体以售后页面说明为准，并且会全额退款。",
        **common,
    )

    assert faithful.passed is True
    assert invented.passed is False
    assert invented.classification == "ungrounded_answer"


def test_return_policy_allows_conditions_wording_but_rejects_unsupported_benefits() -> None:
    """Policy-specific facts must not be confused with a retrieval miss or new benefits."""

    common = {
        "expected_tool": "knowledge_retrieval",
        "expected_outcome": "answer",
        "observed_tool": "knowledge_retrieval",
        "tool_result": (
            '{"matches":[{"question":"退货政策是什么？",'
            '"answer":"商品在符合条件时可申请退货，具体以售后页面说明为准。",'
            '"category":"退货政策"}]}'
        ),
        "terminal_event": "done",
        "tool_succeeded": True,
    }

    faithful = score_case(answer="退货条件请以售后页面说明为准，符合条件即可申请退货。", **common)
    invented = score_case(answer="符合条件可申请退货，规则见售后页面，并支持七天无理由换货。", **common)

    assert faithful.passed is True
    assert invented.passed is False


def test_return_policy_fixed_case_rejects_unconditional_or_seven_day_rights() -> None:
    common = {
        "expected_tool": "knowledge_retrieval",
        "expected_outcome": "answer",
        "observed_tool": "knowledge_retrieval",
        "tool_result": (
            '{"matches":[{"question":"退货政策是什么？",'
            '"answer":"商品在符合条件时可申请退货，具体以售后页面说明为准。",'
            '"category":"退货政策"}]}'
        ),
        "terminal_event": "done",
        "tool_succeeded": True,
    }

    assert score_case(
        answer="不符合条件也可以申请退货，具体规则见售后页面", **common
    ).passed is False
    assert score_case(
        answer="符合条件可申请退货，售后页面说明七日内都能办理", **common
    ).passed is False


def test_postage_case_requires_semantic_hit_and_grounded_answer() -> None:
    common = {
        "expected_tool": "knowledge_retrieval",
        "expected_outcome": "postage_answer",
        "observed_tool": "knowledge_retrieval",
        "tool_result": (
            '{"matches":[{"question":"配送费用如何计算？",'
            '"answer":"配送费用根据订单金额、收货地区和活动规则计算，结算页展示实际运费。",'
            '"category":"配送服务"}]}'
        ),
        "terminal_event": "done",
        "tool_succeeded": True,
    }

    faithful = score_case(
        answer="运费会根据订单金额和收货地区计算，请以结算页展示为准。",
        **common,
    )
    invented = score_case(answer="配送费固定收费 99 元。", **common)

    assert faithful.passed is True
    assert faithful.classification == "postage_answer"
    assert invented.passed is False


async def test_runner_rejects_partial_post_tool_answer_and_cleans_rows(
    mysql_session, capsys
) -> None:
    """A convergence error after a real tool result must exit nonzero and clean up."""

    session_factory = async_sessionmaker(mysql_session.bind, expire_on_commit=False)

    class FailingConvergenceModel(LabelledModel):
        async def astream(self, messages):
            self.agent_calls += 1
            if self.agent_calls == 1:
                yield logistics_call_chunk()
            else:
                yield AIMessageChunk(content="订单 1001 正在运输，包裹位于配送中心")
                raise RuntimeError("private upstream exception secret=do-not-print")

    service = WorkflowChatService(
        settings=make_settings(),
        model=FailingConvergenceModel("物流"),
        session_factory=session_factory,
        rng=Random(7),
        order_data=FakeOrderData(),
        catalog=TestToolCatalog(),
        knowledge_search=LabelledSearch(False),
    )
    before = await table_counts(session_factory)

    try:
        exit_code = await run_cases(
            [logistics_case()], make_settings(), service=service, session_factory=session_factory
        )

        stdout = capsys.readouterr().out
        assert exit_code == 1
        assert "pass=False" in stdout
        assert "private prompt" not in stdout
        assert "正在运输" not in stdout
        assert "in_transit" not in stdout
        assert "secret=" not in stdout
        assert await table_counts(session_factory) == before
    finally:
        await service.close()


async def test_runner_rejects_full_answer_when_final_persistence_fails_and_cleans_rows(
    mysql_session, capsys
) -> None:
    """A final-row persistence error after complete text cannot become acceptance."""

    session_factory = async_sessionmaker(mysql_session.bind, expire_on_commit=False)

    class FinalPersistenceFailure(WorkflowChatService):
        async def _persist_workflow_answer(self, *args, **kwargs):
            raise RuntimeError("private final persistence failure secret=do-not-print")

    model = LabelledModel(
        "物流",
        [logistics_call_chunk(), "订单 1001 已签收，包裹位于收货地址"],
    )
    service = FinalPersistenceFailure(
        settings=make_settings(),
        model=model,
        session_factory=session_factory,
        rng=Random(7),
        order_data=FakeOrderData(),
        catalog=TestToolCatalog(),
        knowledge_search=LabelledSearch(False),
    )
    before = await table_counts(session_factory)

    try:
        exit_code = await run_cases(
            [logistics_case()], make_settings(), service=service, session_factory=session_factory
        )

        stdout = capsys.readouterr().out
        assert exit_code == 1
        assert "terminal=error" in stdout
        assert "pass=False" in stdout
        assert "secret=" not in stdout
        assert await table_counts(session_factory) == before
    finally:
        await service.close()


async def test_runner_success_is_done_succeeded_safe_and_repeatable(
    mysql_session, capsys
) -> None:
    """Repeated workflow evaluation runs must pass cleanly without DB residue."""

    session_factory = async_sessionmaker(mysql_session.bind, expire_on_commit=False)
    before = await table_counts(session_factory)
    for _ in range(2):
        model = LabelledModel(
            "物流",
            [logistics_call_chunk(), "订单 1001 已签收，包裹位于收货地址"],
        )
        service = WorkflowChatService(
            settings=make_settings(),
            model=model,
            session_factory=session_factory,
            rng=Random(7),
            order_data=FakeOrderData(),
            catalog=TestToolCatalog(),
            knowledge_search=LabelledSearch(False),
        )
        try:
            assert await run_cases(
                [logistics_case()],
                make_settings(),
                service=service,
                session_factory=session_factory,
            ) == 0
        finally:
            await service.close()

    stdout = capsys.readouterr().out
    assert stdout.count("terminal=done") == 2
    assert stdout.count("pass=True") == 2
    assert "private prompt" not in stdout
    assert "订单 1001 已签收" not in stdout
    assert "order_id" not in stdout
    assert await table_counts(session_factory) == before


def test_main_hides_model_setup_exception_and_returns_stable_failure(
    monkeypatch, capsys
) -> None:
    """CLI startup errors must not leak a traceback or private provider failure."""

    async def fail_runner(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("private model setup failure secret=do-not-print")

    monkeypatch.setattr(runner, "Settings", lambda: make_settings())
    monkeypatch.setattr(runner, "run_cases", fail_runner)

    assert runner.main() == 1
    output = capsys.readouterr().out
    assert output == "UNAVAILABLE: evaluation runner could not start\n"
    assert "secret=" not in output
