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
