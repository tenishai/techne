"""Worker process for `archivist_query` (techne#1313).

Reads one JSON request on stdin, runs the guarded query with SQLite's heap capped,
and writes one JSON reply on stdout: {"ok": result}, {"denied": message}, or
{"sqlite_error": class name, "message": text}. Started by
`archivist_query._run_in_worker`; not a command for people.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from techne.core.archivist_query import QueryDenied, _execute


def main() -> int:
    req = json.load(sys.stdin)
    try:
        reply = {
            "ok": _execute(
                Path(req["path"]), req["sql"], int(req["cap"]), int(req["heap_limit_bytes"])
            )
        }
    except QueryDenied as e:
        reply = {"denied": str(e)}
    except sqlite3.Error as e:
        reply = {"sqlite_error": type(e).__name__, "message": str(e)}
    json.dump(reply, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
