"""What every chat sub-router shares: its services, and the helpers more than one of them uses
to read the athlete's day and sports or to render a draft card (ticket #330)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from starlette.templating import Jinja2Templates

from ai_trainer.application.ports.chat import ChatRepositoryPort, ConversationRepositoryPort
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.reply import ReplyDone, ReplyServices, asking_sports
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.domain.training import StoredDraft
from ai_trainer.domain.users import User
from ai_trainer.web.drafts import DraftCard, draft_card, draft_cards, open_questions

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
        )

    async def shown_card(self, user: User, draft: StoredDraft) -> DraftCard:
        """A pending draft whose payload no longer reads has no card, so it is not found. A
        pending one asks its open same-session question (#166), and its chip offers no sport
        still asking how to read its log (#164)."""
        pending = draft.status == "pending"
        asked = (
            await self.replies.clarifications.list_shown(user.id, draft.conversation_id)
            if pending
            else ()
        )
        card = draft_card(
            draft,
            sports=self.sports,
            today=self.local_today(user),
            athlete_sports=await self.athlete_sports(user) if pending else (),
            question=open_questions(asked).get(draft.id),
            asking=asking_sports(asked, draft.message_id),
        )
        if card is None:
            raise HTTPException(status_code=NOT_FOUND)
        return card
