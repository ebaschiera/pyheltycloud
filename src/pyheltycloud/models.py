"""Data models of pyheltycloud."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .const import (
    FAULT_MASK,
    FILTER_M3_PER_COUNT,
    FILTER_MIN_LEN,
    IDX_ALARMS,
    IDX_CO2,
    IDX_FAN_EXHAUST,
    IDX_FAN_SUPPLY,
    IDX_FILTER_PARTIAL,
    IDX_FILTER_TOTAL,
    IDX_HUMIDITY,
    IDX_MODE,
    IDX_TEMP_INDOOR,
    IDX_TEMP_OUTDOOR,
    IDX_VOC,
    LAST_STATUS_MAP,
    SCALE,
    SENSOR_MIN_LEN,
    TELEMETRY_MIN_LEN,
    Alarm,
    VmcMode,
)
from .exceptions import HeltyCloudError, HeltyCloudNoDataError

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

    co2: int | None = None
    """CO2 reading, None when the sensor is not installed. Unit unknown."""

    voc: int | None = None
    """VOC reading, None when the sensor is not installed. Unit unknown."""

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
    def alarm_flags(self) -> Alarm | None:
        """Decode the alarm bitmap, or None when there is no raw telemetry."""
        if not self.alarms:
            return None
        try:
            return Alarm(int(self.alarms, 16))
        except ValueError:
            return None

    @property
    def has_alarm(self) -> bool:
        """True when the machine reports a fault.

        Only the documented fault bits count: free cooling reports itself in
        the same bitmap without anything being wrong, and the undocumented
        bits carry at least one start-up transient.
        """
        flags = self.alarm_flags
        return flags is not None and bool(flags & FAULT_MASK)

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
            mode=_mode(values["mode"]),
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
            mode=_mode(data[IDX_MODE]),
            temperature_indoor=_scaled(data[IDX_TEMP_INDOOR]),
            temperature_outdoor=_scaled(data[IDX_TEMP_OUTDOOR]),
            humidity=_humidity(data[IDX_HUMIDITY]),
            fan_supply=int(data[IDX_FAN_SUPPLY]),
            fan_exhaust=int(data[IDX_FAN_EXHAUST]),
            alarms=str(data[IDX_ALARMS]),
            timestamp=timestamp,
            co2=_optional(data, IDX_CO2, SENSOR_MIN_LEN),
            voc=_optional(data, IDX_VOC, SENSOR_MIN_LEN),
            filter_partial_m3=_counter(data, IDX_FILTER_PARTIAL),
            filter_total_m3=_counter(data, IDX_FILTER_TOTAL),
        )


def is_telemetry(data: list[Any]) -> bool:
    """Return True when the payload is a full telemetry and not an empty ack."""
    return len(data) >= TELEMETRY_MIN_LEN


def _optional(data: list[Any], index: int, min_len: int) -> int | None:
    """Read a field that may be absent, or zero because it is not installed.

    Short payloads do not carry it, and a zero is not trusted: for the first
    day it was online this panel returned telemetry with a whole block of
    fields zeroed while the machine was running.
    """
    if len(data) < min_len or not data[index]:
        return None
    return int(data[index])


def _counter(data: list[Any], index: int) -> int | None:
    """Read a filter counter and convert it to cubic metres."""
    counts = _optional(data, index, FILTER_MIN_LEN)
    return None if counts is None else counts * FILTER_M3_PER_COUNT


def _mode(value: Any) -> VmcMode:
    """Decode the mode, refusing to guess at a value we do not know.

    The vendor has no way of warning us before the API changes, so a mode
    added by a future firmware must surface as a cloud error the caller
    already handles, not as a ValueError out of a dataclass.
    """
    try:
        return VmcMode(int(value))
    except ValueError as err:
        raise HeltyCloudError(f"Unknown VMC mode {value!r}") from err


def _scaled(value: float | None) -> float | None:
    return None if value is None else value / SCALE


def _humidity(value: float | None) -> float | None:
    """Like _scaled, but 0 becomes None: 0% is not a real humidity."""
    if value is None or value == NO_SENSOR:
        return None
    return value / SCALE
