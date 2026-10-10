"""Executable, Docker-free reliability demonstration using a real HTTP receiver.

Run: python -m hookrelay.reliability_demo

Uses a temporary SQLite database and synchronous calls to the *same* dispatcher
and delivery functions used by the Celery worker. It does not simulate Redis,
PostgreSQL locking, or process crashes; those need separate integration tests.
"""
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from sqlalchemy import select

from . import create_app
from .delivery import dispatch_due, perform_delivery
from .extensions import db
from .models import Delivery, DeliveryAttempt, Event, utcnow
from .security import sign, verify


class ScriptedReceiver(ThreadingHTTPServer):
    """A local HTTP consumer with deterministic error responses per event."""

    def __init__(self):
        self.signing_secret = None
        self.responses = {
            "demo-retry": [503, 503, 204],
            "demo-dead": [503, 503, 503, 204],
            "demo-crash": [204],
        }
        self.received = []
        super().__init__(("127.0.0.1", 0), ReceiverHandler)


class ReceiverHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        receiver = self.server
        body = self.rfile.read(int(self.headers["Content-Length"]))
        valid = receiver.signing_secret is not None and verify(
            receiver.signing_secret, body,
            self.headers.get("X-HookRelay-Signature"), 300,
        )
        event_id = self.headers.get("X-HookRelay-Event-Id")
        planned = receiver.responses.get(event_id, [])
        status = planned.pop(0) if valid and planned else (204 if valid else 401)
        receiver.received.append((event_id, self.headers.get("X-HookRelay-Delivery-Id"), status, valid))
        self.send_response(status)
        self.end_headers()

    def log_message(self, *_args):
        pass


def run_demo(output=print):
    """Run assertions and print the observed states; raise on any failure."""
    with TemporaryDirectory(prefix="hookrelay-reliability-") as folder:
        database = Path(folder, "demo.sqlite").as_posix()
        app = create_app({
            "TESTING": True,
            "ADMIN_API_KEY": "local-demo-only",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database}",
            "ALLOW_PRIVATE_DESTINATIONS": True,
            "MAX_DELIVERY_ATTEMPTS": 3,
            "RETRY_BASE_SECONDS": 1,
        })
        receiver = ScriptedReceiver()
        thread = Thread(target=receiver.serve_forever, daemon=True)
        thread.start()
        try:
            with app.app_context():
                db.create_all()  # Ephemeral demo DB; the production schema uses Alembic.
            client = app.test_client()
            auth = {"Authorization": "Bearer local-demo-only"}
            endpoint_response = client.post("/api/v1/endpoints", json={"name": "Demo billing events"},
                                            headers=auth)
            assert endpoint_response.status_code == 201
            endpoint = endpoint_response.json
            dest_response = client.post("/api/v1/destinations", json={
                "name": "Local receiver",
                "url": f"http://127.0.0.1:{receiver.server_port}/webhooks",
                "endpoint_id": endpoint["id"],
            }, headers=auth)
            assert dest_response.status_code == 201
            receiver.signing_secret = dest_response.json["signing_secret"]

            def ingest(event_id, body=b'{"type":"invoice.paid"}'):
                return client.post(f"/hooks/{endpoint['public_key']}", data=body,
                                   headers={"Content-Type": "application/json",
                                            "X-HookRelay-Event-Id": event_id,
                                            "X-HookRelay-Signature": sign(endpoint["signing_secret"], body)})

            def lookup_delivery(event_id):
                return db.session.execute(select(Delivery).join(Event).where(
                    Event.external_id == event_id)).scalar_one().id

            def next_attempt(delivery_id):
                with app.app_context():
                    row = db.session.get(Delivery, delivery_id)
                    if row.status == "retry":
                        # Advance the clock manually for a fast deterministic demo.
                        row.next_attempt_at = utcnow() - timedelta(seconds=1)
                        db.session.commit()
                    claims = []
                    assert dispatch_due(lambda *args: claims.append(args)) == 1
                    assert claims[0][0] == delivery_id
                    return perform_delivery(*claims[0])

            first = ingest("demo-retry")
            assert first.status_code == 202
            assert ingest("demo-retry").status_code == 200
            assert ingest("demo-retry", b'{"type":"other"}').status_code == 409
            output("PASS signed ingestion: 202 accepted, 200 deduplicated, 409 conflict")

            with app.app_context():
                retry_id = lookup_delivery("demo-retry")
            assert [next_attempt(retry_id) for _ in range(3)] == ["retry", "retry", "succeeded"]
            with app.app_context():
                codes = [a.status_code for a in db.session.execute(select(DeliveryAttempt).where(
                    DeliveryAttempt.delivery_id == retry_id).order_by(DeliveryAttempt.number)).scalars()]
                assert codes == [503, 503, 204]
            output("PASS real signed HTTP: 503 -> retry -> 503 -> retry -> 204 -> succeeded")

            assert ingest("demo-dead").status_code == 202
            with app.app_context():
                dead_id = lookup_delivery("demo-dead")
            assert [next_attempt(dead_id) for _ in range(3)] == ["retry", "retry", "dead"]
            replay_response = client.post(f"/api/v1/deliveries/{dead_id}/replay", headers=auth)
            assert replay_response.status_code == 202
            replay_id = replay_response.json["delivery"]["id"]
            assert next_attempt(replay_id) == "succeeded"
            with app.app_context():
                assert db.session.get(Delivery, dead_id).status == "dead"
                assert db.session.get(Delivery, replay_id).replay_of_id == dead_id
                assert db.session.get(Delivery, replay_id).attempt_count == 1
            output("PASS dead letter: three 503s -> dead; replay -> new delivery -> 204")

            assert ingest("demo-crash").status_code == 202
            with app.app_context():
                crash_id = lookup_delivery("demo-crash")
                old = []
                assert dispatch_due(lambda *args: old.append(args)) == 1
                row = db.session.get(Delivery, crash_id)
                row.lease_expires_at = utcnow() - timedelta(seconds=1)
                db.session.commit()
                new = []
                assert dispatch_due(lambda *args: new.append(args)) == 1
                assert new[0][1] != old[0][1]
                assert perform_delivery(*old[0]) == "stale"
                assert perform_delivery(*new[0]) == "succeeded"
                assert db.session.get(Delivery, crash_id).attempt_count == 1
            output("PASS expired lease: new worker token supersedes stale queued task")
            assert all(valid for _, _, _, valid in receiver.received)
            assert len(receiver.received) == 8
            output("PASS all 8 outbound requests carried valid HMAC signatures")
            output("\nLocal reliability demo completed. Redis/Celery broker and PostgreSQL concurrency are not exercised.")
        finally:
            receiver.shutdown()
            receiver.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    run_demo()
