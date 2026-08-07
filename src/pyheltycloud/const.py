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
    NIGHT = 22
    HYPERVENTILATION = 23
    SPEED_STEP_UP = 24
    GET_PARAMETERS = 33
    SET_LIGHT = 41
    SPEED_1 = 44
    SPEED_2 = 45  # inferred from the sequence: 44/46/47 are observed
    SPEED_3 = 46
    SPEED_4 = 47
    GET_LIGHT = 48


#: Modes that can be set directly. OFF and FREE_COOLING have no known
#: commandId yet, so they are left out.
MODE_COMMANDS: dict[VmcMode, Command] = {
    VmcMode.SPEED_1: Command.SPEED_1,
    VmcMode.SPEED_2: Command.SPEED_2,
    VmcMode.SPEED_3: Command.SPEED_3,
    VmcMode.SPEED_4: Command.SPEED_4,
    VmcMode.HYPERVENTILATION: Command.HYPERVENTILATION,
    VmcMode.NIGHT: Command.NIGHT,
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
