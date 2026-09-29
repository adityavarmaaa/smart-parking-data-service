from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.dependencies import get_db
from app.schemas.vehicle_event import VehicleEvent

from app.realtime import parking_connections


router = APIRouter(
    prefix="/internal/events",
    tags=["Vehicle Events"],
)


@router.post("/vehicle")
async def receive_vehicle_event(
    event: VehicleEvent,
    db: Session = Depends(get_db),
):

    # =========================================================
    # 1. SAVE RAW EVENT
    # =========================================================

    event_query = text("""
        INSERT INTO parking_events (
            event_type,
            camera_id,
            parking_area_id,
            track_id,
            timestamp,
            vehicle_type,
            vehicle_type_confidence,
            color,
            color_confidence,
            plate_number,
            plate_confidence,
            slot_id,
            slot_confidence,
            vehicle_snapshot,
            plate_snapshot
        )
        VALUES (
            :event_type,
            :camera_id,
            :parking_area_id,
            :track_id,
            :timestamp,
            :vehicle_type,
            :vehicle_type_confidence,
            :color,
            :color_confidence,
            :plate_number,
            :plate_confidence,
            :slot_id,
            :slot_confidence,
            :vehicle_snapshot,
            :plate_snapshot
        )
        RETURNING id
    """)

    result = db.execute(
        event_query,
        {
            "event_type": event.event_type,
            "camera_id": event.camera_id,
            "parking_area_id": event.parking_area_id,
            "track_id": event.track_id,
            "timestamp": event.timestamp,

            "vehicle_type": event.vehicle.type,
            "vehicle_type_confidence": event.vehicle.type_confidence,

            "color": event.vehicle.color,
            "color_confidence": event.vehicle.color_confidence,

            "plate_number": event.vehicle.plate,
            "plate_confidence": event.vehicle.plate_confidence,

            "slot_id": (
                event.parking.slot_id
                if event.parking
                else None
            ),

            "slot_confidence": (
                event.parking.slot_confidence
                if event.parking
                else None
            ),

            "vehicle_snapshot": (
                event.snapshot.vehicle
                if event.snapshot
                else None
            ),

            "plate_snapshot": (
                event.snapshot.plate
                if event.snapshot
                else None
            ),
        },
    )

    event_id = result.scalar_one()

    # =========================================================
    # ENTRY
    # =========================================================

    if event.event_type == "ENTRY":

        plate = event.vehicle.plate

        if not plate:
            raise HTTPException(
                status_code=400,
                detail="ENTRY event requires vehicle plate",
            )

        # -----------------------------------------------------
        # 2. ENSURE CAMERA EXISTS
        # -----------------------------------------------------

        db.execute(
            text("""
                INSERT INTO cameras (
                    camera_id,
                    parking_area_id,
                    name
                )
                VALUES (
                    :camera_id,
                    :parking_area_id,
                    :camera_name
                )
                ON CONFLICT (camera_id)
                DO UPDATE SET
                    parking_area_id = EXCLUDED.parking_area_id,
                    updated_at = NOW()
            """),
            {
                "camera_id": event.camera_id,
                "parking_area_id": event.parking_area_id,
                "camera_name": f"Camera {event.camera_id}",
            },
        )

        # -----------------------------------------------------
        # 3. ENSURE RUNTIME-DISCOVERED SLOT EXISTS
        # -----------------------------------------------------

        slot_id = (
            event.parking.slot_id
            if event.parking
            else None
        )

        if slot_id:

            db.execute(
                text("""
                    INSERT INTO parking_slots (
                        camera_id,
                        slot_id,
                        polygon,
                        status
                    )
                    VALUES (
                        :camera_id,
                        :slot_id,
                        NULL,
                        'available'
                    )
                    ON CONFLICT (camera_id, slot_id)
                    DO NOTHING
                """),
                {
                    "camera_id": event.camera_id,
                    "slot_id": slot_id,
                },
            )

            # -------------------------------------------------
            # 4. ENSURE LIVE SLOT STATE EXISTS
            # -------------------------------------------------

            db.execute(
                text("""
                    INSERT INTO parking_slot_state (
                        camera_id,
                        slot_id,
                        parking_area_id,
                        status
                    )
                    VALUES (
                        :camera_id,
                        :slot_id,
                        :parking_area_id,
                        'available'
                    )
                    ON CONFLICT (camera_id, slot_id)
                    DO UPDATE SET
                        parking_area_id = EXCLUDED.parking_area_id
                """),
                {
                    "camera_id": event.camera_id,
                    "slot_id": slot_id,
                    "parking_area_id": event.parking_area_id,
                },
            )

        # -----------------------------------------------------
        # 5. CREATE / UPDATE VEHICLE
        # -----------------------------------------------------

        vehicle_result = db.execute(
            text("""
                SELECT id
                FROM vehicles
                WHERE plate_number = :plate
                LIMIT 1
            """),
            {
                "plate": plate,
            },
        ).first()

        if vehicle_result:

            db.execute(
                text("""
                    UPDATE vehicles
                    SET
                        vehicle_type = :vehicle_type,
                        color = :color,
                        last_seen_at = :timestamp,
                        updated_at = NOW()
                    WHERE plate_number = :plate
                """),
                {
                    "plate": plate,
                    "vehicle_type": event.vehicle.type,
                    "color": event.vehicle.color,
                    "timestamp": event.timestamp,
                },
            )

        else:

            db.execute(
                text("""
                    INSERT INTO vehicles (
                        plate_number,
                        vehicle_type,
                        color,
                        first_seen_at,
                        last_seen_at
                    )
                    VALUES (
                        :plate,
                        :vehicle_type,
                        :color,
                        :timestamp,
                        :timestamp
                    )
                """),
                {
                    "plate": plate,
                    "vehicle_type": event.vehicle.type,
                    "color": event.vehicle.color,
                    "timestamp": event.timestamp,
                },
            )

        # -----------------------------------------------------
        # 6. CREATE PARKING SESSION
        # -----------------------------------------------------

        session_result = db.execute(
            text("""
                INSERT INTO parking_sessions (
                    plate_number,
                    vehicle_type,
                    color,
                    camera_id,
                    slot_id,
                    entry_time,
                    status,
                    entry_screenshot_url
                )
                VALUES (
                    :plate,
                    :vehicle_type,
                    :color,
                    :camera_id,
                    :slot_id,
                    :entry_time,
                    'parked',
                    :snapshot
                )
                RETURNING id
            """),
            {
                "plate": plate,
                "vehicle_type": event.vehicle.type,
                "color": event.vehicle.color,
                "camera_id": event.camera_id,
                "slot_id": slot_id,
                "entry_time": event.timestamp,
                "snapshot": (
                    event.snapshot.vehicle
                    if event.snapshot
                    else None
                ),
            },
        )

        session_id = session_result.scalar_one()

        # -----------------------------------------------------
        # 7. MARK SLOT OCCUPIED
        # -----------------------------------------------------

        if slot_id:

            slot_update = db.execute(
                text("""
                    UPDATE parking_slot_state
                    SET
                        status = 'occupied',
                        plate_number = :plate,
                        track_id = :track_id,
                        occupied_since = :timestamp,
                        updated_at = NOW()
                    WHERE camera_id = :camera_id
                      AND slot_id = :slot_id
                """),
                {
                    "plate": plate,
                    "track_id": event.track_id,
                    "timestamp": event.timestamp,
                    "camera_id": event.camera_id,
                    "slot_id": slot_id,
                },
            )

            # Safety check:
            # ENTRY must not be accepted if the slot
            # could not actually be marked occupied.

            if slot_update.rowcount != 1:

                db.rollback()

                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Parking slot "
                        f"{event.camera_id}/{slot_id} "
                        f"could not be updated"
                    ),
                )

        # =====================================================
        # DATABASE COMMIT
        # =====================================================

        db.commit()

        # =====================================================
        # REAL-TIME DASHBOARD NOTIFICATION
        # =====================================================

        await parking_connections.broadcast({
            "type": "PARKING_UPDATED",
            "event_type": "ENTRY",
            "camera_id": event.camera_id,
            "parking_area_id": event.parking_area_id,
            "slot_id": slot_id,
            "plate": plate,
            "timestamp": event.timestamp.isoformat(),
        })

        return {
            "status": "accepted",
            "event_id": event_id,
            "event_type": "ENTRY",
            "session_id": session_id,
            "plate": plate,
            "camera_id": event.camera_id,
            "parking_area_id": event.parking_area_id,
            "slot_id": slot_id,
        }

    # =========================================================
    # EXIT
    # =========================================================

    elif event.event_type == "EXIT":

        plate = event.vehicle.plate

        if not plate:
            raise HTTPException(
                status_code=400,
                detail="EXIT event requires vehicle plate",
            )

        # -----------------------------------------------------
        # 2. GET SLOT FROM EXIT EVENT
        # -----------------------------------------------------

        slot_id_from_event = (
            event.parking.slot_id
            if event.parking
            else None
        )

        if not slot_id_from_event:
            raise HTTPException(
                status_code=400,
                detail="EXIT event requires parking.slot_id",
            )

        # -----------------------------------------------------
        # 3. FIND OPEN PARKING SESSION
        # -----------------------------------------------------

        session = db.execute(
            text("""
                SELECT
                    id,
                    entry_time,
                    slot_id,
                    camera_id
                FROM parking_sessions
                WHERE plate_number = :plate
                  AND camera_id = :camera_id
                  AND slot_id = :slot_id
                  AND status = 'parked'
                ORDER BY entry_time DESC
                LIMIT 1
            """),
            {
                "plate": plate,
                "camera_id": event.camera_id,
                "slot_id": slot_id_from_event,
            },
        ).mappings().first()

        if not session:

            db.commit()

            return {
                "status": "accepted",
                "event_id": event_id,
                "event_type": "EXIT",
                "message": "No open parking session found",
                "plate": plate,
                "camera_id": event.camera_id,
                "slot_id": slot_id_from_event,
            }

        # -----------------------------------------------------
        # 4. CALCULATE DURATION
        # -----------------------------------------------------

        duration_result = db.execute(
            text("""
                SELECT EXTRACT(
                    EPOCH FROM (
                        :exit_time - :entry_time
                    )
                )
            """),
            {
                "exit_time": event.timestamp,
                "entry_time": session["entry_time"],
            },
        )

        duration_seconds = int(
            duration_result.scalar() or 0
        )

        # -----------------------------------------------------
        # 5. CLOSE SESSION
        # -----------------------------------------------------

        db.execute(
            text("""
                UPDATE parking_sessions
                SET
                    exit_time = :exit_time,
                    duration_seconds = :duration,
                    status = 'exited',
                    exit_screenshot_url = :snapshot,
                    updated_at = NOW()
                WHERE id = :session_id
            """),
            {
                "exit_time": event.timestamp,
                "duration": duration_seconds,
                "snapshot": (
                    event.snapshot.vehicle
                    if event.snapshot
                    else None
                ),
                "session_id": session["id"],
            },
        )

        # -----------------------------------------------------
        # 6. MARK SLOT AVAILABLE
        # -----------------------------------------------------

        slot_update = db.execute(
            text("""
                UPDATE parking_slot_state
                SET
                    status = 'available',
                    plate_number = NULL,
                    track_id = NULL,
                    occupied_since = NULL,
                    updated_at = NOW()
                WHERE camera_id = :camera_id
                  AND slot_id = :slot_id
            """),
            {
                "camera_id": event.camera_id,
                "slot_id": session["slot_id"],
            },
        )

        if slot_update.rowcount != 1:

            db.rollback()

            raise HTTPException(
                status_code=400,
                detail=(
                    f"Parking slot "
                    f"{event.camera_id}/{session['slot_id']} "
                    f"could not be marked available"
                ),
            )

        # -----------------------------------------------------
        # 7. UPDATE VEHICLE LAST SEEN
        # -----------------------------------------------------

        db.execute(
            text("""
                UPDATE vehicles
                SET
                    last_seen_at = :timestamp,
                    updated_at = NOW()
                WHERE plate_number = :plate
            """),
            {
                "plate": plate,
                "timestamp": event.timestamp,
            },
        )

        # =====================================================
        # DATABASE COMMIT
        # =====================================================

        db.commit()

        # =====================================================
        # REAL-TIME DASHBOARD NOTIFICATION
        # =====================================================

        await parking_connections.broadcast({
            "type": "PARKING_UPDATED",
            "event_type": "EXIT",
            "camera_id": event.camera_id,
            "slot_id": session["slot_id"],
            "plate": plate,
            "duration_seconds": duration_seconds,
            "timestamp": event.timestamp.isoformat(),
        })

        return {
            "status": "accepted",
            "event_id": event_id,
            "event_type": "EXIT",
            "session_id": session["id"],
            "plate": plate,
            "duration_seconds": duration_seconds,
            "duration_minutes": round(
                duration_seconds / 60,
                2,
            ),
            "camera_id": event.camera_id,
            "slot_id": session["slot_id"],
        }

    # =========================================================
    # OTHER EVENTS
    # =========================================================

    else:

        db.commit()

        await parking_connections.broadcast({
            "type": "PARKING_UPDATED",
            "event_type": event.event_type,
            "camera_id": event.camera_id,
            "parking_area_id": event.parking_area_id,
            "slot_id": (
                event.parking.slot_id
                if event.parking
                else None
            ),
            "timestamp": event.timestamp.isoformat(),
        })

        return {
            "status": "accepted",
            "event_id": event_id,
            "event_type": event.event_type,
            "message": "Event stored",
        }