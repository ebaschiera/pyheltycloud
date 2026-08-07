"""Tests of the SRP flow against a fake Cognito.

Cryptographic correctness is verified against the real service; here we
check the mechanics: which calls go out, with which parameters, and how
anomalous responses are handled.
"""

from __future__ import annotations

import base64
from typing import Any

import aiohttp
import pytest

from pyheltycloud.auth import CognitoSrpAuth
from pyheltycloud.exceptions import HeltyCloudAuthError, HeltyCloudConnectionError

from .conftest import COGNITO_PATH, FakeCloud

# Plausible challenge values: they need not be real, only well-formed.
SRP_B = "a3f" + "1" * 100
SALT = "9ab3"
SECRET_BLOCK = base64.standard_b64encode(b"challenge-block").decode()

CHALLENGE: dict[str, Any] = {
    "ChallengeName": "PASSWORD_VERIFIER",
    "ChallengeParameters": {
        "SRP_B": SRP_B,
        "SALT": SALT,
        "SECRET_BLOCK": SECRET_BLOCK,
        "USER_ID_FOR_SRP": "internal-user-42",
    },
}
TOKENS: dict[str, Any] = {
    "AuthenticationResult": {
        "IdToken": "id",
        "AccessToken": "access",
        "RefreshToken": "refresh",
        "ExpiresIn": 3600,
    }
}


@pytest.fixture
async def auth(cloud: FakeCloud) -> Any:
    async with aiohttp.ClientSession() as session:
        srp = CognitoSrpAuth(session, "eu-central-1_lejYSlqKZ", "client-id", "eu-central-1")
        srp._url = f"{cloud.base_url.rstrip('/')}{COGNITO_PATH}"  # type: ignore[attr-defined]
        yield srp


async def test_full_srp_handshake(auth: CognitoSrpAuth, cloud: FakeCloud) -> None:
    cloud.reply_sequence(COGNITO_PATH, [(200, CHALLENGE), (200, TOKENS)])
    tokens = await auth.authenticate("user@example.com", "secret")

    assert tokens.id_token == "id"
    assert tokens.refresh_token == "refresh"
    assert not tokens.expired

    initiate, respond = (r for r in cloud.requests if r.path == COGNITO_PATH)
    assert initiate.json["AuthFlow"] == "USER_SRP_AUTH"
    assert initiate.json["AuthParameters"]["USERNAME"] == "user@example.com"
    assert initiate.json["AuthParameters"]["SRP_A"]

    responses = respond.json["ChallengeResponses"]
    assert respond.json["ChallengeName"] == "PASSWORD_VERIFIER"
    # From here on the internal identifier is used, not the login email.
    assert responses["USERNAME"] == "internal-user-42"
    assert responses["PASSWORD_CLAIM_SECRET_BLOCK"] == SECRET_BLOCK
    assert base64.standard_b64decode(responses["PASSWORD_CLAIM_SIGNATURE"])
    assert "UTC" in responses["TIMESTAMP"]


async def test_password_never_leaves_the_client(auth: CognitoSrpAuth, cloud: FakeCloud) -> None:
    """The point of SRP: the password appears in no request."""
    cloud.reply_sequence(COGNITO_PATH, [(200, CHALLENGE), (200, TOKENS)])
    await auth.authenticate("user@example.com", "super-secret")
    assert all("super-secret" not in str(r.json) for r in cloud.requests)


async def test_wrong_password_is_auth_error(auth: CognitoSrpAuth, cloud: FakeCloud) -> None:
    cloud.reply_sequence(
        COGNITO_PATH,
        [
            (200, CHALLENGE),
            (
                400,
                {"__type": "NotAuthorizedException", "message": "Incorrect username or password."},
            ),
        ],
    )
    with pytest.raises(HeltyCloudAuthError, match="Incorrect username or password"):
        await auth.authenticate("user@example.com", "wrong")


async def test_unexpected_challenge(auth: CognitoSrpAuth, cloud: FakeCloud) -> None:
    """Should they ever enable MFA, better a clear error."""
    cloud.reply(COGNITO_PATH, {"ChallengeName": "SMS_MFA", "ChallengeParameters": {}})
    with pytest.raises(HeltyCloudAuthError, match="SMS_MFA"):
        await auth.authenticate("user@example.com", "secret")


async def test_missing_tokens(auth: CognitoSrpAuth, cloud: FakeCloud) -> None:
    cloud.reply_sequence(COGNITO_PATH, [(200, CHALLENGE), (200, {})])
    with pytest.raises(HeltyCloudAuthError, match="token"):
        await auth.authenticate("user@example.com", "secret")


async def test_invalid_srp_b(auth: CognitoSrpAuth, cloud: FakeCloud) -> None:
    challenge = {
        **CHALLENGE,
        "ChallengeParameters": {**CHALLENGE["ChallengeParameters"], "SRP_B": "0"},
    }
    cloud.reply_sequence(COGNITO_PATH, [(200, challenge), (200, TOKENS)])
    with pytest.raises(HeltyCloudAuthError, match="SRP_B"):
        await auth.authenticate("user@example.com", "secret")


async def test_refresh_reuses_token(auth: CognitoSrpAuth, cloud: FakeCloud) -> None:
    cloud.reply(
        COGNITO_PATH,
        {"AuthenticationResult": {"IdToken": "id2", "AccessToken": "a2", "ExpiresIn": 60}},
    )
    tokens = await auth.refresh("old-refresh")
    assert tokens.id_token == "id2"
    assert tokens.refresh_token == "old-refresh"
    assert cloud.last(COGNITO_PATH).json["AuthFlow"] == "REFRESH_TOKEN_AUTH"


async def test_unreachable_cognito(auth: CognitoSrpAuth) -> None:
    auth._url = "http://127.0.0.1:1/"
    with pytest.raises(HeltyCloudConnectionError):
        await auth.authenticate("user@example.com", "secret")
