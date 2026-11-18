

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.dependencies import get_db
from app.schemas.vehicle_event import VehicleEvent
from app.realtime import parking_connections
from app.security import encrypt_text, plate_hmac, plate_hmac_candidates
from app.auth import require_s2_api_key


router = APIRouter(
    prefix="/internal/events",
    tags=["Vehicle Events"],
)


# =========================================================
# EVENT IDEMPOTENCY
# =========================================================

def _find_existing_event(
    db,
    event,
    vehicle_uid,
    plate_fingerprint,
    plate_fingerprints,
    plate,
    slot_id,
):
    return db.execute(
        text("""
            SELECT id
            FROM parking_events
            WHERE event_type = :event_type
              AND camera_id = :camera_id
              AND parking_area_id = :parking_area_id
              AND track_id IS NOT DISTINCT FROM :track_id
              AND slot_id IS NOT DISTINCT FROM :slot_id
              AND timestamp = :timestamp
              AND (
                  (
                      CAST(:vehicle_uid AS TEXT) IS NOT NULL
                      AND vehicle_uid = CAST(:vehicle_uid AS TEXT)
                  )
                  OR (
                      CAST(:vehicle_uid AS TEXT) IS NULL
                      AND (
                          (
                              cardinality(CAST(:plate_hmacs AS TEXT[])) > 0
                              AND plate_number_hmac =
                                  ANY(CAST(:plate_hmacs AS TEXT[]))
                          )
                      )
                  )
              )
            LIMIT 1
        """),
        {
            "event_type": event.event_type,
            "camera_id": event.camera_id,
            "parking_area_id": event.parking_area_id,
            "track_id": event.track_id,
            "slot_id": slot_id,
            "timestamp": event.timestamp,
            "vehicle_uid": vehicle_uid,
            "plate_hmacs": plate_fingerprints,
        },
    ).scalar()


# =========================================================
# FIND ACTIVE PARKING SESSION
# =========================================================

def _active_session(
    db,
    parking_area_id,
    camera_id,
    vehicle_uid,
    vehicle_id,
):
    if vehicle_uid:
        return db.execute(
            text("""
                SELECT id, slot_id, vehicle_uid, vehicle_fk
                FROM parking_sessions
                WHERE parking_area_id = :area_id
                  AND camera_id = :camera_id
                  AND vehicle_uid = :vehicle_uid
                  AND status = 'parked'
                ORDER BY entry_time DESC
                LIMIT 1
            """),
            {
                "area_id": parking_area_id,
                "camera_id": camera_id,
                "vehicle_uid": vehicle_uid,
            },
        ).mappings().first()

    if vehicle_id is not None:
        return db.execute(
            text("""
                SELECT id, slot_id, vehicle_uid, vehicle_fk
                FROM parking_sessions
                WHERE parking_area_id = :area_id
                  AND camera_id = :camera_id
                  AND vehicle_fk = :vehicle_id
                  AND status = 'parked'
                ORDER BY entry_time DESC
                LIMIT 1
            """),
            {
                "area_id": parking_area_id,
                "camera_id": camera_id,
                "vehicle_id": vehicle_id,
            },
        ).mappings().first()

    return None


# =========================================================
# RECEIVE VEHICLE EVENT
# =========================================================

