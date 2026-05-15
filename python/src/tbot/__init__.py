"""tbot — Python client and CLI for the Timberbot Timberborn HTTP API."""
from tbot.__about__ import OPENAPI_VERSION, __version__
from tbot.api.client import TimberbotClient
from tbot.api.exceptions import TimberbotError

Timberbot = TimberbotClient

__all__ = [
    "TimberbotClient",
    "Timberbot",
    "TimberbotError",
    "__version__",
    "OPENAPI_VERSION",
]
