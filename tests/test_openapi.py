"""Keep the published API contract aligned with Flask route registration."""
import re

from openapi_spec_validator import validate


def test_openapi_is_available_and_schema_valid(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.is_json
    assert response.headers["Content-Type"].startswith("application/json")
    spec = response.get_json()
    validate(spec)
    assert spec["openapi"] == "3.0.3"
    assert spec["info"]["title"] == "HookRelay API"


def test_interactive_swagger_ui_is_served(client):
    response = client.get("/docs")
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    body = response.get_data(as_text=True)
    assert 'id="swagger-ui"' in body
    assert 'url: "/openapi.json"' in body
    assert "swagger-ui-bundle.js" in body
    assert "persistAuthorization: false" in body
    assert 'href="/openapi.json"' in body


def test_spec_has_all_runtime_api_routes_and_methods(app, client):
    spec = client.get("/openapi.json").get_json()
    documented = {
        (path, method.upper())
        for path, operations in spec["paths"].items()
        for method in operations
    }
    actual = {
        (re.sub(r"<(?:(?:string|uuid):)?(\w+)>", r"{\1}", route.rule), method)
        for route in app.url_map.iter_rules()
        if route.rule not in {"/docs", "/openapi.json"}
        and not route.rule.startswith("/static/")
        for method in route.methods - {"OPTIONS", "HEAD"}
    }
    assert documented == actual


def test_all_admin_endpoints_have_bearer_security_and_error_responses(client):
    spec = client.get("/openapi.json").get_json()
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            assert operation["operationId"]
            assert operation["responses"]
            if path.startswith("/api/v1/"):
                assert operation["security"] == [{"AdminBearer": []}]
                assert "401" in operation["responses"]
            elif path.startswith("/hooks/"):
                assert operation["security"] == [{"WebhookHMAC": []}]
                assert "409" in operation["responses"]
            else:
                assert operation["security"] == []


def test_documented_secret_fields_only_in_creation_responses(client):
    spec = client.get("/openapi.json").get_json()
    schemas = spec["components"]["schemas"]
    assert "signing_secret" in schemas["EndpointCreated"]["properties"]
    assert "signing_secret" not in schemas["Endpoint"]["properties"]
    assert "signing_secret" in schemas["DestinationCreated"]["properties"]
    assert "signing_secret" not in schemas["Destination"]["properties"]
    assert "X-HookRelay-Signature" == spec["components"]["securitySchemes"]["WebhookHMAC"]["name"]
