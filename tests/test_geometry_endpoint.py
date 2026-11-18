from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routes import geometry
from app.routes.geometry import GeometrySyncRequest


def make_payload():
    return GeometrySyncRequest(
        camera_id="TEST-CAMERA",
        parking_area_id="TEST-AREA",
        slots=[
            {
                "slot_id": "TEST-SLOT",
                "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
            }
        ],
    )


def test_geometry_update_preserves_existing_slot_status():
    db = MagicMock()
    db.execute.return_value.scalar.return_value = "TEST-AREA"

    with patch.object(geometry, "json_hmac", return_value="test-signature"):
        response = geometry.sync_slot_geometry(
            make_payload(),
            _=None,
            db=db,
        )

    assert response["status"] == "accepted"
    assert response["slots_synced"] == 1
    assert db.execute.call_count == 2
    db.commit.assert_called_once()
    db.rollback.assert_not_called()

    upsert_sql = str(db.execute.call_args_list[1].args[0])
    assert "polygon = EXCLUDED.polygon" in upsert_sql
    assert "polygon_hmac = EXCLUDED.polygon_hmac" in upsert_sql
    assert "status =" not in upsert_sql.split("DO UPDATE SET", 1)[1]


def test_geometry_rejects_unregistered_camera_area():
    db = MagicMock()
    db.execute.return_value.scalar.return_value = None

    with pytest.raises(HTTPException) as exc:
        geometry.sync_slot_geometry(make_payload(), _=None, db=db)

    assert exc.value.status_code == 409
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


def test_geometry_rejects_non_finite_coordinates():
    with pytest.raises(ValueError):
        GeometrySyncRequest(
            camera_id="TEST-CAMERA",
            parking_area_id="TEST-AREA",
            slots=[
                {
                    "slot_id": "TEST-SLOT",
                    "polygon": [[0, 0], [float("nan"), 1], [2, 2]],
                }
            ],
        )
