"""Grade scale detection (ADR-0006). Case matters: "6A" is Fontainebleau, "6a" French, unless
the climb's kind says which: a grade written either way is Font on a boulder and French on a
route, since athletes type the letter in either case (ticket #164, owner-directed 2026-10-21).
A string no pattern matches keeps its original text and gets no scale, so nothing is invented.
Only surrounding whitespace is trimmed."""

import re

from ai_trainer.domain.sports.climbing.payload import ClimbingKind, Grade, GradeScale

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


def _kind_scale(kind: ClimbingKind) -> GradeScale:
    return "font" if kind == "boulder" else "french"


def normalize_grade(original: str, kind: ClimbingKind | None = None) -> Grade:
    """The grade as typed and its scale; with the climb's `kind`, a Font or French grade takes
    the kind's scale whatever its letter case."""
    text = original.strip()
    scale = detect_scale(text)
    if kind is not None and scale in ("font", "french"):
        scale = _kind_scale(kind)
    return Grade(original=text, scale=scale)


def grade_in_scale(original: str, kind: ClimbingKind) -> str:
    """The grade as a reading of `kind` would show it (ticket #164): a Font or French grade in
    the case of the kind's scale, Font for a boulder and French for a route, so "7a" read as a
    boulder is "7A". A grade of any other scale already says its scale and is left as typed."""
    text = original.strip()
    if detect_scale(text) not in ("font", "french"):
        return text
    return text.upper() if _kind_scale(kind) == "font" else text.lower()
