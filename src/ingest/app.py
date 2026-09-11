import json
import os
import re
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError

from common.logging_utils import log
from common.models import Job
from common.state import now_iso

table = boto3.resource("dynamodb").Table(os.environ.get("JOBS_TABLE", "jobs"))
s3 = boto3.client("s3")
sqs = boto3.client("sqs")
BUCKET = os.environ.get("MEDIA_BUCKET", "")
QUEUE_URL = os.environ.get("PROCESSING_QUEUE_URL", "")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")

def response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}

def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        raw = event.get("body") or "{}"
        body = json.loads(raw) if isinstance(raw, str) else raw
        file_name = body.get("fileName")
        content_type = body.get("contentType")
        if not isinstance(file_name, str) or not SAFE_NAME.fullmatch(file_name):
            return response(400, {"error": "fileName must be a safe file name up to 255 characters"})
        if not isinstance(content_type, str) or not content_type.strip() or len(content_type) > 127:
            return response(400, {"error": "contentType is required"})
        job_id = str(uuid.uuid4())
        timestamp = now_iso()
        object_key = f"media/{job_id}/{file_name}"
        job = Job(job_id, file_name, content_type, "QUEUED", timestamp, timestamp, object_key)
        table.put_item(Item=job.as_item(), ConditionExpression="attribute_not_exists(jobId)")
        s3.put_object(Bucket=BUCKET, Key=object_key, ContentType=content_type, Metadata={"job-id": job_id})
        sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps({"jobId": job_id}))
        log("ingest", "QUEUED", job_id, requestId=getattr(context, "aws_request_id", None))
        return response(202, {"jobId": job_id, "status": "QUEUED", "objectKey": object_key})
    except (json.JSONDecodeError, TypeError):
        return response(400, {"error": "Request body must be valid JSON"})
    except ClientError as exc:
        log("ingest", "ERROR", error=str(exc))
        return response(503, {"error": "Unable to accept job; retry the request"})
