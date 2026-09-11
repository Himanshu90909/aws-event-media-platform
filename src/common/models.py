from dataclasses import dataclass
from typing import Any

STATUSES = {"AWAITING_UPLOAD", "QUEUED", "PROCESSING", "COMPLETED", "FAILED"}


@dataclass(frozen=True)
class Job:
    job_id: str
    file_name: str
    content_type: str
    status: str
    created_at: str
    updated_at: str
    object_key: str
    error: str | None = None

    def as_item(self) -> dict[str, Any]:
        item = self.__dict__.copy()
        item["jobId"] = item.pop("job_id")
        item["fileName"] = item.pop("file_name")
        item["contentType"] = item.pop("content_type")
        item["createdAt"] = item.pop("created_at")
        item["updatedAt"] = item.pop("updated_at")
        item["objectKey"] = item.pop("object_key")
        return item
