"""The assistant's reply to an athlete's message (PRD-0003 F1, B13, B14, G3; ADR-0008 skeleton
routing): routed by the Jev fast path or else the LLM router, while a Jev safety screen runs
alongside (ADR-0020), dispatched safety first,
streamed, and saved only once all of it came through, so a retry after a failure always
streams a fresh reply. A log for a sport whose plugin names an extraction template becomes a
pending draft the athlete confirms later (PRD-0004 B1, F2, ADR-0022), offered as an addition to
their latest session in that sport when it came close after it (B16, ADR-0014). A log whose
sport stays split between two or more of the athlete's sports is a choice card first, and the
draft follows the tap (B17, B23, ADR-0015). A pending draft's sport can be changed on its card,
which extracts the same log again in the new sport (B23, ticket #163). A log of several sports
runs their extractions at once, one draft each in message order; one that fails beside a part
that came through is kept as a line its Retry can turn into the draft (B1, B14, ticket #161).
A log beyond the merge window, on the day of the athlete's latest session in its sport, is a
pending draft whose card asks whether it is that session, and the tap settles it (B16, ticket
#166). A log its sport's extraction can't read safely is a choice card of its readings, and the
tap resumes that extraction with the pick, which can then only draft (B17, ticket #164).

One module per use case (ticket #332): `turn` (the reply turn), `route`, `extract` (extract
and save), `clarification`, `change_sport` and `retry`, over what they share in `shared`."""

from ai_trainer.application.reply.change_sport import (
    asking_sports,
    change_draft_sport,
    sports_to_switch_to,
)
from ai_trainer.application.reply.clarification import (
    ClarificationAnswered,
    SessionSettled,
    abandon_clarification,
    answer_clarification,
)
from ai_trainer.application.reply.retry import RetriedDraft, RetryFailed, retry_draft
from ai_trainer.application.reply.route import route_message
from ai_trainer.application.reply.shared import (
    PART_BREAK,
    FailedPartNotFoundError,
    MessageNotFoundError,
    ReplyChunk,
    ReplyDone,
    ReplyEvent,
    ReplyFailed,
    ReplyPhase,
    ReplyServices,
    find_message,
)
from ai_trainer.application.reply.turn import find_saved_reply, start_reply, with_screened_safety

__all__ = [
    "PART_BREAK",
    "ClarificationAnswered",
    "FailedPartNotFoundError",
    "MessageNotFoundError",
    "ReplyChunk",
    "ReplyDone",
    "ReplyEvent",
    "ReplyFailed",
    "ReplyPhase",
    "ReplyServices",
    "RetriedDraft",
    "RetryFailed",
    "SessionSettled",
    "abandon_clarification",
    "answer_clarification",
    "asking_sports",
    "change_draft_sport",
    "find_message",
    "find_saved_reply",
    "retry_draft",
    "route_message",
    "sports_to_switch_to",
    "start_reply",
    "with_screened_safety",
]
