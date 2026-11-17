"""Regression assertions for OSS packaging, compliance, Quickstart, Compose, CI, and ops scripts."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tomllib
from types import SimpleNamespace
from typing import Any

import httpx

from app.config import Settings
from app.topics.taxonomy import TAXONOMY_HASH, TOPIC_NAMES
from app.workflow.memory import SessionMemory
import scripts.start as start_script
import scripts.topics.classify_pool as classify_pool
import scripts.topics.runtime as topic_runtime
from tests.fakes import FakeChatModel

ROOT = Path(__file__).resolve().parents[1]


def _load_pyproject() -> dict[str, Any]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _load_uv_lock() -> dict[str, Any]:
    with (ROOT / "uv.lock").open("rb") as handle:
        return tomllib.load(handle)


def test_pyproject_and_lockfile_support_macos_and_declare_oss_metadata() -> None:
    pyproject = _load_pyproject()
    project = pyproject["project"]

    assert project["readme"] == "README.md"
    assert project["license"] == "MIT"
    assert project["urls"]["Repository"] == "https://github.com/yzd05/PawHelp"
    assert project["urls"]["Issues"].startswith("https://github.com/yzd05/PawHelp")
    assert project["urls"]["Changelog"].startswith("https://github.com/yzd05/PawHelp")
    assert project["scripts"]["pawhelp-start"] == "scripts.start:main"
    assert "scripts*" in pyproject["tool"]["setuptools"]["packages"]["find"]["include"]

    dep_names = {
        entry.split(">=")[0].split("==")[0].split("[")[0].strip().lower()
        for entry in project["dependencies"]
    }
    assert "ipykernel" not in dep_names
    assert "langchain" not in dep_names
    assert "httpx" in dep_names
    assert "uvicorn" in dep_names

    torch_sources = pyproject["tool"]["uv"]["sources"]["torch"]
    assert isinstance(torch_sources, list) and len(torch_sources) == 1
    assert torch_sources[0]["index"] == "pytorch-cu130"
    assert "win32" in torch_sources[0]["marker"]
    assert "linux" in torch_sources[0]["marker"]
    assert "darwin" not in torch_sources[0]["marker"]

    lock = _load_uv_lock()
    torch_packages = [pkg for pkg in lock["package"] if pkg["name"] == "torch"]
    assert torch_packages
    wheel_urls = [
        wheel["url"]
        for pkg in torch_packages
        for wheel in pkg.get("wheels", [])
    ]
    assert any("macosx" in url for url in wheel_urls), "macOS wheel missing from uv.lock"
    assert any("cu130" in url for url in wheel_urls), "CUDA 13.0 wheel missing from uv.lock"


def test_env_example_is_unified_and_prevents_out_of_box_422() -> None:
    assert not (ROOT / ".env.context.example").exists()
    env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
    active_lines = [
        line.strip()
        for line in env_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    active_keys = [line.split("=", 1)[0].strip() for line in active_lines]

    assert len(active_keys) == len(set(active_keys))
    assert "MODEL_CONTEXT_WINDOW=65536" in active_lines
    assert "MODEL_MAX_OUTPUT_TOKENS=1024" in active_lines
    assert "MAX_OUTPUT_TOKENS" not in active_keys
    assert "MILVUS_COLLECTION" not in active_keys
    assert "KNOWLEDGE_SIMILARITY_THRESHOLD" not in active_keys
    assert "CONTEXT_TOKEN_BUDGET" not in active_keys

    for documented_optional in (
        "FAST_OPENAI_MODEL",
        "TOPIC_SERVICE_URL",
        "TOPIC_MODEL_DIR",
        "MOCK_DELAY_SECONDS",
        "MAX_USER_INPUT_TOKENS",
        "CONTEXT_SUMMARY_MAX_TOKENS",
    ):
        assert documented_optional in env_text

    loaded = Settings(_env_file=ROOT / ".env.example")
    assert loaded.model_context_window == 65536
    assert loaded.model_max_output_tokens == 1024
    memory = SessionMemory(loaded, lambda: None, FakeChatModel([]), "system prompt", "[]")
    assert memory.budget.history >= memory.budget.steady


def test_compliance_security_git_compose_and_ci_contracts() -> None:
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "provenance check is still open" not in notices
    assert "Before a public release" not in notices
    assert "Marked 18.1.0" in notices
    assert "DOMPurify 3.4.16" in notices
    assert "MIT License" in notices

    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "https://github.com/yzd05/PawHelp/security/advisories/new" in security
    assert "No public repository" not in security

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".claude/" in gitignore

    gitattributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in gitattributes
    assert "*.ps1 text eol=crlf" in gitattributes
    assert "app/static/vendor/*.js -text" in gitattributes

    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "--default-time-zone=+00:00" in compose

    compose_lf = (ROOT / "compose.langfuse.yaml").read_text(encoding="utf-8")
    assert "cgr.dev/chainguard/minio" not in compose_lf
    assert "quay.io/minio/minio:RELEASE." in compose_lf
    assert "docker.langfuse.com/langfuse/langfuse:4\n" not in compose_lf
    assert "docker.langfuse.com/langfuse/langfuse-worker:4\n" not in compose_lf
    assert "docker.langfuse.com/langfuse/langfuse:4." in compose_lf
    assert "docker.langfuse.com/langfuse/langfuse-worker:4." in compose_lf

    workflow = (ROOT / ".github" / "workflows" / "source-checks.yml").read_text(encoding="utf-8")
    assert "concurrency:" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "image: mysql:8.4" in workflow
    assert "+00:00" in workflow
    assert "health-cmd" in workflow
    assert "uv run --locked alembic upgrade head" in workflow
    assert "DATABASE_URL:" in workflow
    assert "TEST_DATABASE_URL:" in workflow
    assert "uv run --locked pytest" in workflow

    assert (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").is_file()
    assert (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").is_file()
    assert (ROOT / ".github" / "ISSUE_TEMPLATE" / "feature_request.md").is_file()

    agents_md = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "CONTRIBUTING.md" in agents_md
    assert ".scratch/" in agents_md

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    assert "scripts.seed_db" not in readme
    assert "scripts.seed_db" not in operations
    assert "docker compose up -d --wait" in readme
    assert "docker compose up -d --wait" in operations
    assert "--bootstrap" in readme
    assert "HF_ENDPOINT" in readme
    assert "UV_INDEX_URL" in readme


def test_start_script_forwards_custom_mcp_ports_and_bootstraps(monkeypatch: Any) -> None:
    assert start_script.resolve_mcp_port("http://127.0.0.1:18101/mcp", 8101) == 18101
    assert start_script.resolve_mcp_port("http://127.0.0.1/mcp", 8101) == 8101

    spawned: list[tuple[list[str], dict[str, str]]] = []

    class FakeProcess:
        def poll(self) -> None:
            return None

        def send_signal(self, sig: int) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(start_script, "port_open", lambda port: False)
    monkeypatch.setattr(
        start_script.subprocess,
        "Popen",
        lambda cmd, *, cwd, env, stdout, stderr, **kwargs: (
            spawned.append((cmd, env)) or FakeProcess()
        ),
    )

    boot_runs: list[list[str]] = []
    monkeypatch.setattr(
        start_script.subprocess,
        "run",
        lambda cmd, *, cwd=None, check=False, **kwargs: (
            boot_runs.append(list(cmd)) or SimpleNamespace(returncode=0)
        ),
    )
    monkeypatch.setattr(
        start_script,
        "_inspect_knowledge_state",
        lambda settings: _immediate_coro((False, 0)),
    )
    assert start_script.bootstrap_knowledge(object()) == (True, True)
    assert boot_runs == [
        [sys.executable, "-m", "scripts.ingest_knowledge"],
        [sys.executable, "-m", "scripts.vectorize_knowledge"],
    ]

    boot_runs.clear()
    monkeypatch.setattr(
        start_script,
        "_inspect_knowledge_state",
        lambda settings: _immediate_coro((True, 0)),
    )
    assert start_script.bootstrap_knowledge(object()) == (False, False)
    assert boot_runs == []

    monkeypatch.setattr(
        start_script,
        "get_settings",
        lambda: SimpleNamespace(
            mcp_enabled=True,
            mcp_logistics_url="http://127.0.0.1:18101/mcp",
            mcp_aftersales_url="http://127.0.0.1:18102/mcp",
        ),
        raising=False,
    )
    import app.config as app_config

    monkeypatch.setattr(
        app_config,
        "get_settings",
        lambda: SimpleNamespace(
            mcp_enabled=True,
            mcp_logistics_url="http://127.0.0.1:18101/mcp",
            mcp_aftersales_url="http://127.0.0.1:18102/mcp",
        ),
    )
    monkeypatch.setattr(start_script.Stack, "wait", lambda self, name, ready, timeout: None)
    monkeypatch.setattr(start_script, "get_json", lambda url: {"status": "ok", "knowledge_models": "ready"})
    monkeypatch.setattr(
        start_script.time,
        "sleep",
        lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["pawhelp-start", "--no-docker", "--no-classifier", "--bootstrap"],
    )

    start_script.main()

    env_by_module = {
        cmd[2]: env
        for cmd, env in spawned
        if len(cmd) >= 3 and cmd[1] == "-m"
    }
    assert env_by_module["mcp_servers.logistics_server"]["MCP_LOGISTICS_PORT"] == "18101"
    assert env_by_module["mcp_servers.aftersales_server"]["MCP_AFTERSALES_PORT"] == "18102"


async def _immediate_coro(value: Any) -> Any:
    return value


def test_topic_training_spec_and_runtime_paths_do_not_leak_workstation_details(
    tmp_path: Path,
) -> None:
    spec = json.loads((ROOT / "config" / "topic-training.json").read_text(encoding="utf-8"))
    assert "RTX 4060" not in json.dumps(spec)
    assert ".venv/Scripts/python.exe" not in json.dumps(spec)

    inside = ROOT / "data" / "topic-classifier" / "dataset-v1"
    rel = topic_runtime.repo_relative_path(inside)
    assert rel == "data/topic-classifier/dataset-v1"
    assert ":" not in rel and "\\" not in rel

    outside = tmp_path / "external-dataset"
    ext_rel = topic_runtime.repo_relative_path(outside)
    assert ":" not in ext_rel and "\\" not in ext_rel


async def test_classify_pool_reads_topic_service_url_and_commits_per_batch(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TOPIC_SERVICE_URL", "http://127.0.0.1:19110/")
    assert classify_pool.resolve_service_url() == "http://127.0.0.1:19110"

    executed_batches: list[int] = []
    engine_disposed = 0

    class FakeEngine:
        async def dispose(self) -> None:
            nonlocal engine_disposed
            engine_disposed += 1

    class FakeSession:
        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        @contextmanager
        def _noop(self):
            yield

        def begin(self):
            class _Tx:
                async def __aenter__(self_inner) -> None:
                    return None

                async def __aexit__(self_inner, *args: object) -> None:
                    return None

            return _Tx()

        async def execute(self, statement: Any) -> None:
            compiled = statement.compile()
            # Multi-row insert compiles question_id_m0, question_id_m1, ... in one statement.
            question_params = [k for k in compiled.params if k.startswith("question_id")]
            executed_batches.append(len(question_params))

    class FakePool:
        def __init__(self, sessions: Any) -> None:
            self.sessions = sessions

        async def questions(self, *, pending: bool, limit: int) -> list[dict[str, Any]]:
            return [
                {"id": idx, "normalized_question": f"订单退款问题 {idx}"}
                for idx in range(1, 6)
            ]

    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if request.url.path == "/healthz":
            return httpx.Response(
                200,
                json={"status": "ok", "model_version": "m-test", "taxonomy_hash": TAXONOMY_HASH},
            )
        payload = json.loads(request.content.decode("utf-8"))
        scores = {label: (0.9 if label == TOPIC_NAMES[0] else 0.02) for label in TOPIC_NAMES}
        items = [
            {
                "labels": [TOPIC_NAMES[0]],
                "scores": scores,
                "model_version": "m-test",
                "taxonomy_hash": TAXONOMY_HASH,
            }
            for _ in payload["texts"]
        ]
        return httpx.Response(200, json={"items": items})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    monkeypatch.setattr(classify_pool, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(classify_pool, "create_async_engine_from_settings", lambda s: FakeEngine())
    monkeypatch.setattr(classify_pool, "create_session_factory", lambda e: FakeSession)
    monkeypatch.setattr(classify_pool, "TopicPool", FakePool)
    monkeypatch.setattr(
        classify_pool.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, timeout=10),
    )

    await classify_pool.classify(
        tmp_path,
        force=True,
        min_batch=1,
        limit=10,
        batch_size=3,
    )

    assert requested_urls == [
        "http://127.0.0.1:19110/healthz",
        "http://127.0.0.1:19110/classify",
        "http://127.0.0.1:19110/classify",
    ]
    # 5 rows with batch_size=3 -> 2 batch SQL INSERT statements (3 rows, then 2 rows), not 5!
    assert executed_batches == [3, 2]
    assert engine_disposed == 1
    reports = list(tmp_path.glob("classify-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["status"] == "complete"
    assert report["service_url"] == "http://127.0.0.1:19110"
    assert len(report["classified"]) == 5
    assert all(row["status"] == "saved" for row in report["classified"])
