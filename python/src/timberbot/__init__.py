"""timberbot — Python client and `tbot` CLI for the Timberbot Timberborn HTTP API."""
from timberbot.__about__ import OPENAPI_VERSION, __version__
from timberbot.api.client import TimberbotClient
from timberbot.api.exceptions import AuthenticationError, TimberbotError
from timberbot.paths import TimberbotPathError

Timberbot = TimberbotClient

__all__ = [
    "TimberbotClient",
    "Timberbot",
    "TimberbotError",
    "AuthenticationError",
    "TimberbotPathError",
    "__version__",
    "OPENAPI_VERSION",
]
