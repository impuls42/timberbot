"""Single-source version constants.

`OPENAPI_VERSION` ties the Python and C# halves to the same HTTP contract; the
mod surfaces its value via `/api/ping` and `TimberbotClient.ping()` warns if
the major version disagrees with what the installed `tbot` package expects.
"""

__version__ = "0.9.0"

# Major version of the HTTP contract authored at /openapi.yaml. Bump when a
# breaking change ships. The C# side has the same constant in
# TimberbotPure.cs - keep them in lockstep.
OPENAPI_VERSION = "1.0.0"
