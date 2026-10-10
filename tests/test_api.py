from hookrelay.extensions import db
from hookrelay.models import Delivery, Event
from hookrelay.security import sign


def ingest(client, endpoint, external_id="evt-1", raw=b'{"amount": 42}'):
    return client.post(f"/hooks/{endpoint['public_key']}", data=raw,
                       headers={"Content-Type": "application/json", "X-HookRelay-Event-Id": external_id,
                                "X-HookRelay-Signature": sign(endpoint["signing_secret"], raw)})


def test_admin_auth(client):
    assert client.get("/api/v1/endpoints").status_code == 401
    assert client.get("/api/v1/endpoints", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_ingestion_is_durable_and_idempotent(app, client, configured):
    endpoint, _ = configured
    first = ingest(client, endpoint)
    assert first.status_code == 202
    assert first.json["deliveries"] == 1
    second = ingest(client, endpoint)
    assert second.status_code == 200
    assert second.json == {"event_id": first.json["event_id"], "duplicate": True}
    assert ingest(client, endpoint, raw=b'{"amount": 43}').status_code == 409
    with app.app_context():
        assert db.session.query(Event).count() == 1
        assert db.session.query(Delivery).count() == 1


def test_unsigned_or_invalid_payload_rejected(client, configured):
    endpoint, _ = configured
    path = f"/hooks/{endpoint['public_key']}"
    assert client.post(path, data=b"{}", headers={"Content-Type": "application/json"}).status_code == 401
    assert ingest(client, endpoint, raw=b"not-json").status_code == 400
    assert ingest(client, endpoint, external_id="").status_code == 400


def test_replay_creates_new_auditable_delivery(app, client, configured, admin_headers):
    endpoint, _ = configured
    first = ingest(client, endpoint)
    with app.app_context():
        delivery = db.session.query(Delivery).one()
        delivery_id = delivery.id
        delivery.status = "dead"
        delivery.next_attempt_at = None
        db.session.commit()
    response = client.post(f"/api/v1/deliveries/{delivery_id}/replay", headers=admin_headers)
    assert response.status_code == 202
    assert response.json["delivery"]["replay_of_id"] == delivery_id
    assert response.json["delivery"]["id"] != delivery_id
    with app.app_context():
        assert db.session.query(Event).count() == 1
        assert db.session.query(Delivery).count() == 2
