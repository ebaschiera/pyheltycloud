"""Constants of the Helty cloud (hcloud).

Extracted from the "Helty Home" app 1.2.2 (Flutter + AWS Amplify) and
verified against the real API. No official documentation exists.
"""

from __future__ import annotations

from enum import IntEnum

USER_POOL_ID = "eu-central-1_lejYSlqKZ"
CLIENT_ID = "7k0c21g92bk3413frij8rso6rk"
REGION = "eu-central-1"
API_BASE = "https://api.hcloud.heltyair.com"

EP_INSTALLATIONS = "/board/installation/all"
EP_LAST_STATUS = "/log/commandlogs/laststatus"
EP_COMMAND_LOGS = "/log/commandlogs/search"
# The service prefix is doubled (like /user/user/...), and the parameter is
# the serial of the BOARD, not the one of the VMC.
EP_SEND_COMMAND = "/board/board/sendcommand/{board_serial}"

# Physical quantities arrive multiplied by 10, as on the local protocol.
SCALE = 10


class VmcMode(IntEnum):
    """VMC mode: same enum as the local protocol (port 5001)."""

    OFF = 0
    SPEED_1 = 1
    SPEED_2 = 2
    SPEED_3 = 3
    SPEED_4 = 4
    HYPERVENTILATION = 5
    NIGHT = 6
    FREE_COOLING = 7


class Command(IntEnum):
    """commandId values accepted by EP_SEND_COMMAND.

    Derived by watching the command log while operating the app: verified
    by observation, not documented.
    """

    GET_STATUS = 0
    GET_INFO = 1
    OFF = 20
    FREE_COOLING = 21
    NIGHT = 22
    HYPERVENTILATION = 23
    SPEED_STEP_UP = 24
    GET_PARAMETERS = 33
    # The "Auto" toggle of the app ("auto_group" in its command switch).
    # Write only: the vendor confirmed the commands take effect but that the
    # setting cannot be read back from anywhere, which is why the app shows
    # its own local idea of the toggle. In Auto the unit picks the speed from
    # its humidity, VOC and CO2 sensors, but only while the outdoor
    # temperature is at least 5 °C below the indoor one and indoor humidity
    # is above 65%, so on a unit with humidity only nothing happens in summer.
    AUTO_ON = 38
    AUTO_OFF = 39
    # Verified live: 40 switches the whole panel off, display and speed LEDs
    # together, and 41 switches it back on. Neither takes an argument, and
    # nothing in the telemetry reflects which of the two is in force.
    PANEL_OFF = 40
    PANEL_ON = 41
    SPEED_1 = 44
    SPEED_2 = 45
    SPEED_3 = 46
    SPEED_4 = 47
    # Answers with [level]: the stored brightness, not whether the panel is
    # lit. It reads 100 with the panel dark.
    GET_PANEL_LEVEL = 48


#: Command that sets each mode. All modes are covered.
MODE_COMMANDS: dict[VmcMode, Command] = {
    VmcMode.OFF: Command.OFF,
    VmcMode.SPEED_1: Command.SPEED_1,
    VmcMode.SPEED_2: Command.SPEED_2,
    VmcMode.SPEED_3: Command.SPEED_3,
    VmcMode.SPEED_4: Command.SPEED_4,
    VmcMode.HYPERVENTILATION: Command.HYPERVENTILATION,
    VmcMode.NIGHT: Command.NIGHT,
    VmcMode.FREE_COOLING: Command.FREE_COOLING,
}

# Positions in the telemetry payload of Command.GET_STATUS.
IDX_ALARMS = 0
IDX_MODE = 1
IDX_TEMP_INDOOR = 2
IDX_TEMP_OUTDOOR = 3
IDX_FAN_SUPPLY = 4
IDX_FAN_EXHAUST = 5
IDX_HUMIDITY = 10
TELEMETRY_MIN_LEN = IDX_HUMIDITY + 1

# EP_LAST_STATUS fields and the corresponding HeltyState attribute.
LAST_STATUS_MAP = {
    "VMCStatus": "mode",
    "TemperaturaInterna": "temperature_indoor",
    "TemperaturaEsterna": "temperature_outdoor",
    "Humidity": "humidity",
}
