"""Run the Docker-free demonstration as part of ordinary pytest."""

from hookrelay import reliability_demo
from hookrelay.extensions import db


def test_reliability_demo_runs_successfully(monkeypatch):
    # Retain the original engine pool so that we can verify the demo disposes
    # its SQLite connections before TemporaryDirectory deletes the DB file.
    original_create_app = reliability_demo.create_app
    created = []

    def capture_app(test_config):
        app = original_create_app(test_config)
        with app.app_context():
            created.append((app, db.engine.pool))
        return app

    monkeypatch.setattr(reliability_demo, "create_app", capture_app)
    messages = []
    reliability_demo.run_demo(output=messages.append)
    assert len([message for message in messages if message.startswith("PASS ")]) == 5
    app, initial_pool = created[0]
    with app.app_context():
        assert db.engine.pool is not initial_pool, "demo must dispose pooled SQLite connections"
