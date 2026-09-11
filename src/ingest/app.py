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
BUCKET = os.environ.get("MEDIA_BUCKET", "")
try:
    UPLOAD_URL_EXPIRES_IN = max(60, min(int(os.environ.get("UPLOAD_URL_EXPIRES_IN", "900")), 3600))
except ValueError:
    UPLOAD_URL_EXPIRES_IN = 900
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


def response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        raw = event.get("body") or "{}"
        body = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(body, dict):
            return response(400, {"error": "Request body must be a JSON object"})
        file_name = body.get("fileName")
        content_type = body.get("contentType")
        if not isinstance(file_name, str) or not SAFE_NAME.fullmatch(file_name):
            return response(400, {"error": "fileName must be a safe file name up to 255 characters"})
        if not isinstance(content_type, str) or not content_type.strip() or len(content_type) > 127:
            return response(400, {"error": "contentType is required"})
        content_type = content_type.strip()
        job_id = str(uuid.uuid4())
        timestamp = now_iso()
        object_key = f"media/{job_id}/{file_name}"
        upload_url = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": BUCKET, "Key": object_key, "ContentType": content_type},
            ExpiresIn=UPLOAD_URL_EXPIRES_IN,
            HttpMethod="PUT",
        )
        job = Job(job_id, file_name, content_type, "AWAITING_UPLOAD", timestamp, timestamp, object_key)
        table.put_item(Item=job.as_item(), ConditionExpression="attribute_not_exists(jobId)")
        log("ingest", "AWAITING_UPLOAD", job_id, requestId=getattr(context, "aws_request_id", None))
        return response(
            202,
            {
                "jobId": job_id,
                "status": "AWAITING_UPLOAD",
                "objectKey": object_key,
                "uploadUrl": upload_url,
                "uploadUrlExpiresIn": UPLOAD_URL_EXPIRES_IN,
                "next": f"POST /jobs/{job_id}/complete after uploading with the presigned PUT URL",
            },
        )
    except (json.JSONDecodeError, TypeError):
        return response(400, {"error": "Request body must be valid JSON"})
    except ClientError as exc:
        log("ingest", "ERROR", error=str(exc))
        return response(503, {"error": "Unable to create upload job; retry the request"})
