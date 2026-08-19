"""Console helpers."""
from __future__ import annotations

import io
import sys


def ensure_utf8_stdout() -> None:
    """Windows consoles default to cp1252, which chokes on emoji team names
    that show up in real Sleeper league data. Force UTF-8 on stdout/stderr.
    """
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
