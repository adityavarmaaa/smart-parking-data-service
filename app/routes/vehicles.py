from fastapi import APIRouter, Depends, HTTPException
from app.auth import require_s2_api_key
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.dependencies import get_db
from app.security import plate_hmac_candidates


router = APIRouter(
    prefix="/api/vehicles",
    tags=["Vehicles"],
)


@router.get("/{plate_number}")
def get_vehicle(
    plate_number: str,
    _: None = Depends(require_s2_api_key),
    db: Session = Depends(get_db),
):
    """
    Get complete vehicle information.

    Normalized relationships:

        vehicles
            â”œâ”€â”€ vehicle_type_fk -> vehicle_types
            â”œâ”€â”€ color_fk        -> vehicle_colors
            â”‚
            â””â”€â”€ vehicle.id
                    â”œâ”€â”€ parking_events.vehicle_fk
                    â”œâ”€â”€ parking_sessions.vehicle_fk
                    â””â”€â”€ parking_slot_state.vehicle_fk

    Legacy vehicle_type/color columns are NOT used.
    Legacy parking_area_id on cameras is NOT used.
    """

    # =========================================================
    # 1. GET CANONICAL VEHICLE
    # =========================================================

    vehicle = db.execute(
        text("""
            SELECT
                v.id,
              
                vt.code AS vehicle_type,
                vc.code AS color,
                v.first_seen_at,
                v.last_seen_at
            FROM vehicles v

            LEFT JOIN vehicle_types vt
                ON vt.id = v.vehicle_type_fk

            LEFT JOIN vehicle_colors vc
                ON vc.id = v.color_fk

           WHERE v.plate_number_hmac = ANY(CAST(:plate_hmacs AS TEXT[]))
            LIMIT 1
        """),
        {
            "plate_hmacs": plate_hmac_candidates(plate_number),
        },
    ).mappings().first()

    if not vehicle:
        raise HTTPException(
            status_code=404,
            detail="Vehicle not found",
        )

    # =========================================================
    # 2. GET CURRENT PARKING STATE
    # =========================================================
    #
    # Area is resolved through:
    #
    # parking_slot_state
    #       â†“
    # cameras
    #       â†“
    # parking_area_fk
    #       â†“
    # parking_areas
    #
    # We intentionally do NOT use:
    #
    # cameras.parking_area_id
    #
    # =========================================================

    current_slot = db.execute(
        text("""
            SELECT
                pss.camera_id,

                pa.area_code AS parking_area_id,

                pss.slot_id,
                pss.status,
                pss.track_id,
                pss.occupied_since,
                pss.updated_at

            FROM parking_slot_state pss

            JOIN cameras c
                ON c.camera_id = pss.camera_id

            JOIN parking_areas pa
                ON pa.id = c.parking_area_fk

            WHERE pss.vehicle_fk = :vehicle_id
              AND pss.status = 'occupied'

            ORDER BY pss.updated_at DESC
            LIMIT 1
        """),
        {
            "vehicle_id": vehicle["id"],
        },
    ).mappings().first()

    # =========================================================
    # 3. GET PARKING HISTORY
    # =========================================================

    sessions = db.execute(
        text("""
            SELECT
                ps.id,
                ps.camera_id,
                ps.slot_id,
                ps.entry_time,
                ps.exit_time,
                ps.duration_seconds,
                ps.status,
                ps.entry_screenshot_url,
                ps.exit_screenshot_url
            FROM parking_sessions ps

            WHERE ps.vehicle_fk = :vehicle_id

            ORDER BY ps.entry_time DESC
        """),
        {
            "vehicle_id": vehicle["id"],
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
