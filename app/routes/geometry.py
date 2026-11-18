import json
import math

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.dependencies import get_db
from app.auth import require_s2_api_key
from app.security import json_hmac


router = APIRouter(
    prefix="/internal/geometry",
    tags=["Calibrated Geometry"],
)


class SlotGeometry(BaseModel):
    slot_id: str = Field(min_length=1, max_length=128)
    polygon: list[list[float]] = Field(min_length=3)

    @field_validator("polygon")
    @classmethod
    def validate_polygon(cls, polygon):
        if len(polygon) < 3:
            raise ValueError("A polygon requires at least three points")

        for point in polygon:
            if len(point) != 2:
                raise ValueError("Each polygon point must contain x and y")
            if not all(math.isfinite(value) for value in point):
                raise ValueError("Polygon coordinates must be finite")

        return polygon


class GeometrySyncRequest(BaseModel):
    camera_id: str = Field(min_length=1, max_length=128)
    parking_area_id: str = Field(min_length=1, max_length=128)
    slots: list[SlotGeometry] = Field(min_length=1)

    @field_validator("slots")
    @classmethod
    def validate_unique_slots(cls, slots):
        ids = [slot.slot_id for slot in slots]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate slot IDs in geometry request")
        return slots


@router.post("/slots")
def sync_slot_geometry(
    payload: GeometrySyncRequest,
    _: None = Depends(require_s2_api_key),
    db: Session = Depends(get_db),
):
    try:
        registered = db.execute(
            text("""
                SELECT c.parking_area_id
                FROM cameras c
                JOIN parking_areas pa
                  ON pa.area_code = c.parking_area_id
                WHERE c.camera_id = :camera_id
                  AND c.parking_area_id = :area_id
                  AND pa.status = 'active'
                LIMIT 1
            """),
            {
                "camera_id": payload.camera_id,
                "area_id": payload.parking_area_id,
            },
        ).scalar()

        if registered is None:
            raise HTTPException(
                status_code=409,
                detail="Camera is not registered to the requested active parking area",
            )

        for slot in payload.slots:
            polygon_data = slot.polygon
            polygon_signature = json_hmac(polygon_data)

            db.execute(
                text("""
                    INSERT INTO parking_slots (
                        camera_id,
                        slot_id,
                        polygon,
                        status,
                        polygon_hmac
                    )
                    VALUES (
                        :camera_id,
                        :slot_id,
                        CAST(:polygon AS JSONB),
                        'available',
                        :polygon_hmac
                    )
                    ON CONFLICT (camera_id, slot_id)
                    DO UPDATE SET
                        polygon = EXCLUDED.polygon,
                        polygon_hmac = EXCLUDED.polygon_hmac,
                        updated_at = NOW()
                """),
                {
                    "camera_id": payload.camera_id,
                    "slot_id": slot.slot_id,
                    "polygon": json.dumps(polygon_data),
                    "polygon_hmac": polygon_signature,
                },
            )

        db.commit()

        return {
            "status": "accepted",
            "camera_id": payload.camera_id,
            "parking_area_id": payload.parking_area_id,
            "slots_synced": len(payload.slots),
            "slot_ids": [slot.slot_id for slot in payload.slots],
        }

    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Geometry synchronization failed",
        )
