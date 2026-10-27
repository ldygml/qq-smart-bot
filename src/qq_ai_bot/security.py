from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def request_is_authorized(
    *,
    token: str,
    headers: Mapping[str, str],
    body: bytes,
    client_host: str | None,
) -> bool:
    if not token:
        # Binding to loopback remains usable without a token during local testing.
        return client_host in LOOPBACK_HOSTS

    authorization = headers.get("authorization", "")
    bearer = ""
    if authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()

    provided_tokens = (
        bearer,
        headers.get("x-onebot-token", "").strip(),
        headers.get("x-access-token", "").strip(),
    )
    if any(hmac.compare_digest(value, token) for value in provided_tokens if value):
        return True

    signature = headers.get("x-signature", "").strip()
    if signature.startswith("sha1="):
        expected = hmac.new(
            token.encode("utf-8"),
            body,
            hashlib.sha1,
        ).hexdigest()
        return hmac.compare_digest(signature[5:], expected)

    return False

