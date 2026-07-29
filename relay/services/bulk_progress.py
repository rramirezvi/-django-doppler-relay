from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Count, Q

from relay.models import BulkSend, BulkSendRecipient


@dataclass(frozen=True)
class BulkImportProgress:
    total_rows: int
    valid_rows: int
    invalid_rows: int
    pending_rows: int
    reconciled: bool


def get_bulk_import_progress(
    bulk: BulkSend, *, reconcile: bool = False
) -> BulkImportProgress:
    if bulk.engine_version != BulkSend.ENGINE_V2:
        return BulkImportProgress(0, 0, 0, 0, True)
    if not reconcile:
        return BulkImportProgress(
            total_rows=int(bulk.imported_rows or 0),
            valid_rows=int(bulk.valid_rows or 0),
            invalid_rows=int(bulk.invalid_rows or 0),
            pending_rows=int(bulk.valid_rows or 0),
            reconciled=True,
        )

    counts = bulk.recipient_occurrences.aggregate(
        total=Count("id"),
        valid=Count("id", filter=Q(status=BulkSendRecipient.STATUS_PENDING)),
        invalid=Count("id", filter=Q(status=BulkSendRecipient.STATUS_INVALID)),
    )
    total = int(counts["total"] or 0)
    valid = int(counts["valid"] or 0)
    invalid = int(counts["invalid"] or 0)
    return BulkImportProgress(
        total_rows=total,
        valid_rows=valid,
        invalid_rows=invalid,
        pending_rows=valid,
        reconciled=(
            total == int(bulk.imported_rows or 0)
            and valid == int(bulk.valid_rows or 0)
            and invalid == int(bulk.invalid_rows or 0)
        ),
    )
