"""Deterministic failure injection plus actual localhost HTTP delivery.

No broker or Docker required. PostgreSQL concurrency is checked separately in CI.
"""
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest
import requests

from hookrelay.delivery import backoff_seconds, dispatch_due, perform_delivery
from hookrelay.extensions import db
from hookrelay.models import Delivery, DeliveryAttempt, Event, utcnow
from hookrelay.security import sign, verify
from test_api import ingest


@pytest.fixture
def local_receiver():
    state = {"secret": None, "statuses": [], "received": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            authenticated = verify(state["secret"], body,
                                   self.headers.get("X-HookRelay-Signature"), 300)
            status = (state["statuses"].pop(0) if authenticated else 401)
            state["received"].append({"body": body, "status": status,
                                      "authenticated": authenticated,
                                      "event_id": self.headers.get("X-HookRelay-Event-Id"),
                                      "delivery_id": self.headers.get("X-HookRelay-Delivery-Id")})
            self.send_response(status)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/callback", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def make_live_delivery(client, admin_headers, local_receiver):
    url, state = local_receiver
    endpoint = client.post("/api/v1/endpoints", json={"name": "local test"},
                           headers=admin_headers).json
    destination = client.post("/api/v1/destinations",
                              json={"name": "HTTP sink", "url": url,
                                    "endpoint_id": endpoint["id"]},
                              headers=admin_headers).json
    state["secret"] = destination["signing_secret"]
    assert ingest(client, endpoint).status_code == 202
    return endpoint, destination


def send_next_due(delivery_id):
    row = db.session.get(Delivery, delivery_id)
    if row.status == "retry":
        # Move simulated time forward without sleeping or weakening prod backoff.
        row.next_attempt_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    tokens = []
    assert dispatch_due(lambda *args: tokens.append(args)) == 1
    assert tokens[0][0] == delivery_id
    return perform_delivery(*tokens[0])


def test_real_http_receiver_retries_and_verifies_signature(app, client, admin_headers, local_receiver):
    endpoint, destination = make_live_delivery(client, admin_headers, local_receiver)
    _, state = local_receiver
    state["statuses"] = [503, 503, 204]
    with app.app_context():
        delivery_id = db.session.query(Delivery).one().id
        assert send_next_due(delivery_id) == "retry"
        first = db.session.get(Delivery, delivery_id)
        assert first.next_attempt_at > utcnow()
        unexpected = []
        assert dispatch_due(lambda *args: unexpected.append(args)) == 0
        assert unexpected == []
        assert send_next_due(delivery_id) == "retry"
        assert send_next_due(delivery_id) == "succeeded"
        attempts = db.session.query(DeliveryAttempt).order_by(DeliveryAttempt.number).all()
        assert [(a.number, a.status_code) for a in attempts] == [(1, 503), (2, 503), (3, 204)]
        assert db.session.get(Delivery, delivery_id).delivered_at is not None
        assert dispatch_due(lambda *args: unexpected.append(args)) == 0
        assert unexpected == []

    assert len(state["received"]) == 3
    assert all(item["authenticated"] for item in state["received"])
    assert all(item["body"] == b'{"amount": 42}' for item in state["received"])
    assert all(item["event_id"] == "evt-1" for item in state["received"])
    assert {item["delivery_id"] for item in state["received"]} == {delivery_id}
    assert destination["url"].startswith("http://127.0.0.1:")


def test_dead_letter_then_replay_has_independent_attempts(app, client, admin_headers, local_receiver):
    endpoint, _ = make_live_delivery(client, admin_headers, local_receiver)
    _, state = local_receiver
    state["statuses"] = [503, 503, 503, 204]
    with app.app_context():
        original_id = db.session.query(Delivery).one().id
        assert [send_next_due(original_id) for _ in range(3)] == ["retry", "retry", "dead"]
        unexpected = []
        assert dispatch_due(lambda *args: unexpected.append(args)) == 0
        assert unexpected == []
    response = client.post(f"/api/v1/deliveries/{original_id}/replay", headers=admin_headers)
    assert response.status_code == 202
    replay_id = response.json["delivery"]["id"]
    with app.app_context():
        assert send_next_due(replay_id) == "succeeded"
        original = db.session.get(Delivery, original_id)
        replay = db.session.get(Delivery, replay_id)
        assert original.status == "dead" and original.attempt_count == 3
        assert replay.status == "succeeded" and replay.attempt_count == 1
        assert replay.event_id == original.event_id and replay.replay_of_id == original_id
        assert db.session.query(Event).count() == 1
        assert db.session.query(DeliveryAttempt).filter_by(delivery_id=original_id).count() == 3
        assert db.session.query(DeliveryAttempt).filter_by(delivery_id=replay_id).count() == 1
    assert [item["delivery_id"] for item in state["received"]] == [original_id] * 3 + [replay_id]


def test_stale_worker_cannot_overwrite_successful_new_owner(app, client, configured):
    endpoint, _ = configured
    ingest(client, endpoint)
    with app.app_context():
        old = []
        assert dispatch_due(lambda *args: old.append(args)) == 1
        received = []

        def slow_sender(*_args, **_kwargs):
            row = db.session.get(Delivery, old[0][0])
            row.lease_expires_at = utcnow() - timedelta(seconds=1)
            db.session.commit()
            new = []
            assert dispatch_due(lambda *args: new.append(args)) == 1
            assert new[0][1] != old[0][1]
            assert perform_delivery(*new[0], sender=lambda *_a, **_k: SimpleNamespace(status_code=204)) == "succeeded"
            received.append(new[0])
            return SimpleNamespace(status_code=503)

        assert perform_delivery(*old[0], sender=slow_sender) == "stale"
        assert len(received) == 1
        final = db.session.get(Delivery, old[0][0])
        assert final.status == "succeeded" and final.attempt_count == 1
        assert db.session.query(DeliveryAttempt).count() == 1
        # Two outgoing HTTP calls can still happen; exactly-once is NOT guaranteed.


def test_network_timeout_is_recorded_and_retried(app, client, configured):
    endpoint, _ = configured
    ingest(client, endpoint)
    with app.app_context():
        tasks = []
        dispatch_due(lambda *args: tasks.append(args))

        def timed_out(*_args, **_kwargs):
            raise requests.Timeout("simulated read timeout")

        assert perform_delivery(*tasks[0], sender=timed_out) == "retry"
        row = db.session.get(Delivery, tasks[0][0])
        assert row.attempt_count == 1 and "simulated read timeout" in row.last_error
        attempt = db.session.query(DeliveryAttempt).one()
        assert attempt.status_code is None and "simulated read timeout" in attempt.error


def test_retry_backoff_is_exponential_and_capped(app):
    with app.app_context():
        app.config.update(RETRY_BASE_SECONDS=4, RETRY_CAP_SECONDS=20)
        assert [backoff_seconds(i) for i in range(1, 7)] == [4, 8, 16, 20, 20, 20]


def test_broker_failure_only_resets_failed_claim(app, client, configured):
    endpoint, _ = configured
    for index in range(3):
        assert ingest(client, endpoint, external_id=f"outage-{index}").status_code == 202
    with app.app_context():
        sent, failed = [], []

        def sometimes_fail(delivery_id, token):
            if not failed:
                failed.append(delivery_id)
                raise ConnectionError("simulated broker outage")
            sent.append((delivery_id, token))

        assert dispatch_due(sometimes_fail) == 2
        assert db.session.get(Delivery, failed[0]).status == "retry"
        assert all(db.session.get(Delivery, delivery_id).status == "queued" for delivery_id, _ in sent)
        recovered = []
        assert dispatch_due(lambda *args: recovered.append(args)) == 1
        assert recovered[0][0] == failed[0]
        assert recovered[0][0] not in {delivery_id for delivery_id, _ in sent}
