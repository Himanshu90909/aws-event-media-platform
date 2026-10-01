"""Agent loop: route question -> retrieve evidence -> compose cited answer.

The planner is deliberately deterministic (rule-based routing, not LLM
tool-calling): every question first hits the retrieval tool, then the LLM
answers strictly from the retrieved excerpts. That keeps behavior
reproducible, cheap to evaluate offline, and honest about what the
retrieval layer actually returned.
"""

import time
from dataclasses import dataclass, field
from typing import Any

from agent.llm import LLMProvider
from agent.prompts import render_query_prompt
from common.metrics import emit_metric

TOP_K = 5


@dataclass
class AgentAnswer:
    answer: str
    citations: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    tool_trace: list[str] = field(default_factory=list)


class MediaJobsAgent:
    def __init__(self, tools: Any, llm: LLMProvider) -> None:
        self._tools = tools
        self._llm = llm

    def answer(self, question: str) -> AgentAnswer:
        """Answer a question with retrieved evidence and citations."""
        start = time.perf_counter()
        hits: list[dict[str, Any]] = self._tools.execute("search_jobs", query=question, top_k=TOP_K)
        excerpts = [str(hit["text"]) for hit in hits]
        citations = [str(hit["docId"]) for hit in hits]
        system, user = render_query_prompt(question, excerpts)
        answer_text = self._llm.complete(system, user)
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        emit_metric("AgentQueryLatency", latency_ms, unit="Milliseconds", outcome="ok" if hits else "no_hits")
        return AgentAnswer(
            answer=answer_text,
            citations=citations,
            latency_ms=latency_ms,
            tool_trace=["search_jobs"] if hits else ["search_jobs:0_hits"],
        )