@router.post("/vehicle")
async def receive_vehicle_event(
    event: VehicleEvent,
    _: None = Depends(require_s2_api_key),
    db: Session = Depends(get_db),
):
    try:
        # =================================================
        # 0. IDENTITY AND PLATE SECURITY
        # =================================================

        event_time = event.captured_at or event.timestamp

        vehicle_uid = (
            event.vehicle_uid.strip()
            if event.vehicle_uid and event.vehicle_uid.strip()
            else None
        )

        plate = (
            event.vehicle.plate.strip().upper()
            if event.vehicle.plate and event.vehicle.plate.strip()
            else None
        )

        # Never store the plate in plaintext.
        plate_ciphertext = encrypt_text(plate) if plate else None
        plate_fingerprint = plate_hmac(plate) if plate else None
        plate_fingerprints = plate_hmac_candidates(plate) if plate else []

        slot_id = event.parking.slot_id if event.parking else None

        if event.event_type in ("ENTRY", "EXIT"):
            if not vehicle_uid and not plate:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{event.event_type} requires vehicle_uid "
                        "or a confirmed plate"
                    ),
                )

            if not slot_id:
                raise HTTPException(
                    status_code=400,
                    detail=f"{event.event_type} requires parking.slot_id",
                )

        # =================================================
        # 1. EVENT TYPE
        # =================================================

        event_type_fk = db.execute(
            text("""
                SELECT id
                FROM event_types
                WHERE code = :code
                LIMIT 1
            """),
            {"code": event.event_type},
        ).scalar()

        if event_type_fk is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported event type: {event.event_type}",
            )

        # =================================================
        # 2. PARKING AREA
        # =================================================

        parking_area_fk = db.execute(
            text("""
                SELECT id
                FROM parking_areas
                WHERE area_code = :area_code
                LIMIT 1
            """),
            {"area_code": event.parking_area_id},
        ).scalar()

        if parking_area_fk is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown parking area: {event.parking_area_id}",
            )

        # =================================================
        # 3. VEHICLE TYPE
        # =================================================

        vehicle_type_fk = None

        if event.vehicle.type:
            vehicle_type_fk = db.execute(
                text("""
                    SELECT id
                    FROM vehicle_types
                    WHERE code = :code
                    LIMIT 1
                """),
                {"code": event.vehicle.type},
            ).scalar()

            if vehicle_type_fk is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown vehicle type: {event.vehicle.type}",
                )

        # =================================================
        # 4. VEHICLE COLOR
        # =================================================

        color_fk = None

        if event.vehicle.color:
            color_fk = db.execute(
                text("""
                    SELECT id
                    FROM vehicle_colors
                    WHERE code = :code
                    LIMIT 1
                """),
                {"code": event.vehicle.color},
            ).scalar()

            if color_fk is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown vehicle color: {event.vehicle.color}",
                )

        # =================================================
        # 5. IDEMPOTENCY
        # =================================================

        existing_event = _find_existing_event(
            db,
            event,
            vehicle_uid,
            plate_fingerprint,
            plate_fingerprints,
            plate,
            slot_id,
        )

        if existing_event is not None:
            db.rollback()

            return {
                "status": "duplicate",
                "event_id": existing_event,
                "event_type": event.event_type,
                "camera_id": event.camera_id,
                "parking_area_id": event.parking_area_id,
                "slot_id": slot_id,
            }

        # =================================================
        # 6. ENSURE CAMERA
        # =================================================

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

        # =================================================
        # 7. ENSURE SLOT AND SLOT STATE
        # =================================================

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

            db.execute(
                text("""
                    INSERT INTO parking_slot_state (
                        camera_id,
                        slot_id,
                        parking_area_id,
                        status,
                        plate_number,
                        plate_number_ciphertext,
                        plate_number_hmac,
                        vehicle_fk,
                        vehicle_uid
                    )
                    VALUES (
                        :camera_id,
                        :slot_id,
                        :area_id,
                        'available',
                        NULL,
                        NULL,
                        NULL,
                        NULL,
                        NULL
                    )
                    ON CONFLICT (camera_id, slot_id)
                    DO NOTHING
                """),
                {
                    "camera_id": event.camera_id,
                    "slot_id": slot_id,
                    "area_id": event.parking_area_id,
                },
            )

        # =================================================
        # 8. RESOLVE / CREATE VEHICLE
        # =================================================

        vehicle_id = None

        if plate:
            vehicle_result = db.execute(
                text("""
                    SELECT id
                    FROM vehicles
                    WHERE plate_number_hmac = ANY(CAST(:plate_hmacs AS TEXT[]))
                    LIMIT 1
                """),
                {
                    "plate_hmacs": plate_fingerprints,
                },
            ).mappings().first()

            if vehicle_result:
                vehicle_id = vehicle_result["id"]

                db.execute(
                    text("""
                        UPDATE vehicles
                        SET
                            plate_number = NULL,
                            plate_number_ciphertext = :ciphertext,
                            plate_number_hmac = :plate_hmac,
                            vehicle_type_fk = COALESCE(
                                :vehicle_type_fk,
                                vehicle_type_fk
                            ),
                            color_fk = COALESCE(
                                :color_fk,
                                color_fk
                            ),
                            last_seen_at = :timestamp,
                            updated_at = NOW()
                        WHERE id = :vehicle_id
                    """),
                    {
                        "vehicle_id": vehicle_id,
                        "ciphertext": plate_ciphertext,
                        "plate_hmac": plate_fingerprint,
                        "vehicle_type_fk": vehicle_type_fk,
                        "color_fk": color_fk,
                        "timestamp": event_time,
                    },
                )

            else:
                vehicle_id = db.execute(
                    text("""
                        INSERT INTO vehicles (
                            plate_number,
                            plate_number_ciphertext,
                            plate_number_hmac,
                            vehicle_type_fk,
                            color_fk,
                            first_seen_at,
                            last_seen_at,
                            vehicle_type,
                            color
                        )
                        VALUES (
                            NULL,
                            :ciphertext,
                            :plate_hmac,
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
                        "ciphertext": plate_ciphertext,
                        "plate_hmac": plate_fingerprint,
                        "vehicle_type_fk": vehicle_type_fk,
                        "color_fk": color_fk,
                        "timestamp": event_time,
                        "vehicle_type": event.vehicle.type,
                        "color": event.vehicle.color,
                    },
                ).scalar_one()

        # =================================================
        # 9. ENTRY: CHECK EXISTING ACTIVE SESSION
        # =================================================

        if event.event_type == "ENTRY":
            active = _active_session(
                db,
                event.parking_area_id,
                event.camera_id,
                vehicle_uid,
                vehicle_id,
            )

            if active:
                db.rollback()

                if active["slot_id"] == slot_id:
                    return {
                        "status": "already_parked",
                        "message": (
                            "An active parking session already exists"
                        ),
                        "session_id": active["id"],
                        "slot_id": active["slot_id"],
                        "vehicle_uid": active["vehicle_uid"],
                    }

                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Vehicle already has an active session "
                        "in another slot"
                    ),
                )

            # Never overwrite a slot occupied by another vehicle.
            slot_state = db.execute(
                text("""
                    SELECT status, vehicle_uid, vehicle_fk
                    FROM parking_slot_state
                    WHERE camera_id = :camera_id
                      AND slot_id = :slot_id
                      AND parking_area_id = :area_id
                    FOR UPDATE
                """),
                {
                    "camera_id": event.camera_id,
                    "slot_id": slot_id,
                    "area_id": event.parking_area_id,
                },
            ).mappings().first()

            if not slot_state:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Parking slot state could not be initialized"
                    ),
                )

            if slot_state["status"] == "occupied":
                same_uid = (
                    vehicle_uid is not None
                    and slot_state["vehicle_uid"] == vehicle_uid
                )

                same_vehicle = (
                    vehicle_uid is None
                    and vehicle_id is not None
                    and slot_state["vehicle_fk"] == vehicle_id
                )

                if not (same_uid or same_vehicle):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Parking slot is already occupied "
                            "by another vehicle"
                        ),
                    )

        # =================================================
        # 10. SAVE PARKING EVENT
        # =================================================

        event_id = db.execute(
            text("""
                INSERT INTO parking_events (
                    event_type,
                    event_type_fk,
                    camera_id,
                    parking_area_id,
                    vehicle_uid,
                    track_id,
                    timestamp,
                    captured_at,
                    vehicle_type,
                    vehicle_type_confidence,
                    color,
                    color_confidence,
                    plate_number,
                    plate_number_ciphertext,
                    plate_number_hmac,
                    plate_confidence,
                    slot_id,
                    slot_confidence,
                    vehicle_snapshot,
                    plate_snapshot,
                    vehicle_fk,
                    observed_vehicle_type,
                    observed_color,
                    observed_plate_number,
                    observed_plate_number_ciphertext,
                    observed_plate_number_hmac
                )
                VALUES (
                    :event_type,
                    :event_type_fk,
                    :camera_id,
                    :area_id,
                    :vehicle_uid,
                    :track_id,
                    :timestamp,
                    :captured_at,
                    :vehicle_type,
                    :vehicle_type_confidence,
                    :color,
                    :color_confidence,
                    NULL,
                    :plate_ciphertext,
                    :plate_hmac,
                    :plate_confidence,
                    :slot_id,
                    :slot_confidence,
                    :vehicle_snapshot,
                    :plate_snapshot,
                    :vehicle_fk,
                    :observed_vehicle_type,
                    :observed_color,
                    NULL,
                    :observed_plate_ciphertext,
                    :observed_plate_hmac
                )
                RETURNING id
            """),
            {
                "event_type": event.event_type,
                "event_type_fk": event_type_fk,
                "camera_id": event.camera_id,
                "area_id": event.parking_area_id,
                "vehicle_uid": vehicle_uid,
                "track_id": event.track_id,
                "timestamp": event.timestamp,
                "captured_at": event_time,
                "vehicle_type": event.vehicle.type,
                "vehicle_type_confidence": (
                    event.vehicle.type_confidence
                ),
                "color": event.vehicle.color,
                "color_confidence": event.vehicle.color_confidence,
                "plate_ciphertext": plate_ciphertext,
                "plate_hmac": plate_fingerprint,
                "plate_confidence": event.vehicle.plate_confidence,
                "slot_id": slot_id,
                "slot_confidence": (
                    event.parking.slot_confidence
                    if event.parking else None
                ),
                "vehicle_snapshot": (
                    event.snapshot.vehicle
                    if event.snapshot else None
                ),
                "plate_snapshot": (
                    event.snapshot.plate
                    if event.snapshot else None
                ),
                "vehicle_fk": vehicle_id,
                "observed_vehicle_type": event.vehicle.type,
                "observed_color": event.vehicle.color,
                "observed_plate_ciphertext": plate_ciphertext,
                "observed_plate_hmac": plate_fingerprint,
            },
        ).scalar_one()

        # =================================================
        # 11. ENTRY: CREATE SESSION AND OCCUPY SLOT
        # =================================================

        if event.event_type == "ENTRY":
            session_id = db.execute(
                text("""
                    INSERT INTO parking_sessions (
                        plate_number,
                        plate_number_ciphertext,
                        plate_number_hmac,
                        vehicle_type,
                        color,
                        vehicle_fk,
                        vehicle_uid,
                        parking_area_id,
                        camera_id,
                        slot_id,
                        entry_time,
                        status,
                        entry_screenshot_url
                    )
                    VALUES (
                        NULL,
                        :ciphertext,
                        :plate_hmac,
                        :vehicle_type,
                        :color,
                        :vehicle_fk,
                        :vehicle_uid,
                        :area_id,
                        :camera_id,
                        :slot_id,
                        :entry_time,
                        'parked',
                        :snapshot
                    )
                    ON CONFLICT (
                        parking_area_id,
                        camera_id,
                        vehicle_uid
                    )
                    WHERE vehicle_uid IS NOT NULL
                      AND status = 'parked'
                    DO NOTHING
                    RETURNING id
                """),
                {
                    "ciphertext": plate_ciphertext,
                    "plate_hmac": plate_fingerprint,
                    "vehicle_type": event.vehicle.type,
                    "color": event.vehicle.color,
                    "vehicle_fk": vehicle_id,
                    "vehicle_uid": vehicle_uid,
                    "area_id": event.parking_area_id,
                    "camera_id": event.camera_id,
                    "slot_id": slot_id,
                    "entry_time": event_time,
                    "snapshot": (
                        event.snapshot.vehicle
                        if event.snapshot else None
                    ),
                },
            ).scalar()

            if session_id is None:
                db.rollback()

                active = _active_session(
                    db,
                    event.parking_area_id,
                    event.camera_id,
                    vehicle_uid,
                    vehicle_id,
                )

                if active:
                    if active["slot_id"] == slot_id:
                        return {
                            "status": "already_parked",
                            "session_id": active["id"],
                            "slot_id": active["slot_id"],
                            "vehicle_uid": active["vehicle_uid"],
                        }

                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Vehicle already has an active session "
                            "in another slot"
                        ),
                    )

                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Could not create parking session; "
                        "retry the event"
                    ),
                )

            slot_update = db.execute(
                text("""
                    UPDATE parking_slot_state
                    SET
                        status = 'occupied',
                        vehicle_fk = :vehicle_id,
                        vehicle_uid = :vehicle_uid,
                        plate_number = NULL,
                        plate_number_ciphertext = :ciphertext,
                        plate_number_hmac = :plate_hmac,
                        track_id = :track_id,
                        occupied_since = :timestamp,
                        updated_at = NOW()
                    WHERE camera_id = :camera_id
                      AND slot_id = :slot_id
                      AND parking_area_id = :area_id
                      AND status <> 'occupied'
                """),
                {
                    "vehicle_id": vehicle_id,
                    "vehicle_uid": vehicle_uid,
                    "ciphertext": plate_ciphertext,
                    "plate_hmac": plate_fingerprint,
                    "track_id": event.track_id,
                    "timestamp": event_time,
                    "camera_id": event.camera_id,
                    "slot_id": slot_id,
                    "area_id": event.parking_area_id,
                },
            )

            if slot_update.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Parking slot is occupied or could not be updated"
                    ),
                )

            db.commit()

            await parking_connections.broadcast({
                "type": "PARKING_UPDATED",
                "event_type": "ENTRY",
                "camera_id": event.camera_id,
                "parking_area_id": event.parking_area_id,
                "slot_id": slot_id,
                "vehicle_id": vehicle_id,
                "vehicle_uid": vehicle_uid,
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
                "vehicle_uid": vehicle_uid,
                "camera_id": event.camera_id,
                "parking_area_id": event.parking_area_id,
                "slot_id": slot_id,
            }

        # =================================================
        # 12. EXIT: FIND AND CLOSE ACTIVE SESSION
        # =================================================

        if event.event_type == "EXIT":

            # Use UID first. If there is no UID, use the
            # resolved vehicle database ID.
            if vehicle_uid:
                session = db.execute(
                    text("""
                        SELECT
                            id,
                            vehicle_fk,
                            vehicle_uid,
                            entry_time,
                            slot_id,
                            camera_id
                        FROM parking_sessions
                        WHERE parking_area_id = :area_id
                          AND camera_id = :camera_id
                          AND slot_id = :slot_id
                          AND status = 'parked'
                          AND vehicle_uid = :vehicle_uid
                        ORDER BY entry_time DESC
                        LIMIT 1
                        FOR UPDATE
                    """),
                    {
                        "area_id": event.parking_area_id,
                        "camera_id": event.camera_id,
                        "slot_id": slot_id,
                        "vehicle_uid": vehicle_uid,
                    },
                ).mappings().first()

            elif vehicle_id is not None:
                session = db.execute(
                    text("""
                        SELECT
                            id,
                            vehicle_fk,
                            vehicle_uid,
                            entry_time,
                            slot_id,
                            camera_id
                        FROM parking_sessions
                        WHERE parking_area_id = :area_id
                          AND camera_id = :camera_id
                          AND slot_id = :slot_id
                          AND status = 'parked'
                          AND vehicle_fk = :vehicle_id
                        ORDER BY entry_time DESC
                        LIMIT 1
                        FOR UPDATE
                    """),
                    {
                        "area_id": event.parking_area_id,
                        "camera_id": event.camera_id,
                        "slot_id": slot_id,
                        "vehicle_id": vehicle_id,
                    },
                ).mappings().first()

            else:
                session = None

            if not session:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "No matching open parking session found; "
                        "EXIT was not recorded"
                    ),
                )

            duration_seconds = max(
                0,
                int(
                    db.execute(
                        text("""
                            SELECT EXTRACT(
                                EPOCH FROM (:exit_time - :entry_time)
                            )
                        """),
                        {
                            "exit_time": event_time,
                            "entry_time": session["entry_time"],
                        },
                    ).scalar() or 0
                ),
            )

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
                        if event.snapshot else None
                    ),
                    "session_id": session["id"],
                },
            )

            # Clear slot state using the identity stored
            # in the session that was actually closed.
            if session["vehicle_uid"]:
                slot_update = db.execute(
                    text("""
                        UPDATE parking_slot_state
                        SET
                            status = 'available',
                            vehicle_fk = NULL,
                            vehicle_uid = NULL,
                            plate_number = NULL,
                            plate_number_ciphertext = NULL,
                            plate_number_hmac = NULL,
                            track_id = NULL,
                            occupied_since = NULL,
                            updated_at = NOW()
                        WHERE camera_id = :camera_id
                          AND slot_id = :slot_id
                          AND parking_area_id = :area_id
                          AND vehicle_uid = :vehicle_uid
                    """),
                    {
                        "camera_id": event.camera_id,
                        "slot_id": session["slot_id"],
                        "area_id": event.parking_area_id,
                        "vehicle_uid": session["vehicle_uid"],
                    },
                )

            elif session["vehicle_fk"] is not None:
                slot_update = db.execute(
                    text("""
                        UPDATE parking_slot_state
                        SET
                            status = 'available',
                            vehicle_fk = NULL,
                            vehicle_uid = NULL,
                            plate_number = NULL,
                            plate_number_ciphertext = NULL,
                            plate_number_hmac = NULL,
                            track_id = NULL,
                            occupied_since = NULL,
                            updated_at = NOW()
                        WHERE camera_id = :camera_id
                          AND slot_id = :slot_id
                          AND parking_area_id = :area_id
                          AND vehicle_fk = :vehicle_id
                    """),
                    {
                        "camera_id": event.camera_id,
                        "slot_id": session["slot_id"],
                        "area_id": event.parking_area_id,
                        "vehicle_id": session["vehicle_fk"],
                    },
                )

            else:
                slot_update = None

            if slot_update is None or slot_update.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Slot state does not match the exiting vehicle"
                    ),
                )

            if session["vehicle_fk"] is not None:
                db.execute(
                    text("""
                        UPDATE vehicles
                        SET
                            last_seen_at = :timestamp,
                            updated_at = NOW()
                        WHERE id = :vehicle_id
                    """),
                    {
                        "vehicle_id": session["vehicle_fk"],
                        "timestamp": event_time,
                    },
                )

            db.commit()

            await parking_connections.broadcast({
                "type": "PARKING_UPDATED",
                "event_type": "EXIT",
                "camera_id": event.camera_id,
                "parking_area_id": event.parking_area_id,
                "slot_id": session["slot_id"],
                "vehicle_id": session["vehicle_fk"],
                "vehicle_uid": session["vehicle_uid"],
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
                "vehicle_id": session["vehicle_fk"],
                "vehicle_uid": session["vehicle_uid"],
                "duration_seconds": duration_seconds,
                "duration_minutes": round(
                    duration_seconds / 60,
                    2,
                ),
                "camera_id": event.camera_id,
                "parking_area_id": event.parking_area_id,
                "slot_id": session["slot_id"],
            }

        # =================================================
        # 13. OTHER EVENT TYPES
        # =================================================

        db.commit()

        await parking_connections.broadcast({
            "type": "PARKING_UPDATED",
            "event_type": event.event_type,
            "camera_id": event.camera_id,
            "parking_area_id": event.parking_area_id,
            "slot_id": slot_id,
            "vehicle_uid": vehicle_uid,
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

    except IntegrityError:
        db.rollback()

        raise HTTPException(
            status_code=409,
            detail=(
                "Database constraint conflict; "
                "event was not committed"
            ),
        )

    except Exception as error:
        db.rollback()

        # Never log event payloads or plate values.
        print(
            f"[EVENT ERROR] {type(error).__name__}: "
            f"{str(error)[:1500]}",
            flush=True,
        )

        raise
