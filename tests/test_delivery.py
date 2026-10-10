from datetime import timedelta
from types import SimpleNamespace

from hookrelay.delivery import dispatch_due, perform_delivery
from hookrelay.extensions import db
from hookrelay.models import Delivery, DeliveryAttempt, utcnow
from hookrelay.security import sign
from test_api import ingest


def test_http_failures_retry_then_dead_letter(app, client, configured):
    endpoint, dest = configured
    ingest(client, endpoint)
    with app.app_context():
        delivery_id = db.session.query(Delivery).one().id
        seen = []
        for number in range(3):
            row = db.session.get(Delivery, delivery_id)
            if row.next_attempt_at:
                row.next_attempt_at = utcnow() - timedelta(seconds=1)
                db.session.commit()
            publish = lambda delivery_id, token: seen.append((delivery_id, token))
            assert dispatch_due(publish) == 1
            assert perform_delivery(*seen[-1], sender=lambda *a, **k: SimpleNamespace(status_code=503)) == (
                "dead" if number == 2 else "retry"
            )
        row = db.session.get(Delivery, delivery_id)
        assert row.status == "dead"
        assert row.attempt_count == 3
        assert [a.number for a in db.session.query(DeliveryAttempt).order_by(DeliveryAttempt.number)] == [1, 2, 3]
        assert dispatch_due(lambda *_: None) == 0


def test_success_signs_payload_and_stale_task_does_nothing(app, client, configured):
    endpoint, dest = configured
    ingest(client, endpoint)
    with app.app_context():
        tasks = []
        assert dispatch_due(lambda *args: tasks.append(args)) == 1
        assert perform_delivery(tasks[0][0], "invalid-token") == "stale"
        captured = {}

        def sender(url, **kwargs):
            captured.update(url=url, **kwargs)
            return SimpleNamespace(status_code=204)

        assert perform_delivery(*tasks[0], sender=sender) == "succeeded"
        assert captured["data"] == b'{"amount": 42}'
        assert captured["allow_redirects"] is False
        assert captured["headers"]["X-HookRelay-Signature"].startswith("t=")
        assert captured["url"] == dest["url"]
        assert db.session.get(Delivery, tasks[0][0]).status == "succeeded"
        assert perform_delivery(*tasks[0], sender=sender) == "stale"
        assert db.session.query(DeliveryAttempt).count() == 1


def test_broker_failure_resets_claim_for_recovery(app, client, configured):
    endpoint, _ = configured
    ingest(client, endpoint)
    with app.app_context():
        def fail(*args):
            raise ConnectionError("Broker unavailable")
        assert dispatch_due(fail) == 0
        row = db.session.query(Delivery).one()
        assert row.status == "retry"
        assert row.lease_token is None
        dispatched = []
        assert dispatch_due(lambda *args: dispatched.append(args)) == 1
        assert len(dispatched) == 1


def test_crashed_worker_lease_is_recovered(app, client, configured):
    endpoint, _ = configured
    ingest(client, endpoint)
    with app.app_context():
        old = []
        assert dispatch_due(lambda *args: old.append(args)) == 1
        row = db.session.query(Delivery).one()
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
        new = []
        assert dispatch_due(lambda *args: new.append(args)) == 1
        assert old[0][1] != new[0][1]
        assert perform_delivery(*old[0]) == "stale"
