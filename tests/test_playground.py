"""Local-only playground contracts and an actual threaded localhost HTTP smoke test."""
from threading import Thread

import pytest
from werkzeug.serving import make_server
from sqlalchemy import select

from hookrelay.delivery import dispatch_due
from hookrelay.extensions import db
from hookrelay.models import Delivery, DeliveryAttempt, Destination, Endpoint
from hookrelay.security import sign

TEST_SECRET = "this-is-a-long-unique-test-session-secret-for-flask-playground"


def _admin(app, client, base=None):
    app.secret_key = TEST_SECRET
    response = client.post("/dashboard/login", data={"admin_key": "local-test-key"},
                           base_url=base or "http://localhost")
    assert response.status_code == 303
    with client.session_transaction() as sess:
        return sess["dashboard_csrf"]


def _post(client, path, csrf, base=None):
    return client.post(path, data={"csrf_token": csrf}, base_url=base or "http://localhost")


def test_playground_disabled_by_default(app, client):
    app.secret_key = TEST_SECRET
    assert client.get("/dashboard/playground").status_code == 404
    assert client.post("/dashboard/playground/step").status_code == 404
    assert client.post("/dashboard/playground/receiver/dead/missing").status_code == 404
    assert b"Local playground" not in client.get("/dashboard/login").data


def test_loopback_only_and_setup_requires_login_csrf(app, client):
    app.config["LOCAL_PLAYGROUND_ENABLED"] = True
    app.secret_key = TEST_SECRET
    assert client.get("/dashboard/playground").status_code == 302
    assert client.get("/dashboard/playground", base_url="http://outside.example").status_code == 404
    assert client.get("/dashboard/playground", environ_overrides={"REMOTE_ADDR": "192.0.2.5"}).status_code == 404
    csrf = _admin(app, client)
    assert client.post("/dashboard/playground/setup").status_code == 400
    assert client.post("/dashboard/playground/setup", data={"csrf_token": "invalid"}).status_code == 400
    assert _post(client, "/dashboard/playground/setup", csrf).status_code == 303
    assert _post(client, "/dashboard/playground/setup", csrf).status_code == 303
    with app.app_context():
        assert db.session.query(Endpoint).count() == 3
        assert db.session.query(Destination).count() == 3
        for dest in db.session.query(Destination).all():
            assert dest.url.startswith("http://localhost/dashboard/playground/receiver/")
    page = client.get("/dashboard/playground")
    assert page.status_code == 200
    assert b"Advance one step" in page.data
    assert b"Signed" in page.data or b"signed" in page.data
    assert client.post("/dashboard/playground/send/unknown", data={"csrf_token": csrf}).status_code == 404
    assert client.post("/dashboard/playground/receiver/success/nope").status_code == 404
    assert b"Local playground" in client.get("/dashboard/").data


def test_scoped_dispatch_does_not_claim_regular_deliveries(app, client, configured):
    endpoint, _ = configured
    from test_api import ingest
    assert ingest(client, endpoint).status_code == 202
    with app.app_context():
        publish = []
        assert dispatch_due(lambda *args: publish.append(args), only_delivery_ids=[]) == 0
        assert not publish
        assert db.session.query(Delivery).one().status == "pending"


def test_real_local_webhook_playground_end_to_end(app):
    """Real HTTP ingress and callbacks, no Redis/Celery and no mocked sender."""
    app.config["LOCAL_PLAYGROUND_ENABLED"] = True
    app.config["ALLOW_PRIVATE_DESTINATIONS"] = False  # demonstrate scoped override
    app.config["MAX_DELIVERY_ATTEMPTS"] = 5
    # Werkzeug's threaded server allows an HTTP callback into the same Flask process.
    server = make_server("127.0.0.1", 0, app, threaded=True)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    client = app.test_client()
    try:
        csrf = _admin(app, client, base)
        assert _post(client, "/dashboard/playground/setup", csrf, base).status_code == 303
        with app.app_context():
            source = db.session.execute(select(Endpoint).where(Endpoint.name ==
                                    "HookRelay Playground / success")).scalar_one()
            dest = db.session.execute(select(Destination).where(Destination.endpoint_id == source.id)).scalar_one()
            receiver_url = f"/dashboard/playground/receiver/success/{dest.id}"
        # Outbound receiver is not a public webhook API; HMAC is mandatory.
        invalid = client.post(receiver_url, data=b"{}", base_url=base)
        assert invalid.status_code == 401
        for slug in ("success", "recover", "dead"):
            response = _post(client, "/dashboard/playground/send/" + slug, csrf, base)
            assert response.status_code == 303
        with app.app_context():
            assert db.session.query(Delivery).count() == 3
            assert all(d.status == "pending" for d in db.session.query(Delivery).all())
        # Processing is deliberate, not background; each click = one attempt.
        for _ in range(3):
            response = _post(client, "/dashboard/playground/step", csrf, base)
            assert response.status_code == 303
        with app.app_context():
            statuses = {d.destination.endpoint.name.removeprefix("HookRelay Playground / "): d.status
                        for d in db.session.query(Delivery).all()}
            assert statuses == {"success": "succeeded", "recover": "succeeded", "dead": "retry"}
            counts = {d.destination.endpoint.name.removeprefix("HookRelay Playground / "): d.attempt_count
                      for d in db.session.query(Delivery).all()}
            assert counts == {"success": 1, "recover": 3, "dead": 3}
        for _ in range(2):
            assert _post(client, "/dashboard/playground/step", csrf, base).status_code == 303
        with app.app_context():
            dead = db.session.execute(select(Delivery).join(Delivery.destination).join(Destination.endpoint)
                .where(Endpoint.name == "HookRelay Playground / dead")).scalar_one()
            assert dead.status == "dead" and dead.attempt_count == 5
            dead_id = dead.id
            assert [a.status_code for a in db.session.execute(select(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_id == dead_id).order_by(DeliveryAttempt.number)).scalars()] == [503] * 5
        # Dashboard replay records a separate, auditable pending delivery.
        replay = _post(client, f"/dashboard/deliveries/{dead_id}/replay", csrf, base)
        assert replay.status_code == 303
        assert _post(client, "/dashboard/playground/step", csrf, base).status_code == 303
        with app.app_context():
            clone = db.session.execute(select(Delivery).where(Delivery.replay_of_id == dead_id)).scalar_one()
            assert clone.status == "succeeded" and clone.attempt_count == 1
            codes = db.session.execute(select(DeliveryAttempt.status_code)
                .where(DeliveryAttempt.delivery_id == clone.id)).scalars().all()
            assert codes == [204]
        html = client.get("/dashboard/playground", base_url=base).data
        assert b"demo-dead-" in html and b"Inspect / replay" in html
        assert b"HTTP 503" in html
        assert client.get("/dashboard", base_url=base).status_code == 200  # current overview
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
