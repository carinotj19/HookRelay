"""Behavior checks for externally visible error codes in the OpenAPI contract."""
from hookrelay.extensions import db
from hookrelay.models import Delivery, Event
from hookrelay.security import sign
from test_api import ingest


def test_endpoint_and_destination_validation(client, configured, admin_headers):
    endpoint, _ = configured
    for name in ("", "  ", "a" * 121, 23, None):
        response = client.post("/api/v1/endpoints", json={"name": name}, headers=admin_headers)
        assert response.status_code == 400
    bad_id = client.post("/api/v1/destinations", json={
        "endpoint_id": "missing", "name": "target", "url": "https://example.com/callback"
    }, headers=admin_headers)
    assert bad_id.status_code == 404
    no_name = client.post("/api/v1/destinations", json={
        "endpoint_id": endpoint["id"], "url": "https://example.com/callback"
    }, headers=admin_headers)
    assert no_name.status_code == 400
    invalid_url = client.post("/api/v1/destinations", json={
        "endpoint_id": endpoint["id"], "name": "target", "url": "ftp://example.com/"
    }, headers=admin_headers)
    assert invalid_url.status_code == 400


def test_signature_and_content_validation(app, client, configured):
    endpoint, _ = configured
    path = f"/hooks/{endpoint['public_key']}"
    body = b'{"amount":42}'
    signed = sign(endpoint["signing_secret"], body)
    assert client.post(path, data=body, headers={
        "Content-Type": "text/plain", "X-HookRelay-Event-Id": "evt-content-type",
        "X-HookRelay-Signature": signed,
    }).status_code == 415
    assert client.post(path, data=body, headers={
        "Content-Type": "application/json", "X-HookRelay-Event-Id": "evt-expired",
        "X-HookRelay-Signature": sign(endpoint["signing_secret"], body, timestamp=1),
    }).status_code == 401
    assert ingest(client, endpoint, external_id="x" * 201).status_code == 400
    assert client.post("/hooks/not-a-real-endpoint", data=b"{}", content_type="application/json").status_code == 404
    with app.app_context():
        assert db.session.query(Event).count() == 0


def test_delivery_filters_and_replay_state_validation(app, client, configured, admin_headers):
    endpoint, _ = configured
    ingest(client, endpoint)
    with app.app_context():
        delivery_id = db.session.query(Delivery).one().id
    assert client.get("/api/v1/deliveries?status=invalid", headers=admin_headers).status_code == 400
    assert client.get("/api/v1/deliveries?status=pending&limit=1", headers=admin_headers).json["items"][0]["id"] == delivery_id
    assert client.get("/api/v1/deliveries/unknown/attempts", headers=admin_headers).status_code == 404
    assert client.post(f"/api/v1/deliveries/{delivery_id}/replay", headers=admin_headers).status_code == 409
    assert client.post("/api/v1/deliveries/missing/replay", headers=admin_headers).status_code == 404
