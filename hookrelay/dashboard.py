"""Minimal server-rendered operations console for single-admin installations.

The dashboard uses signed, expiring Flask sessions. The API remains bearer-token
based, and the session grants *no* permissions to the JSON API.
"""
import hashlib
import hmac
import secrets
from functools import wraps

from flask import (
    Blueprint, abort, current_app, redirect, render_template, request,
    session, url_for,
)
from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload

from .extensions import db
from .models import Delivery, DeliveryAttempt, Destination, Endpoint, Event
from .security import generate_secret, validate_destination_url

bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")
STATUSES = ("pending", "queued", "sending", "retry", "succeeded", "dead")
PAGE_SIZE = 20


def _fingerprint():
    """Invalidate existing sessions if the admin key changes."""
    key = current_app.config.get("ADMIN_API_KEY") or ""
    return hmac.new(current_app.secret_key.encode(), key.encode(), hashlib.sha256).hexdigest()


def _signed_in():
    expected = session.get("dashboard_fingerprint", "")
    return isinstance(expected, str) and bool(expected) and hmac.compare_digest(expected, _fingerprint())


def _csrf():
    supplied = request.form.get("csrf_token", "")
    expected = session.get("dashboard_csrf", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        abort(400, description="Invalid or missing CSRF token")


def protected(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not _signed_in():
            return redirect(url_for("dashboard.login"))
        return fn(*args, **kwargs)
    return wrapped


@bp.before_request
def ensure_session_configuration():
    # Fail closed: without an explicit secret, an ephemeral worker-specific key
    # would invalidate sessions across processes or after restarts.
    secret = current_app.secret_key
    if not isinstance(secret, str) or len(secret) < 32:
        return "Dashboard unavailable: set DASHBOARD_SESSION_SECRET (32+ characters)", 503


@bp.after_request
def secure_dashboard_response(response):
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'self'; img-src 'self' data:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if _signed_in():
            return redirect(url_for("dashboard.overview"))
        return render_template("dashboard/login.html", error=None)
    supplied = request.form.get("admin_key", "")
    expected = current_app.config.get("ADMIN_API_KEY") or ""
    if not expected or not secrets.compare_digest(supplied, expected):
        return render_template("dashboard/login.html", error="Invalid admin key."), 401
    session.clear()
    session["dashboard_fingerprint"] = _fingerprint()
    session["dashboard_csrf"] = secrets.token_urlsafe(32)
    session.permanent = True
    return redirect(url_for("dashboard.overview"), code=303)


@bp.post("/logout")
@protected
def logout():
    _csrf()
    session.clear()
    return redirect(url_for("dashboard.login"), code=303)


@bp.get("")
@bp.get("/")
@protected
def overview():
    counts = dict(db.session.execute(
        select(Delivery.status, func.count(Delivery.id)).group_by(Delivery.status)
    ).all())
    summary = {status: counts.get(status, 0) for status in STATUSES}
    recent = db.session.execute(
        select(Delivery).options(joinedload(Delivery.event), joinedload(Delivery.destination))
        .order_by(Delivery.created_at.desc(), Delivery.id.desc()).limit(8)
    ).scalars().all()
    total = sum(summary.values())
    return render_template("dashboard/overview.html", active="overview", summary=summary,
                           total=total, recent=recent,
                           endpoints=db.session.scalar(select(func.count(Endpoint.id))),
                           destinations=db.session.scalar(select(func.count(Destination.id))))


@bp.get("/deliveries")
@protected
def deliveries():
    status = request.args.get("status", "").strip()
    if status and status not in STATUSES:
        abort(400, description="Unknown delivery status")
    search = request.args.get("search", "").strip()[:100]
    page = request.args.get("page", default=1, type=int)
    page = max(1, min(page or 1, 100000))
    stmt = select(Delivery).join(Delivery.event).join(Delivery.destination)
    if status:
        stmt = stmt.where(Delivery.status == status)
    if search:
        # Treat user-entered % and _ as literals instead of SQL LIKE wildcards.
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        condition = or_(Event.external_id.ilike(f"%{escaped}%", escape="\\"),
                        Destination.name.ilike(f"%{escaped}%", escape="\\"))
        stmt = stmt.where(condition)
    count = db.session.scalar(select(func.count()).select_from(stmt.subquery()))
    last_page = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, last_page)
    rows = db.session.execute(
        stmt.options(joinedload(Delivery.event), joinedload(Delivery.destination))
        .order_by(Delivery.created_at.desc(), Delivery.id.desc())
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    ).scalars().all()
    return render_template("dashboard/deliveries.html", active="deliveries",
                           rows=rows, status=status, search=search, page=page,
                           pages=last_page, count=count, statuses=STATUSES)


@bp.get("/deliveries/<string:delivery_id>")
@protected
def delivery_detail(delivery_id):
    delivery = db.session.execute(
        select(Delivery).options(joinedload(Delivery.event), joinedload(Delivery.destination))
        .where(Delivery.id == delivery_id)
    ).scalar_one_or_none()
    if delivery is None:
        abort(404)
    attempts = db.session.execute(
        select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == delivery_id)
        .order_by(DeliveryAttempt.number.desc())
    ).scalars().all()
    return render_template("dashboard/detail.html", active="deliveries", delivery=delivery,
                           attempts=attempts, can_replay=delivery.status in ("dead", "succeeded")
                           and delivery.destination.active)


