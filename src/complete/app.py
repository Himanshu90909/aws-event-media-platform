import json
import os
import re
from typing import Any

import boto3
from botocore.exceptions import ClientError

from common.logging_utils import log
from common.state import transition

table = boto3.resource("dynamodb").Table(os.environ.get("JOBS_TABLE", "jobs"))
s3 = boto3.client("s3")
sqs = boto3.client("sqs")
BUCKET = os.environ.get("MEDIA_BUCKET", "")
QUEUE_URL = os.environ.get("PROCESSING_QUEUE_URL", "")
JOB_ID = re.compile(r"^[0-9a-fA-F-]{36}$")


def response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
    }


def enqueue(job_id: str) -> None:
    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps({"jobId": job_id}))


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    job_id = (event.get("pathParameters") or {}).get("jobId", "")
    if not JOB_ID.fullmatch(job_id):
        return response(400, {"error": "Invalid jobId"})

    try:
        item = table.get_item(Key={"jobId": job_id}).get("Item")
        if not item:
            return response(404, {"error": "Job not found"})

        status = item.get("status")
        if status == "QUEUED":
            # A retry after a successful state update but failed SQS call is safe.
            enqueue(job_id)
            return response(202, {"jobId": job_id, "status": "QUEUED"})
        if status != "AWAITING_UPLOAD":
            return response(409, {"error": f"Job cannot be finalized from status {status}"})

        object_key = item.get("objectKey")
        if not object_key:
            return response(500, {"error": "Job is missing its object key"})
        metadata = s3.head_object(Bucket=BUCKET, Key=object_key)
        if metadata.get("ContentLength", 0) <= 0:
            return response(400, {"error": "Uploaded media must not be empty"})
        uploaded_type = metadata.get("ContentType")
        if uploaded_type and uploaded_type != item.get("contentType"):
            return response(400, {"error": "Uploaded Content-Type does not match the job"})

        if not transition(table, job_id, "AWAITING_UPLOAD", "QUEUED"):
            return response(409, {"error": "Job is already being finalized"})
        enqueue(job_id)
        log("complete", "QUEUED", job_id)
        return response(202, {"jobId": job_id, "status": "QUEUED"})
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return response(409, {"error": "Upload not found; PUT the media before finalizing"})
        log("complete", "ERROR", job_id, error=str(exc))
        return response(503, {"error": "Unable to finalize upload; retry the request"})
