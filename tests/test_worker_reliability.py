"""Worker reliability tests: concurrent claims, poison messages, idempotency."""

import json
from pathlib import Path
from unittest.mock import Mock

from botocore.exceptions import ClientError

import worker.app as worker_app
from common.state import transition


def error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "UpdateItem")


def make_record(job_id: str = "j-1", body: str | None = None) -> dict:
    return {
        "messageId": "m-1",
        "body": body if body is not None else json.dumps({"jobId": job_id}),
    }


def load_worker(monkeypatch, table: Mock, s3: Mock | None = None) -> None:
    monkeypatch.setattr(worker_app, "table", table)
    if s3 is not None:
        monkeypatch.setattr(worker_app, "s3", s3)


class TestConcurrentClaims:
    def test_second_worker_loses_claim_race_and_skips(self, monkeypatch):
        """Two pollers race for one QUEUED job: stale reader's claim fails
        on the conditional update and the job is skipped, never double-processed."""
        table = Mock()
        item = {"jobId": "j-1", "status": "QUEUED", "objectKey": "media/j-1"}
        table.get_item.return_value = {"Item": item}
        # A claims, A completes, B's stale claim fails the condition check
        table.update_item.side_effect = [None, None, error("ConditionalCheckFailedException")]
        s3 = Mock()
        s3.head_object.return_value = {"ContentLength": 100}

        load_worker(monkeypatch, table, s3)
        worker_app.handle_record(make_record())  # worker A wins the claim
        assert worker_app.handle_record(make_record()) is None  # worker B: no-op
        assert table.update_item.call_count == 3  # A claim + A complete + B rejected claim
        s3.head_object.assert_called_once()  # media fetched exactly once

    def test_completion_race_is_idempotent(self, monkeypatch):
        """PROCESSING->COMPLETED race: the losing worker skips instead of
        double-completing, and no error escapes to SQS."""
        table = Mock()
        table.get_item.return_value = {"Item": {"jobId": "j-1", "status": "QUEUED", "objectKey": "k"}}
        table.update_item.side_effect = [None, error("ConditionalCheckFailedException")]
        s3 = Mock()
        s3.head_object.return_value = {"ContentLength": 5}
        load_worker(monkeypatch, table, s3)
        assert worker_app.handle_record(make_record()) is None
        assert table.update_item.call_count == 2

class TestPoisonMessages:
    def test_poison_payload_reports_batch_failure_for_redrive(self, monkeypatch):
        table = Mock()
        table.get_item.return_value = {"Item": {"jobId": "j-9", "status": "QUEUED", "objectKey": "k"}}
        load_worker(monkeypatch, table, Mock())
        result = worker_app.handler(
            {"Records": [make_record(job_id="j-9", body="not json{")]}, None
        )
        assert [f["itemIdentifier"] for f in result["batchItemFailures"]] == ["m-1"]

    def test_missing_jobid_is_also_a_poison_message(self, monkeypatch):
        load_worker(monkeypatch, Mock(), Mock())
        result = worker_app.handler(
            {"Records": [make_record(body=json.dumps({"file": "x.mp4"}))]}, None
        )
        assert len(result["batchItemFailures"]) == 1

    def test_unknown_job_is_skipped_not_failed(self, monkeypatch):
        table = Mock()
        table.get_item.return_value = {}
        load_worker(monkeypatch, table)
        result = worker_app.handler({"Records": [make_record()]}, None)
        assert result["batchItemFailures"] == []

    def test_dlq_redrive_policy_in_template(self):
        text = Path("template.yaml").read_text()
        assert "maxReceiveCount: 3" in text
        assert "ProcessingDLQ" in text


class TestIdempotency:
    def test_already_completed_job_is_skipped_without_transition(self, monkeypatch):
        table = Mock()
        table.get_item.return_value = {"Item": {"jobId": "j-1", "status": "COMPLETED", "objectKey": "k"}}
        load_worker(monkeypatch, table, Mock())
        worker_app.handle_record(make_record())
        table.update_item.assert_not_called()

    def test_transition_rejects_invalid_state_pair(self):
        assert transition(Mock(), "j-1", "COMPLETED", "PROCESSING") is False
