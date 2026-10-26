"""Shared fixtures.

Instead of intercepting aiohttp with a mocking library (which struggles to
keep up with aiohttp releases) the tests talk to a real local server: the
full HTTP path is exercised, headers included.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import pyheltycloud.client as client_module
from pyheltycloud import HeltyCloud
from pyheltycloud.auth import Tokens

BOARD_SERIAL = "DCB4D9A8C966"
SERIAL = "64000175853003"
COGNITO_PATH = "/cognito"


@dataclass
class Recorded:
    """A request that reached the fake cloud."""

    method: str
    path: str
    json: Any
    headers: dict[str, str]


@dataclass
class Raw:
    """A body that is not JSON, like the error page of a proxy."""

    text: str
    content_type: str = "text/html"


@dataclass
class FakeCloud:
    """Fake backend: programmable responses and recorded requests."""

    responses: dict[str, tuple[int, Any]] = field(default_factory=dict)
    requests: list[Recorded] = field(default_factory=list)

    def reply(self, path: str, payload: Any, status: int = 200) -> None:
        self.responses[path] = (status, payload)

    def reply_sequence(self, path: str, replies: list[tuple[int, Any]]) -> None:
        """Different responses to successive calls on the same path.

        Needed for Cognito, where InitiateAuth and RespondToAuthChallenge
        hit the same URL and differ only in the X-Amz-Target header.
        """
        self.responses[path] = list(replies)  # type: ignore[assignment]

    def reply_with(self, path: str, handler: Callable[[Any], tuple[int, Any]]) -> None:
        """Compute each response from the request body, for stateful fakes."""
        self.responses[path] = handler  # type: ignore[assignment]

    def last(self, path: str) -> Recorded:
        matching = [r for r in self.requests if r.path == path]
        assert matching, f"no request to {path}; seen: {[r.path for r in self.requests]}"
        return matching[-1]

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json() if request.body_exists else None
        self.requests.append(Recorded(request.method, request.path, body, dict(request.headers)))
        entry = self.responses.get(request.path, (200, {}))
        if callable(entry):
            status, payload = entry(body)
        elif isinstance(entry, list):
            status, payload = entry.pop(0) if entry else (200, {})
        else:
            status, payload = entry
        if isinstance(payload, Raw):
            return web.Response(text=payload.text, status=status, content_type=payload.content_type)
        return web.json_response(payload, status=status)


def make_id_token(claims: dict[str, Any]) -> str:
    """Build an unsigned JWT carrying the given claims."""

    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


def srp_login_replies(id_token: str) -> list[tuple[int, Any]]:
    """Build the two responses of a successful SRP login."""
    return [
        (
            200,
            {
                "ChallengeName": "PASSWORD_VERIFIER",
                "ChallengeParameters": {
                    "SRP_B": "a3f" + "1" * 100,
                    "SALT": "9ab3",
                    "SECRET_BLOCK": "Ymxvcg==",
                    "USER_ID_FOR_SRP": "u42",
                },
            },
        ),
        (
            200,
            {
                "AuthenticationResult": {
                    "IdToken": id_token,
                    "AccessToken": "access",
                    "RefreshToken": "refresh",
                    "ExpiresIn": 3600,
                }
            },
        ),
    ]


@pytest.fixture
def installation() -> dict[str, Any]:
    """Provide an installation as the cloud returns it, trimmed to the essentials."""
    return {
        "serialNumber": SERIAL,
        "boardSerialNumber": BOARD_SERIAL,
        "currentInstallation": {"name": "Living room"},
        "productType": {"model": "1VMC01012B FlowPLUS"},
        "cloudBoard": {"firmware": "124", "serialNumber": BOARD_SERIAL},
    }


@pytest.fixture
def telemetry() -> list[Any]:
    """Provide a GET_STATUS payload actually observed (night mode)."""
    return [
        "0000000000000000",
        6,
        308,
        374,
        17,
        15,
        0,
        110,
        130,
        60,
        385,
        285,
        385,
        285,
        0,
        0,
        5796,
        54,
        23087,
        0,
    ]


@pytest.fixture
async def cloud(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeCloud]:
    """Start the fake backend and point the library at it."""
    fake = FakeCloud()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(client_module, "API_BASE", str(server.make_url("")).rstrip("/"))
    fake.base_url = str(server.make_url(""))  # type: ignore[attr-defined]
    try:
        yield fake
    finally:
        await server.close()


@pytest.fixture
async def client(cloud: FakeCloud) -> AsyncIterator[HeltyCloud]:
    """Provide an already "authenticated" client, so tests skip Cognito."""
    async with aiohttp.ClientSession() as session:
        helty = HeltyCloud("user@example.com", "secret", session=session)
        helty._tokens = Tokens(
            id_token="id-token",
            access_token="access-token",
            refresh_token="refresh-token",
            expires_at=time.time() + 3600,
        )
        # Cognito goes through the fake backend too, for token renewal tests.
        helty._auth._url = f"{cloud.base_url.rstrip('/')}{COGNITO_PATH}"  # type: ignore[attr-defined, union-attr]
        yield helty
