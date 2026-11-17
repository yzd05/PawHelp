"""Bounded standalone pool batch, persistent inference provenance, no LLM calls."""
import argparse
import asyncio
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import uuid
import httpx
from sqlalchemy.dialects.mysql import insert

from app.config import get_settings
from app.database.models import TopicClassification
from app.database.session import create_async_engine_from_settings, create_session_factory
from app.topics.data import digest, exclusive_run, redact, write_json
from app.topics.pool import TopicPool
from app.topics.taxonomy import TAXONOMY_HASH, TOPIC_NAMES, validate_labels

DEFAULT_TOPIC_SERVICE_URL = "http://127.0.0.1:8110"


def resolve_service_url(settings: object | None = None, explicit_url: str | None = None) -> str:
    if explicit_url and explicit_url.strip():
        return explicit_url.strip().rstrip("/")
    env_url = os.getenv("TOPIC_SERVICE_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    configured = getattr(settings, "topic_service_url", None) if settings is not None else None
    if isinstance(configured, str) and configured.strip():
        return configured.strip().rstrip("/")
    return DEFAULT_TOPIC_SERVICE_URL


async def save_classified_batch(
    pool: TopicPool,
    batch_items: list[tuple[int, list[str]]],
) -> None:
    """Commit one batch of idempotent topic classifications in a single transaction."""
    if not batch_items:
        return
    if hasattr(pool, "save_batch"):
        await pool.save_batch(batch_items)
        return
    if hasattr(pool, "sessions"):
        classified_at = datetime.now(timezone.utc).replace(tzinfo=None)
        values = [
            {
                "question_id": question_id,
                "labels": validate_labels(labels),
                "classified_at": classified_at,
            }
            for question_id, labels in batch_items
        ]
        statement = insert(TopicClassification).values(values)
        statement = statement.on_duplicate_key_update(
            question_id=statement.inserted.question_id
        )
        async with pool.sessions() as session, session.begin():
            await session.execute(statement)
        return
    for question_id, labels in batch_items:
        await pool.save(question_id, labels)


async def classify(
    output: Path,
    *,
    force: bool,
    min_batch: int,
    limit: int,
    batch_size: int = 20,
    service_url: str | None = None,
):
    settings = get_settings()
    base_url = resolve_service_url(settings, service_url)
    engine = create_async_engine_from_settings(settings)
    try:
        pool = TopicPool(create_session_factory(engine))
        with exclusive_run(output / "batch.lock"):
            rows = await pool.questions(pending=True, limit=limit)
            run = {
                "run_id": uuid.uuid4().hex,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "service_url": base_url,
                "status": "started",
                "pending": len(rows),
                "classified": [],
                "failed": [],
                "taxonomy_hash": TAXONOMY_HASH,
            }
            path = output / f"classify-{run['run_id']}.json"
            write_json(path, run)
            if len(rows) < min_batch and not force:
                run["status"] = "waiting_for_batch"
                write_json(path, run)
                print(f"waiting: eligible={len(rows)}, min_batch={min_batch}")
                return
            if not rows:
                run["status"] = "no_pending"
                write_json(path, run)
                print("no pending merged questions; unchanged results")
                return
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                health = await client.get(f"{base_url}/healthz")
                health.raise_for_status()
                meta = health.json()
                if meta.get("taxonomy_hash") != TAXONOMY_HASH:
                    raise ValueError("classifier taxonomy mismatch")
                run["model_version"] = meta["model_version"]
                for start in range(0, len(rows), batch_size):
                    batch = rows[start : start + batch_size]
                    texts = [redact(row["normalized_question"]) for row in batch]
                    response = await client.post(
                        f"{base_url}/classify",
                        json={"texts": texts},
                    )
                    response.raise_for_status()
                    items = response.json()["items"]
                    if len(items) != len(batch):
                        raise ValueError("classifier response count mismatch")
                    batch_records: list[dict[str, object]] = []
                    batch_writes: list[tuple[int, list[str]]] = []
                    for row, text, item in zip(batch, texts, items, strict=True):
                        labels = validate_labels(item["labels"])
                        scores = item["scores"]
                        if (
                            item.get("model_version") != meta["model_version"]
                            or item.get("taxonomy_hash") != TAXONOMY_HASH
                        ):
                            raise ValueError("classifier model changed during batch")
                        if set(scores) != set(TOPIC_NAMES) or any(
                            not isinstance(s, (int, float))
                            or not math.isfinite(s)
                            or not 0 <= s <= 1
                            for s in scores.values()
                        ):
                            raise ValueError("invalid classifier scores")
                        record = {
                            "question_id": row["id"],
                            "input_hash": digest(text),
                            "labels": labels,
                            "scores": scores,
                            "status": "write_pending",
                        }
                        batch_records.append(record)
                        batch_writes.append((row["id"], labels))
                    # Record uncertain batch writes BEFORE SQL so a failed DB commit
                    # never silently loses model/input provenance.
                    run["classified"].extend(batch_records)
                    write_json(path, run)
                    await save_classified_batch(pool, batch_writes)
                    for record in batch_records:
                        record["status"] = "saved"
                    write_json(path, run)
                    print(f"classified={len(run['classified'])}/{len(rows)}", flush=True)
            run.update(status="complete", finished_at=datetime.now(timezone.utc).isoformat())
            write_json(path, run)
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--min-batch", type=int, default=10)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--service-url", type=str, default=None)
    parser.add_argument("--output", type=Path, default=Path("data/topic-classifier/reports/batches"))
    args = parser.parse_args()
    if not 1 <= args.min_batch <= args.limit <= 500:
        parser.error("1 <= min-batch <= limit <= 500 required")
    if not 1 <= args.batch_size <= 40:
        parser.error("1 <= batch-size <= 40 required")
    asyncio.run(
        classify(
            args.output,
            force=args.force,
            min_batch=args.min_batch,
            limit=args.limit,
            batch_size=args.batch_size,
            service_url=args.service_url,
        )
    )


if __name__ == "__main__":
    main()
