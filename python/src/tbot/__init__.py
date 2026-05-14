"""tbot — Python client and CLI for the Timberbot Timberborn HTTP API."""
from tbot.api.client import TimberbotClient
from tbot.api.exceptions import TimberbotError

Timberbot = TimberbotClient

__version__ = "0.9.0"
__all__ = ["TimberbotClient", "Timberbot", "TimberbotError", "__version__"]
