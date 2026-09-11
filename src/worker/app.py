import json
import os
from typing import Any

import boto3

from common.logging_utils import log
from common.state import transition

table = boto3.resource("dynamodb").Table(os.environ.get("JOBS_TABLE", "jobs"))
s3 = boto3.client("s3")
BUCKET = os.environ.get("MEDIA_BUCKET", "")

def process_media(job_id: str, object_key: str) -> dict[str, Any]:
    # This is intentionally a lightweight, deterministic processing step suitable for Lambda.
    # Replace with ffmpeg/media service integration when production media transforms are needed.
    metadata = s3.head_object(Bucket=BUCKET, Key=object_key)
    return {"processedBytes": metadata.get("ContentLength", 0), "processor": "lambda-metadata-v1"}

def handle_record(record: dict[str, Any]) -> str | None:
    try:
        payload = json.loads(record.get("body", "{}"))
        job_id = payload.get("jobId")
        if not job_id:
            raise ValueError("Missing jobId")
        current = table.get_item(Key={"jobId": job_id}).get("Item")
        if not current:
            log("worker", "SKIPPED", job_id, reason="job_not_found")
            return None
        status = current.get("status")
        if status == "COMPLETED":
            log("worker", "SKIPPED", job_id, reason="already_completed")
            return None
        if status == "FAILED":
            log("worker", "SKIPPED", job_id, reason="already_failed")
            return None
        if status != "QUEUED":
            raise RuntimeError(f"Unexpected job status: {status}")
        if not transition(table, job_id, "QUEUED", "PROCESSING"):
            log("worker", "SKIPPED", job_id, reason="claimed_by_another_delivery")
            return None
        result = process_media(job_id, current["objectKey"])
        if not transition(table, job_id, "PROCESSING", "COMPLETED", result=json.dumps(result)):
            log("worker", "SKIPPED", job_id, reason="completion_race")
        else:
            log("worker", "COMPLETED", job_id, result=result)
        return None
    except Exception as exc:
        job_id = locals().get("job_id")
        if job_id:
            try:
                transition(table, job_id, "PROCESSING", "FAILED", error=str(exc)[:1000])
            except Exception as update_exc:  # noqa: BLE001 - preserve original failure for SQS retry
                log("worker", "ERROR", job_id, error=f"failure update failed: {update_exc}")
        log("worker", "ERROR", job_id, error=str(exc))
        raise

def handler(event: dict[str, Any], context: Any) -> dict[str, list[dict[str, str]]]:
    failures = []
    for record in event.get("Records", []):
        try:
            handle_record(record)
        except Exception:  # noqa: BLE001 - SQS must retry any processing failure
            failures.append({"itemIdentifier": record.get("messageId", "unknown")})
    return {"batchItemFailures": failures}
