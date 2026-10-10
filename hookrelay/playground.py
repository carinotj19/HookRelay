"""Explicitly enabled, loopback-only HTTP webhook playground.

This is a manual *demonstration* dispatcher, not a substitute for Celery/Beat.
It uses the real Flask ingestion API, real local HTTP callbacks, durable
Delivery/DeliveryAttempt records, and real HMAC signatures.
"""
import hmac
import ipaddress
import json
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

import requests
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from .dashboard import _csrf, protected
from .delivery import dispatch_due, perform_delivery
from .extensions import db
from .models import Delivery, Destination, Endpoint, utcnow
from .security import generate_secret, sign, verify

bp = Blueprint("playground", __name__, url_prefix="/dashboard/playground")

SCENARIOS = {
    "success": ("Immediate success", 0, "Callback returns HTTP 204 on the first attempt."),
    "recover": ("Transient failures", 2, "Callback returns two HTTP 503s, then HTTP 204."),
    "dead": ("Dead-letter recovery", 1000, "Callback keeps returning HTTP 503 until attempts are exhausted."),
}
PREFIX = "HookRelay Playground / "


def _local_request():
    """Enforce both remote address and Host to mitigate exposed dev servers."""
    try:
        remote = ipaddress.ip_address(request.remote_addr or "")
        host = urlsplit(request.host_url).hostname or ""
        loopback_host = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
    return remote.is_loopback and loopback_host


@bp.before_request
def local_only():
    if not current_app.config.get("LOCAL_PLAYGROUND_ENABLED") or not _local_request():
        abort(404)


def _sources():
    rows = db.session.execute(select(Endpoint).where(Endpoint.public_key.like("play-%")))\
        .scalars().all()
    return {slug: next((row for row in rows if row.name == PREFIX + slug), None)
            for slug in SCENARIOS}


def _destinations(sources):
    result = {}
    for slug, source in sources.items():
        result[slug] = (db.session.execute(select(Destination).where(
            Destination.endpoint_id == source.id).order_by(Destination.created_at)).scalars().first()
            if source else None)
    return result


def _demo_ids():
    return db.session.execute(
        select(Delivery.id).join(Delivery.destination).join(Destination.endpoint)
        .where(Endpoint.public_key.like("play-%"),
               Endpoint.name.in_([PREFIX + slug for slug in SCENARIOS]))
    ).scalars().all()


@bp.get("")
@bp.get("/")
@protected
def index():
    sources = _sources()
    dests = _destinations(sources)
    records = db.session.execute(
        select(Delivery).join(Delivery.destination).join(Destination.endpoint)
        .where(Endpoint.public_key.like("play-%"),
               Endpoint.name.in_([PREFIX + slug for slug in SCENARIOS]))
        .options(joinedload(Delivery.event), joinedload(Delivery.destination))
        .order_by(Delivery.created_at.desc(), Delivery.id.desc()).limit(16)
    ).scalars().all()
    labels = {dest.id: SCENARIOS[slug][0] for slug, dest in dests.items() if dest}
    return render_template("dashboard/playground.html", active="playground", scenarios=SCENARIOS,
                           sources=sources, targets=dests, deliveries=records, labels=labels)


@bp.post("/setup")
@protected
def setup():
    _csrf()
    sources = _sources()
    targets = _destinations(sources)
    # Only localhost can reach this handler; port comes from the checked Host.
    root = request.host_url.rstrip("/")
    for slug in SCENARIOS:
        source = sources[slug]
        if source is None:
            source = Endpoint(name=PREFIX + slug, public_key="play-" + secrets.token_urlsafe(18),
                              signing_secret=generate_secret())
            db.session.add(source)
            db.session.flush()
        target = targets[slug]
        if target is None:
            target = Destination(endpoint_id=source.id, name=SCENARIOS[slug][0],
                                 url="", signing_secret=generate_secret())
            db.session.add(target)
            db.session.flush()
        target.url = f"{root}/dashboard/playground/receiver/{slug}/{target.id}"
    db.session.commit()
    flash("Three local webhook scenarios are ready.")
    return redirect(url_for("playground.index"), code=303)


