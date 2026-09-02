"""Tests of the models and payload decoding."""

from __future__ import annotations

from typing import Any

import pytest

from pyheltycloud import HeltyCloudNoDataError, HeltyDevice, HeltyState, VmcMode
from pyheltycloud.const import IDX_FILTER_PARTIAL, IDX_FILTER_TOTAL, IDX_HUMIDITY
from pyheltycloud.models import is_telemetry


def test_device_from_json(installation: dict[str, Any]) -> None:
    device = HeltyDevice.from_json(installation)
    assert device.serial_number == "64000175853003"
    assert device.board_serial == "DCB4D9A8C966"
    assert device.name == "Living room"
    assert device.model == "1VMC01012B FlowPLUS"
    assert device.firmware == "124"


def test_device_name_falls_back_to_serial() -> None:
    device = HeltyDevice.from_json({"serialNumber": "123", "boardSerialNumber": "AA"})
    assert device.name == "123"


def test_device_board_serial_from_cloud_board() -> None:
    """BoardSerialNumber may be missing: fall back to cloudBoard."""
    device = HeltyDevice.from_json({"serialNumber": "123", "cloudBoard": {"serialNumber": "BB"}})
    assert device.board_serial == "BB"


def test_state_from_last_status() -> None:
    state = HeltyState.from_last_status(
        [
            {"field": "VMCStatus", "value": 6},
            {"field": "TemperaturaInterna", "value": 308},
            {"field": "TemperaturaEsterna", "value": 374},
            {"field": "Humidity", "value": 385},
            {"field": "UnknownField", "value": 1},
        ]
    )
    assert state.mode is VmcMode.NIGHT
    assert state.temperature_indoor == 30.8
    assert state.temperature_outdoor == 37.4
    assert state.humidity == 38.5
    # laststatus does not carry the fans.
    assert state.fan_supply is None


def test_state_from_last_status_without_mode_raises() -> None:
    """Without VMCStatus, do not invent "off": it is missing data."""
    with pytest.raises(HeltyCloudNoDataError):
        HeltyState.from_last_status([])


def test_humidity_zero_means_no_sensor(telemetry: list[Any]) -> None:
    """The cloud sends 0 when the humidity sensor is not installed."""
    data = telemetry.copy()
    data[IDX_HUMIDITY] = 0
    assert HeltyState.from_telemetry(data).humidity is None

    state = HeltyState.from_last_status(
        [{"field": "VMCStatus", "value": 1}, {"field": "Humidity", "value": 0}]
    )
    assert state.humidity is None


def test_state_from_telemetry(telemetry: list[Any]) -> None:
    state = HeltyState.from_telemetry(telemetry, timestamp=1786110225000)
    assert state.mode is VmcMode.NIGHT
    assert state.temperature_indoor == 30.8
    assert state.humidity == 38.5
    assert state.fan_supply == 17
    assert state.fan_exhaust == 15
    assert state.alarms == "0000000000000000"
    assert state.timestamp == 1786110225000
    assert state.filter_partial_m3 == 57960
    assert state.filter_total_m3 == 230870


def test_filter_counters_absent_from_short_telemetry(telemetry: list[Any]) -> None:
    """A payload that stops before the counters must not invent them."""
    state = HeltyState.from_telemetry(telemetry[:11])
    assert state.filter_partial_m3 is None
    assert state.filter_total_m3 is None


def test_filter_counters_zero_means_unknown(telemetry: list[Any]) -> None:
    """Observed live: a whole block of fields comes back zeroed while running."""
    data = telemetry.copy()
    data[IDX_FILTER_PARTIAL] = 0
    data[IDX_FILTER_TOTAL] = 0
    state = HeltyState.from_telemetry(data)
    assert state.filter_partial_m3 is None
    assert state.filter_total_m3 is None


def test_is_on() -> None:
    assert HeltyState(mode=VmcMode.NIGHT).is_on
    assert HeltyState(mode=VmcMode.SPEED_1).is_on
    assert not HeltyState(mode=VmcMode.OFF).is_on


def test_has_alarm() -> None:
    assert not HeltyState(mode=VmcMode.NIGHT, alarms="0000000000000000").has_alarm
    assert HeltyState(mode=VmcMode.NIGHT, alarms="0000000000000020").has_alarm
    # Without raw telemetry we know nothing about the alarms.
    assert not HeltyState(mode=VmcMode.NIGHT).has_alarm


def test_is_telemetry_rejects_command_ack() -> None:
    """Write commands answer with an empty list."""
    assert not is_telemetry([])
    assert not is_telemetry([100])
    assert is_telemetry(["0" * 16, 6, 308, 374, 17, 15, 0, 110, 130, 60, 385])
