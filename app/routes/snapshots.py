from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.dependencies import get_db


router = APIRouter(
    prefix="/api/snapshots",
    tags=["Snapshots"],
)


@router.get("/vehicle/{plate_number}")
def get_vehicle_snapshots(
    plate_number: str,
    db: Session = Depends(get_db),
):
    """
    Return vehicle and license-plate snapshots for a vehicle.

    Canonical vehicle identity is resolved through:

        vehicles.id
            ↓
        parking_sessions.vehicle_fk
            ↓
        parking_events.vehicle_fk

    parking_events.vehicle_snapshot and plate_snapshot contain
    the snapshot paths captured for each event.
    """

    # =========================================================
    # 1. NORMALIZE PLATE
    # =========================================================

    plate_number = plate_number.strip().upper()

    # =========================================================
    # 2. RESOLVE CANONICAL VEHICLE
    # =========================================================

    vehicle = db.execute(
        text("""
            SELECT
                id,
                plate_number
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
            detail="Vehicle not found",
        )

    vehicle_id = vehicle["id"]

    # =========================================================
    # 3. GET LATEST PARKING SESSION
    # =========================================================

    session = db.execute(
        text("""
            SELECT
                id,
                entry_screenshot_url,
                exit_screenshot_url
            FROM parking_sessions
            WHERE vehicle_fk = :vehicle_id
            ORDER BY entry_time DESC
            LIMIT 1
        """),
        {
            "vehicle_id": vehicle_id,
        },
    ).mappings().first()

    # =========================================================
    # 4. GET SNAPSHOT EVENTS
    # =========================================================

    events = db.execute(
        text("""
            SELECT
                pe.id,
                et.code AS event_type,
                pe.timestamp,
                pe.vehicle_snapshot,
                pe.plate_snapshot

            FROM parking_events pe

            LEFT JOIN event_types et
                ON et.id = pe.event_type_fk

            WHERE pe.vehicle_fk = :vehicle_id

              AND (
                    pe.vehicle_snapshot IS NOT NULL
                    OR pe.plate_snapshot IS NOT NULL
                  )

            ORDER BY pe.timestamp DESC
        """),
        {
            "vehicle_id": vehicle_id,
        },
    ).mappings().all()

    # =========================================================
    # 5. FORMAT SNAPSHOTS
    # =========================================================

    vehicle_snapshots = []
    plate_snapshots = []

    for event in events:

        if event["vehicle_snapshot"]:
            vehicle_snapshots.append(
                {
                    "event_id": event["id"],
                    "event_type": event["event_type"],
                    "timestamp": event["timestamp"],
                    "url": event["vehicle_snapshot"],
                }
            )

        if event["plate_snapshot"]:
            plate_snapshots.append(
                {
                    "event_id": event["id"],
                    "event_type": event["event_type"],
                    "timestamp": event["timestamp"],
                    "url": event["plate_snapshot"],
                }
            )

    # =========================================================
    # 6. RESPONSE
    # =========================================================

    return {
        "status": "success",
        "plate_number": plate_number,

        "vehicle_snapshot": (
            session["entry_screenshot_url"]
            if session
            and session["entry_screenshot_url"]
            else None
        ),

        "plate_snapshot": (
            session["exit_screenshot_url"]
            if session
            and session["exit_screenshot_url"]
            else None
        ),

        "vehicle_snapshots": vehicle_snapshots,

        "plate_snapshots": plate_snapshots,
    }