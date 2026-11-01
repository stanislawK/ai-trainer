"""What every chat sub-router shares: its services, and the helpers more than one of them uses
to read the athlete's day and sports or to render a draft card (ticket #330)."""

from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from pydantic import ValidationError
from starlette.templating import Jinja2Templates

from ai_trainer.application.ports.chat import ChatRepositoryPort, ConversationRepositoryPort
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.reply import ReplyDone, ReplyServices, asking_sports
from ai_trainer.domain.clarification import StoredClarification
from ai_trainer.domain.exercises import ExerciseLabel
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.domain.training import StoredDraft
from ai_trainer.domain.users import User
from ai_trainer.web.drafts import (
    DraftCard,
    answered_names,
    draft_card,
    draft_cards,
    open_questions,
)

UNPROCESSABLE = 422
NOT_FOUND = 404
CONFLICT = 409


def parse_id(raw: str) -> UUID:
    """A malformed ID is as unknown as a missing one: a 404, never a 422."""
    try:
        return UUID(raw)
    except ValueError:
        raise HTTPException(status_code=NOT_FOUND) from None


def is_htmx(request: Request) -> bool:
    """A swap of part of the page. htmx 4's Back/Forward restore also sends `HX-Request` but
    swaps the whole body, so it gets the full page (#213)."""
    return (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-History-Restore-Request") != "true"
    )


@dataclass(frozen=True, slots=True)
class ChatContext:
    templates: Jinja2Templates
    chat: ChatRepositoryPort
    replies: ReplyServices
    sports: SportRegistry
    retention_days: int

    @property
    def clock(self) -> ClockPort:
        return self.replies.clock

    @property
    def conversations(self) -> ConversationRepositoryPort:
        return self.replies.conversations

    def render(self, template_name: str, **context: Any) -> str:  # noqa: ANN401 -- template context is any value
        return self.templates.get_template(template_name).render(**context)

    def local_today(self, user: User) -> date:
        return self.clock.now().astimezone(ZoneInfo(user.timezone)).date()

    async def athlete_sports(self, user: User) -> Sequence[str]:
        """The athlete's own sports, the ones a pending card's chip can offer (#163)."""
        return await self.replies.profile.list_sports(user.id)

    async def draft_labels(
        self,
        user: User,
        drafts: Iterable[StoredDraft],
        questions: Iterable[StoredClarification] = (),
    ) -> Mapping[UUID, Mapping[str, ExerciseLabel]]:
        """How each draft's card names its settled entries, by the draft's ID, for a sport that
        settles names (F23, ticket #402). A payload that no longer reads has no labels, and a
        discarded or undone draft's row shows no names, so it reads none."""
        shown = list(questions)
        labels: dict[UUID, Mapping[str, ExerciseLabel]] = {}
        for draft in drafts:
            settler = self.replies.settlers.get(draft.sport_id)
            undone = draft.status == "confirmed" and draft.session_id is None
            if settler is None or undone or draft.status == "discarded":
                continue
            answered = {
                question.clarification.subject or "" for question in answered_names(shown, draft.id)
            }
            with suppress(ValidationError):
                labels[draft.id] = await settler.labels(
                    user.id, draft.draft.payload, answered=answered
                )
        return labels

    def current_answers(
        self, draft: StoredDraft, questions: Iterable[StoredClarification]
    ) -> tuple[StoredClarification, ...]:
        """The draft's answered exercise questions whose name is still on it: a sport change or
        a rename leaves a row that no longer says what will be logged (ticket #402)."""
        settler = self.replies.settlers.get(draft.sport_id)
        if settler is None:
            return ()
        answers = answered_names(questions, draft.id)
        try:
            return tuple(
                answer
                for answer in answers
                if settler.mentions(
                    draft.draft.payload,
                    answer.clarification.typed_name or answer.clarification.subject or "",
                )
            )
        except ValidationError:
            return ()

    def draft_answers(
        self, drafts: Iterable[StoredDraft], questions: Iterable[StoredClarification]
    ) -> Mapping[UUID, tuple[StoredClarification, ...]]:
        """`current_answers` of each draft, by the draft's ID."""
        shown = list(questions)
        return {draft.id: self.current_answers(draft, shown) for draft in drafts}

    async def reply_cards(
        self, user: User, message_id: UUID, done: ReplyDone
    ) -> Mapping[UUID, Sequence[DraftCard]]:
        return draft_cards(
            done.messages,
            done.drafts,
            sports=self.sports,
            today=self.local_today(user),
            athlete_sports=await self.athlete_sports(user) if done.drafts else (),
            log_id=message_id,
            questions=done.clarifications,
            labels=await self.draft_labels(user, done.drafts, done.clarifications),
            answers=self.draft_answers(done.drafts, done.clarifications),
        )

    async def shown_card(self, user: User, draft: StoredDraft) -> DraftCard:
        """A pending draft whose payload no longer reads has no card, so it is not found. A
        pending one asks its open same-session question (#166), and its chip offers no sport
        still asking how to read its log (#164). Its response also removes the rows above it that
        an edit or a sport change made stale, since it swaps only the card (#402)."""
        pending = draft.status == "pending"
        asked = (
            await self.replies.clarifications.list_shown(user.id, draft.conversation_id)
            if pending
            else ()
        )
        labels = await self.draft_labels(user, [draft], asked)
        current = self.current_answers(draft, asked)
        card = draft_card(
            draft,
            sports=self.sports,
            today=self.local_today(user),
            athlete_sports=await self.athlete_sports(user) if pending else (),
            question=open_questions(asked).get(draft.id),
            asking=asking_sports(asked, draft.message_id),
            labels=labels.get(draft.id),
            dropped=tuple(
                answer for answer in answered_names(asked, draft.id) if answer not in current
            ),
        )
        if card is None:
            raise HTTPException(status_code=NOT_FOUND)
        return card
