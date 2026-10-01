"""Tool registry for the agent.

Tools are plain callables with a name and description, registered in a
ToolRegistry. The agent loop (and tests) call tools by name through the
registry, so adding a tool never requires changing the agent.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent.index import SearchIndex


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    fn: Callable[..., Any]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

    def execute(self, name: str, **kwargs: Any) -> Any:
        return self.get(name).fn(**kwargs)

    def describe(self) -> list[str]:
        return [f"{t.name}: {t.description}" for t in self._tools.values()]


def build_tools(index: SearchIndex, job_lookup: Callable[[str], dict[str, Any] | None]) -> ToolRegistry:
    """Wire the standard tool set: retrieval plus a job-status lookup.

    job_lookup is injected so Lambda passes a DynamoDB reader and tests pass
    a stub; the agent never talks to AWS directly.
    """
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="search_jobs",
            description="Search indexed job records by free-text query. Returns top scored excerpts.",
            fn=lambda query, top_k=5: [
                {"docId": sd.document.doc_id, "score": round(sd.score, 4), "text": sd.document.text}
                for sd in index.retrieve(query, top_k=top_k)
            ],
        )
    )
    registry.register(
        Tool(
            name="get_job",
            description="Fetch a single job record by jobId. Returns the raw item or None.",
            fn=lambda job_id: job_lookup(job_id),
        )
    )
    return registry
