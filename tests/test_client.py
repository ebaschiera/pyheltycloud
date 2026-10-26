"""Client tests against a fake local cloud."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest

from pyheltycloud import (
    Command,
    HeltyCloud,
    HeltyCloudAuthError,
    HeltyCloudConnectionError,
    HeltyCloudError,
    HeltyCloudNoDataError,
    VmcMode,
)
from pyheltycloud.const import EP_COMMAND_LOGS, EP_INSTALLATIONS, EP_LAST_STATUS, MODE_COMMANDS

from .conftest import (
    BOARD_SERIAL,
    COGNITO_PATH,
    SERIAL,
    FakeCloud,
    Raw,
    make_id_token,
    srp_login_replies,
)

SEND_PATH = f"/board/board/sendcommand/{BOARD_SERIAL}"


async def test_get_devices(
    client: HeltyCloud, cloud: FakeCloud, installation: dict[str, Any]
) -> None:
    cloud.reply(EP_INSTALLATIONS, [installation])
    devices = await client.get_devices()
    assert len(devices) == 1
    assert devices[0].board_serial == BOARD_SERIAL


async def test_uses_id_token_as_bearer(
    client: HeltyCloud, cloud: FakeCloud, installation: dict[str, Any]
) -> None:
    """The API rejects the access token: the ID token must travel."""
    cloud.reply(EP_INSTALLATIONS, [installation])
    await client.get_devices()
    assert cloud.last(EP_INSTALLATIONS).headers["Authorization"] == "Bearer id-token"


async def test_get_state(client: HeltyCloud, cloud: FakeCloud) -> None:
    cloud.reply(EP_LAST_STATUS, [{"field": "VMCStatus", "value": 3}])
    state = await client.get_state(SERIAL)
    assert state.mode is VmcMode.SPEED_3


async def test_get_state_without_recent_data(client: HeltyCloud, cloud: FakeCloud) -> None:
    """The laststatus 404 means "silent board", not "error"."""
    cloud.reply(EP_LAST_STATUS, {"error": "No getstatus log"}, status=404)
    with pytest.raises(HeltyCloudNoDataError):
        await client.get_state(SERIAL)


async def test_get_last_telemetry_skips_command_acks(
    client: HeltyCloud, cloud: FakeCloud, telemetry: list[Any]
) -> None:
    """In the history, write commands have an empty data field: skip them."""
    cloud.reply(
        "/log/commandlogs/search",
        {
            "data": [
                {"commandId": 22, "timestamp": 3, "data": []},
                {"commandId": 48, "timestamp": 2, "data": [100]},
                {"commandId": 0, "timestamp": 1, "data": telemetry},
            ]
        },
    )
    state = await client.get_last_telemetry(SERIAL)
    assert state.fan_supply == 17
    assert state.timestamp == 1


async def test_get_last_telemetry_when_empty(client: HeltyCloud, cloud: FakeCloud) -> None:
    cloud.reply("/log/commandlogs/search", {"data": []})
    with pytest.raises(HeltyCloudNoDataError):
        await client.get_last_telemetry(SERIAL)


async def test_expired_token_is_refreshed(client: HeltyCloud, cloud: FakeCloud) -> None:
    """An expired token is renewed without redoing the SRP login."""
    assert client._tokens is not None
    client._tokens.expires_at = time.time() - 1
    cloud.reply(
        COGNITO_PATH,
        {
            "AuthenticationResult": {
                "IdToken": "new-id",
                "AccessToken": "new-access",
                "ExpiresIn": 3600,
            }
        },
    )
    cloud.reply(EP_INSTALLATIONS, [])
    await client.get_devices()

    assert client._tokens.id_token == "new-id"
    # The refresh token is not repeated in the response: it must be kept.
    assert client._tokens.refresh_token == "refresh-token"
    assert cloud.last(COGNITO_PATH).json["AuthFlow"] == "REFRESH_TOKEN_AUTH"


async def test_unauthorized_raises_auth_error(client: HeltyCloud, cloud: FakeCloud) -> None:
    cloud.reply(EP_INSTALLATIONS, {"err": "Invalid token"}, status=401)
    with pytest.raises(HeltyCloudAuthError):
        await client.get_devices()


async def test_server_error_raises(client: HeltyCloud, cloud: FakeCloud) -> None:
    cloud.reply(EP_INSTALLATIONS, {"err": "boom"}, status=500)
    with pytest.raises(HeltyCloudError, match="500"):
        await client.get_devices()


async def test_send_command_shape(client: HeltyCloud, cloud: FakeCloud) -> None:
    """Values is mandatory even when empty: without it the API answers 400."""
    await client.send_command(BOARD_SERIAL, Command.GET_STATUS)
    assert cloud.last(SEND_PATH).json == {"commandId": 0, "values": []}


async def test_set_mode(client: HeltyCloud, cloud: FakeCloud) -> None:
    await client.set_mode(BOARD_SERIAL, VmcMode.NIGHT)
    assert cloud.last(SEND_PATH).json["commandId"] == Command.NIGHT


@pytest.mark.parametrize(
    ("mode", "command"),
    [
        (VmcMode.OFF, Command.OFF),
        (VmcMode.SPEED_2, Command.SPEED_2),
        (VmcMode.FREE_COOLING, Command.FREE_COOLING),
    ],
)
async def test_set_mode_covers_every_mode(
    client: HeltyCloud, cloud: FakeCloud, mode: VmcMode, command: Command
) -> None:
    """All modes map to a commandId, including off and free cooling."""
    await client.set_mode(BOARD_SERIAL, mode)
    assert cloud.last(SEND_PATH).json["commandId"] == command


def _search_reply(timestamp: int, data: list[Any]) -> tuple[int, Any]:
    return (200, {"data": [{"commandId": 0, "timestamp": timestamp, "data": data}]})


async def test_update_waits_for_fresh_telemetry(
    client: HeltyCloud,
    cloud: FakeCloud,
    installation: dict[str, Any],
    telemetry: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update() wakes the board and waits for a newer telemetry."""
    monkeypatch.setattr("pyheltycloud.client.REFRESH_POLL_INTERVAL", 0)
    cloud.reply(EP_INSTALLATIONS, [installation])
    cloud.reply_sequence(
        "/log/commandlogs/search",
        [_search_reply(1, telemetry), _search_reply(2, telemetry)],
    )
    device = (await client.get_devices())[0]
    state = await client.update(device)

    assert cloud.last(SEND_PATH).json["commandId"] == Command.GET_STATUS
    assert state.mode is VmcMode.NIGHT
    # It must return the telemetry that arrived AFTER the refresh, not the old one.
    assert state.timestamp == 2


