import json
import logging
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def log(operation: str, status: str, job_id: str | None = None, **details: Any) -> None:
    payload = {"operation": operation, "status": status, **details}
    if job_id:
        payload["jobId"] = job_id
    logger.info(json.dumps(payload, default=str, sort_keys=True))
