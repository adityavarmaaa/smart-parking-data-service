from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.dependencies import get_db


router = APIRouter(
    prefix="/api",
    tags=["Occupancy"],
)


@router.get("/occupancy")
def get_occupancy(
    db: Session = Depends(get_db),
):
    """
    Return the current parking occupancy state.

    Normalized relationships:

        parking_slot_state
            ├── camera_id -> cameras.camera_id
            │                    └── parking_area_fk -> parking_areas.id
            │
            └── vehicle_fk -> vehicles.id

    Legacy cameras.parking_area_id and
    parking_slot_state.plate_number are not used.
    """

    # =========================================================
    # 1. GET CURRENT SLOT STATE
    # =========================================================

    result = db.execute(
        text("""
            SELECT
                pss.camera_id,
                pss.slot_id,

                pa.area_code AS parking_area_id,

                pss.status,

                v.plate_number AS plate_number,

                pss.vehicle_fk,
                pss.track_id,
                pss.occupied_since,
                pss.updated_at

            FROM parking_slot_state pss

            JOIN cameras c
                ON c.camera_id = pss.camera_id

            JOIN parking_areas pa
                ON pa.id = c.parking_area_fk

            LEFT JOIN vehicles v
                ON v.id = pss.vehicle_fk

            ORDER BY
                pss.camera_id,
                pss.slot_id
        """)
    ).mappings().all()

    # =========================================================
    # 2. CALCULATE OCCUPANCY
    # =========================================================

    total_slots = len(result)

    occupied_slots = sum(
        1
        for slot in result
        if slot["status"] == "occupied"
    )

    available_slots = total_slots - occupied_slots

    # =========================================================
    # 3. FORMAT SLOT DATA
    # =========================================================

    slots = []

    for slot in result:
        slots.append(
            {
                "camera_id": slot["camera_id"],
                "slot_id": slot["slot_id"],
                "parking_area_id": slot["parking_area_id"],
                "status": slot["status"],
                "plate_number": slot["plate_number"],
                "track_id": slot["track_id"],
                "occupied_since": slot["occupied_since"],
                "updated_at": slot["updated_at"],
            }
        )

    # =========================================================
    # 4. RESPONSE
    # =========================================================

    return {
        "status": "success",
        "total_slots": total_slots,
        "occupied_slots": occupied_slots,
        "available_slots": available_slots,
        "slots": slots,
    }