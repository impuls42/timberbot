"""Exception types raised by the Timberbot client."""
from __future__ import annotations

from typing import Any


class TimberbotError(Exception):
    """API returned an error response.

    `code` is the prefix before ':' in the error string; `response` is the full
    decoded payload from the mod.
    """

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.error = response.get("error", "unknown")
        self.code = self.error.split(":")[0].strip()
        super().__init__(self.error)


class AuthenticationError(TimberbotError):
    """The mod rejected the request with 401 Unauthorized.

    Raised when `authToken` is set on the server but the client either sent
    no `Authorization` header or sent the wrong bearer token. Callers can
    catch this specifically to prompt for a token or re-resolve credentials;
    `except TimberbotError` still works because it's a subclass.

    The response dict carries the server's error body when one was returned,
    or a synthesised `{"error": "unauthorized: ..."}` when the server replied
    401 with an unparseable body.
    """
