"""Document model for the agent's index.

The agent layer follows a LlamaIndex-style pattern (load -> index -> retrieve)
but is implemented with the standard library so the Lambda stays dependency
free. Each stage lives behind a small interface, so swapping in LlamaIndex or
a vector store later means replacing one module, not rewriting the agent.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Document:
    """A retrievable text unit with metadata and a stable id."""

    doc_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


def job_item_to_document(item: dict[str, Any]) -> Document:
    """Convert a DynamoDB job item into an indexable document."""
    job_id = str(item.get("jobId", ""))
    parts = [
        f"Job {job_id}",
        f"file: {item.get('fileName', '')}",
        f"content type: {item.get('contentType', '')}",
        f"status: {item.get('status', '')}",
    ]
    if item.get("result"):
        parts.append(f"result: {item['result']}")
    if item.get("error"):
        parts.append(f"error: {item['error']}")
    return Document(
        doc_id=f"job:{job_id}",
        text=" | ".join(parts),
        metadata={"jobId": job_id, "kind": "job"},
    )
