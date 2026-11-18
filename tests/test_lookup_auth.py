import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.mark.parametrize(
    "path",
    [
        "/api/vehicles/TEST-PLATE",
        "/api/snapshots/vehicle/TEST-PLATE",
    ],
)
@pytest.mark.parametrize("api_key", [None, "incorrect-test-key"])
def test_lookup_routes_reject_unauthorized_requests(
    monkeypatch, path, api_key
):
    monkeypatch.setenv("S2_API_KEY", "temporary-test-key")

    headers = {}
    if api_key is not None:
        headers["X-API-Key"] = api_key

    response = TestClient(app).get(path, headers=headers)

    assert response.status_code == 401

class FakeLookupResult:
    def mappings(self):
        return self

    def first(self):
        return None


class FakeLookupDB:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((str(statement), params))
        return FakeLookupResult()


@pytest.mark.parametrize(
    ("module_name", "path"),
    [
        (
            "app.routes.vehicles",
            "/api/vehicles/TEST-PLATE",
        ),
        (
            "app.routes.snapshots",
            "/api/snapshots/vehicle/TEST-PLATE",
        ),
    ],
)
def test_authenticated_lookup_uses_hmac_only(
    monkeypatch, module_name, path
):
    import importlib

    from app.database.dependencies import get_db

    monkeypatch.setenv("S2_API_KEY", "temporary-test-key")

    route_module = importlib.import_module(module_name)
    monkeypatch.setattr(
        route_module,
        "plate_hmac_candidates",
        lambda plate: ["test-hmac-current", "test-hmac-previous"],
    )

    db = FakeLookupDB()
    app.dependency_overrides[get_db] = lambda: db

    try:
        response = TestClient(app).get(
            path,
            headers={"X-API-Key": "temporary-test-key"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 404
    assert len(db.calls) == 1

    sql, params = db.calls[0]

    assert "plate_number_hmac" in sql
    assert "ANY(CAST(:plate_hmacs AS TEXT[]))" in sql
    assert "plate_number IS NULL" not in sql
    assert "plate_number = :plate" not in sql
    assert "plate" not in params
    assert params == {
        "plate_hmacs": [
            "test-hmac-current",
            "test-hmac-previous",
        ]
    }
