"""Lambda entrypoint for the agent query API (POST /agent/query).

Per invocation this scans recent job items (bounded by AGENT_INDEX_SCAN_LIMIT,
default 200), builds the in-memory index, and answers the caller's question
with citations. For this dataset size that is honest and simple; the index
module is the swap point for a persistent vector store later.

Event shape (API Gateway proxy): {"body": "{\"question\": \"...\"}"}
Response: {"answer": ..., "citations": [...], "latencyMs": ...}
"""

import json
import os
from typing import Any

import boto3

from agent.agent import MediaJobsAgent
from agent.documents import job_item_to_document
from agent.index import SearchIndex
from agent.llm import get_llm
from agent.tools import build_tools
from common.logging_utils import log
from common.metrics import emit_metric

SCAN_LIMIT = int(os.environ.get("AGENT_INDEX_SCAN_LIMIT", "200"))

_table = None


def get_table() -> Any:
    """Lazy DynamoDB table handle (import-time boto3 breaks local tests)."""
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ.get("JOBS_TABLE", "jobs"))
    return _table


def scan_recent_job_items(limit: int = SCAN_LIMIT) -> list[dict[str, Any]]:
    """Scan up to `limit` job items. Raises boto3 errors to the Lambda runtime."""
    table = get_table()
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"Limit": min(limit, 100)}  # DynamoDB Scan caps Limit at 100/page
    while len(items) < limit:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        kwargs["ExclusiveStartKey"] = response.get("LastEvaluatedKey")
        if not kwargs["ExclusiveStartKey"]:
            break
    return items[:limit]


def build_agent() -> MediaJobsAgent:
    index = SearchIndex()
    index.ingest([job_item_to_document(item) for item in scan_recent_job_items()])
    tools = build_tools(index, job_lookup=lambda job_id: get_table().get_item(Key={"jobId": job_id}).get("Item"))
    return MediaJobsAgent(tools, llm=get_llm())


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        body = json.loads(event.get("body") or "{}")
        question = (body.get("question") or "").strip()
        if not question:
            return {"statusCode": 400, "body": json.dumps({"error": "question is required"})}
        agent = build_agent()
        result = agent.answer(question)
        emit_metric("AgentQueries", 1, outcome="ok")
        return {
            "statusCode": 200,
            "body": json.dumps(
                {"answer": result.answer, "citations": result.citations, "latencyMs": result.latency_ms},
                default=str,
            ),
        }
    except Exception as exc:  # noqa: BLE001 - API must return a structured error
        log("agent", "ERROR", error=str(exc)[:500])
        emit_metric("AgentQueries", 1, outcome="error")
        return {"statusCode": 500, "body": json.dumps({"error": "agent query failed"})}
