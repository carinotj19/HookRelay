import hashlib
import json
import secrets
from functools import wraps

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import func, text

from .extensions import db
from .models import Delivery, DeliveryAttempt, Destination, Endpoint, Event
from .security import generate_secret, validate_destination_url, verify

bp = Blueprint("api", __name__)


def admin_only(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        expected = current_app.config["ADMIN_API_KEY"]
        auth = request.headers.get("Authorization", "")
        supplied = auth[7:] if auth.startswith("Bearer ") else ""
        if not expected or not secrets.compare_digest(supplied, expected):
            return jsonify(error="unauthorized"), 401
        return fn(*args, **kwargs)
    return wrapped


def error(message, code=400):
    return jsonify(error=message), code


def json_object():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def delivery_dict(d):
    return {
        "id": d.id, "event_id": d.event_id, "destination_id": d.destination_id,
        "status": d.status, "attempt_count": d.attempt_count,
        "max_attempts": d.max_attempts, "last_error": d.last_error,
        "replay_of_id": d.replay_of_id,
        "next_attempt_at": d.next_attempt_at.isoformat() + "Z" if d.next_attempt_at else None,
    }


@bp.get("/health/live")
def liveness():
    return jsonify(status="ok")


@bp.get("/health/ready")
def readiness():
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify(status="ready")
    except Exception:
        current_app.logger.exception("Database healthcheck failed")
        return jsonify(status="unavailable"), 503


@bp.post("/api/v1/endpoints")
@admin_only
def create_endpoint():
    data = json_object()
    name = data.get("name") if data else None
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        return error("name must be between 1 and 120 characters")
    endpoint = Endpoint(name=name.strip(), public_key=secrets.token_urlsafe(24),
                        signing_secret=generate_secret())
    db.session.add(endpoint)
    db.session.commit()
    return jsonify(id=endpoint.id, name=endpoint.name, public_key=endpoint.public_key,
                   signing_secret=endpoint.signing_secret,
                   ingest_path=f"/hooks/{endpoint.public_key}"), 201


@bp.get("/api/v1/endpoints")
@admin_only
def list_endpoints():
    rows = db.session.execute(select(Endpoint).order_by(Endpoint.created_at.desc())).scalars()
    return jsonify(items=[{"id": e.id, "name": e.name, "public_key": e.public_key,
                           "active": e.active} for e in rows])


@bp.post("/api/v1/destinations")
@admin_only
def create_destination():
    data = json_object()
    if not data:
        return error("Expected JSON object")
    endpoint = db.session.get(Endpoint, data.get("endpoint_id")) if isinstance(data.get("endpoint_id"), str) else None
    if endpoint is None:
        return error("Unknown endpoint_id", 404)
    name, url = data.get("name"), data.get("url")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        return error("name must be between 1 and 120 characters")
    try:
        validate_destination_url(url, current_app.config["ALLOW_PRIVATE_DESTINATIONS"])
    except ValueError as exc:
        return error(str(exc))
    dest = Destination(endpoint_id=endpoint.id, name=name.strip(), url=url,
                       signing_secret=generate_secret())
    db.session.add(dest)
    db.session.commit()
    return jsonify(id=dest.id, endpoint_id=dest.endpoint_id, url=dest.url,
                   signing_secret=dest.signing_secret), 201


@bp.get("/api/v1/destinations")
@admin_only
def list_destinations():
    rows = db.session.execute(select(Destination).order_by(Destination.created_at.desc())).scalars()
    return jsonify(items=[{"id": d.id, "endpoint_id": d.endpoint_id, "name": d.name,
                           "url": d.url, "active": d.active} for d in rows])


@bp.post("/hooks/<string:public_key>")
def receive(public_key):
    endpoint = db.session.execute(select(Endpoint).where(Endpoint.public_key == public_key,
                                                         Endpoint.active.is_(True))).scalar_one_or_none()
    if endpoint is None:
        return error("Unknown webhook endpoint", 404)
    body = request.get_data(cache=False)
    if not verify(endpoint.signing_secret, body, request.headers.get("X-HookRelay-Signature"),
                  current_app.config["SIGNATURE_TOLERANCE_SECONDS"]):
        return error("Invalid or expired signature", 401)
    ext_id = request.headers.get("X-HookRelay-Event-Id")
    if not ext_id or len(ext_id) > 200:
        return error("X-HookRelay-Event-Id is required (max 200 characters)")
    if request.mimetype != "application/json":
        return error("Content-Type must be application/json", 415)
    try:
        json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return error("Payload must be valid JSON")

    digest = hashlib.sha256(body).hexdigest()
    event = Event(endpoint_id=endpoint.id, external_id=ext_id, payload=body, payload_sha256=digest)
    db.session.add(event)
    try:
        db.session.flush()
        destinations = db.session.execute(select(Destination).where(Destination.endpoint_id == endpoint.id,
                                                                    Destination.active.is_(True))).scalars().all()
        for dest in destinations:
            db.session.add(Delivery(event_id=event.id, destination_id=dest.id,
                                    max_attempts=current_app.config["MAX_DELIVERY_ATTEMPTS"]))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        previous = db.session.execute(select(Event).where(Event.endpoint_id == endpoint.id,
                                                         Event.external_id == ext_id)).scalar_one_or_none()
        if previous is None:
            raise
        if previous.payload_sha256 != digest:
            return error("Idempotency key used for a different payload", 409)
        return jsonify(event_id=previous.id, duplicate=True), 200
    # The dispatcher discovers committed rows. No broker dependency in this path.
    return jsonify(event_id=event.id, duplicate=False, deliveries=len(destinations)), 202


@bp.get("/api/v1/events")
@admin_only
def list_events():
    limit = min(max(request.args.get("limit", 30, type=int), 1), 100)
    events = db.session.execute(select(Event).order_by(Event.received_at.desc(), Event.id).limit(limit)).scalars()
    return jsonify(items=[{"id": e.id, "endpoint_id": e.endpoint_id, "external_id": e.external_id,
                           "payload_sha256": e.payload_sha256,
                           "received_at": e.received_at.isoformat() + "Z"} for e in events])


@bp.get("/api/v1/deliveries")
@admin_only
def list_deliveries():
    limit = min(max(request.args.get("limit", 30, type=int), 1), 100)
    stmt = select(Delivery)
    status = request.args.get("status")
    if status:
        if status not in {"pending", "queued", "sending", "retry", "succeeded", "dead"}:
            return error("Invalid status filter")
        stmt = stmt.where(Delivery.status == status)
    rows = db.session.execute(stmt.order_by(Delivery.created_at.desc(), Delivery.id).limit(limit)).scalars()
    return jsonify(items=[delivery_dict(d) for d in rows])


@bp.get("/api/v1/deliveries/<string:delivery_id>/attempts")
@admin_only
def list_attempts(delivery_id):
    if db.session.get(Delivery, delivery_id) is None:
        return error("Unknown delivery", 404)
    rows = db.session.execute(select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == delivery_id)
                              .order_by(DeliveryAttempt.number)).scalars()
    return jsonify(items=[{"number": a.number, "status_code": a.status_code,
                           "error": a.error, "duration_ms": a.duration_ms,
                           "created_at": a.created_at.isoformat() + "Z"} for a in rows])


@bp.post("/api/v1/deliveries/<string:delivery_id>/replay")
@admin_only
def replay_delivery(delivery_id):
    original = db.session.get(Delivery, delivery_id)
    if original is None:
        return error("Unknown delivery", 404)
    if original.status not in {"dead", "succeeded"}:
        return error("Only terminal deliveries can be replayed", 409)
    if not original.destination.active:
        return error("Destination is disabled", 409)
    replay = Delivery(event_id=original.event_id, destination_id=original.destination_id,
                      replay_of_id=original.id, max_attempts=current_app.config["MAX_DELIVERY_ATTEMPTS"])
    db.session.add(replay)
    db.session.commit()
    return jsonify(delivery=delivery_dict(replay)), 202


@bp.get("/api/v1/stats")
@admin_only
def stats():
    counts = db.session.execute(select(Delivery.status, func.count(Delivery.id)).group_by(Delivery.status)).all()
    return jsonify(deliveries={status: count for status, count in counts})
