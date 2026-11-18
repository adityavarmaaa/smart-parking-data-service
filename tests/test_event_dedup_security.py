from types import SimpleNamespace

from app.routes.events import _find_existing_event


class FakeDB:
    def __init__(self, result=None):
        self.result = result
        self.sql = None
        self.params = None

    def execute(self, statement, params):
        self.sql = str(statement)
        self.params = params
        return self

    def scalar(self):
        return self.result


def make_event():
    return SimpleNamespace(
        event_type="ENTRY",
        camera_id="CAM-TEST",
        parking_area_id="AREA-TEST",
        track_id=1,
        timestamp="2026-01-01T10:00:00+05:30",
    )


def test_event_dedup_uses_uid_when_available():
    db = FakeDB(result="existing-event")

    result = _find_existing_event(
        db=db,
        event=make_event(),
        vehicle_uid="generated-test-uid",
        plate_fingerprint=None,
        plate_fingerprints=[],
        plate=None,
        slot_id="TEST-SLOT",
    )

    assert result == "existing-event"
    assert "vehicle_uid = CAST(:vehicle_uid AS TEXT)" in db.sql
    assert db.params["vehicle_uid"] == "generated-test-uid"


def test_event_dedup_uses_hmac_when_uid_is_absent():
    db = FakeDB(result="existing-event")

    result = _find_existing_event(
        db=db,
        event=make_event(),
        vehicle_uid=None,
        plate_fingerprint="test-fingerprint",
        plate_fingerprints=["current-hmac", "historical-hmac"],
        plate="TEST-PLATE",
        slot_id="TEST-SLOT",
    )

    assert result == "existing-event"
    assert "plate_number_hmac" in db.sql
    assert "ANY(CAST(:plate_hmacs AS TEXT[]))" in db.sql
    assert "plate_number IS NULL" not in db.sql
    assert "plate_number = :plate" not in db.sql
    assert "plate" not in db.params
    assert db.params["plate_hmacs"] == [
        "current-hmac",
        "historical-hmac",
    ]
