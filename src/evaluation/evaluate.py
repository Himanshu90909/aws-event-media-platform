"""Offline evaluation for the agent's retrieval stage and pipeline health.

Two evaluations:

1. Retrieval quality — run the golden cases through SearchIndex and score
   MRR, Precision@k and Recall@k. This is the number that tells you whether
   an index or embedding swap helped or hurt.

2. Job processing health — given exported job items, compute the terminal
   success rate and per-status counts. Same math backs the CloudWatch
   metrics, so offline evaluation and production dashboards agree.
"""

from typing import Any

from agent.documents import Document
from agent.index import SearchIndex
from evaluation.golden import GoldenCase

PASSING_MRR = 0.5  # threshold below which `evaluate` exits non-zero


def precision_at_k(retrieved: list[str], expected: list[str], k: int) -> float:
    if not expected or k <= 0:
        return 0.0
    top = retrieved[:k]
    hits = sum(1 for doc_id in top if doc_id in expected)
    return hits / min(k, len(top)) if top else 0.0


def recall_at_k(retrieved: list[str], expected: list[str], k: int) -> float:
    if not expected:
        return 0.0
    hits = sum(1 for doc_id in retrieved[:k] if doc_id in expected)
    return hits / len(expected)


def mean_reciprocal_rank(retrieved: list[str], expected: list[str]) -> float:
    for rank, doc_id in enumerate(retrieved, start=1):
        if doc_id in expected:
            return 1.0 / rank
    return 0.0


def evaluate_case(index: SearchIndex, case: GoldenCase, k: int = 5) -> dict[str, Any]:
    retrieved = [sd.document.doc_id for sd in index.retrieve(case.question, top_k=k)]
    return {
        "question": case.question,
        "retrieved": retrieved,
        "mrr": mean_reciprocal_rank(retrieved, case.expected_doc_ids),
        "precision_at_k": precision_at_k(retrieved, case.expected_doc_ids, k),
        "recall_at_k": recall_at_k(retrieved, case.expected_doc_ids, k),
    }


def run_retrieval_evaluation(
    documents: list[Document],
    cases: list[GoldenCase],
    k: int = 5,
) -> dict[str, Any]:
    """Index documents, score every golden case, and report aggregate metrics."""
    index = SearchIndex()
    index.ingest(documents)
    results = [evaluate_case(index, case, k) for case in cases]
    mrr = sum(r["mrr"] for r in results) / len(results) if results else 0.0
    p_at_k = sum(r["precision_at_k"] for r in results) / len(results) if results else 0.0
    r_at_k = sum(r["recall_at_k"] for r in results) / len(results) if results else 0.0
    return {
        "cases": len(results),
        "documents_indexed": len(index),
        "mean_reciprocal_rank": round(mrr, 4),
        "mean_precision_at_k": round(p_at_k, 4),
        "mean_recall_at_k": round(r_at_k, 4),
        "ok": mrr >= PASSING_MRR,
        "details": results,
    }


def job_success_rate(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Terminal-state health report over exported job items."""
    terminal = [i for i in items if i.get("status") in {"COMPLETED", "FAILED"}]
    completed = sum(1 for i in terminal if i.get("status") == "COMPLETED")
    per_status: dict[str, int] = {}
    for item in items:
        status = str(item.get("status", "UNKNOWN"))
        per_status[status] = per_status.get(status, 0) + 1
    return {
        "total": len(items),
        "terminal": len(terminal),
        "completed": completed,
        "failed": len(terminal) - completed,
        "success_rate": round(completed / len(terminal), 4) if terminal else None,
        "per_status": per_status,
    }
