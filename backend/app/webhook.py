"""Final-record delivery webhook (Sprint 2).

Idempotent delivery: a record is POSTed to the webhook sink exactly once per
revision, tracked via ``delivered_revision`` in SQLite.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx

from .protocol import FinalRecord, SessionState
from .store import SessionStore


def _banner(session_id: str, revision: int, outcome: str) -> str:
    return f"=== [EVENT=FINAL_RECORD_DELIVERED] Session={session_id} Rev={revision} Outcome={outcome} ==="


def deliver_final_record(
    store: SessionStore,
    state: SessionState,
    record: FinalRecord,
    webhook_url: Optional[str] = None,
) -> bool:
    """Deliver a final record exactly once per revision.

    Returns ``True`` when the record was delivered, ``False`` when it was
    skipped because it was already delivered (idempotency guard).
    """
    if state.delivered_revision >= record.revision:
        return False

    # Structured log banner + indented JSON record to stdout.
    record_json = json.dumps(
        json.loads(record.model_dump_json(by_alias=True, exclude_none=True)),
        indent=2,
    )
    print(_banner(record.session_id, record.revision, record.outcome))
    print(record_json)

    # Append delivery event to the SQLite timeline.
    store.append_event(
        record.session_id,
        "system",
        "final_record_delivered",
        {
            "revision": record.revision,
            "outcome": record.outcome,
            "outcome_reason": record.outcome_reason,
        },
    )

    # Dispatch HTTP POST to the configured webhook URL (graceful on failure).
    target = webhook_url or os.getenv("WEBHOOK_URL")
    if target:
        try:
            with httpx.Client(timeout=5.0) as client:
                client.post(
                    target,
                    json=json.loads(record.model_dump_json(by_alias=True, exclude_none=True)),
                )
        except Exception:
            # Webhook delivery is best-effort; the record is still marked
            # delivered so we do not retry indefinitely.
            pass

    # Mark revision as delivered (idempotent via store).
    store.mark_revision_delivered(record.session_id, record.revision)
    state.delivered_revision = record.revision
    return True
