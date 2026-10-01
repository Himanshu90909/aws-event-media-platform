"""Local CLI for the agent and evaluation harness.

Run from the repo root:
    PYTHONPATH=src python -m agent.cli query "what failed in the last batch"
    PYTHONPATH=src python -m agent.cli evaluate
    PYTHONPATH=src python -m agent.cli dlq-depth

The CLI builds the index from a JSON export of job items (DynamoDB scan
output saved with --save in a real account) or from an inline fixture file,
so everything runs offline against the same code paths Lambda uses.
"""

import argparse
import json
import sys
from typing import Any

from agent.agent import MediaJobsAgent
from agent.documents import job_item_to_document
from agent.index import SearchIndex
from agent.llm import get_llm
from agent.tools import build_tools
from evaluation.evaluate import run_retrieval_evaluation
from evaluation.golden import GOLDEN_CASES


def load_items(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload.get("Items", payload) if isinstance(payload, dict) else payload


def build_agent_from_items(items: list[dict[str, Any]]) -> MediaJobsAgent:
    index = SearchIndex()
    index.ingest([job_item_to_document(item) for item in items])
    tools = build_tools(index, job_lookup=lambda job_id: next((i for i in items if i.get("jobId") == job_id), None))
    return MediaJobsAgent(tools, llm=get_llm())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-cli", description="Media-jobs agent tools")
    parser.add_argument("--items", default="tests/fixtures/job_items.json", help="JSON file of job items")
    sub = parser.add_subparsers(dest="command", required=True)

    query_parser = sub.add_parser("query", help="Ask the agent a question")
    query_parser.add_argument("question")

    sub.add_parser("evaluate", help="Score retrieval quality against the golden set")

    dlq_parser = sub.add_parser("dlq-depth", help="Print and emit the DLQ depth metric")
    dlq_parser.add_argument("--queue-url", default="")

    args = parser.parse_args(argv)
    items = load_items(args.items)

    if args.command == "query":
        agent = build_agent_from_items(items)
        result = agent.answer(args.question)
        print(json.dumps({"answer": result.answer, "citations": result.citations, "latencyMs": result.latency_ms}, indent=2))
        return 0

    if args.command == "evaluate":
        docs = [job_item_to_document(item) for item in items]
        report = run_retrieval_evaluation(docs, GOLDEN_CASES)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    if args.command == "dlq-depth":
        import boto3  # imported here: only this subcommand needs AWS

        queue_url = args.queue_url or input("Processing queue DLQ URL: ").strip()
        attributes = boto3.client("sqs").get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=["ApproximateNumberOfMessages"],
        )
        depth = int(attributes["Attributes"]["ApproximateNumberOfMessages"])
        from common.metrics import emit_metric

        emit_metric("DlqDepth", depth)
        print(f"DlqDepth={depth}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
