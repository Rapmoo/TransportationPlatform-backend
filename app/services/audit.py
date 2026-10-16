from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.user import User


def create_audit_log(
    db: Session,
    action: str,
    actor: User | None = None,
    submission_id: int | None = None,
    old_status: str | None = None,
    new_status: str | None = None,
    details: str | None = None,
):
    audit_log = AuditLog(
        submission_id=submission_id,
        actor_id=actor.id if actor else None,
        actor_role=actor.role if actor else None,
        action=action,
        old_status=old_status,
        new_status=new_status,
        details=details,
    )

    db.add(audit_log)

    return audit_log