from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.dependencies import get_db


router = APIRouter(
    prefix="/api/analytics",
    tags=["Analytics"],
)


@router.get("/summary")
def get_analytics_summary(
    db: Session = Depends(get_db),
):
    # =========================================================
    # 1. CURRENT SLOT OCCUPANCY
    # =========================================================

    occupancy = db.execute(
        text("""
            SELECT
                COUNT(*) AS total_slots,
                COUNT(*) FILTER (
                    WHERE status = 'occupied'
                ) AS occupied_slots,
                COUNT(*) FILTER (
                    WHERE status = 'available'
                ) AS available_slots
            FROM parking_slot_state
        """)
    ).mappings().one()

    # =========================================================
    # 2. TODAY'S ENTRIES
    # =========================================================

    entries_result = db.execute(
        text("""
            SELECT COUNT(*)
            FROM parking_events
            WHERE event_type = 'ENTRY'
              AND timestamp >= CURRENT_DATE
              AND timestamp < CURRENT_DATE + INTERVAL '1 day'
        """)
    )

    total_entries_today = entries_result.scalar() or 0

    # =========================================================
    # 3. TODAY'S EXITS
    # =========================================================

    exits_result = db.execute(
        text("""
            SELECT COUNT(*)
            FROM parking_events
            WHERE event_type = 'EXIT'
              AND timestamp >= CURRENT_DATE
              AND timestamp < CURRENT_DATE + INTERVAL '1 day'
        """)
    )

    total_exits_today = exits_result.scalar() or 0

    # =========================================================
    # 4. CURRENTLY PARKED
    # =========================================================

    parked_result = db.execute(
        text("""
            SELECT COUNT(*)
            FROM parking_sessions
            WHERE status = 'parked'
        """)
    )

    currently_parked = parked_result.scalar() or 0

    # =========================================================
    # 5. AVERAGE PARKING DURATION
    # Only completed sessions
    # =========================================================

    average_result = db.execute(
        text("""
            SELECT AVG(duration_seconds)
            FROM parking_sessions
            WHERE status = 'exited'
              AND duration_seconds IS NOT NULL
        """)
    )

    average_duration_seconds = average_result.scalar()

    if average_duration_seconds is None:
        average_duration_minutes = 0
    else:
        average_duration_minutes = round(
            float(average_duration_seconds) / 60,
            2,
        )

    # =========================================================
    # 6. RESPONSE
    # =========================================================

    return {
        "status": "success",
        "generated_at": datetime.now(timezone.utc),

        "total_slots": int(
            occupancy["total_slots"] or 0
        ),

        "occupied_slots": int(
            occupancy["occupied_slots"] or 0
        ),

        "available_slots": int(
            occupancy["available_slots"] or 0
        ),

        "total_entries_today": int(
            total_entries_today
        ),

        "total_exits_today": int(
            total_exits_today
        ),

        "currently_parked": int(
            currently_parked
        ),

        "average_parking_duration_minutes":
            average_duration_minutes,
    }