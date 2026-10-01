from datetime import datetime, time, timedelta

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
    """
    Return parking analytics summary.

    Normalized data sources:

        parking_slot_state
            -> current occupancy

        parking_events.event_type_fk
            -> ENTRY / EXIT events

        parking_sessions
            -> active sessions and parking duration
    """

    # =========================================================
    # 1. CURRENT OCCUPANCY
    # =========================================================

    occupancy = db.execute(
        text("""
            SELECT
                COUNT(*) AS total_slots,

                COUNT(*) FILTER (
                    WHERE status = 'occupied'
                ) AS occupied_slots

            FROM parking_slot_state
        """)
    ).mappings().first()

    total_slots = int(
        occupancy["total_slots"] or 0
    )

    occupied_slots = int(
        occupancy["occupied_slots"] or 0
    )

    available_slots = total_slots - occupied_slots

    # =========================================================
    # 2. TODAY'S DATE RANGE
    # =========================================================
    #
    # Use:
    #
    #   >= start_of_day
    #   <  start_of_next_day
    #
    # instead of <= end_of_day.
    #
    # This avoids timestamp precision issues.
    # =========================================================

    today = datetime.now().date()

    start_of_day = datetime.combine(
        today,
        time.min,
    )

    start_of_next_day = start_of_day + timedelta(days=1)

    # =========================================================
    # 3. ENTRIES TODAY
    # =========================================================

    entries = db.execute(
        text("""
            SELECT COUNT(*)
            FROM parking_events pe

            JOIN event_types et
                ON et.id = pe.event_type_fk

            WHERE et.code = 'ENTRY'
              AND pe.timestamp >= :start_of_day
              AND pe.timestamp < :start_of_next_day
        """),
        {
            "start_of_day": start_of_day,
            "start_of_next_day": start_of_next_day,
        },
    ).scalar() or 0

    # =========================================================
    # 4. EXITS TODAY
    # =========================================================

    exits = db.execute(
        text("""
            SELECT COUNT(*)
            FROM parking_events pe

            JOIN event_types et
                ON et.id = pe.event_type_fk

            WHERE et.code = 'EXIT'
              AND pe.timestamp >= :start_of_day
              AND pe.timestamp < :start_of_next_day
        """),
        {
            "start_of_day": start_of_day,
            "start_of_next_day": start_of_next_day,
        },
    ).scalar() or 0

    # =========================================================
    # 5. CURRENTLY PARKED
    # =========================================================

    currently_parked = db.execute(
    text("""
        SELECT COUNT(*)
        FROM parking_slot_state
        WHERE status = 'occupied'
    """)
    ).scalar() or 0

    # =========================================================
    # 6. AVERAGE COMPLETED PARKING DURATION
    # =========================================================

    average_duration = db.execute(
        text("""
            SELECT AVG(duration_seconds)
            FROM parking_sessions

            WHERE duration_seconds IS NOT NULL
        """)
    ).scalar()

    if average_duration is not None:
        average_parking_duration_minutes = round(
            float(average_duration) / 60,
            2,
        )
    else:
        average_parking_duration_minutes = 0

    # =========================================================
    # 7. RESPONSE
    # =========================================================

    return {
        "status": "success",

        "total_slots": total_slots,

        "occupied_slots": occupied_slots,

        "available_slots": available_slots,

        "total_entries_today": int(entries),

        "total_exits_today": int(exits),

        "currently_parked": int(currently_parked),

        "average_parking_duration_minutes":
            average_parking_duration_minutes,
    }