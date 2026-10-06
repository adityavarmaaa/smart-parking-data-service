from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class VehicleData(BaseModel):
    type: Optional[str] = None
    type_confidence: Optional[float] = None
    color: Optional[str] = None
    color_confidence: Optional[float] = None
    plate: Optional[str] = None
    plate_confidence: Optional[float] = None


class ParkingData(BaseModel):
    slot_id: Optional[str] = None
    slot_confidence: Optional[float] = None


class SnapshotData(BaseModel):
    vehicle: Optional[str] = None
    plate: Optional[str] = None


class VehicleEvent(BaseModel):
    event_type: str
    camera_id: str
    parking_area_id: str
    track_id: Optional[int] = None
    timestamp: datetime
    captured_at: Optional[datetime] = None

    vehicle: VehicleData
    parking: Optional[ParkingData] = None
    snapshot: Optional[SnapshotData] = None
