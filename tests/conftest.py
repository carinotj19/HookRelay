import pytest

from hookrelay import create_app
from hookrelay.extensions import db


@pytest.fixture
def app(tmp_path):
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
        "ADMIN_API_KEY": "local-test-key",
        "RETRY_BASE_SECONDS": 1,
        "MAX_DELIVERY_ATTEMPTS": 3,
        "ALLOW_PRIVATE_DESTINATIONS": True,
    })
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_headers():
    return {"Authorization": "Bearer local-test-key"}


@pytest.fixture
def configured(client, admin_headers):
    endpoint = client.post("/api/v1/endpoints", json={"name": "billing"}, headers=admin_headers).json
    dest = client.post("/api/v1/destinations", json={"name": "customer app",
                        "endpoint_id": endpoint["id"], "url": "http://receiver:8080/hooks"},
                       headers=admin_headers).json
    return endpoint, dest
