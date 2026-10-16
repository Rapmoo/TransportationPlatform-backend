
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database.session import SessionLocal  # noqa: E402
from app.models.duplicate_employee_notification import (  # noqa: E402
    DuplicateEmployeeNotification,
    DuplicateNotificationStatus,
)
from app.services.audit import create_audit_log  # noqa: E402
from app.services.duplicates import find_duplicate_employee_ids  # noqa: E402


def main(apply: bool) -> None:
    db = SessionLocal()
    try:
        created = skipped = 0
        for employee_id, count in find_duplicate_employee_ids(db):
            already_active = (
                db.query(DuplicateEmployeeNotification.id)
                .filter(
                    DuplicateEmployeeNotification.employee_id == employee_id,
                    DuplicateEmployeeNotification.status
                    == DuplicateNotificationStatus.ACTIVE,
                )
                .first()
            )
            if already_active:
                skipped += 1
                continue

            print(f"{'CREATE' if apply else 'WOULD CREATE'}: {employee_id} ({count} submissions)")
            created += 1

            if apply:
                db.add(DuplicateEmployeeNotification(employee_id=employee_id))
                create_audit_log(
                    db=db,
                    action="DUPLICATE_EMPLOYEE_ID_DETECTED",
                    details=(
                        f"Backfill: employee ID {employee_id} is used by "
                        f"{count} existing submissions."
                    ),
                )

        if apply:
            db.commit()
        print(f"\n{'Created' if apply else 'Would create'}: {created}, already active: {skipped}")
        if not apply:
            print("Dry run only. Re-run with --apply to write.")
    finally:
        db.close()


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)