"""Generating one exercise picture (ADR-0031 *Picture generation*, #533): an active admin's run,
for one catalogue exercise, writing nothing to the media tables.

The brief is written from the exercise's name and public texts only (`exercise_texts` and the
run's own `search_knowledge` passages), never from the name alone. The run is bounded by
`IMAGE_GENERATION_SPEND_CAP_USD`, summed from its own `llm_calls` costs: a call whose cost is
unknown stops it, and it stops before the image call when the picture's price from `Settings`
would take it over the cap. Admin-run, so it is outside ADR-0029's per-user budget; the cap and
one exercise per run bound it instead (ADR-0031, owner-directed 2026-11-08)."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from ai_trainer.application.ports.llm_calls import LlmCallsRepositoryPort
from ai_trainer.application.ports.media_generation import (
    ImageGenerationPort,
    PassageSearchPort,
    PictureBriefWriterPort,
    PictureSubjectsPort,
)
from ai_trainer.application.ports.users import ActiveUsersByEmailPort
from ai_trainer.domain.knowledge_search import KnowledgeSearchQuery
from ai_trainer.domain.llm_calls import LlmCall, NewLlmCall
from ai_trainer.domain.media_generation import (
    AmbiguousExerciseName,
    BriefFailed,
    ImageGenerationFailed,
    NoBrief,
    PictureBrief,
    PictureFailed,
    PictureMade,
    PictureRefused,
    PictureResult,
    PictureSkipped,
    PictureSpendLimits,
    PictureSubject,
    SpendCapReached,
    needs_no_model,
    picture_brief_deps,
)
from ai_trainer.domain.users import is_admin_email

# The template's locale (`media.picture_brief` v1 is English), so its text is read in English
# first, else in any language.
_TEXT_LANG = "en"


class RunSpend:
    """Implements `LlmCallsRepositoryPort` over another (a Decorator): every row passes through,
    and the run's spend is summed from the rows it records. Built per run, in the composition
    root, so only this run's calls count toward its cap."""

    def __init__(self, calls: LlmCallsRepositoryPort) -> None:
        self._calls = calls
        self.spent_usd = Decimal(0)
        self.cost_unknown = False

    @property
    def reported_usd(self) -> Decimal | None:
        """The spend a result reports: unknown once a call's cost is unknown."""
        return None if self.cost_unknown else self.spent_usd

    async def record(self, call: NewLlmCall) -> LlmCall:
        recorded = await self._calls.record(call)
        if call.cost is None:
            self.cost_unknown = True
        else:
            self.spent_usd += call.cost
        return recorded

    async def list_for_user(self, user_id: UUID) -> list[LlmCall]:
        return await self._calls.list_for_user(user_id)


@dataclass(frozen=True, slots=True)
class PicturePorts:
    admins: ActiveUsersByEmailPort
    subjects: PictureSubjectsPort
    search: PassageSearchPort
    briefs: PictureBriefWriterPort
    images: ImageGenerationPort


class ExercisePictureService:
    def __init__(
        self,
        ports: PicturePorts,
        *,
        spend: RunSpend,
        limits: PictureSpendLimits,
        admin_emails: Sequence[str],
    ) -> None:
        self._ports = ports
        self._spend = spend
        self._limits = limits
        self._admin_emails = admin_emails

    async def generate(self, *, admin_email: str, exercise_name: str) -> PictureResult:
        admin = await self._admin(admin_email)
        if admin is None:
            return PictureRefused(reason="not_admin")
        subject = await self._ports.subjects.catalogue_exercise(exercise_name, lang=_TEXT_LANG)
        if subject is None:
            return PictureSkipped(reason="no_such_exercise", spent_usd=self._spend.reported_usd)
        if isinstance(subject, AmbiguousExerciseName):
            return PictureRefused(reason="ambiguous_name")
        brief = await self._brief(admin, subject)
        if not isinstance(brief, PictureBrief):
            return brief
        if stop := self._stop_before(self._limits.price_usd):
            return stop
        image = await self._ports.images.generate(user_id=admin, prompt=brief.image_prompt)
        if isinstance(image, ImageGenerationFailed):
            return PictureFailed(
                step="image", reason=image.reason, spent_usd=self._spend.reported_usd
            )
        return PictureMade(
            exercise_id=subject.exercise_id,
            brief=brief,
            image=image,
            spent_usd=self._spend.reported_usd,
        )

    async def _admin(self, email: str) -> UUID | None:
        """The admin's user ID: the email is in `ADMIN_EMAILS` (ADR-0005) and an active account
        has it, so no unverified `user_id` is written to `llm_calls` (ADR-0031)."""
        if not is_admin_email(email, self._admin_emails):
            return None
        user = await self._ports.admins.active_by_email(email)
        return None if user is None else user.id

    async def _brief(
        self, admin: UUID, subject: PictureSubject
    ) -> PictureBrief | PictureSkipped | SpendCapReached | PictureFailed:
        search = await self._ports.search.search(
            admin, KnowledgeSearchQuery(text=subject.name, sport_id=subject.sport_id)
        )
        deps = picture_brief_deps(subject.name, subject.text, search)
        if needs_no_model(deps) is not None:
            return PictureSkipped(reason="no_description", spent_usd=self._spend.reported_usd)
        # The brief's price isn't in `Settings`: only a cost already unknown or over the cap
        # stops the run here.
        if stop := self._stop_before(Decimal(0)):
            return stop
        brief = await self._ports.briefs.write(user_id=admin, deps=deps)
        if isinstance(brief, NoBrief):
            return PictureSkipped(reason="no_description", spent_usd=self._spend.reported_usd)
        if isinstance(brief, BriefFailed):
            return PictureFailed(
                step="brief", reason=brief.reason, spent_usd=self._spend.reported_usd
            )
        return brief

    def _stop_before(self, price_usd: Decimal) -> SpendCapReached | None:
        limits = self._limits
        if self._spend.cost_unknown:
            return SpendCapReached(
                spent_usd=None, price_usd=limits.price_usd, cap_usd=limits.cap_usd
            )
        if self._spend.spent_usd + price_usd > limits.cap_usd:
            return SpendCapReached(
                spent_usd=self._spend.spent_usd, price_usd=limits.price_usd, cap_usd=limits.cap_usd
            )
        return None
