from datetime import datetime

from pydantic import BaseModel


class DuplicateSubmissionItem(BaseModel):
    submission_number: str | None
    employee_name: str
    organization_name: str
    submitted_at: datetime | None


class DuplicateNotificationItem(BaseModel):
    id: int
    employee_id: str
    message: str
    status: str
    submission_count: int
    submissions: list[DuplicateSubmissionItem]
    detected_at: datetime
    acknowledged_at: datetime | None = None


class DuplicateNotificationList(BaseModel):
    items: list[DuplicateNotificationItem]
    total: int
    page: int
    page_size: int
    total_pages: int