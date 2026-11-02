"""Optional read-only telemetry; the model and collector perform no I/O."""

from yoke.telemetry.collector import TelemetryCollector
from yoke.telemetry.model import DeviceSnapshot, Snapshot

__all__ = ["DeviceSnapshot", "Snapshot", "TelemetryCollector"]
