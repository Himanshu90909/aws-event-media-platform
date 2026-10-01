"""Evaluation harness tests: metric math, golden run, and job health report."""

import json
from pathlib import Path

from agent.documents import job_item_to_document
from evaluation.evaluate import (
    job_success_rate,
    mean_reciprocal_rank,
    precision_at_k,
    recall_at_k,
    run_retrieval_evaluation,
)
from evaluation.golden import GOLDEN_CASES

FIXTURE = json.loads(Path("tests/fixtures/job_items.json").read_text())["Items"]


def test_ranking_metric_math():
    assert mean_reciprocal_rank(["a", "b"], ["b"]) == 0.5
    assert mean_reciprocal_rank(["a"], ["b"]) == 0.0
    assert precision_at_k(["a", "b"], ["a"], 1) == 1.0
    assert precision_at_k(["a", "b"], ["a"], 2) == 0.5
    assert recall_at_k(["a"], ["a", "b"], 5) == 0.5
    assert precision_at_k([], ["a"], 5) == 0.0


def test_golden_retrieval_run_passes_threshold():
    docs = [job_item_to_document(i) for i in FIXTURE]
    report = run_retrieval_evaluation(docs, GOLDEN_CASES)
    assert report["documents_indexed"] == len(FIXTURE)
    assert report["cases"] == len(GOLDEN_CASES)
    assert report["mean_reciprocal_rank"] >= 0.5
    assert report["ok"] is True


def test_job_success_rate_counts_terminal_states_only():
    report = job_success_rate(FIXTURE)
    assert report["total"] == 5
    assert report["terminal"] == 4  # QUEUED job is not terminal
    assert report["success_rate"] == 0.5
    assert report["per_status"]["FAILED"] == 2


def test_job_success_rate_empty_is_not_divided_by_zero():
    report = job_success_rate([])
    assert report["success_rate"] is None
