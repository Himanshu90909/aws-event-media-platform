import importlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from common.state import can_transition, transition


def error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "UpdateItem")


def load(name):
    return importlib.import_module(name)


def test_state_transition_rules():
    assert can_transition("AWAITING_UPLOAD", "QUEUED")
    assert can_transition("QUEUED", "PROCESSING")
    assert can_transition("PROCESSING", "COMPLETED")
    assert not can_transition("COMPLETED", "PROCESSING")
    assert not can_transition("AWAITING_UPLOAD", "COMPLETED")


def test_conditional_transition_is_idempotent():
    table = Mock()
    assert transition(table, "job-1", "AWAITING_UPLOAD", "QUEUED") is True
    table.update_item.side_effect = error("ConditionalCheckFailedException")
    assert transition(table, "job-1", "AWAITING_UPLOAD", "QUEUED") is False


def test_ingest_returns_presigned_upload_url(monkeypatch):
    mod = load("ingest.app")
    mod.table = Mock()
    mod.s3 = Mock()
    mod.s3.generate_presigned_url.return_value = "https://upload.example.test"
    mod.BUCKET = "bucket"
    result = mod.handler(
        {"body": json.dumps({"fileName": "video.mp4", "contentType": "video/mp4"})},
        SimpleNamespace(aws_request_id="req"),
    )
    assert result["statusCode"] == 202
    body = json.loads(result["body"])
    assert body["status"] == "AWAITING_UPLOAD"
    assert body["uploadUrl"] == "https://upload.example.test"
    mod.table.put_item.assert_called_once()
    mod.s3.generate_presigned_url.assert_called_once()
    mod.s3.put_object.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [{}, {"fileName": "bad/name.mp4", "contentType": "video/mp4"}, {"fileName": "x.mp4"}, []],
)
def test_ingest_invalid_request(monkeypatch, body):
    mod = load("ingest.app")
    assert mod.handler({"body": json.dumps(body)}, None)["statusCode"] == 400


def test_complete_finalizes_uploaded_object():
    mod = load("complete.app")
    mod.table = Mock()
    mod.s3 = Mock()
    mod.sqs = Mock()
    job_id = "00000000-0000-0000-0000-000000000000"
    mod.table.get_item.return_value = {
        "Item": {
            "jobId": job_id,
            "status": "AWAITING_UPLOAD",
            "objectKey": "media/key",
            "contentType": "video/mp4",
        }
    }
    mod.s3.head_object.return_value = {"ContentLength": 10, "ContentType": "video/mp4"}
    result = mod.handler({"pathParameters": {"jobId": job_id}}, None)
    assert result["statusCode"] == 202
    assert json.loads(result["body"])["status"] == "QUEUED"
    mod.s3.head_object.assert_called_once()
    mod.sqs.send_message.assert_called_once()


def test_complete_rejects_missing_upload():
    mod = load("complete.app")
    mod.table = Mock()
    mod.s3 = Mock()
    job_id = "00000000-0000-0000-0000-000000000000"
    mod.table.get_item.return_value = {
        "Item": {"jobId": job_id, "status": "AWAITING_UPLOAD", "objectKey": "media/key"}
    }
    mod.s3.head_object.side_effect = error("NotFound")
    assert mod.handler({"pathParameters": {"jobId": job_id}}, None)["statusCode"] == 409


def test_complete_retry_requeues_after_partial_failure():
    mod = load("complete.app")
    mod.table = Mock()
    mod.sqs = Mock()
    job_id = "00000000-0000-0000-0000-000000000000"
    mod.table.get_item.return_value = {"Item": {"jobId": job_id, "status": "QUEUED"}}
    result = mod.handler({"pathParameters": {"jobId": job_id}}, None)
    assert result["statusCode"] == 202
    mod.sqs.send_message.assert_called_once()


def test_status_existing_and_missing():
    mod = load("status.app")
    mod.table = Mock()
    job_id = "00000000-0000-0000-0000-000000000000"
    mod.table.get_item.return_value = {"Item": {"jobId": job_id, "status": "AWAITING_UPLOAD"}}
    result = mod.handler({"pathParameters": {"jobId": job_id}}, None)
    assert result["statusCode"] == 200
    mod.table.get_item.return_value = {}
    assert mod.handler({"pathParameters": {"jobId": job_id}}, None)["statusCode"] == 404
    assert mod.handler({"pathParameters": {"jobId": "bad"}}, None)["statusCode"] == 400


def test_worker_processes_and_skips_duplicate(monkeypatch):
    mod = load("worker.app")
    mod.table = Mock()
    mod.s3 = Mock()
    mod.BUCKET = "bucket"
    job_id = "00000000-0000-0000-0000-000000000000"
    mod.table.get_item.return_value = {"Item": {"jobId": job_id, "status": "QUEUED", "objectKey": "media/key"}}
    mod.s3.head_object.return_value = {"ContentLength": 10}
    event = {"Records": [{"messageId": "m1", "body": json.dumps({"jobId": job_id})}]}
    assert mod.handler(event, None) == {"batchItemFailures": []}
    assert mod.table.update_item.call_count == 2
    mod.table.get_item.return_value = {"Item": {"jobId": job_id, "status": "COMPLETED", "objectKey": "media/key"}}
    assert mod.handler(event, None) == {"batchItemFailures": []}


def test_worker_returns_batch_failure_for_retry(monkeypatch):
    mod = load("worker.app")
    mod.table = Mock()
    mod.s3 = Mock()
    job_id = "00000000-0000-0000-0000-000000000000"
    mod.table.get_item.return_value = {"Item": {"jobId": job_id, "status": "QUEUED", "objectKey": "media/key"}}
    mod.table.update_item.side_effect = RuntimeError("DynamoDB unavailable")
    event = {"Records": [{"messageId": "m1", "body": json.dumps({"jobId": job_id})}]}
    result = mod.handler(event, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
