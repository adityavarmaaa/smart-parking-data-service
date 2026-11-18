
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.routes import events as events_module
from app.schemas.vehicle_event import VehicleEvent


def result(scalar=None, scalar_one=None, session=None, rowcount=None):
    item = MagicMock()
    item.scalar.return_value = scalar
    item.scalar_one.return_value = scalar_one
    item.mappings.return_value.first.return_value = session
    item.rowcount = rowcount
    return item


def make_event():
    return VehicleEvent(
        event_type="EXIT",
        camera_id=f"TEST-CAMERA-{uuid.uuid4().hex[:8]}",
        parking_area_id=f"TEST-AREA-{uuid.uuid4().hex[:8]}",
        vehicle_uid=uuid.uuid4().hex,
        timestamp=datetime.now(timezone.utc),
        captured_at=datetime.now(timezone.utc),
        vehicle={},
        parking={"slot_id": f"TEST-SLOT-{uuid.uuid4().hex[:8]}"},
    )


def make_db(session, slot_rowcount=1):
    db = MagicMock()
    db.execute.side_effect = [
        result(scalar=uuid.uuid4().hex),       # event type
        result(scalar=uuid.uuid4().hex),       # parking area
        result(scalar=None),                   # duplicate check
        result(),                              # camera upsert
        result(),                              # parking slot insert
        result(),                              # slot state insert
        result(scalar_one=uuid.uuid4().hex),   # event insert
        result(session=session),               # active session lookup
        result(scalar=120),                     # duration
        result(),                              # close session
        result(rowcount=slot_rowcount),         # clear slot
    ]
    return db


def run_endpoint(event, db):
    return asyncio.run(
        events_module.receive_vehicle_event(event, _=None, db=db)
    )


def test_exit_closes_matching_session_and_frees_slot():
    event = make_event()
    session = {
        "id": uuid.uuid4().hex,
        "vehicle_fk": None,
        "vehicle_uid": event.vehicle_uid,
        "entry_time": event.timestamp - timedelta(minutes=2),
        "slot_id": event.parking.slot_id,
        "camera_id": event.camera_id,
    }
    db = make_db(session)

    with patch.object(events_module, "encrypt_text", return_value=None), \
         patch.object(events_module, "plate_hmac", return_value=None), \
         patch.object(
             events_module.parking_connections,
             "broadcast",
             new_callable=AsyncMock,
         ):
        response = run_endpoint(event, db)

    assert response["status"] == "accepted"
    assert response["event_type"] == "EXIT"
    assert response["vehicle_uid"] == event.vehicle_uid
    assert response["slot_id"] == event.parking.slot_id
    db.commit.assert_called_once()

    # Verify the active-session lookup uses the correct identity.
    session_lookup_sql = str(db.execute.call_args_list[7].args[0])
    session_lookup_params = db.execute.call_args_list[7].args[1]

    assert "vehicle_uid = :vehicle_uid" in session_lookup_sql
    assert session_lookup_params["vehicle_uid"] == event.vehicle_uid
    assert session_lookup_params["camera_id"] == event.camera_id
    assert session_lookup_params["area_id"] == event.parking_area_id
    assert session_lookup_params["slot_id"] == event.parking.slot_id

    # Verify slot clearing uses the same vehicle identity.
    slot_clear_sql = str(db.execute.call_args_list[10].args[0])
    slot_clear_params = db.execute.call_args_list[10].args[1]

    assert "vehicle_uid = :vehicle_uid" in slot_clear_sql
    assert slot_clear_params["vehicle_uid"] == event.vehicle_uid
    assert slot_clear_params["slot_id"] == event.parking.slot_id
    assert slot_clear_params["camera_id"] == event.camera_id
    assert slot_clear_params["area_id"] == event.parking_area_id

    db.rollback.assert_not_called()


def test_exit_without_matching_session_returns_409():
    event = make_event()
    db = make_db(None)

    with patch.object(events_module, "encrypt_text", return_value=None), \
         patch.object(events_module, "plate_hmac", return_value=None):
        with pytest.raises(HTTPException) as exc:
            run_endpoint(event, db)

    assert exc.value.status_code == 409
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


def test_exit_slot_identity_mismatch_rolls_back():
    event = make_event()
    session = {
        "id": uuid.uuid4().hex,
        "vehicle_fk": None,
        "vehicle_uid": event.vehicle_uid,
        "entry_time": event.timestamp - timedelta(minutes=2),
        "slot_id": event.parking.slot_id,
        "camera_id": event.camera_id,
    }
    db = make_db(session, slot_rowcount=0)

    with patch.object(events_module, "encrypt_text", return_value=None), \
         patch.object(events_module, "plate_hmac", return_value=None):
        with pytest.raises(HTTPException) as exc:
            run_endpoint(event, db)

    assert exc.value.status_code == 409
    assert "Slot state does not match" in exc.value.detail
    db.rollback.assert_called_once()
    db.commit.assert_not_called()
from fastapi.testclient import TestClient
from app.main import app


def test_http_exit_route_accepts_valid_api_key(monkeypatch):
    event = make_event()
    session = {
        "id": uuid.uuid4().hex,
        "vehicle_fk": None,
        "vehicle_uid": event.vehicle_uid,
        "entry_time": event.timestamp - timedelta(minutes=2),
        "slot_id": event.parking.slot_id,
        "camera_id": event.camera_id,
    }
    db = make_db(session)
    monkeypatch.setenv("S2_API_KEY", "temporary-test-key")

    def override_get_db():
        yield db

    previous_override = app.dependency_overrides.get(events_module.get_db)
    app.dependency_overrides[events_module.get_db] = override_get_db

    try:
        with patch.object(events_module, "encrypt_text", return_value=None), \
             patch.object(events_module, "plate_hmac", return_value=None), \
             patch.object(
                 events_module.parking_connections,
                 "broadcast",
                 new_callable=AsyncMock,
             ):
            response = TestClient(app).post(
                "/internal/events/vehicle",
                json=event.model_dump(mode="json"),
                headers={"X-API-Key": "temporary-test-key"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "accepted"
        assert body["event_type"] == "EXIT"
        assert body["vehicle_uid"] == event.vehicle_uid
        assert body["slot_id"] == event.parking.slot_id
        db.commit.assert_called_once()
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(events_module.get_db, None)
        else:
            app.dependency_overrides[events_module.get_db] = previous_override


def test_http_exit_route_rejects_missing_api_key(monkeypatch):
    event = make_event()
    db = MagicMock()
    monkeypatch.setenv("S2_API_KEY", "temporary-test-key")

    def override_get_db():
        yield db

    previous_override = app.dependency_overrides.get(events_module.get_db)
    app.dependency_overrides[events_module.get_db] = override_get_db

    try:
        response = TestClient(app).post(
            "/internal/events/vehicle",
            json=event.model_dump(mode="json"),
        )

        assert response.status_code == 401
        db.execute.assert_not_called()
        db.commit.assert_not_called()
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(events_module.get_db, None)
        else:
            app.dependency_overrides[events_module.get_db] = previous_override
