"""Database-backed outbox and idempotent task leases.

Guarantee: at-least-once, not exactly-once. An HTTP receiver may accept a
request before our worker records success, so consumers must deduplicate.
"""
import random
import time
from datetime import timedelta

import requests
from flask import current_app
from sqlalchemy import and_, or_, select

from .extensions import db
from .models import Delivery, DeliveryAttempt, new_id, utcnow
from .security import sign, validate_destination_url


def dispatch_due(publish, *, only_delivery_ids=None):
    """Claim due DB records before publishing; expired leases are recoverable."""
    now = utcnow()
    due = or_(
        and_(Delivery.status.in_(["pending", "retry"]), Delivery.next_attempt_at <= now),
        and_(Delivery.status.in_(["queued", "sending"]), Delivery.lease_expires_at <= now),
    )
    stmt = (select(Delivery).where(due).order_by(Delivery.created_at)
            .limit(current_app.config["DISPATCH_BATCH_SIZE"]).with_for_update(skip_locked=True))
    if only_delivery_ids is not None:
        stmt = stmt.where(Delivery.id.in_(only_delivery_ids))
    rows = db.session.execute(stmt).scalars().all()
    claimed = []
    for row in rows:
        token = new_id()
        row.status = "queued"
        row.lease_token = token
        row.lease_expires_at = now + timedelta(seconds=current_app.config["LEASE_SECONDS"])
        claimed.append((row.id, token))
    db.session.commit()
    published = 0
    for delivery_id, token in claimed:
        try:
            publish(delivery_id, token)
            published += 1
        except Exception:
            # The task may have reached the broker before its acknowledgement was lost.
            # Resetting can cause another send; idempotent consumers are mandatory.
            row = db.session.get(Delivery, delivery_id)
            if row and row.status == "queued" and row.lease_token == token:
                row.status = "retry"
                row.lease_token = None
                row.lease_expires_at = None
                row.next_attempt_at = utcnow()
                db.session.commit()
            current_app.logger.exception("Could not enqueue delivery %s", delivery_id)
    return published


def backoff_seconds(number: int) -> float:
    base = current_app.config["RETRY_BASE_SECONDS"]
    cap = current_app.config["RETRY_CAP_SECONDS"]
    nominal = min(cap, base * (2 ** min(number - 1, 30)))
    return nominal * (1 if current_app.config["TESTING"] else random.uniform(0.9, 1.1))


def perform_delivery(delivery_id: str, token: str, sender=None, *, allow_private_destination=False) -> str:
    """A task is valid only while it owns its DB-issued token."""
    row = db.session.execute(select(Delivery).where(Delivery.id == delivery_id).with_for_update()).scalar_one_or_none()
    if row is None or row.status != "queued" or row.lease_token != token:
        db.session.rollback()
        return "stale"
    if not row.destination.active:
        row.status = "dead"
        row.last_error = "Destination disabled"
        row.lease_token = None
        row.lease_expires_at = None
        db.session.commit()
        return "dead"
    row.status = "sending"
    # Workers that die after this commit are re-queued after lease expiry.
    db.session.commit()

    started = time.monotonic()
    code = None
    error = None
    try:
        validate_destination_url(row.destination.url,
                                 current_app.config["ALLOW_PRIVATE_DESTINATIONS"] or allow_private_destination)
        headers = {
            "Content-Type": "application/json",
            "X-HookRelay-Signature": sign(row.destination.signing_secret, row.event.payload),
            "X-HookRelay-Event-Id": row.event.external_id,
            "X-HookRelay-Delivery-Id": row.id,
        }
        post = sender or requests.post
        response = post(row.destination.url, data=row.event.payload, headers=headers,
                        timeout=current_app.config["DELIVERY_TIMEOUT_SECONDS"], allow_redirects=False)
        code = response.status_code
        if not 200 <= code < 300:
            error = f"HTTP {code}"
    except (requests.RequestException, ValueError) as exc:
        error = str(exc)[:500]
    elapsed = int((time.monotonic() - started) * 1000)

    # Re-check lease after network I/O; an expired task must not overwrite the
    # new owner's result, even if its remote HTTP call already succeeded.
    db.session.expire_all()
    row = db.session.execute(select(Delivery).where(Delivery.id == delivery_id).with_for_update()).scalar_one()
    if row.status != "sending" or row.lease_token != token:
        db.session.rollback()
        return "stale"
    number = row.attempt_count + 1
    db.session.add(DeliveryAttempt(delivery_id=row.id, number=number, status_code=code,
                                   error=error, duration_ms=elapsed))
    row.attempt_count = number
    row.last_error = error
    row.lease_token = None
    row.lease_expires_at = None
    if error is None:
        row.status = "succeeded"
        row.next_attempt_at = None
        row.delivered_at = utcnow()
    elif number >= row.max_attempts:
        row.status = "dead"
        row.next_attempt_at = None
    else:
        row.status = "retry"
        row.next_attempt_at = utcnow() + timedelta(seconds=backoff_seconds(number))
    db.session.commit()
    return row.status
