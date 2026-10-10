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