async def test_update_falls_back_when_board_silent(
    client: HeltyCloud,
    cloud: FakeCloud,
    installation: dict[str, Any],
    telemetry: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the board does not answer, stale data beats an error."""
    monkeypatch.setattr("pyheltycloud.client.REFRESH_POLL_INTERVAL", 0)
    monkeypatch.setattr("pyheltycloud.client.REFRESH_TIMEOUT", 0.05)
    cloud.reply(EP_INSTALLATIONS, [installation])
    # The history always returns the same telemetry: nothing new arrives.
    cloud.reply(
        "/log/commandlogs/search",
        {"data": [{"commandId": 0, "timestamp": 1, "data": telemetry}]},
    )
    device = (await client.get_devices())[0]
    state = await client.update(device)
    assert state.timestamp == 1


async def test_update_returns_first_telemetry_ever(
    client: HeltyCloud,
    cloud: FakeCloud,
    installation: dict[str, Any],
    telemetry: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an empty history, the first telemetry that arrives is fine."""
    monkeypatch.setattr("pyheltycloud.client.REFRESH_POLL_INTERVAL", 0)
    cloud.reply(EP_INSTALLATIONS, [installation])
    cloud.reply_sequence(
        "/log/commandlogs/search",
        [(200, {"data": []}), _search_reply(5, telemetry)],
    )
    device = (await client.get_devices())[0]
    state = await client.update(device)
    assert state.timestamp == 5


async def test_update_without_any_telemetry(
    client: HeltyCloud,
    cloud: FakeCloud,
    installation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pyheltycloud.client.REFRESH_POLL_INTERVAL", 0)
    monkeypatch.setattr("pyheltycloud.client.REFRESH_TIMEOUT", 0.05)
    cloud.reply(EP_INSTALLATIONS, [installation])
    cloud.reply("/log/commandlogs/search", {"data": []})
    device = (await client.get_devices())[0]
    with pytest.raises(HeltyCloudNoDataError):
        await client.update(device)


class FakeBoard:
    """A panel behind the fake cloud, with a history that moves.

    Every mode command leaves its ack and then its telemetries in the
    history the way a live board does, one clock tick (a second for the
    cloud) per command, and a refresh adds a telemetry with the current
    mode. Tests script the misbehaviour through `on_mode`, which returns
    the modes of the telemetries a mode command leaves (empty: the command
    is lost), through `answers_refresh`, and through `delayed`: rows that
    become visible only on the read after the one they were added for.
    """

    def __init__(self, cloud: FakeCloud, telemetry: list[Any], mode: VmcMode) -> None:
        """Take over the command and history paths of the fake cloud."""
        self.telemetry = telemetry
        self.mode = mode
        self.clock = 0
        self.rows: list[dict[str, Any]] = []
        self.delayed: list[dict[str, Any]] = []
        self.answers_refresh = True
        self.on_mode: Callable[[VmcMode], list[VmcMode]] = self.apply
        self.sent: list[int] = []
        cloud.reply_with(SEND_PATH, self._command)
        cloud.reply_with("/log/commandlogs/search", self._search)

    def apply(self, mode: VmcMode) -> list[VmcMode]:
        """Default behaviour: switch mode and report it once."""
        self.mode = mode
        return [mode]

    def row(self, mode: VmcMode) -> dict[str, Any]:
        data = self.telemetry.copy()
        data[1] = int(mode)
        return {"commandId": 0, "timestamp": self.clock, "data": data}

    def _command(self, body: Any) -> tuple[int, Any]:
        command = body["commandId"]
        self.sent.append(command)
        if command == Command.GET_STATUS:
            if self.answers_refresh:
                self.rows.append(self.row(self.mode))
            return (200, {})
        self.clock += 1
        self.rows.append({"commandId": command, "timestamp": self.clock, "data": []})
        mode = next(m for m, c in MODE_COMMANDS.items() if c == command)
        self.rows.extend(self.row(m) for m in self.on_mode(mode))
        return (200, {})

    def _search(self, body: Any) -> tuple[int, Any]:
        visible = list(self.rows)
        # Rows held back on this read join the history for the next one.
        self.rows.extend(self.delayed)
        self.delayed = []
        newest_first = sorted(visible, key=lambda row: -row["timestamp"])
        return (200, {"data": newest_first[: body["pageSize"]]})


def _lose_first(board: FakeBoard) -> Callable[[VmcMode], list[VmcMode]]:
    """Drop the first mode command and apply the others."""
    lost = [True]

    def on_mode(mode: VmcMode) -> list[VmcMode]:
        if lost:
            lost.pop()
            return []
        return board.apply(mode)

    return on_mode


async def test_get_last_telemetry_with_tied_rows_keeps_cloud_order(
    client: HeltyCloud, cloud: FakeCloud, telemetry: list[Any]
) -> None:
    """Polling still reads the first of the telemetries sharing the newest second."""
    other = telemetry.copy()
    other[1] = int(VmcMode.SPEED_4)
    cloud.reply(
        "/log/commandlogs/search",
        {"data": [{"commandId": 0, "timestamp": 7, "data": data} for data in (telemetry, other)]},
    )
    state = await client.get_last_telemetry(SERIAL)
    assert state.mode is VmcMode.NIGHT


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real waits, and a short deadline for a board that stays silent."""
    monkeypatch.setattr("pyheltycloud.client.REFRESH_POLL_INTERVAL", 0)
    monkeypatch.setattr("pyheltycloud.client.CONFIRM_SETTLE_DELAY", 0)
    monkeypatch.setattr("pyheltycloud.client.REFRESH_TIMEOUT", 0.05)


async def _device(client: HeltyCloud, cloud: FakeCloud, installation: dict[str, Any]) -> Any:
    cloud.reply(EP_INSTALLATIONS, [installation])
    return (await client.get_devices())[0]


@pytest.mark.usefixtures("fast")
async def test_set_mode_verified_confirms_at_once(
    client: HeltyCloud, cloud: FakeCloud, installation: dict[str, Any], telemetry: list[Any]
) -> None:
    board = FakeBoard(cloud, telemetry, VmcMode.SPEED_1)
    board.rows.append(board.row(VmcMode.SPEED_1))
    device = await _device(client, cloud, installation)

    state = await client.set_mode_verified(device, VmcMode.NIGHT)

    assert state.mode is VmcMode.NIGHT
    assert board.sent.count(Command.NIGHT) == 1


@pytest.mark.usefixtures("fast")
async def test_set_mode_verified_with_empty_history(
    client: HeltyCloud, cloud: FakeCloud, installation: dict[str, Any], telemetry: list[Any]
) -> None:
    """With nothing in the history yet, the first report confirms."""
    board = FakeBoard(cloud, telemetry, VmcMode.SPEED_1)
    device = await _device(client, cloud, installation)

    state = await client.set_mode_verified(device, VmcMode.NIGHT)

    assert state.mode is VmcMode.NIGHT
    assert board.sent.count(Command.NIGHT) == 1


@pytest.mark.usefixtures("fast")
async def test_set_mode_verified_retries_lost_command(
    client: HeltyCloud, cloud: FakeCloud, installation: dict[str, Any], telemetry: list[Any]
) -> None:
    """A command lost on the way is resent until the board reports it."""
    board = FakeBoard(cloud, telemetry, VmcMode.SPEED_2)
    board.rows.append(board.row(VmcMode.SPEED_2))
    board.on_mode = _lose_first(board)
    device = await _device(client, cloud, installation)

    state = await client.set_mode_verified(device, VmcMode.NIGHT)

    assert state.mode is VmcMode.NIGHT
    assert board.sent.count(Command.NIGHT) == 2


@pytest.mark.usefixtures("fast")
async def test_set_mode_verified_gives_up(
    client: HeltyCloud, cloud: FakeCloud, installation: dict[str, Any], telemetry: list[Any]
) -> None:
    board = FakeBoard(cloud, telemetry, VmcMode.SPEED_2)
    board.rows.append(board.row(VmcMode.SPEED_2))
    board.on_mode = lambda mode: []
    device = await _device(client, cloud, installation)

    with pytest.raises(HeltyCloudError, match="NIGHT"):
        await client.set_mode_verified(device, VmcMode.NIGHT, attempts=2)
    assert board.sent.count(Command.NIGHT) == 2


@pytest.mark.usefixtures("fast")
async def test_set_mode_verified_ignores_stale_history(
    client: HeltyCloud, cloud: FakeCloud, installation: dict[str, Any], telemetry: list[Any]
) -> None:
    """A history that already shows the mode does not confirm a lost command."""
    board = FakeBoard(cloud, telemetry, VmcMode.SPEED_4)
    board.rows.append(board.row(VmcMode.SPEED_1))
    board.on_mode = lambda mode: []
    board.answers_refresh = False
    device = await _device(client, cloud, installation)

    with pytest.raises(HeltyCloudError, match="SPEED_1"):
        await client.set_mode_verified(device, VmcMode.SPEED_1, attempts=2)


@pytest.mark.usefixtures("fast")
async def test_set_mode_verified_resends_on_disagreeing_tie(
    client: HeltyCloud, cloud: FakeCloud, installation: dict[str, Any], telemetry: list[Any]
) -> None:
    """A tie between the new mode and the old one does not confirm the command.

    Seen on a live board: asked for speed 1 seconds after speed 4, it kept
    speed 4 but left two telemetries in the same second, the first with the
    new mode and the second with the old, and then stayed silent.
    """
    board = FakeBoard(cloud, telemetry, VmcMode.SPEED_4)
    board.rows.append(board.row(VmcMode.SPEED_4))
    dropped = [True]

    def drop_first(mode: VmcMode) -> list[VmcMode]:
        if dropped:
            dropped.pop()
            return [mode, board.mode]
        board.mode = mode
        return [mode, mode]

    board.on_mode = drop_first
    board.answers_refresh = False
    device = await _device(client, cloud, installation)

    state = await client.set_mode_verified(device, VmcMode.SPEED_1)

    assert state.mode is VmcMode.SPEED_1
    assert board.sent.count(Command.SPEED_1) == 2


@pytest.mark.usefixtures("fast")
async def test_set_mode_verified_waits_for_the_late_half_of_a_tie(
    client: HeltyCloud, cloud: FakeCloud, installation: dict[str, Any], telemetry: list[Any]
) -> None:
    """The old-mode twin of a telemetry can show up a read later: no early yes."""
    board = FakeBoard(cloud, telemetry, VmcMode.SPEED_4)
    board.rows.append(board.row(VmcMode.SPEED_4))
    dropped = [True]

    def drop_first_late(mode: VmcMode) -> list[VmcMode]:
        if dropped:
            dropped.pop()
            board.delayed.append(board.row(board.mode))
            return [mode]
        return board.apply(mode)

    board.on_mode = drop_first_late
    board.answers_refresh = False
    device = await _device(client, cloud, installation)

    state = await client.set_mode_verified(device, VmcMode.SPEED_1)

    assert state.mode is VmcMode.SPEED_1
    assert board.sent.count(Command.SPEED_1) == 2


@pytest.mark.usefixtures("fast")
async def test_set_mode_verified_accepts_agreeing_tie(
    client: HeltyCloud, cloud: FakeCloud, installation: dict[str, Any], telemetry: list[Any]
) -> None:
    """The usual pair of telemetries in the same second confirms at once."""
    board = FakeBoard(cloud, telemetry, VmcMode.SPEED_1)
    board.rows.append(board.row(VmcMode.SPEED_1))
    board.on_mode = lambda mode: [*board.apply(mode), mode]
    device = await _device(client, cloud, installation)

    await client.set_mode_verified(device, VmcMode.NIGHT)

    assert board.sent.count(Command.NIGHT) == 1


@pytest.mark.usefixtures("fast")
async def test_set_mode_verified_serializes_commands_per_board(
    client: HeltyCloud, cloud: FakeCloud, installation: dict[str, Any], telemetry: list[Any]
) -> None:
    """The retries of an older call never land after a newer command."""
    board = FakeBoard(cloud, telemetry, VmcMode.SPEED_1)
    board.rows.append(board.row(VmcMode.SPEED_1))
    board.on_mode = _lose_first(board)
    device = await _device(client, cloud, installation)

    speed_4 = asyncio.create_task(client.set_mode_verified(device, VmcMode.SPEED_4))
    await asyncio.sleep(0)
    speed_1 = asyncio.create_task(client.set_mode_verified(device, VmcMode.SPEED_1))
    await asyncio.gather(speed_4, speed_1)

    first_speed_1 = board.sent.index(Command.SPEED_1)
    assert Command.SPEED_4 not in board.sent[first_speed_1:]
    assert board.sent.count(Command.SPEED_4) == 2
    assert board.mode is VmcMode.SPEED_1


def test_constructor_needs_no_event_loop() -> None:
    """The constructor must not touch aiohttp: the session is born on first use."""
    helty = HeltyCloud("user@example.com", "secret")
    assert helty._session is None


async def test_context_manager_closes_own_session() -> None:
    """A session created by the client is closed by the client; a borrowed one is not."""
    async with HeltyCloud("user@example.com", "secret") as helty:
        session, _ = helty._ensure_started()
    assert session.closed


async def test_borrowed_session_is_left_open(client: HeltyCloud) -> None:
    session = client._session
    await client.close()
    assert session is not None and not session.closed


async def test_login_authenticates_when_no_tokens(cloud: FakeCloud, client: HeltyCloud) -> None:
    """Without tokens in memory, the full SRP login runs."""
    client._tokens = None
    cloud.reply_sequence(COGNITO_PATH, srp_login_replies("fresh-id"))
    cloud.reply(EP_INSTALLATIONS, [])
    await client.get_devices()
    assert cloud.last(EP_INSTALLATIONS).headers["Authorization"] == "Bearer fresh-id"


async def test_user_id_is_the_cognito_subject(cloud: FakeCloud, client: HeltyCloud) -> None:
    client._tokens = None
    sub = "00000000-1111-2222-3333-444444444444"
    cloud.reply_sequence(
        COGNITO_PATH, srp_login_replies(make_id_token({"sub": sub, "email": "user@example.com"}))
    )
    await client.login()
    assert client.user_id == sub


async def test_user_id_before_login(client: HeltyCloud) -> None:
    client._tokens = None
    with pytest.raises(HeltyCloudError, match="login"):
        client.user_id  # noqa: B018


@pytest.mark.parametrize(
    "id_token",
    ["id-token", "a.!!!.c", make_id_token({"email": "user@example.com"}), "a.WzFd.c"],
    ids=["not-a-jwt", "bad-base64", "no-sub", "not-an-object"],
)
async def test_user_id_from_an_unreadable_token(client: HeltyCloud, id_token: str) -> None:
    assert client._tokens is not None
    client._tokens.id_token = id_token
    with pytest.raises(HeltyCloudAuthError):
        client.user_id  # noqa: B018


async def test_relogin_when_refresh_token_rejected(cloud: FakeCloud, client: HeltyCloud) -> None:
    """When the refresh token has expired too, log in again from scratch."""
    assert client._tokens is not None
    client._tokens.expires_at = time.time() - 1
    cloud.reply_sequence(
        COGNITO_PATH,
        [
            (400, {"__type": "NotAuthorizedException", "message": "Refresh Token expired"}),
            *srp_login_replies("reborn-id"),
        ],
    )
    cloud.reply(EP_INSTALLATIONS, [])
    await client.get_devices()
    assert client._tokens.id_token == "reborn-id"


async def test_connection_error(client: HeltyCloud, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyheltycloud.client.API_BASE", "http://127.0.0.1:1")
    with pytest.raises(HeltyCloudConnectionError):
        await client.get_devices()


async def test_explicit_login(cloud: FakeCloud, client: HeltyCloud) -> None:
    """login() lets the config flow validate credentials right away."""
    cloud.reply_sequence(COGNITO_PATH, srp_login_replies("login-id"))
    await client.login()
    assert client._tokens is not None and client._tokens.id_token == "login-id"


async def test_non_json_body_is_a_connection_error(client: HeltyCloud, cloud: FakeCloud) -> None:
    """An HTML page from the proxy must not escape as a ValueError."""
    cloud.reply(EP_INSTALLATIONS, Raw("<html>502 Bad Gateway</html>"))
    with pytest.raises(HeltyCloudConnectionError, match="non-JSON"):
        await client.get_devices()


async def test_last_telemetry_picks_the_newest(
    client: HeltyCloud, cloud: FakeCloud, telemetry: list[Any]
) -> None:
    """The newest row wins even if the cloud stops sorting the history."""
    older = list(telemetry)
    older[1] = int(VmcMode.SPEED_1)
    cloud.reply(
        EP_COMMAND_LOGS,
        {
            "data": [
                {"commandId": 0, "timestamp": 1000, "data": older},
                {"commandId": 0, "timestamp": 9000, "data": telemetry},
            ]
        },
    )
    state = await client.get_last_telemetry(SERIAL)
    assert state.timestamp == 9000
    assert state.mode is VmcMode.NIGHT
