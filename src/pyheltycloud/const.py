"""Constants of the Helty cloud (hcloud).

Extracted from the "Helty Home" app 1.2.2 (Flutter + AWS Amplify) and
verified against the real API. No official documentation exists.
"""

from __future__ import annotations

from enum import IntEnum, IntFlag

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


class Alarm(IntFlag):
    """Bits of the alarm bitmap, from the table the vendor supplied.

    The table stops at bit 8; every higher bit is undocumented. IntFlag
    keeps unknown bits rather than rejecting them, so the raw value stays
    readable through the flags.
    """

    ICE_RISK = 0x001
    #: High indoor humidity. The board raises it on two bits (codes 7 and 8)
    #: that only its own humidity algorithm tells apart; the vendor says to
    #: treat them as one alarm. Either bit is enough, so test it with
    #: ``flags & Alarm.HUMIDITY``: ``in`` would want both.
    HUMIDITY = 0x006
    TEMPERATURE_DELTA = 0x008
    CO2_OVER_LIMIT = 0x010
    FILTER = 0x020
    #: Code 12 in the vendor's table, whose description column is empty.
    UNDOCUMENTED_12 = 0x040
    #: Not a fault: the machine reports that it went into free cooling by
    #: itself. It sits in the alarm bitmap all the same.
    FREE_COOLING_AUTO = 0x080
    VOC_OVER_LIMIT = 0x100


#: Bits that mean something is wrong. Free cooling is a state, not a fault,
#: and the undocumented bits are left out on purpose: the only one ever seen
#: on a real unit, 0x0000000C00000000, lasted two seconds while the fans were
#: spinning up, so treating unknown bits as faults would fire a notification
#: at every start.
FAULT_MASK = (
    Alarm.ICE_RISK
    | Alarm.HUMIDITY
    | Alarm.TEMPERATURE_DELTA
    | Alarm.CO2_OVER_LIMIT
    | Alarm.FILTER
    | Alarm.VOC_OVER_LIMIT
)


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
    # Zeroes the partial filter counter after a filter change. Given by the
    # vendor, who is not certain of it either ("dovrebbe essere il 37"), and
    # never sent: trying it would destroy the only record of how old the
    # filter is.
    RESET_FILTER = 37
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
    # Answers with [level]. The vendor says the level belongs to the lights
    # of the Elite version, not to this panel, which would explain why it
    # reads 100 whether the panel is lit or dark.
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

# Optional sensors, absent on most units: they read 0 when not installed.
# Both are passed on unscaled, as the vendor specifies: CO2 (register 1005,
# CO2Probe) in ppm, VOC (register 1015, Voc) as a VOC index that the firmware
# thresholds as ppb.
IDX_CO2 = 14
IDX_VOC = 15
SENSOR_MIN_LEN = IDX_VOC + 1

# The two counters of treated air. The vendor confirmed that 16 is the
# partial one, zeroed by the filter reset, and that 18 is the lifetime one:
# their difference is constant and is what the machine had treated when the
# filter was last changed.
IDX_FILTER_PARTIAL = 16
IDX_FILTER_TOTAL = 18
FILTER_MIN_LEN = IDX_FILTER_TOTAL + 1

#: Cubic metres per count. The register is "mc/10": the vendor confirmed
#: that it holds tens of cubic metres, which matches the measured rates
#: (0.73 counts per hour at speed 1, that is 7.3 m3/h).
FILTER_M3_PER_COUNT = 10

#: Cubic metres after which the panel shows the filter alarm. The vendor
#: gives this for the current firmware and warns that it can change with
#: later ones, and it is in none of the 27 values of GET_PARAMETERS: it is a
#: default to start from, not a reading.
FILTER_ALARM_M3 = 90_000

# EP_LAST_STATUS fields and the corresponding HeltyState attribute.
LAST_STATUS_MAP = {
    "VMCStatus": "mode",
    "TemperaturaInterna": "temperature_indoor",
    "TemperaturaEsterna": "temperature_outdoor",
    "Humidity": "humidity",
}