@bp.post("/send/<slug>")
@protected
def send(slug):
    _csrf()
    if slug not in SCENARIOS:
        abort(404)
    source = _sources()[slug]
    if source is None:
        abort(409, description="Set up the playground before generating events")
    public_key, secret = source.public_key, source.signing_secret
    # Release SQLite's read transaction before calling back into Flask.
    db.session.remove()
    event_id = "demo-" + slug + "-" + secrets.token_hex(5)
    raw = json.dumps({"type": "playground.test", "scenario": slug, "event_id": event_id},
                     separators=(",", ":")).encode()
    endpoint = request.host_url.rstrip("/") + "/hooks/" + public_key
    try:
        response = requests.post(endpoint, data=raw, headers={
            "Content-Type": "application/json", "X-HookRelay-Event-Id": event_id,
            "X-HookRelay-Signature": sign(secret, raw),
        }, timeout=5, allow_redirects=False, proxies={"http": "", "https": ""})
    except requests.RequestException as exc:
        flash("Could not reach the local Flask ingestion endpoint: " + str(exc)[:140])
    else:
        flash(f"Signed event {event_id}: ingestion HTTP {response.status_code}.")
    return redirect(url_for("playground.index"), code=303)


@bp.post("/step")
@protected
def step():
    _csrf()
    ids = _demo_ids()
    if not ids:
        flash("Generate a demo event first.")
        return redirect(url_for("playground.index"), code=303)
    # Explicit *demo-only* simulated clock advancement. The core backoff
    # algorithm remains unchanged; only its waiting period is accelerated.
    retries = db.session.execute(
        select(Delivery).where(Delivery.id.in_(ids), Delivery.status == "retry")
    ).scalars().all()
    for row in retries:
        row.next_attempt_at = utcnow() - timedelta(seconds=1)
    db.session.commit()

    claims = []
    dispatch_due(lambda delivery_id, token: claims.append((delivery_id, token)),
                 only_delivery_ids=ids)
    if not claims:
        flash("No due playground deliveries. Generate an event or replay a dead letter.")
    else:
        counts = {}
        for delivery_id, token in claims:
            # This bypass is limited to destinations seeded by this local-only
            # feature; it does NOT change global destination URL policy.
            outcome = perform_delivery(delivery_id, token, allow_private_destination=True)
            counts[outcome] = counts.get(outcome, 0) + 1
        summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
        flash("Manually processed " + str(len(claims)) + " demo delivery(s): " + summary + ".")
    return redirect(url_for("playground.index"), code=303)


@bp.post("/receiver/<slug>/<destination_id>")
def receiver(slug, destination_id):
    """Actual loopback callback target; requires the outbound HMAC signature."""
    if slug not in SCENARIOS:
        abort(404)
    target = db.session.get(Destination, destination_id)
    if target is None or target.endpoint.name != PREFIX + slug or not target.endpoint.public_key.startswith("play-"):
        abort(404)
    delivery_id = request.headers.get("X-HookRelay-Delivery-Id", "")
    delivery = db.session.get(Delivery, delivery_id)
    raw = request.get_data(cache=False)
    if (delivery is None or delivery.destination_id != target.id or delivery.status != "sending"
            or not hmac.compare_digest(raw, delivery.event.payload)
            or not verify(target.signing_secret, raw, request.headers.get("X-HookRelay-Signature"),
                          current_app.config["SIGNATURE_TOLERANCE_SECONDS"])):
        abort(401)
    # A replay in the dead-letter scenario represents an operator fixing the
    # remote service before replaying. The original HTTP 503 history stays.
    if slug == "dead" and delivery.replay_of_id:
        return "", 204
    # attempt_count reflects the number of attempts *before* the current one.
    return ("Simulated temporary failure", 503) if delivery.attempt_count < SCENARIOS[slug][1] else ("", 204)
