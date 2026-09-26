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
    # Get latest session containing snapshot paths
    session = db.execute(
        text("""
            SELECT
                entry_screenshot_url,
                exit_screenshot_url
            FROM parking_sessions
            WHERE plate_number = :plate
            ORDER BY entry_time DESC
            LIMIT 1
        """),
        {
            "plate": plate_number,
        },
    ).mappings().first()

    if not session:
        raise HTTPException(
            status_code=404,
            detail=f"No snapshots found for vehicle {plate_number}",
        )

    # Get latest raw event containing vehicle/plate snapshot paths
    event = db.execute(
        text("""
            SELECT
                vehicle_snapshot,
                plate_snapshot
            FROM parking_events
            WHERE plate_number = :plate
            ORDER BY timestamp DESC
            LIMIT 1
        """),
        {
            "plate": plate_number,
        },
    ).mappings().first()

    return {
        "status": "success",
        "plate_number": plate_number,
        "snapshots": {
            "vehicle": (
                event["vehicle_snapshot"]
                if event
                else session["entry_screenshot_url"]
            ),
            "plate": (
                event["plate_snapshot"]
                if event
                else None
            ),
        },
        "session": {
            "entry_snapshot": session["entry_screenshot_url"],
            "exit_snapshot": session["exit_screenshot_url"],
        },
        "message": (
            "Snapshot paths are stored successfully. "
            "Actual image files will be supplied by Service 1."
        ),
    }