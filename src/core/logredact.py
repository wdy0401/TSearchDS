"""Keep search keywords out of the log files.

The program promises not to keep a record of what was searched, and a log line
is a record.  Keywords travel in the query string of the index URLs
(``/q.php?q=火影忍者``), and third-party libraries are happy to log whole
requests: urllib3 at ``DEBUG`` prints every request line it sends, which is
exactly the case ``--verbose`` turns on.

A filter on the handlers rewrites the query string before anything is written,
so the promise holds no matter which library does the logging.
"""
from __future__ import annotations

import logging
import re

#: ``?key=value&...`` -- requires an ``=`` so that ordinary question marks in
#: messages are left alone, and stops at whitespace/quotes so the rest of the
#: line (status code, timing) survives.
_QUERY = re.compile(r"\?[^\s\"'=&]*=[^\s\"']*")

REDACTED = "?<已隐去>"


class QueryRedactingFilter(logging.Filter):
    """Rewrites any ``?key=value`` in a log record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken record must not be lost
            return True
        if "?" not in msg:
            return True
        red = _QUERY.sub(REDACTED, msg)
        if red != msg:
            # already formatted: drop the args so they are not formatted again
            record.msg = red
            record.args = ()
        return True


_FILTER = QueryRedactingFilter()


def install(*handlers: logging.Handler) -> None:
    """Attach the filter to each handler (idempotent)."""
    for h in handlers:
        if h is None:
            continue
        if not any(isinstance(f, QueryRedactingFilter) for f in h.filters):
            h.addFilter(_FILTER)


def redact(text: str) -> str:
    """Same rewriting, for strings that are logged by hand."""
    return _QUERY.sub(REDACTED, str(text or ""))
