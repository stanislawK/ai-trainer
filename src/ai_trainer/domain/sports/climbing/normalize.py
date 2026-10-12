"""Grade scale detection (ADR-0006). Case matters: "6A" is Fontainebleau, "6a" French. A
string no pattern matches keeps its original text and gets no scale, so nothing is invented.
Only surrounding whitespace is trimmed."""

import re

from ai_trainer.domain.sports.climbing.payload import Grade, GradeScale

_SCALE_PATTERNS: tuple[tuple[GradeScale, re.Pattern[str]], ...] = (
    ("font", re.compile(r"[3-9][ABC]\+?")),
    ("french", re.compile(r"[3-9][abc]\+?")),
    # ASCII digits only, and only grades that exist: V0-V17 (plus VB), 5.0-5.15.
    ("v", re.compile(r"[Vv](B|1[0-7]|[0-9])[+-]?")),
    ("yds", re.compile(r"5\.(1[0-5]|[0-9])([abcd]|[+-])?")),
)


def detect_scale(original: str) -> GradeScale | None:
    for scale, pattern in _SCALE_PATTERNS:
        if pattern.fullmatch(original):
            return scale
    return None


def normalize_grade(original: str) -> Grade:
    text = original.strip()
    return Grade(original=text, scale=detect_scale(text))
