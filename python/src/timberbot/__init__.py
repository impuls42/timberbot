"""timberbot — Python client and `tbot` CLI for the Timberbot Timberborn HTTP API."""
from timberbot.__about__ import OPENAPI_VERSION, __version__
from timberbot.api.client import TimberbotClient
from timberbot.api.exceptions import AuthenticationError, TimberbotError

Timberbot = TimberbotClient

__all__ = [
    "TimberbotClient",
    "Timberbot",
    "TimberbotError",
    "AuthenticationError",
    "__version__",
    "OPENAPI_VERSION",
]
