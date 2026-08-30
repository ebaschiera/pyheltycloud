"""AWS Cognito authentication (USER_SRP_AUTH) on aiohttp.

The Helty pool only accepts the SRP flow, so the password never travels:
knowledge of it is proven by signing a challenge. Implemented here instead
of using pycognito/boto3 because those libraries are synchronous and Home
Assistant wants async I/O.

Reference: SRP-6a with Cognito's specifics (key derived via HKDF and HMAC
signature of the challenge block).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp

from .exceptions import HeltyCloudAuthError, HeltyCloudConnectionError

# 3072-bit prime and generator, fixed by the Cognito protocol.
N_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05"
    "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB"
    "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AAAC42DAD33170D04507A33"
    "A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864"
    "D87602733EC86A64521F2B18177B200CBBE117577A615D6C770988C0BAD946E2"
    "08E24FA074E5AB3143DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF"
)
G_HEX = "2"
INFO_BITS = b"Caldera Derived Key"

# strftime is locale-dependent: Cognito wants the English names, so we spell
# them out.
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

# Renew the token a bit before it expires, to avoid being caught mid-call.
_EXPIRY_MARGIN = 60.0

# Overall timeout for each call to Cognito.
_TIMEOUT = aiohttp.ClientTimeout(total=30)


def _hash_sha256(buf: bytes | bytearray) -> str:
    digest = hashlib.sha256(buf).hexdigest()
    return digest.rjust(64, "0")


def _hex_hash(hex_string: str) -> str:
    return _hash_sha256(bytearray.fromhex(hex_string))


def _pad_hex(value: int | str) -> str:
    """Normalize a hex string the way SRP expects.

    Even length, and a leading zero byte if the first nibble has the high
    bit set (it would otherwise be read as a negative number).
    """
    hex_str = value if isinstance(value, str) else f"{value:x}"
    if len(hex_str) % 2 == 1:
        return f"0{hex_str}"
    if hex_str[0] in "89ABCDEFabcdef":
        return f"00{hex_str}"
    return hex_str


def _compute_hkdf(ikm: bytes | bytearray, salt: bytes | bytearray) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, INFO_BITS + b"\x01", hashlib.sha256).digest()[:16]


def _cognito_timestamp() -> str:
    now = datetime.now(UTC)
    return (
        f"{_WEEKDAYS[now.weekday()]} {_MONTHS[now.month - 1]} {now.day} "
        f"{now:%H:%M:%S} UTC {now.year}"
    )


@dataclass(slots=True)
class Tokens:
    """Cognito session tokens."""

    id_token: str
    access_token: str
    refresh_token: str
    expires_at: float

    @property
    def expired(self) -> bool:
        """True when it is time to renew before the next call."""
        return time.time() >= self.expires_at - _EXPIRY_MARGIN


class CognitoSrpAuth:
    """SRP login and token renewal against a Cognito user pool."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        user_pool_id: str,
        client_id: str,
        region: str,
    ) -> None:
        """Prepare the fixed SRP parameters of the pool."""
        self._session = session
        self._user_pool_id = user_pool_id
        self._client_id = client_id
        self._url = f"https://cognito-idp.{region}.amazonaws.com/"
        self._big_n = int(N_HEX, 16)
        self._g = int(G_HEX, 16)
        self._k = int(_hex_hash(f"00{N_HEX}0{G_HEX}"), 16)
        self._new_ephemeral()

    def _new_ephemeral(self) -> None:
        """Draw a fresh SRP secret and its public value.

        One per login: reusing the pair across authentications is outside
        what the protocol assumes of the ephemeral keys.
        """
        self._small_a = int(binascii.hexlify(os.urandom(128)), 16) % self._big_n
        self._big_a = pow(self._g, self._small_a, self._big_n)

    @property
    def _pool_name(self) -> str:
        """The pool id without the region prefix (needed in the signature)."""
        return self._user_pool_id.split("_", 1)[1]

    async def _call(self, target: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": f"AWSCognitoIdentityProviderService.{target}",
        }
        try:
            async with self._session.post(
                self._url, headers=headers, json=payload, timeout=_TIMEOUT
            ) as response:
                status = response.status
                text = await response.text()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise HeltyCloudConnectionError(f"Cognito is unreachable: {err}") from err

        # A proxy in front of Cognito answers with its own HTML error page,
        # which json() would blow up on with a ValueError: not one of ours,
        # and callers only guard against HeltyCloudError.
        try:
            body = json.loads(text)
        except ValueError as err:
            raise HeltyCloudConnectionError(
                f"{target}: Cognito answered HTTP {status} with a non-JSON body: {text[:200]}"
            ) from err
        if not isinstance(body, dict):
            raise HeltyCloudConnectionError(f"{target}: unexpected answer {text[:200]}")

        if status != 200:
            message = body.get("message") or body.get("__type") or str(body)
            raise HeltyCloudAuthError(f"{target}: {message}")
        return body

    def _password_key(self, username: str, password: str, srp_b_hex: str, salt_hex: str) -> bytes:
        big_b = int(srp_b_hex, 16)
        if big_b % self._big_n == 0:
            raise HeltyCloudAuthError("Invalid SRP_B returned by Cognito")

        u_value = int(_hex_hash(_pad_hex(self._big_a) + _pad_hex(big_b)), 16)
        if u_value == 0:
            raise HeltyCloudAuthError("U parameter is zero in the SRP negotiation")

        id_hash = _hash_sha256(f"{self._pool_name}{username}:{password}".encode())
        x_value = int(_hex_hash(_pad_hex(salt_hex) + id_hash), 16)

        g_mod_pow_xn = pow(self._g, x_value, self._big_n)
        base = (big_b - self._k * g_mod_pow_xn) % self._big_n
        s_value = pow(base, self._small_a + u_value * x_value, self._big_n)

        return _compute_hkdf(
            bytearray.fromhex(_pad_hex(s_value)),
            bytearray.fromhex(_pad_hex(f"{u_value:x}")),
        )

    async def authenticate(self, username: str, password: str) -> Tokens:
        """Run the full SRP login."""
        self._new_ephemeral()
        init = await self._call(
            "InitiateAuth",
            {
                "AuthFlow": "USER_SRP_AUTH",
                "ClientId": self._client_id,
                "AuthParameters": {
                    "USERNAME": username,
                    "SRP_A": f"{self._big_a:x}",
                },
            },
        )
        challenge = init.get("ChallengeParameters", {})
        if init.get("ChallengeName") != "PASSWORD_VERIFIER":
            raise HeltyCloudAuthError(
                f"Unexpected challenge from Cognito: {init.get('ChallengeName')}"
            )

        # Cognito may answer with an internal identifier different from the
        # one we logged in with: from here on, that one must be used.
        user_id = challenge["USER_ID_FOR_SRP"]
        secret_block = challenge["SECRET_BLOCK"]
        timestamp = _cognito_timestamp()

        hkdf = self._password_key(user_id, password, challenge["SRP_B"], challenge["SALT"])
        message = (
            self._pool_name.encode()
            + user_id.encode()
            + base64.standard_b64decode(secret_block)
            + timestamp.encode()
        )
        signature = base64.standard_b64encode(
            hmac.new(hkdf, message, hashlib.sha256).digest()
        ).decode()

        result = await self._call(
            "RespondToAuthChallenge",
            {
                "ChallengeName": "PASSWORD_VERIFIER",
                "ClientId": self._client_id,
                "ChallengeResponses": {
                    "USERNAME": user_id,
                    "TIMESTAMP": timestamp,
                    "PASSWORD_CLAIM_SECRET_BLOCK": secret_block,
                    "PASSWORD_CLAIM_SIGNATURE": signature,
                },
            },
        )
        return self._tokens_from(result)

    async def refresh(self, refresh_token: str) -> Tokens:
        """Renew the tokens without redoing the whole SRP dance."""
        result = await self._call(
            "InitiateAuth",
            {
                "AuthFlow": "REFRESH_TOKEN_AUTH",
                "ClientId": self._client_id,
                "AuthParameters": {"REFRESH_TOKEN": refresh_token},
            },
        )
        # The refresh response does not repeat the refresh token: reuse it.
        return self._tokens_from(result, refresh_token=refresh_token)

    @staticmethod
    def _tokens_from(payload: dict[str, Any], refresh_token: str = "") -> Tokens:
        auth = payload.get("AuthenticationResult")
        if not auth:
            raise HeltyCloudAuthError("Cognito did not return the tokens")
        return Tokens(
            id_token=auth["IdToken"],
            access_token=auth["AccessToken"],
            refresh_token=auth.get("RefreshToken", refresh_token),
            expires_at=time.time() + auth.get("ExpiresIn", 3600),
        )
