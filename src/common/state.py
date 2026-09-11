from datetime import datetime, timezone
from typing import Any

VALID_TRANSITIONS = {
    "AWAITING_UPLOAD": {"QUEUED", "FAILED"},
    "QUEUED": {"PROCESSING", "FAILED"},
    "PROCESSING": {"QUEUED", "COMPLETED", "FAILED"},
    "COMPLETED": set(),
    "FAILED": set(),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def can_transition(current: str, target: str) -> bool:
    return target in VALID_TRANSITIONS.get(current, set())


def transition(table: Any, job_id: str, current: str, target: str, **updates: Any) -> bool:
    if not can_transition(current, target):
        return False
    values = {":expected": current, ":status": target, ":updated": now_iso()}
    names = {"#status": "status", "#updated": "updatedAt"}
    set_parts = ["#status = :status", "#updated = :updated"]
    for key, value in updates.items():
        token = f":{key}"
        values[token] = value
        names[f"#{key}"] = key
        set_parts.append(f"#{key} = {token}")
    from botocore.exceptions import ClientError

    try:
        table.update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression="#status = :expected",
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
