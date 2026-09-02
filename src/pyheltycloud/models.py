"""Data models of pyheltycloud."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .const import (
    FILTER_M3_PER_COUNT,
    FILTER_MIN_LEN,
    IDX_ALARMS,
    IDX_FAN_EXHAUST,
    IDX_FAN_SUPPLY,
    IDX_FILTER_PARTIAL,
    IDX_FILTER_TOTAL,
    IDX_HUMIDITY,
    IDX_MODE,
    IDX_TEMP_INDOOR,
    IDX_TEMP_OUTDOOR,
    LAST_STATUS_MAP,
    SCALE,
    TELEMETRY_MIN_LEN,
    VmcMode,
)
from .exceptions import HeltyCloudNoDataError

#: Value the cloud uses when an optional sensor is not installed.
NO_SENSOR = 0


@dataclass(slots=True)
class HeltyDevice:
    """A VMC unit as the cloud knows it."""

    serial_number: str
    """Serial of the machine: identifies the device in reads."""

    board_serial: str
    """Serial of the board (= MAC of the panel): needed to send commands."""

    name: str
    model: str
    firmware: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> HeltyDevice:
        """Build the device from the EP_INSTALLATIONS response."""
        board = data.get("cloudBoard") or {}
        installation = data.get("currentInstallation") or {}
        product = data.get("productType") or {}
        serial = data["serialNumber"]
        return cls(
            serial_number=serial,
            board_serial=data.get("boardSerialNumber") or board.get("serialNumber", ""),
            name=installation.get("name") or serial,
            model=product.get("model", ""),
            firmware=board.get("firmware", ""),
            raw=data,
        )


@dataclass(slots=True)
class HeltyState:
    """State of the VMC at a point in time."""

    mode: VmcMode
    temperature_indoor: float | None = None
    temperature_outdoor: float | None = None
    humidity: float | None = None
    fan_supply: int | None = None
    """Supply fan percentage (only from raw telemetry)."""

    fan_exhaust: int | None = None
    """Exhaust fan percentage (only from raw telemetry)."""

    alarms: str | None = None
    """Hex alarm bitmap; "0000000000000000" means no alarms."""

    filter_partial_m3: int | None = None
    """Cubic metres treated since the last filter reset."""

    filter_total_m3: int | None = None
    """Cubic metres treated over the life of the machine."""

    timestamp: int | None = None
    """Epoch milliseconds of the message this state derives from."""

    @property
    def is_on(self) -> bool:
        """True when the machine is ventilating, in any mode."""
        return self.mode is not VmcMode.OFF

    @property
    def has_alarm(self) -> bool:
        """True when the alarm bitmap has at least one bit set."""
        return bool(self.alarms) and set(self.alarms or "") != {"0"}

    @classmethod
    def from_last_status(cls, rows: list[dict[str, Any]]) -> HeltyState:
        """Build the state from the EP_LAST_STATUS response.

        Each row is {"field": …, "value": …}; values are scaled integers.
        Raises HeltyCloudNoDataError when VMCStatus is missing: "no data"
        must not be mistaken for "machine off".
        """
        values: dict[str, float] = {}
        for row in rows:
            attribute = LAST_STATUS_MAP.get(row.get("field", ""))
            if attribute is not None:
                values[attribute] = row["value"]
        if "mode" not in values:
            raise HeltyCloudNoDataError("laststatus response without VMCStatus")
        return cls(
            mode=VmcMode(int(values["mode"])),
            temperature_indoor=_scaled(values.get("temperature_indoor")),
            temperature_outdoor=_scaled(values.get("temperature_outdoor")),
            humidity=_humidity(values.get("humidity")),
        )

    @classmethod
    def from_telemetry(cls, data: list[Any], timestamp: int | None = None) -> HeltyState:
        """Build the state from the raw Command.GET_STATUS payload.

        Richer than from_last_status(): adds fans and alarms.
        """
        return cls(
            mode=VmcMode(int(data[IDX_MODE])),
            temperature_indoor=_scaled(data[IDX_TEMP_INDOOR]),
            temperature_outdoor=_scaled(data[IDX_TEMP_OUTDOOR]),
            humidity=_humidity(data[IDX_HUMIDITY]),
            fan_supply=int(data[IDX_FAN_SUPPLY]),
            fan_exhaust=int(data[IDX_FAN_EXHAUST]),
            alarms=str(data[IDX_ALARMS]),
            timestamp=timestamp,
            filter_partial_m3=_counter(data, IDX_FILTER_PARTIAL),
            filter_total_m3=_counter(data, IDX_FILTER_TOTAL),
        )


def is_telemetry(data: list[Any]) -> bool:
    """Return True when the payload is a full telemetry and not an empty ack."""
    return len(data) >= TELEMETRY_MIN_LEN


def _counter(data: list[Any], index: int) -> int | None:
    """Read a filter counter and convert it to cubic metres.

    Short payloads have no counters, and a zero is not trusted: for the first
    day it was online this panel returned telemetry with a whole block of
    fields zeroed, the counters among them, while the machine was running.
    """
    if len(data) < FILTER_MIN_LEN or not data[index]:
        return None
    return int(data[index]) * FILTER_M3_PER_COUNT


def _scaled(value: float | None) -> float | None:
    return None if value is None else value / SCALE


def _humidity(value: float | None) -> float | None:
    """Like _scaled, but 0 becomes None: 0% is not a real humidity."""
    if value is None or value == NO_SENSOR:
        return None
    return value / SCALE
