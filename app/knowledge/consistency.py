"""Write-time check of a new knowledge entry against the existing knowledge."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.knowledge.search import RetrievalStrategy
from app.prompts import render_knowledge_consistency_messages

logger = logging.getLogger(__name__)


def _char_ngrams(text: str, n: int = 2) -> set[str]:
    cleaned = re.sub(r"[^\w]+", "", text, flags=re.UNICODE).casefold()
    if len(cleaned) < n:
        return {cleaned} if cleaned else set()
    return {cleaned[index : index + n] for index in range(len(cleaned) - n + 1)}


def lexical_overlap_score(query_question: str, query_answer: str, entry: object) -> int:
    q_grams = _char_ngrams(query_question)
    cand_grams = _char_ngrams(getattr(entry, "questions", ""))
    overlap = len(q_grams & cand_grams)
    if overlap >= 2:
        return overlap * 2
    full_grams = q_grams | _char_ngrams(query_answer)
    entry_grams = cand_grams | _char_ngrams(getattr(entry, "answer", ""))
    return len(full_grams & entry_grams)


class ConsistencyDecision(BaseModel):
    verdict: Literal["duplicate", "conflict", "new"]
    existing_id: int | None = None
    reason: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True)
class ConsistencyResult:
    verdict: Literal["duplicate", "conflict", "new", "unavailable"]
    reason: str
    chunk_id: int | None = None
    label: str | None = None

    @property
    def message(self) -> str:
        target = (
            f"已有知识 #{self.chunk_id}（{self.label}）"
            if self.chunk_id and self.chunk_id > 0
            else f"同批候选（{self.label}）"
            if self.label
            else "已有知识"
        )
        label = {"duplicate": "与" + target + "重复", "conflict": "与" + target + "冲突",
                 "unavailable": "冲突检查不可用，未写入知识库", "new": "可以写入"}[self.verdict]
        return label if self.verdict == "unavailable" else f"{label}：{self.reason}"


class KnowledgeConsistencyChecker:
    """Retrieve the closest existing entries locally, then ask the model once."""

    def __init__(self, search, model, *, method: str, limit: int = 5) -> None:
        self._search, self._model, self._method, self._limit = search, model, method, limit

    async def check(
        self,
        question: str,
        answer: str,
        *,
        exclude_id: int | None = None,
        extra_entries: Sequence[object] = (),
    ) -> ConsistencyResult:
        try:
            results = await asyncio.gather(*(
                self._search.retrieve(query, strategy=RetrievalStrategy.HYBRID_RERANK, limit=self._limit + 1)
                for query in (question, answer)))
            by_id = {}
            for result in results:
                for match in result.matches:
                    if match.chunk.id != exclude_id and match.score > by_id.get(match.chunk.id, (None, -1.0))[1]:
                        by_id[match.chunk.id] = (match.chunk, match.score)
            closest = [chunk for chunk, _ in sorted(by_id.values(), key=lambda item: item[1], reverse=True)][:self._limit]
            seen_ids = {chunk.id for chunk in closest}
            ranked_extras = sorted(
                (
                    (entry, lexical_overlap_score(question, answer, entry))
                    for entry in extra_entries
                    if getattr(entry, "id", None) != exclude_id
                    and getattr(entry, "id", None) not in seen_ids
                ),
                key=lambda item: item[1],
                reverse=True,
            )
            matched_extras = [entry for entry, score in ranked_extras if score >= 2][:3]
            combined = [*closest, *matched_extras]
            if not combined:
                return ConsistencyResult("new", "知识库中没有相近知识")
            existing = [
                {
                    "id": chunk.id,
                    "source": "document" if getattr(chunk, "source_kind", None) == "markdown" else "added",
                    "section_path": getattr(chunk, "section_path", None),
                    "question": chunk.questions,
                    "answer": chunk.answer,
                }
                for chunk in combined
            ]
            payload = json.dumps({"candidate": {"question": question, "answer": answer}, "existing": existing},
                                 ensure_ascii=False, separators=(",", ":"))
            runnable = self._model.with_structured_output(ConsistencyDecision, method=self._method)
            decision = ConsistencyDecision.model_validate(
                await runnable.ainvoke(render_knowledge_consistency_messages(payload)))
        except Exception:
            logger.exception("knowledge consistency check failed")
            return ConsistencyResult("unavailable", "冲突检查不可用")
        if decision.verdict == "new":
            return ConsistencyResult("new", decision.reason)
        cited = next((chunk for chunk in combined if chunk.id == decision.existing_id), None)
        label = (getattr(cited, "section_path", None) or cited.questions) if cited else None
        chunk_id = cited.id if cited and cited.id > 0 else None
        return ConsistencyResult(decision.verdict, decision.reason, chunk_id, label)