@bp.post("/deliveries/<string:delivery_id>/replay")
@protected
def replay(delivery_id):
    _csrf()
    # For concurrent operator requests, serialize against the original row on
    # PostgreSQL. Multiple *intentional* replays remain permissible/auditable.
    original = db.session.execute(
        select(Delivery).where(Delivery.id == delivery_id).with_for_update()
    ).scalar_one_or_none()
    if original is None:
        abort(404)
    if original.status not in ("dead", "succeeded") or not original.destination.active:
        db.session.rollback()
        abort(409, description="Only terminal deliveries to active destinations can be replayed")
    retry = Delivery(event_id=original.event_id, destination_id=original.destination_id,
                     replay_of_id=original.id, max_attempts=current_app.config["MAX_DELIVERY_ATTEMPTS"])
    db.session.add(retry)
    db.session.commit()
    return redirect(url_for("dashboard.delivery_detail", delivery_id=retry.id), code=303)


@bp.get("/endpoints")
@protected
def endpoints():
    sources = db.session.execute(select(Endpoint).order_by(Endpoint.created_at.desc())).scalars().all()
    targets = db.session.execute(
        select(Destination).options(joinedload(Destination.endpoint))
        .order_by(Destination.created_at.desc())
    ).scalars().all()
    return render_template("dashboard/endpoints.html", active="endpoints", sources=sources, targets=targets,
                           error=None)


@bp.post("/endpoints")
@protected
def create_endpoint():
    _csrf()
    name = request.form.get("name", "").strip()
    if not 1 <= len(name) <= 120:
        return _form_error("Endpoint name must contain 1–120 characters.")
    row = Endpoint(name=name, public_key=secrets.token_urlsafe(24), signing_secret=generate_secret())
    db.session.add(row)
    db.session.commit()
    return render_template("dashboard/created.html", active="endpoints", kind="Endpoint",
                           name=row.name, signing_secret=row.signing_secret,
                           identifier=row.public_key, identifier_label="Public key",
                           detail=f"/hooks/{row.public_key}"), 201


@bp.post("/destinations")
@protected
def create_destination():
    _csrf()
    name = request.form.get("name", "").strip()
    endpoint_id = request.form.get("endpoint_id", "")
    url = request.form.get("url", "").strip()
    if not 1 <= len(name) <= 120:
        return _form_error("Destination name must contain 1–120 characters.")
    endpoint = db.session.get(Endpoint, endpoint_id)
    if endpoint is None:
        return _form_error("Select an existing endpoint.")
    try:
        validate_destination_url(url, current_app.config["ALLOW_PRIVATE_DESTINATIONS"])
    except ValueError as exc:
        return _form_error(str(exc))
    row = Destination(endpoint_id=endpoint.id, name=name, url=url, signing_secret=generate_secret())
    db.session.add(row)
    db.session.commit()
    return render_template("dashboard/created.html", active="endpoints", kind="Destination",
                           name=row.name, signing_secret=row.signing_secret, identifier=row.id,
                           identifier_label="Destination ID", detail=row.url), 201


def _form_error(message):
    sources = db.session.execute(select(Endpoint).order_by(Endpoint.created_at.desc())).scalars().all()
    targets = db.session.execute(select(Destination).options(joinedload(Destination.endpoint))
                                 .order_by(Destination.created_at.desc())).scalars().all()
    return render_template("dashboard/endpoints.html", active="endpoints", sources=sources,
                           targets=targets, error=message), 400
