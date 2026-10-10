"""Run with HOOKRELAY_TEST_POSTGRES_URL after `alembic upgrade head`."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from hookrelay import create_app
from hookrelay.delivery import dispatch_due, perform_delivery
from hookrelay.extensions import db
from hookrelay.models import Delivery, DeliveryAttempt, Destination, Endpoint, Event
from hookrelay.security import sign


@pytest.mark.skipif(not os.getenv("HOOKRELAY_TEST_POSTGRES_URL"), reason="PostgreSQL integration URL not set")
def test_migrated_schema_and_competing_dispatchers():
    app = create_app({
        "TESTING": True,
        "ADMIN_API_KEY": "postgres-integration-key",
        "SQLALCHEMY_DATABASE_URI": os.environ["HOOKRELAY_TEST_POSTGRES_URL"],
        "ALLOW_PRIVATE_DESTINATIONS": True,
        "DISPATCH_BATCH_SIZE": 1,
    })
    with app.app_context():
        # The schema must already exist from `alembic upgrade head`.
        from sqlalchemy import inspect
        assert {"endpoints", "destinations", "events", "deliveries", "delivery_attempts"} <= (
            set(inspect(db.engine).get_table_names())
        )
        source = Endpoint(name="integration", public_key="ci-integration", signing_secret="test-secret")
        db.session.add(source)
        db.session.flush()
        target = Destination(endpoint_id=source.id, name="receiver", url="http://receiver:1234/x",
                             signing_secret="outbound-secret")
        db.session.add(target)
        db.session.commit()

        client = app.test_client()
        raw = b'{"type":"billing.paid"}'
        headers = {"Content-Type": "application/json", "X-HookRelay-Event-Id": "ci-event-1",
                   "X-HookRelay-Signature": sign(source.signing_secret, raw)}
        assert client.post(f"/hooks/{source.public_key}", data=raw, headers=headers).status_code == 202
        assert client.post(f"/hooks/{source.public_key}", data=raw, headers=headers).status_code == 200
        delivery_id = db.session.query(Delivery).filter_by(destination_id=target.id).one().id

    barrier = Barrier(2)

    def concurrent_dispatch():
        with app.app_context():
            barrier.wait(timeout=10)
            claimed = []
            dispatch_due(lambda *args: claimed.append(args))
            return claimed

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(lambda _: concurrent_dispatch(), range(2)))
    claims = a + b
    assert len(claims) == 1
    assert claims[0][0] == delivery_id

    with app.app_context():
        class Accepted:
            status_code = 204

        assert perform_delivery(*claims[0], sender=lambda *args, **kwargs: Accepted()) == "succeeded"
        assert db.session.get(Delivery, delivery_id).attempt_count == 1
        assert db.session.query(DeliveryAttempt).filter_by(delivery_id=delivery_id).count() == 1


@pytest.mark.skipif(not os.getenv("HOOKRELAY_TEST_POSTGRES_URL"), reason="PostgreSQL integration URL not set")
def test_postgres_broker_outage_expired_lease_and_replay():
    """Exercise broker-failure recovery and lease fencing on real PostgreSQL."""
    from datetime import timedelta
    from hookrelay.models import new_id, utcnow

    app = create_app({
        "TESTING": True,
        "ADMIN_API_KEY": "postgres-integration-key",
        "SQLALCHEMY_DATABASE_URI": os.environ["HOOKRELAY_TEST_POSTGRES_URL"],
        "ALLOW_PRIVATE_DESTINATIONS": True,
        "MAX_DELIVERY_ATTEMPTS": 3,
        "RETRY_BASE_SECONDS": 1,
    })
    with app.app_context():
        key = f"ci-recovery-{new_id()}"
        source = Endpoint(name="recovery", public_key=key, signing_secret="recovery-secret")
        db.session.add(source)
        db.session.flush()
        destination = Destination(endpoint_id=source.id, name="receiver",
                                  url="http://receiver:1234/hook", signing_secret="outbound-secret")
        db.session.add(destination)
        db.session.commit()

        raw = b'{"type":"worker.recovery"}'
        response = app.test_client().post(f"/hooks/{key}", data=raw, headers={
            "Content-Type": "application/json", "X-HookRelay-Event-Id": "recovery-event",
            "X-HookRelay-Signature": sign(source.signing_secret, raw),
        })
        assert response.status_code == 202
        original_id = db.session.query(Delivery).filter_by(destination_id=destination.id).one().id

        def broker_down(*_args):
            raise ConnectionError("simulated Redis outage")

        assert dispatch_due(broker_down) == 0
        row = db.session.get(Delivery, original_id)
        assert row.status == "retry" and row.lease_token is None

        old = []
        assert dispatch_due(lambda *args: old.append(args)) == 1
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
        new = []
        assert dispatch_due(lambda *args: new.append(args)) == 1
        assert old[0][1] != new[0][1]
        assert perform_delivery(*old[0]) == "stale"

        class Unavailable:
            status_code = 503

        for attempt in range(3):
            token = new[0] if attempt == 0 else None
            if token is None:
                row = db.session.get(Delivery, original_id)
                row.next_attempt_at = utcnow() - timedelta(seconds=1)
                db.session.commit()
                followup = []
                assert dispatch_due(lambda *args: followup.append(args)) == 1
                token = followup[0]
            assert perform_delivery(*token, sender=lambda *_a, **_kw: Unavailable()) == (
                "dead" if attempt == 2 else "retry"
            )
        assert db.session.get(Delivery, original_id).status == "dead"
        assert db.session.query(DeliveryAttempt).filter_by(delivery_id=original_id).count() == 3

        response = app.test_client().post(
            f"/api/v1/deliveries/{original_id}/replay",
            headers={"Authorization": "Bearer postgres-integration-key"},
        )
        assert response.status_code == 202
        replay_id = response.json["delivery"]["id"]
        claims = []
        assert dispatch_due(lambda *args: claims.append(args)) == 1

        class Accepted:
            status_code = 204

        assert perform_delivery(*claims[0], sender=lambda *_a, **_kw: Accepted()) == "succeeded"
        assert db.session.get(Delivery, replay_id).replay_of_id == original_id
        assert db.session.get(Delivery, original_id).status == "dead"
        assert db.session.query(DeliveryAttempt).filter_by(delivery_id=replay_id).count() == 1
