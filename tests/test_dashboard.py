"""Dashboard contract: access control, HTML operations, secrets, CSRF and audit trail."""
import re
from datetime import timedelta

from hookrelay.extensions import db
from hookrelay.models import Delivery, DeliveryAttempt, utcnow
from test_api import ingest

TEST_SECRET = "this-is-a-long-unique-test-session-secret-for-flask-dashboard"


def _login(app, client):
    app.secret_key = TEST_SECRET
    res = client.post("/dashboard/login", data={"admin_key": "local-test-key"})
    assert res.status_code == 303
    with client.session_transaction() as sess:
        return sess["dashboard_csrf"]


def test_console_fails_closed_without_session_secret(app, client):
    app.secret_key = None
    res = client.get("/dashboard")
    assert res.status_code == 503
    assert b"DASHBOARD_SESSION_SECRET" in res.data
    # Existing JSON API is unaffected by dashboard being disabled.
    assert client.get("/health/live").status_code == 200


def test_dashboard_requires_admin_and_does_not_grant_api_access(app, client):
    app.secret_key = TEST_SECRET
    assert client.get("/dashboard").status_code == 302
    assert "/dashboard/login" in client.get("/dashboard").headers["Location"]
    assert client.get("/dashboard/deliveries").status_code == 302
    assert client.post("/dashboard/login", data={"admin_key": "bad-key"}).status_code == 401
    csrf = _login(app, client)
    result = client.get("/dashboard")
    assert result.status_code == 200
    assert b"Delivery overview" in result.data
    assert result.headers["Cache-Control"] == "no-store, private"
    assert result.headers["X-Frame-Options"] == "DENY"
    assert b"'self'" in result.headers["Content-Security-Policy"].encode()
    # A browser cookie must not authorize the JSON API, which still requires bearer auth.
    assert client.get("/api/v1/endpoints").status_code == 401
    assert client.post("/dashboard/logout", data={}).status_code == 400
    assert client.post("/dashboard/logout", data={"csrf_token": csrf}).status_code == 303
    assert client.get("/dashboard").status_code == 302


def test_changed_admin_key_invalidates_session(app, client):
    _login(app, client)
    app.config["ADMIN_API_KEY"] = "a-new-administrator-key"
    assert client.get("/dashboard").status_code == 302


def test_endpoint_creation_shows_secret_once_and_validates_forms(app, client):
    csrf = _login(app, client)
    assert client.post("/dashboard/endpoints", data={"name": "finance"}).status_code == 400
    assert client.post("/dashboard/endpoints", data={"csrf_token": csrf, "name": " "}).status_code == 400
    response = client.post("/dashboard/endpoints", data={"csrf_token": csrf, "name": "<demo>"})
    assert response.status_code == 201
    assert b"SIGNING SECRET" in response.data
    assert b"&lt;demo&gt;" in response.data  # escaped / no XSS
    assert response.headers["Cache-Control"] == "no-store, private"
    with app.app_context():
        from hookrelay.models import Endpoint
        row = db.session.query(Endpoint).one()
        secret = row.signing_secret.encode()
        assert secret in response.data
        assert secret not in client.get("/dashboard/endpoints").data
        assert secret not in client.get("/dashboard").data


def test_destination_creation_validates_endpoint_url_and_one_time_secret(app, client):
    csrf = _login(app, client)
    assert client.post("/dashboard/destinations", data={"csrf_token": csrf, "name": "x",
                       "endpoint_id": "missing", "url": "https://example.net/x"}).status_code == 400
    from hookrelay.models import Endpoint
    with app.app_context():
        row = Endpoint(name="billing", public_key="billing-key", signing_secret="secret")
        db.session.add(row)
        db.session.commit()
        source_id = row.id
    assert client.post("/dashboard/destinations", data={"csrf_token": csrf, "name": "receiver",
                       "endpoint_id": source_id, "url": "ftp://example.org/"}).status_code == 400
    res = client.post("/dashboard/destinations", data={"csrf_token": csrf, "name": "receiver",
                      "endpoint_id": source_id, "url": "https://receiver.example.org/hooks"})
    assert res.status_code == 201
    with app.app_context():
        from hookrelay.models import Destination
        dest = db.session.query(Destination).one()
        assert dest.signing_secret.encode() in res.data
        assert dest.signing_secret.encode() not in client.get("/dashboard/endpoints").data


