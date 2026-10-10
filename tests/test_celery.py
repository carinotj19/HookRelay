"""Run the production Celery task wrappers in eager mode without Redis.

This verifies task wiring, not Redis acknowledgement or worker-crash behavior.
"""
from types import SimpleNamespace

from hookrelay.config import Config
from hookrelay.extensions import db
from hookrelay.models import Delivery, DeliveryAttempt
from test_api import ingest


def test_eager_celery_dispatch_and_worker(app, client, configured, monkeypatch):
    # Config captures environment variables when its module is imported, not
    # when celery_app imports it. Patch the config value itself so this test
    # works in local Windows shells without a preconfigured ADMIN_API_KEY.
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "test-worker-import-key")
    from hookrelay import celery_app as worker

    monkeypatch.setattr(worker, "flask_app", app)
    monkeypatch.setattr("hookrelay.delivery.requests.post",
                        lambda *_a, **_kw: SimpleNamespace(status_code=204))
    worker.celery.conf.update(task_always_eager=True, task_eager_propagates=True)
    try:
        endpoint, _ = configured
        assert ingest(client, endpoint).status_code == 202
        assert worker.dispatch.apply().get() == 1
        with app.app_context():
            row = db.session.query(Delivery).one()
            assert row.status == "succeeded" and row.attempt_count == 1
            assert db.session.query(DeliveryAttempt).one().status_code == 204
    finally:
        worker.celery.conf.update(task_always_eager=False)
