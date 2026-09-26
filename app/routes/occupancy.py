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
    # Get current slot state
    result = db.execute(
        text("""
            SELECT
                camera_id,
                slot_id,
                parking_area_id,
                status,
                plate_number,
                track_id,
                occupied_since,
                updated_at
            FROM parking_slot_state
            ORDER BY camera_id, slot_id
        """)
    ).mappings().all()

    total_slots = len(result)

    occupied_slots = sum(
        1 for slot in result
        if slot["status"] == "occupied"
    )

    available_slots = total_slots - occupied_slots

    return {
        "status": "success",
        "total_slots": total_slots,
        "occupied_slots": occupied_slots,
        "available_slots": available_slots,
        "slots": [
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
            for slot in result
        ],
    }