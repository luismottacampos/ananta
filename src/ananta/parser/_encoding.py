"""Encoding-fallback utilities for parsers that read text files.

Real-world repositories contain files in mixed encodings (Latin-1 author
names in CHANGELOGs, HTML test fixtures, etc.). Parsers must not abort
the entire repo ingest on a single bad byte, so we detect with chardet
and fall back to UTF-8 with replacement characters.
"""

from __future__ import annotations

from pathlib import Path

import chardet


def read_text_with_encoding_fallback(path: Path) -> str:
    """Read *path* as text, detecting encoding with chardet.

    Falls back to ``utf-8`` with ``errors="replace"`` if the detected
    encoding fails to decode (wrong guess, corrupt file, mixed encodings).
    Always returns a string; never raises ``UnicodeDecodeError``.
    """
    raw = path.read_bytes()
    detected = chardet.detect(raw)
    encoding = detected.get("encoding") or "utf-8"
    try:
        return raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return raw.decode("utf-8", errors="replace")
