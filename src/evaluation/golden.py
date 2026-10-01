"""Golden evaluation set for retrieval quality.

Each case pairs a question with the job doc_ids that a good retriever must
return. Cases reference tests/fixtures/job_items.json so the evaluation is
fully reproducible offline, with no AWS access.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class GoldenCase:
    question: str
    expected_doc_ids: list[str]


GOLDEN_CASES: list[GoldenCase] = [
    GoldenCase("which jobs failed?", ["job:j-002", "job:j-005"]),
    GoldenCase("any video uploads completed?", ["job:j-001", "job:j-003"]),
    GoldenCase("what was the largest file processed?", ["job:j-003"]),
    GoldenCase("show me the failed image job", ["job:j-005"]),
    GoldenCase("which job had a decode error", ["job:j-002"]),
]
