import json
import os
import re
from typing import Any

import boto3
from botocore.exceptions import ClientError

from common.logging_utils import log

table = boto3.resource("dynamodb").Table(os.environ.get("JOBS_TABLE", "jobs"))
JOB_ID = re.compile(r"^[0-9a-fA-F-]{36}$")

def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    job_id = (event.get("pathParameters") or {}).get("jobId", "")
    if not JOB_ID.fullmatch(job_id):
        return {"statusCode": 400, "body": json.dumps({"error": "Invalid jobId"})}
    try:
        result = table.get_item(Key={"jobId": job_id})
        item = result.get("Item")
        if not item:
            return {"statusCode": 404, "body": json.dumps({"error": "Job not found"})}
        item.setdefault("error", None)
        log("status", item["status"], job_id)
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": json.dumps(item)}
    except ClientError as exc:
        log("status", "ERROR", job_id, error=str(exc))
        return {"statusCode": 503, "body": json.dumps({"error": "Status temporarily unavailable"})}
