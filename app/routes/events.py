from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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
    """
    Receive a vehicle event from Service 1.

    Normalized relationships:

        event_types
            ↓
        parking_events.event_type_fk

        parking_areas
            ↓
        cameras.parking_area_fk

        vehicle_types
            ↓
        vehicles.vehicle_type_fk

        vehicle_colors
            ↓
        vehicles.color_fk

        vehicles
            ↓
        parking_events.vehicle_fk

        vehicles
            ↓
        parking_sessions.vehicle_fk

        vehicles
            ↓
        parking_slot_state.vehicle_fk

    Service 1 payload is NOT changed.

    captured_at:
        Actual camera/NVR capture time when provided.
        Falls back to timestamp when captured_at is absent.

    Idempotency:
        The same event is processed only once.

        We DO NOT deduplicate by plate alone because
        the same vehicle can legitimately have:

            ENTRY -> EXIT -> ENTRY -> EXIT

        The database unique index provides final protection
        against concurrent duplicate requests.
    """

    try:

        # =====================================================
        # 0. RESOLVE AUTHORITATIVE EVENT TIME
        # =====================================================

        # captured_at = actual camera/NVR capture time.
        # timestamp   = backward-compatible fallback.
        event_time = event.captured_at or event.timestamp

        # =====================================================
        # 1. RESOLVE EVENT TYPE
        # =====================================================

        event_type_result = db.execute(
            text("""
                SELECT id
                FROM event_types
                WHERE code = :event_type
                LIMIT 1
            """),
            {
                "event_type": event.event_type,
            },
        ).first()

        if not event_type_result:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported event type: {event.event_type}",
            )

        event_type_fk = event_type_result[0]

        # =====================================================
        # 2. RESOLVE PARKING AREA
        # =====================================================

        area_result = db.execute(
            text("""
                SELECT id
                FROM parking_areas
                WHERE area_code = :area_code
                LIMIT 1
            """),
            {
                "area_code": event.parking_area_id,
            },
        ).first()

        if not area_result:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown parking area: {event.parking_area_id}",
            )

        parking_area_fk = area_result[0]

        # =====================================================
        # 3. RESOLVE VEHICLE TYPE
        # =====================================================

        vehicle_type_fk = None

        if event.vehicle.type:

            type_result = db.execute(
                text("""
                    SELECT id
                    FROM vehicle_types
                    WHERE code = :code
                    LIMIT 1
                """),
                {
                    "code": event.vehicle.type,
                },
            ).first()

            if not type_result:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unknown vehicle type: "
                        f"{event.vehicle.type}"
                    ),
                )

            vehicle_type_fk = type_result[0]

        # =====================================================
        # 4. RESOLVE VEHICLE COLOR
        # =====================================================

        color_fk = None

        if event.vehicle.color:

            color_result = db.execute(
                text("""
                    SELECT id
                    FROM vehicle_colors
                    WHERE code = :code
                    LIMIT 1
                """),
                {
                    "code": event.vehicle.color,
                },
            ).first()

            if not color_result:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unknown vehicle color: "
                        f"{event.vehicle.color}"
                    ),
                )

            color_fk = color_result[0]

        # =====================================================
        # 5. READ SLOT
        # =====================================================

        slot_id = (
            event.parking.slot_id
            if event.parking
            else None
        )

        # =====================================================
        # 5.5. IDEMPOTENCY CHECK
        # =====================================================
        #
        # IMPORTANT:
        #
        # We do NOT deduplicate by plate alone.
        #
        # A legitimate vehicle lifecycle can be:
        #
        # ENTRY 10:00
        # EXIT 11:00
        # ENTRY 14:00
        # EXIT 15:00
        #
        # These are four different events.
        #
        # The same exact event, however, can be resent by
        # Service 1 because of network retries.
        #
        # The fields below match the database unique index:
        #
        # event_type
        # camera_id
        # track_id
        # slot_id
        # timestamp
        # plate_number
        #
        # IS NOT DISTINCT FROM allows NULL values to compare
        # safely as equal.
        # =====================================================

        existing_event = db.execute(
            text("""
                SELECT id
                FROM parking_events
                WHERE event_type = :event_type
                  AND camera_id = :camera_id
                  AND track_id IS NOT DISTINCT FROM :track_id
                  AND slot_id IS NOT DISTINCT FROM :slot_id
                  AND timestamp = :timestamp
                  AND plate_number IS NOT DISTINCT FROM :plate_number
                LIMIT 1
            """),
            {
                "event_type": event.event_type,
                "camera_id": event.camera_id,
                "track_id": event.track_id,
                "slot_id": slot_id,
                "timestamp": event.timestamp,
                "plate_number": event.vehicle.plate,
            },
        ).scalar()

        if existing_event is not None:

            print(
                f"[IDEMPOTENCY] Duplicate event ignored: "
                f"event_id={existing_event}, "
                f"type={event.event_type}, "
                f"camera={event.camera_id}, "
                f"track={event.track_id}, "
                f"slot={slot_id}, "
                f"plate={event.vehicle.plate}",
                flush=True,
            )

            return {
                "status": "duplicate",
                "message": "Event already processed",
                "event_id": existing_event,
                "event_type": event.event_type,
                "camera_id": event.camera_id,
                "parking_area_id": event.parking_area_id,
                "slot_id": slot_id,
                "plate": event.vehicle.plate,
            }

        # =====================================================
        # 6. ENSURE CAMERA EXISTS
        # =====================================================

        # cameras.parking_area_id is currently retained as
        # a compatibility column.
        #
        # parking_area_fk is the normalized relationship.

        db.execute(
            text("""
                INSERT INTO cameras (
                    camera_id,
                    parking_area_id,
                    parking_area_fk,
                    name
                )
                VALUES (
                    :camera_id,
                    :parking_area_id,
                    :parking_area_fk,
                    :camera_name
                )
                ON CONFLICT (camera_id)
                DO UPDATE SET
                    parking_area_id = EXCLUDED.parking_area_id,
                    parking_area_fk = EXCLUDED.parking_area_fk,
                    updated_at = NOW()
            """),
            {
                "camera_id": event.camera_id,
                "parking_area_id": event.parking_area_id,
                "parking_area_fk": parking_area_fk,
                "camera_name": f"Camera {event.camera_id}",
            },
        )

        # =====================================================
        # 7. ENSURE PARKING SLOT
        # =====================================================

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

            # =================================================
            # 8. ENSURE SLOT STATE
            # =================================================

            db.execute(
                text("""
                    INSERT INTO parking_slot_state (
                        camera_id,
                        slot_id,
                        parking_area_id,
                        status,
                        plate_number,
                        vehicle_fk
                    )
                    VALUES (
                        :camera_id,
                        :slot_id,
                        :parking_area_id,
                        'available',
                        NULL,
                        NULL
                    )
                    ON CONFLICT (camera_id, slot_id)
                    DO NOTHING
                """),
                {
                    "camera_id": event.camera_id,
                    "slot_id": slot_id,
                    "parking_area_id": event.parking_area_id,
                },
            )

        # =====================================================
        # 9. RESOLVE / CREATE VEHICLE
        # =====================================================

        plate = event.vehicle.plate
        vehicle_id = None

        if plate:

            vehicle_result = db.execute(
                text("""
                    SELECT
                        id,
                        vehicle_type_fk,
                        color_fk
                    FROM vehicles
                    WHERE plate_number = :plate
                    LIMIT 1
                """),
                {
                    "plate": plate,
                },
            ).mappings().first()

            # -------------------------------------------------
            # EXISTING VEHICLE
            # -------------------------------------------------

            if vehicle_result:

                vehicle_id = vehicle_result["id"]

                db.execute(
                    text("""
                        UPDATE vehicles
                        SET
                            vehicle_type_fk =
                                COALESCE(
                                    :vehicle_type_fk,
                                    vehicle_type_fk
                                ),
                            color_fk =
                                COALESCE(
                                    :color_fk,
                                    color_fk
                                ),
                            last_seen_at = :timestamp,
                            updated_at = NOW()
                        WHERE id = :vehicle_id
                    """),
                    {
                        "vehicle_id": vehicle_id,
                        "vehicle_type_fk": vehicle_type_fk,
                        "color_fk": color_fk,
                        "timestamp": event_time,
                    },
                )

            # -------------------------------------------------
            # NEW VEHICLE
            # -------------------------------------------------

            else:

                vehicle_insert = db.execute(
                    text("""
                        INSERT INTO vehicles (
                            plate_number,
                            vehicle_type_fk,
                            color_fk,
                            first_seen_at,
                            last_seen_at,
                            vehicle_type,
                            color
                        )
                        VALUES (
                            :plate,
                            :vehicle_type_fk,
                            :color_fk,
                            :timestamp,
                            :timestamp,
                            :vehicle_type,
                            :color
                        )
                        RETURNING id
                    """),
                    {
                        "plate": plate,
                        "vehicle_type_fk": vehicle_type_fk,
                        "color_fk": color_fk,
                        "timestamp": event_time,
                        "vehicle_type": event.vehicle.type,
                        "color": event.vehicle.color,
                    },
                )

                vehicle_id = vehicle_insert.scalar_one()

        # =====================================================
        # 10. SAVE EVENT
        # =====================================================

        event_query = text("""
            INSERT INTO parking_events (
                event_type,
                event_type_fk,
                camera_id,
                parking_area_id,
                track_id,
                timestamp,
                captured_at,

                vehicle_type,
                vehicle_type_confidence,

                color,
                color_confidence,

                plate_number,
                plate_confidence,

                slot_id,
                slot_confidence,

                vehicle_snapshot,
                plate_snapshot,

                vehicle_fk,

                observed_vehicle_type,
                observed_color,
                observed_plate_number
            )
            VALUES (
                :event_type,
                :event_type_fk,
                :camera_id,
                :parking_area_id,
                :track_id,
                :timestamp,
                :captured_at,

                :vehicle_type,
                :vehicle_type_confidence,

                :color,
                :color_confidence,

                :plate_number,
                :plate_confidence,

                :slot_id,
                :slot_confidence,

                :vehicle_snapshot,
                :plate_snapshot,

                :vehicle_fk,

                :observed_vehicle_type,
                :observed_color,
                :observed_plate_number
            )
            RETURNING id
        """)

        try:

            result = db.execute(
                event_query,
                {
                    # Compatibility
                    "event_type": event.event_type,
                    "parking_area_id": event.parking_area_id,

                    # Normalized
                    "event_type_fk": event_type_fk,
                    "vehicle_fk": vehicle_id,

                    # Camera / tracking
                    "camera_id": event.camera_id,
                    "track_id": event.track_id,
                    "timestamp": event.timestamp,
                    "captured_at": event_time,

                    # Vehicle observation
                    "vehicle_type": event.vehicle.type,
                    "vehicle_type_confidence": (
                        event.vehicle.type_confidence
                    ),

                    "color": event.vehicle.color,
                    "color_confidence": (
                        event.vehicle.color_confidence
                    ),

                    "plate_number": event.vehicle.plate,
                    "plate_confidence": (
                        event.vehicle.plate_confidence
                    ),

                    # Parking
                    "slot_id": slot_id,
                    "slot_confidence": (
                        event.parking.slot_confidence
                        if event.parking
                        else None
                    ),

                    # Snapshots
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

                    # Audit
                    "observed_vehicle_type": event.vehicle.type,
                    "observed_color": event.vehicle.color,
                    "observed_plate_number": event.vehicle.plate,
                },
            )

            event_id = result.scalar_one()

        except IntegrityError:

            # =================================================
            # CONCURRENT DUPLICATE
            # =================================================
            #
            # Two identical requests can arrive at almost
            # exactly the same time.
            #
            # The application-level check above cannot fully
            # protect against that race.
            #
            # PostgreSQL unique index is the final protection.
            # =================================================

            db.rollback()

            existing_event = db.execute(
                text("""
                    SELECT id
                    FROM parking_events
                    WHERE event_type = :event_type
                      AND camera_id = :camera_id
                      AND track_id IS NOT DISTINCT FROM :track_id
                      AND slot_id IS NOT DISTINCT FROM :slot_id
                      AND timestamp = :timestamp
                      AND plate_number IS NOT DISTINCT FROM :plate_number
                    LIMIT 1
                """),
                {
                    "event_type": event.event_type,
                    "camera_id": event.camera_id,
                    "track_id": event.track_id,
                    "slot_id": slot_id,
                    "timestamp": event.timestamp,
                    "plate_number": event.vehicle.plate,
                },
            ).scalar()

            if existing_event is not None:

                print(
                    f"[IDEMPOTENCY] Concurrent duplicate ignored: "
                    f"event_id={existing_event}",
                    flush=True,
                )

                return {
                    "status": "duplicate",
                    "message": "Event already processed",
                    "event_id": existing_event,
                    "event_type": event.event_type,
                    "camera_id": event.camera_id,
                    "parking_area_id": event.parking_area_id,
                    "slot_id": slot_id,
                    "plate": event.vehicle.plate,
                }

            raise

        # =====================================================
        # 11. ENTRY
        # =====================================================

        if event.event_type == "ENTRY":

            if not plate:

                db.rollback()

                raise HTTPException(
                    status_code=400,
                    detail="ENTRY event requires vehicle plate",
                )

            if not slot_id:

                db.rollback()

                raise HTTPException(
                    status_code=400,
                    detail="ENTRY event requires parking.slot_id",
                )

            if vehicle_id is None:

                db.rollback()

                raise HTTPException(
                    status_code=400,
                    detail=(
                        "ENTRY event requires "
                        "a resolvable vehicle"
                    ),
                )

            # -------------------------------------------------
            # CREATE SESSION
            # -------------------------------------------------

            session_result = db.execute(
                text("""
                    INSERT INTO parking_sessions (
                        plate_number,
                        vehicle_type,
                        color,

                        vehicle_fk,

                        camera_id,
                        slot_id,
                        entry_time,
                        status,
                        entry_screenshot_url
                    )
                    VALUES (
                        :plate_number,
                        :vehicle_type,
                        :color,

                        :vehicle_fk,

                        :camera_id,
                        :slot_id,
                        :entry_time,
                        'parked',
                        :snapshot
                    )
                    RETURNING id
                """),
                {
                    "plate_number": plate,
                    "vehicle_type": event.vehicle.type,
                    "color": event.vehicle.color,
                    "vehicle_fk": vehicle_id,
                    "camera_id": event.camera_id,
                    "slot_id": slot_id,
                    "entry_time": event_time,
                    "snapshot": (
                        event.snapshot.vehicle
                        if event.snapshot
                        else None
                    ),
                },
            )

            session_id = session_result.scalar_one()

            # -------------------------------------------------
            # OCCUPY SLOT
            # -------------------------------------------------

            slot_update = db.execute(
                text("""
                    UPDATE parking_slot_state
                    SET
                        status = 'occupied',
                        vehicle_fk = :vehicle_id,
                        plate_number = :plate_number,
                        track_id = :track_id,
                        occupied_since = :timestamp,
                        updated_at = NOW()
                    WHERE camera_id = :camera_id
                      AND slot_id = :slot_id
                """),
                {
                    "vehicle_id": vehicle_id,
                    "plate_number": plate,
                    "track_id": event.track_id,
                    "timestamp": event_time,
                    "camera_id": event.camera_id,
                    "slot_id": slot_id,
                },
            )

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

            # -------------------------------------------------
            # COMMIT
            # -------------------------------------------------

            db.commit()

            # -------------------------------------------------
            # WEBSOCKET
            # -------------------------------------------------

            await parking_connections.broadcast({
                "type": "PARKING_UPDATED",
                "event_type": "ENTRY",
                "camera_id": event.camera_id,
                "parking_area_id": event.parking_area_id,
                "slot_id": slot_id,
                "plate": plate,
                "vehicle_id": vehicle_id,
                "session_id": session_id,
                "timestamp": event_time.isoformat(),
                "captured_at": event_time.isoformat(),
            })

            return {
                "status": "accepted",
                "event_id": event_id,
                "event_type": "ENTRY",
                "session_id": session_id,
                "vehicle_id": vehicle_id,
                "plate": plate,
                "camera_id": event.camera_id,
                "parking_area_id": event.parking_area_id,
                "slot_id": slot_id,
            }

        # =====================================================
        # 12. EXIT
        # =====================================================

        elif event.event_type == "EXIT":

            if not plate:

                db.rollback()

                raise HTTPException(
                    status_code=400,
                    detail="EXIT event requires vehicle plate",
                )

            if not slot_id:

                db.rollback()

                raise HTTPException(
                    status_code=400,
                    detail="EXIT event requires parking.slot_id",
                )

            # -------------------------------------------------
            # UNKNOWN VEHICLE
            # -------------------------------------------------

            if vehicle_id is None:

                db.commit()

                return {
                    "status": "accepted",
                    "event_id": event_id,
                    "event_type": "EXIT",
                    "message": "Vehicle not found",
                    "plate": plate,
                    "camera_id": event.camera_id,
                    "slot_id": slot_id,
                }

            # -------------------------------------------------
            # FIND OPEN SESSION
            # -------------------------------------------------

            session = db.execute(
                text("""
                    SELECT
                        id,
                        vehicle_fk,
                        entry_time,
                        slot_id,
                        camera_id
                    FROM parking_sessions
                    WHERE vehicle_fk = :vehicle_id
                      AND camera_id = :camera_id
                      AND slot_id = :slot_id
                      AND status = 'parked'
                    ORDER BY entry_time DESC
                    LIMIT 1
                """),
                {
                    "vehicle_id": vehicle_id,
                    "camera_id": event.camera_id,
                    "slot_id": slot_id,
                },
            ).mappings().first()

            # -------------------------------------------------
            # NO OPEN SESSION
            # -------------------------------------------------

            if not session:

                db.commit()

                return {
                    "status": "accepted",
                    "event_id": event_id,
                    "event_type": "EXIT",
                    "message": "No open parking session found",
                    "plate": plate,
                    "camera_id": event.camera_id,
                    "slot_id": slot_id,
                }

            # -------------------------------------------------
            # CALCULATE DURATION
            # -------------------------------------------------

            duration_result = db.execute(
                text("""
                    SELECT EXTRACT(
                        EPOCH FROM (
                            :exit_time - :entry_time
                        )
                    )
                """),
                {
                    "exit_time": event_time,
                    "entry_time": session["entry_time"],
                },
            )

            duration_seconds = int(
                duration_result.scalar() or 0
            )

            # -------------------------------------------------
            # CLOSE SESSION
            # -------------------------------------------------

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
                    "exit_time": event_time,
                    "duration": duration_seconds,
                    "snapshot": (
                        event.snapshot.vehicle
                        if event.snapshot
                        else None
                    ),
                    "session_id": session["id"],
                },
            )

            # -------------------------------------------------
            # FREE SLOT
            # -------------------------------------------------

            slot_update = db.execute(
                text("""
                    UPDATE parking_slot_state
                    SET
                        status = 'available',
                        vehicle_fk = NULL,
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
                        f"{event.camera_id}/"
                        f"{session['slot_id']} "
                        f"could not be marked available"
                    ),
                )

            # -------------------------------------------------
            # UPDATE VEHICLE
            # -------------------------------------------------

            db.execute(
                text("""
                    UPDATE vehicles
                    SET
                        last_seen_at = :timestamp,
                        updated_at = NOW()
                    WHERE id = :vehicle_id
                """),
                {
                    "vehicle_id": vehicle_id,
                    "timestamp": event_time,
                },
            )

            # -------------------------------------------------
            # COMMIT
            # -------------------------------------------------

            db.commit()

            # -------------------------------------------------
            # WEBSOCKET
            # -------------------------------------------------

            await parking_connections.broadcast({
                "type": "PARKING_UPDATED",
                "event_type": "EXIT",
                "camera_id": event.camera_id,
                "parking_area_id": event.parking_area_id,
                "slot_id": session["slot_id"],
                "plate": plate,
                "vehicle_id": vehicle_id,
                "session_id": session["id"],
                "duration_seconds": duration_seconds,
                "timestamp": event_time.isoformat(),
                "captured_at": event_time.isoformat(),
            })

            return {
                "status": "accepted",
                "event_id": event_id,
                "event_type": "EXIT",
                "session_id": session["id"],
                "vehicle_id": vehicle_id,
                "plate": plate,
                "duration_seconds": duration_seconds,
                "duration_minutes": round(
                    duration_seconds / 60,
                    2,
                ),
                "camera_id": event.camera_id,
                "slot_id": session["slot_id"],
            }

        # =====================================================
        # 13. OTHER VALID EVENT TYPES
        # =====================================================

        else:

            db.commit()

            await parking_connections.broadcast({
                "type": "PARKING_UPDATED",
                "event_type": event.event_type,
                "camera_id": event.camera_id,
                "parking_area_id": event.parking_area_id,
                "slot_id": slot_id,
                "timestamp": event_time.isoformat(),
                "captured_at": event_time.isoformat(),
            })

            return {
                "status": "accepted",
                "event_id": event_id,
                "event_type": event.event_type,
                "message": "Event stored",
            }

    except HTTPException:

        db.rollback()
        raise

    except Exception as error:

        db.rollback()

        print(
            f"[EVENT ERROR] "
            f"{type(error).__name__}: {error}",
            flush=True,
        )

        raise