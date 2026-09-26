from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.dependencies import get_db


router = APIRouter(
    prefix="/api/vehicles",
    tags=["Vehicles"],
)


@router.get("/{plate_number}")
def get_vehicle(
    plate_number: str,
    db: Session = Depends(get_db),
):
    # =========================================================
    # 1. GET VEHICLE
    # =========================================================

    vehicle = db.execute(
        text("""
            SELECT
                id,
                plate_number,
                vehicle_type,
                color,
                first_seen_at,
                last_seen_at
            FROM vehicles
            WHERE plate_number = :plate
            LIMIT 1
        """),
        {
            "plate": plate_number,
        },
    ).mappings().first()

    if not vehicle:
        raise HTTPException(
            status_code=404,
            detail=f"Vehicle {plate_number} not found",
        )

    # =========================================================
    # 2. GET CURRENT PARKING STATE
    # =========================================================

    current_slot = db.execute(
        text("""
            SELECT
                camera_id,
                parking_area_id,
                slot_id,
                status,
                track_id,
                occupied_since,
                updated_at
            FROM parking_slot_state
            WHERE plate_number = :plate
              AND status = 'occupied'
            ORDER BY updated_at DESC
            LIMIT 1
        """),
        {
            "plate": plate_number,
        },
    ).mappings().first()

    # =========================================================
    # 3. GET PARKING HISTORY
    # =========================================================

    sessions = db.execute(
        text("""
            SELECT
                id,
                camera_id,
                slot_id,
                entry_time,
                exit_time,
                duration_seconds,
                status,
                entry_screenshot_url,
                exit_screenshot_url
            FROM parking_sessions
            WHERE plate_number = :plate
            ORDER BY entry_time DESC
        """),
        {
            "plate": plate_number,
        },
    ).mappings().all()

    # =========================================================
    # 4. FORMAT HISTORY
    # =========================================================

    history = []

    for session in sessions:

        duration_minutes = None

        if session["duration_seconds"] is not None:
            duration_minutes = round(
                float(session["duration_seconds"]) / 60,
                2,
            )

        history.append({
            "session_id": session["id"],
            "camera_id": session["camera_id"],
            "slot_id": session["slot_id"],
            "entry_time": session["entry_time"],
            "exit_time": session["exit_time"],
            "duration_seconds": session["duration_seconds"],
            "duration_minutes": duration_minutes,
            "status": session["status"],
            "entry_snapshot": session["entry_screenshot_url"],
            "exit_snapshot": session["exit_screenshot_url"],
        })

    # =========================================================
    # 5. CURRENT STATUS
    # =========================================================

    if current_slot:
        current_status = "parked"
    else:
        current_status = "not_parked"

    # =========================================================
    # 6. RESPONSE
    # =========================================================

    return {
        "status": "success",

        "vehicle": {
            "id": vehicle["id"],
            "plate_number": vehicle["plate_number"],
            "vehicle_type": vehicle["vehicle_type"],
            "color": vehicle["color"],
            "first_seen_at": vehicle["first_seen_at"],
            "last_seen_at": vehicle["last_seen_at"],
        },

        "current_status": current_status,

        "current_location": (
            {
                "camera_id": current_slot["camera_id"],
                "parking_area_id": current_slot["parking_area_id"],
                "slot_id": current_slot["slot_id"],
                "track_id": current_slot["track_id"],
                "occupied_since": current_slot["occupied_since"],
                "updated_at": current_slot["updated_at"],
            }
            if current_slot
            else None
        ),

        "total_sessions": len(history),

        "history": history,
    }