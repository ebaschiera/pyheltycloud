"""Async client for the Helty cloud (hcloud)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from types import TracebackType
from typing import Any, Self

import aiohttp

from .auth import CognitoSrpAuth, Tokens
from .const import (
    API_BASE,
    CLIENT_ID,
    EP_COMMAND_LOGS,
    EP_INSTALLATIONS,
    EP_LAST_STATUS,
    EP_SEND_COMMAND,
    MODE_COMMANDS,
    REGION,
    USER_POOL_ID,
    Command,
    VmcMode,
)
from .exceptions import (
    HeltyCloudAuthError,
    HeltyCloudConnectionError,
    HeltyCloudError,
    HeltyCloudNoDataError,
)
from .models import HeltyDevice, HeltyState, is_telemetry

_LOGGER = logging.getLogger(__name__)

#: Overall timeout for each HTTP call to the API.
TIMEOUT = aiohttp.ClientTimeout(total=15)

#: How often to re-check the history while waiting for the board to answer.
REFRESH_POLL_INTERVAL = 2.0

#: How long to wait overall for the board to answer before falling back to
#: the last available data.
REFRESH_TIMEOUT = 15.0

#: History search window when reading raw telemetry. Measured on 5334
#: spontaneous reports: the board reports every 5.5 minutes at the median
#: and every 33 at the 99th percentile, so two hours is wide enough to
#: always find something without asking the cloud to scan a long window.
DEFAULT_HISTORY_HOURS = 2

#: How long to wait before reading the history once more to confirm a
#: command. The two telemetries that answer a command can share a second
#: and need not become visible together: a second read lets the late one
#: show up.
CONFIRM_SETTLE_DELAY = 2.0

#: Rows to ask for. Only the newest second is used, but it can hold the
#: acks of the commands next to two telemetries: a command sent seconds
#: after another left five rows in a row on a live board.
HISTORY_PAGE_SIZE = 10


class HeltyCloud:
    """Session towards api.hcloud.heltyair.com.

    The cloud keeps no device state: it returns the last message received
    from the board, which pushes over its own AWS IoT connection. To get
    current data the board must be asked to report (refresh()), otherwise
    you read whatever is there.
    """

    def __init__(
        self,
        email: str,
        password: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Without a session the client creates and closes its own.

        The owned session is created on the first request, not here: aiohttp
        needs a running event loop to create one.
        """
        self._email = email
        self._password = password
        self._session = session
        self._owns_session = session is None
        self._auth = (
            None if session is None else CognitoSrpAuth(session, USER_POOL_ID, CLIENT_ID, REGION)
        )
        self._tokens: Tokens | None = None
        self._lock = asyncio.Lock()
        self._command_locks: dict[str, asyncio.Lock] = {}

    def _ensure_started(self) -> tuple[aiohttp.ClientSession, CognitoSrpAuth]:
        """Session and auth, created lazily inside the current event loop."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
        if self._auth is None:
            self._auth = CognitoSrpAuth(self._session, USER_POOL_ID, CLIENT_ID, REGION)
        return self._session, self._auth

    async def __aenter__(self) -> Self:
        """Enter the async context."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Exit the context, closing the session."""
        await self.close()

    async def close(self) -> None:
        """Close the HTTP session, if we created it ourselves."""
        if self._owns_session and self._session is not None:
            await self._session.close()

    # -- authentication ----------------------------------------------------

    async def login(self) -> None:
        """Authenticate and store the tokens. Useful to validate credentials."""
        _, auth = self._ensure_started()
        self._tokens = await auth.authenticate(self._email, self._password)

    @property
    def user_id(self) -> str:
        """Identifier of the account, stable across email changes.

        It is the Cognito ``sub``: the email is only a login alias of the
        pool, so it can change while this stays the same. Available once
        logged in, after login() or any other call.
        """
        if self._tokens is None:
            raise HeltyCloudError("Not logged in: call login() first")
        return self._tokens.user_id

    async def _id_token(self) -> str:
        """Return a valid token, renewing it or logging in again if needed."""
        _, auth = self._ensure_started()
        async with self._lock:
            if self._tokens is None:
                self._tokens = await auth.authenticate(self._email, self._password)
            elif self._tokens.expired:
                try:
                    self._tokens = await auth.refresh(self._tokens.refresh_token)
                except HeltyCloudAuthError:
                    # The refresh token expired or was revoked: start over.
                    _LOGGER.debug("Refresh failed, retrying the full SRP login")
                    self._tokens = await auth.authenticate(self._email, self._password)
            return self._tokens.id_token

    # -- transport ---------------------------------------------------------

    async def _post(self, path: str, payload: dict[str, Any], *, allow_404: bool = False) -> Any:
        session, _ = self._ensure_started()
        headers = {
            # The API validates the ID token: the access token gets a 401.
            "Authorization": f"Bearer {await self._id_token()}",
            "Accept": "application/json",
        }
        try:
            async with session.post(
                API_BASE + path, headers=headers, json=payload, timeout=TIMEOUT
            ) as response:
                status = response.status
                text = await response.text()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise HeltyCloudConnectionError(f"{path}: {err}") from err

        if status == 401:
            raise HeltyCloudAuthError(text)
        if status == 404 and allow_404:
            return None
        if status >= 400:
            raise HeltyCloudError(f"HTTP {status} on {path}: {text[:200]}")

        # nginx sits in front of the API and answers with its own HTML pages,
        # so a 200 is not a promise of JSON. Parsing here keeps a ValueError
        # from escaping past the exceptions this library documents.
        try:
            return json.loads(text)
        except ValueError as err:
            raise HeltyCloudConnectionError(
                f"{path}: the cloud answered HTTP {status} with a non-JSON body: {text[:200]}"
            ) from err

    # -- reading -----------------------------------------------------------

    async def get_devices(self) -> list[HeltyDevice]:
        """List the VMC units associated with the account."""
        data = await self._post(EP_INSTALLATIONS, {})
        return [HeltyDevice.from_json(item) for item in data]

    async def get_state(self, serial_number: str) -> HeltyState:
        """Last known state, from the fields already normalized by the cloud.

        Raises HeltyCloudNoDataError if the board has not reported for over
        an hour: in that case use refresh() or get_last_telemetry().
        """
        rows = await self._post(EP_LAST_STATUS, {"serialNumber": serial_number}, allow_404=True)
        if rows is None:
            raise HeltyCloudNoDataError(f"No recent telemetry for {serial_number}")
        return HeltyState.from_last_status(rows)

    async def get_last_telemetry(
        self, serial_number: str, hours: int = DEFAULT_HISTORY_HOURS
    ) -> HeltyState:
        """Last state from the raw history: includes fans and alarms."""
        return (await self._newest_telemetry(serial_number, hours))[0]

    async def _newest_telemetry(
        self, serial_number: str, hours: int = DEFAULT_HISTORY_HOURS
    ) -> list[HeltyState]:
        """Every telemetry that carries the newest timestamp, in cloud order.

        The cloud stamps the history to the second and gives the rows no id,
        and the board answers a command with two telemetries, one of its own
        and one for the refresh that follows: when they land in the same
        second there is no telling which came last. They normally agree, but
        a board that drops a command can leave one with the new mode and one
        with the old, so callers that confirm a command must see them all.
        """
        now = int(time.time() * 1000)
        page = await self._post(
            EP_COMMAND_LOGS,
            {
                "serialNumber": serial_number,
                "pageSize": HISTORY_PAGE_SIZE,
                "pageNumber": 0,
                "fromTs": now - hours * 3600 * 1000,
                "toTs": now,
            },
        )
        rows = [
            row
            for row in page.get("data", [])
            if row.get("commandId") == Command.GET_STATUS and is_telemetry(row.get("data") or [])
        ]
        if not rows:
            raise HeltyCloudNoDataError(
                f"No telemetry for {serial_number} in the last {hours} hours"
            )
        # The cloud happens to return the newest first, but nothing documents
        # it, and update() decides whether the board answered by comparing
        # timestamps: pick the newest explicitly.
        newest = max(row.get("timestamp") or 0 for row in rows)
        return [
            HeltyState.from_telemetry(row["data"], row.get("timestamp"))
            for row in rows
            if (row.get("timestamp") or 0) == newest
        ]

    async def update(self, device: HeltyDevice) -> HeltyState:
        """Fetch the current state: ask the board to report, then read.

        This is the method to use for polling. It waits for a telemetry
        newer than the one already known (up to REFRESH_TIMEOUT seconds);
        if the board does not answer it falls back to the last available
        data, and raises HeltyCloudNoDataError only if there is nothing
        to read at all.
        """
        try:
            last: HeltyState | None = await self.get_last_telemetry(device.serial_number)
        except HeltyCloudNoDataError:
            last = None

        states = await self._fresh_telemetry(device, after=_timestamp(last))
        if states is not None:
            return states[0]
        if last is not None:
            _LOGGER.debug(
                "Board %s did not answer the refresh: using the last known data",
                device.serial_number,
            )
            return last
        raise HeltyCloudNoDataError(
            f"No telemetry for {device.serial_number}, even after a refresh"
        )

    async def _fresh_telemetry(
        self, device: HeltyDevice, after: int | None
    ) -> list[HeltyState] | None:
        """Ask the board to report and wait for a second newer than `after`.

        `after` None accepts any telemetry. Returns None if nothing newer
        arrives within REFRESH_TIMEOUT.
        """
        await self.refresh(device.board_serial)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + REFRESH_TIMEOUT
        while loop.time() < deadline:
            await asyncio.sleep(REFRESH_POLL_INTERVAL)
            try:
                states = await self._newest_telemetry(device.serial_number)
            except HeltyCloudNoDataError:
                continue
            if after is None or (_timestamp(states[0]) or 0) > after:
                return states
        return None

    # -- writing -----------------------------------------------------------

    async def send_command(self, board_serial: str, command: Command) -> None:
        """Send an argument-less command to the board.

        The cloud answers 200 as soon as it takes the command, before the
        board has acted on it, and the answer says nothing about delivery.
        Callers who need certainty should read the state back.

        Commands that carry an argument are not supported: "values" has to
        hold objects with "id" and "value" (the keys are in the app's own
        code), but every combination tried against the live API was refused
        with 400 "Malformed Command". The vendor places the only argument we
        went looking for, the brightness level, on the lights of the Elite
        version, so there may be nothing here to support on a FlowPLUS.
        """
        body: dict[str, Any] = {"commandId": int(command), "values": []}
        await self._post(EP_SEND_COMMAND.format(board_serial=board_serial), body)

    async def refresh(self, board_serial: str) -> None:
        """Ask the board to report its state right away."""
        await self.send_command(board_serial, Command.GET_STATUS)

    async def set_mode(self, board_serial: str, mode: VmcMode) -> None:
        """Set the VMC mode. Single send, no verification."""
        await self.send_command(board_serial, MODE_COMMANDS[mode])

    async def set_mode_verified(
        self, device: HeltyDevice, mode: VmcMode, attempts: int = 3
    ) -> HeltyState:
        """set_mode() with verification: re-read the state and resend if lost.

        Returns the state reflecting the requested mode. Raises
        HeltyCloudError if after `attempts` sends the VMC has not applied it.

        Only telemetry newer than what the history held before the send
        counts, and every telemetry of its second must carry the mode, on
        two reads CONFIRM_SETTLE_DELAY apart: an old report, or one half of
        a pair that disagrees, says nothing about this command. Calls for
        the same board run one at a time, so the retries of an older call
        never land after a newer command.
        """
        lock = self._command_locks.setdefault(device.board_serial, asyncio.Lock())
        async with lock:
            try:
                seen = _timestamp(await self.get_last_telemetry(device.serial_number))
            except HeltyCloudNoDataError:
                seen = None

            for attempt in range(1, attempts + 1):
                await self.set_mode(device.board_serial, mode)
                states = await self._fresh_telemetry(device, after=seen)
                if states is not None:
                    seen = _timestamp(states[0])
                    if _all_in_mode(states, mode):
                        await asyncio.sleep(CONFIRM_SETTLE_DELAY)
                        states = await self._newest_telemetry(device.serial_number)
                        seen = _timestamp(states[0])
                        if _all_in_mode(states, mode):
                            return states[0]
                _LOGGER.debug(
                    "Command %s not confirmed by %s (attempt %d/%d)",
                    mode.name,
                    device.serial_number,
                    attempt,
                    attempts,
                )
        raise HeltyCloudError(f"The VMC did not apply mode {mode.name} after {attempts} attempts")


def _timestamp(state: HeltyState | None) -> int | None:
    """Timestamp of a state, None when there is no state."""
    return None if state is None else state.timestamp or 0


def _all_in_mode(states: list[HeltyState], mode: VmcMode) -> bool:
    """Whether every telemetry of a second reports the mode."""
    return all(state.mode is mode for state in states)
