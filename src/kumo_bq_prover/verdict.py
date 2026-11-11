"""Verdicts. Unknown is better than wrong."""

from __future__ import annotations

import enum


class Verdict(enum.Enum):
    #: Proven to return the same rows on every input (sound, never a guess).
    PROVEN = "proven"
    #: A concrete input on which the two queries return different rows.
    REFUTED = "refuted"
    #: Neither. Includes "the emulator agreed on the data it saw".
    UNKNOWN = "unknown"