def test_delivery_explorer_filters_paginates_and_shows_attempts(app, client, configured):
    csrf = _login(app, client)
    endpoint, _ = configured
    for i in range(26):
        assert ingest(client, endpoint, external_id=f"evt-{i:03d}").status_code == 202
    with app.app_context():
        records = db.session.query(Delivery).order_by(Delivery.created_at, Delivery.id).all()
        target = records[0]
        target.status = "retry"
        target.attempt_count = 1
        target.next_attempt_at = utcnow() + timedelta(minutes=1)
        db.session.add(DeliveryAttempt(delivery_id=target.id, number=1, status_code=503,
                                       error="HTTP 503", duration_ms=137))
        db.session.commit()
        target_id = target.id
    overview = client.get("/dashboard")
    assert b"Delivery overview" in overview.data
    assert b"26" in overview.data
    first = client.get("/dashboard/deliveries")
    assert first.status_code == 200
    assert b"Page 1 of 2" in first.data
    assert b"Page 2" not in first.data
    second = client.get("/dashboard/deliveries?page=2")
    assert b"Page 2 of 2" in second.data
    filtered = client.get("/dashboard/deliveries?status=retry")
    assert b"evt-" in filtered.data
    assert b"1 matching records" in filtered.data
    assert client.get("/dashboard/deliveries?status=invalid").status_code == 400
    search = client.get("/dashboard/deliveries?search=evt-025")
    assert b"evt-025" in search.data
    assert b"1 matching records" in search.data
    missing = client.get("/dashboard/deliveries?search=%25")
    assert b"0 matching records" in missing.data  # percent is a literal, not wildcard
    detail = client.get(f"/dashboard/deliveries/{target_id}")
    assert detail.status_code == 200
    assert b"HTTP 503" in detail.data and b"137 ms" in detail.data
    # Retry is not terminal; cannot replay it, even with a valid CSRF token.
    assert client.post(f"/dashboard/deliveries/{target_id}/replay",
                       data={"csrf_token": csrf}).status_code == 409


def test_replay_requires_csrf_and_creates_auditable_delivery(app, client, configured):
    csrf = _login(app, client)
    endpoint, _ = configured
    assert ingest(client, endpoint).status_code == 202
    with app.app_context():
        delivery = db.session.query(Delivery).one()
        delivery.status = "dead"
        delivery.next_attempt_at = None
        db.session.commit()
        original = delivery.id
    uri = f"/dashboard/deliveries/{original}/replay"
    assert client.post(uri).status_code == 400
    response = client.post(uri, data={"csrf_token": csrf})
    assert response.status_code == 303
    assert "/dashboard/deliveries/" in response.headers["Location"]
    with app.app_context():
        rows = db.session.query(Delivery).all()
        assert len(rows) == 2
        clone = next(r for r in rows if r.id != original)
        assert clone.replay_of_id == original
        assert clone.status == "pending"
        assert clone.attempt_count == 0
    assert b"Replayed from" in client.get(response.headers["Location"]).data


def test_disabled_destination_cannot_be_replayed(app, client, configured):
    csrf = _login(app, client)
    endpoint, _ = configured
    ingest(client, endpoint)
    with app.app_context():
        row = db.session.query(Delivery).one()
        row.status = "dead"
        row.destination.active = False
        db.session.commit()
        delivery_id = row.id
    response = client.post(f"/dashboard/deliveries/{delivery_id}/replay", data={"csrf_token": csrf})
    assert response.status_code == 409
