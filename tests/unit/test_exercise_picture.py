"""Generating one exercise picture (ADR-0031 *Picture generation*, #533): an active admin only, a
catalogue exercise with a text, its brief through the text model, then the picture through the
image port, stopping before the image call when its price would pass the run's cap or a cost is
unknown. Every port is a fake: no network in pytest."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from ai_trainer.application.exercise_picture import ExercisePictureService, PicturePorts, RunSpend
from ai_trainer.domain.exercises import ExerciseText
from ai_trainer.domain.knowledge import EvidenceTier, SourceLanguage, SourceType
from ai_trainer.domain.knowledge_document import PageLocator
from ai_trainer.domain.knowledge_search import (
    KnowledgeHit,
    KnowledgeSearchFailed,
    KnowledgeSearchPage,
    KnowledgeSearchQuery,
)
from ai_trainer.domain.llm_calls import LlmCall, LlmCallOutcome, NewLlmCall
from ai_trainer.domain.media_generation import (
    BriefFailed,
    GeneratedImage,
    ImageGenerationFailed,
    NoBrief,
    PictureBrief,
    PictureBriefDeps,
    PictureFailed,
    PictureMade,
    PictureRefused,
    PictureSkipped,
    PictureSpendLimits,
    PictureSubject,
    SpendCapReached,
)
from ai_trainer.domain.users import User, UserStatus

_AT = datetime(2026, 11, 8, tzinfo=UTC)
_ADMIN_EMAIL = "Owner@Example.com"
_PULL_UP_TEXT = ExerciseText(
    lang="en",
    instructions="Hang from a pull-up bar with your arms fully extended, then pull your chest up.",
    steps=(),
)
_BRIEF = PictureBrief(
    exercise_name="Pull-Up",
    start_position="Hanging, arms extended",
    end_position="Chest at the bar",
    image_prompt="One person hanging, then chest at the bar. Plain background. No text.",
)
_IMAGE = GeneratedImage(data=b"\xff\xd8jpeg", media_type="image/jpeg")
_PRICE = Decimal("0.0336")


class FakeCalls:
    def __init__(self) -> None:
        self.recorded: list[NewLlmCall] = []

    async def record(self, call: NewLlmCall) -> LlmCall:
        self.recorded.append(call)
        return LlmCall(id=uuid4(), created_at=_AT, **call.model_dump())

    async def list_for_user(self, user_id: UUID) -> list[LlmCall]:
        return [
            LlmCall(id=uuid4(), created_at=_AT, **call.model_dump())
            for call in self.recorded
            if call.user_id == user_id
        ]


def _row(user_id: UUID, cost: Decimal | None, template_id: str = "x") -> NewLlmCall:
    return NewLlmCall(
        user_id=user_id,
        template_id=template_id,
        template_version=1,
        model="m",
        input_tokens=1,
        output_tokens=1,
        cost=cost,
        latency_ms=1,
        outcome=LlmCallOutcome.SUCCESS,
    )


class FakeAdmins:
    def __init__(self, users: list[User]) -> None:
        self.users = users
        self.asked: list[str] = []

    async def active_by_email(self, email: str) -> User | None:
        self.asked.append(email)
        return next(
            (
                user
                for user in self.users
                if user.email.casefold() == email.casefold() and user.status is UserStatus.ACTIVE
            ),
            None,
        )


class FakeSubjects:
    def __init__(self, subjects: dict[str, PictureSubject]) -> None:
        self.subjects = subjects
        self.langs: list[str] = []

    async def catalogue_exercise(self, name: str, *, lang: str) -> PictureSubject | None:
        self.langs.append(lang)
        return self.subjects.get(name.casefold())


@dataclass
class FakeSearch:
    """Charges each search's embedding call to the run, as the real one does."""

    spend: RunSpend
    result: KnowledgeSearchPage | KnowledgeSearchFailed
    cost: Decimal | None = Decimal("0.00001")
    queries: list[tuple[UUID, KnowledgeSearchQuery]] = field(default_factory=list)

    async def search(
        self, user_id: UUID, query: KnowledgeSearchQuery
    ) -> KnowledgeSearchPage | KnowledgeSearchFailed:
        self.queries.append((user_id, query))
        await self.spend.record(_row(user_id, self.cost, "knowledge.query"))
        return self.result


@dataclass
class FakeBriefs:
    spend: RunSpend
    reply: PictureBrief | NoBrief | BriefFailed = field(default_factory=lambda: _BRIEF)
    cost: Decimal | None = Decimal("0.0002")
    written: list[tuple[UUID, PictureBriefDeps]] = field(default_factory=list)

    async def write(
        self, *, user_id: UUID, deps: PictureBriefDeps
    ) -> PictureBrief | NoBrief | BriefFailed:
        self.written.append((user_id, deps))
        await self.spend.record(_row(user_id, self.cost, "media.picture_brief"))
        return self.reply


@dataclass
class FakeImageGenerator:
    spend: RunSpend
    reply: GeneratedImage | ImageGenerationFailed = _IMAGE
    cost: Decimal | None = _PRICE
    prompts: list[tuple[UUID, str]] = field(default_factory=list)

    async def generate(
        self, *, user_id: UUID, prompt: str
    ) -> GeneratedImage | ImageGenerationFailed:
        self.prompts.append((user_id, prompt))
        await self.spend.record(_row(user_id, self.cost, "media.picture"))
        return self.reply


def _user(email: str = "owner@example.com", status: UserStatus = UserStatus.ACTIVE) -> User:
    return User(
        id=uuid4(),
        sub=f"sub-{uuid4()}",
        email=email,
        name=None,
        locale="en",
        status=status,
        created_at=_AT,
    )


def _hit(text: str) -> KnowledgeHit:
    return KnowledgeHit(
        chunk_id=uuid4(),
        text=text,
        heading_path=("Pulling",),
        language=SourceLanguage.EN,
        source_id=uuid4(),
        title="A guide",
        author=None,
        evidence_tier=EvidenceTier.COACHING_BOOK,
        source_type=SourceType.BOOK,
        source_url=None,
        attribution=None,
        locator=PageLocator(page=1),
        exercise_id=None,
    )


@dataclass
class Run:
    service: ExercisePictureService
    calls: FakeCalls
    admins: FakeAdmins
    subjects: FakeSubjects
    search: FakeSearch
    briefs: FakeBriefs
    images: FakeImageGenerator


def _run(
    *,
    users: list[User] | None = None,
    text: ExerciseText | None = _PULL_UP_TEXT,
    search: KnowledgeSearchPage | KnowledgeSearchFailed | None = None,
    cap: Decimal = Decimal("0.25"),
    admin_emails: tuple[str, ...] = ("owner@example.com",),
) -> Run:
    calls = FakeCalls()
    spend = RunSpend(calls)
    admins = FakeAdmins([_user()] if users is None else users)
    subjects = FakeSubjects({"pull-up": PictureSubject(name="Pull-Up", sport_id="gym", text=text)})
    searcher = FakeSearch(spend, search or KnowledgeSearchFailed(reason="no_match"))
    briefs = FakeBriefs(spend)
    images = FakeImageGenerator(spend)
    service = ExercisePictureService(
        PicturePorts(
            admins=admins, subjects=subjects, search=searcher, briefs=briefs, images=images
        ),
        spend=spend,
        limits=PictureSpendLimits(cap_usd=cap, price_usd=_PRICE),
        admin_emails=admin_emails,
    )
    return Run(service, calls, admins, subjects, searcher, briefs, images)


async def test_service_uses_only_the_fake_image_port() -> None:
    """AC: tests use a fake port; no network in pytest. The picture comes from the image port,
    drawn from the brief's image prompt, as the admin."""
    run = _run()

    result = await run.service.generate(admin_email=_ADMIN_EMAIL, exercise_name="pull-up")

    assert isinstance(result, PictureMade)
    assert result.image == _IMAGE
    assert result.brief == _BRIEF
    [admin] = run.admins.users
    assert run.images.prompts == [(admin.id, _BRIEF.image_prompt)]
    assert result.spent_usd == Decimal("0.00001") + Decimal("0.0002") + _PRICE


async def test_the_brief_reads_the_catalogue_text_and_the_runs_own_passages() -> None:
    run = _run(
        search=KnowledgeSearchPage(hits=(_hit("Pull until the chin clears."),), truncated=False)
    )

    await run.service.generate(admin_email=_ADMIN_EMAIL, exercise_name="Pull-Up")

    [admin] = run.admins.users
    assert run.subjects.langs == ["en"]
    [(searched_as, query)] = run.search.queries
    assert searched_as == admin.id
    assert query == KnowledgeSearchQuery(text="Pull-Up", sport_id="gym")
    [(written_as, deps)] = run.briefs.written
    assert written_as == admin.id
    assert deps.exercise_name == "Pull-Up"
    assert deps.description == _PULL_UP_TEXT.instructions
    assert [passage.text for passage in deps.passages] == ["Pull until the chin clears."]


async def test_stops_before_the_image_call_when_the_cap_would_be_exceeded() -> None:
    """AC: a run stops before the call when the cap would be exceeded."""
    run = _run(cap=Decimal("0.03"))

    result = await run.service.generate(admin_email=_ADMIN_EMAIL, exercise_name="pull-up")

    assert result == SpendCapReached(
        spent_usd=Decimal("0.00021"), price_usd=_PRICE, cap_usd=Decimal("0.03")
    )
    assert run.images.prompts == []


async def test_a_picture_that_fits_the_cap_exactly_is_drawn() -> None:
    cap = Decimal("0.00001") + Decimal("0.0002") + _PRICE

    result = await _run(cap=cap).service.generate(admin_email=_ADMIN_EMAIL, exercise_name="pull-up")

    assert isinstance(result, PictureMade)


async def test_null_cost_stops_the_run() -> None:
    """A call whose cost is unknown stops the run before the next call (ADR-0031)."""
    run = _run()
    run.briefs.cost = None

    result = await run.service.generate(admin_email=_ADMIN_EMAIL, exercise_name="pull-up")

    assert result == SpendCapReached(spent_usd=None, price_usd=_PRICE, cap_usd=Decimal("0.25"))
    assert run.images.prompts == []


async def test_a_search_with_an_unknown_cost_stops_the_run_before_the_brief() -> None:
    run = _run()
    run.search.cost = None

    result = await run.service.generate(admin_email=_ADMIN_EMAIL, exercise_name="pull-up")

    assert isinstance(result, SpendCapReached)
    assert result.spent_usd is None
    assert run.briefs.written == []
    assert run.images.prompts == []


async def test_a_cap_already_spent_stops_the_run_before_the_brief() -> None:
    run = _run(cap=Decimal("0.000001"))

    result = await run.service.generate(admin_email=_ADMIN_EMAIL, exercise_name="pull-up")

    assert result == SpendCapReached(
        spent_usd=Decimal("0.00001"), price_usd=_PRICE, cap_usd=Decimal("0.000001")
    )
    assert run.briefs.written == []


async def test_an_email_outside_admin_emails_is_refused_with_no_call() -> None:
    run = _run(users=[_user("athlete@example.com")])

    result = await run.service.generate(admin_email="athlete@example.com", exercise_name="pull-up")

    assert result == PictureRefused(reason="not_admin")
    assert run.admins.asked == []
    assert run.calls.recorded == []
    assert run.subjects.langs == []


async def test_a_listed_email_with_no_active_user_is_refused_with_no_call() -> None:
    run = _run(users=[_user(status=UserStatus.PENDING)])

    result = await run.service.generate(admin_email=_ADMIN_EMAIL, exercise_name="pull-up")

    assert result == PictureRefused(reason="not_admin")
    assert run.calls.recorded == []
    assert run.subjects.langs == []


async def test_no_such_exercise_ends_the_run_with_no_call() -> None:
    run = _run()

    result = await run.service.generate(admin_email=_ADMIN_EMAIL, exercise_name="Moon Walk")

    assert result == PictureSkipped(reason="no_such_exercise", spent_usd=Decimal(0))
    assert run.calls.recorded == []


async def test_no_text_and_no_passage_is_skipped_without_a_brief_or_image_call() -> None:
    """Never drawn from the name alone (ADR-0031, finding 8)."""
    run = _run(text=None)

    result = await run.service.generate(admin_email=_ADMIN_EMAIL, exercise_name="pull-up")

    assert result == PictureSkipped(reason="no_description", spent_usd=Decimal("0.00001"))
    assert run.briefs.written == []
    assert run.images.prompts == []


async def test_a_model_no_brief_is_skipped_without_an_image_call() -> None:
    run = _run()
    run.briefs.reply = NoBrief(reason="it doesn't say how")

    result = await run.service.generate(admin_email=_ADMIN_EMAIL, exercise_name="pull-up")

    assert result == PictureSkipped(reason="no_description", spent_usd=Decimal("0.00021"))
    assert run.images.prompts == []


async def test_a_failed_brief_fails_the_run_without_an_image_call() -> None:
    run = _run()
    run.briefs.reply = BriefFailed(reason="timeout")

    result = await run.service.generate(admin_email=_ADMIN_EMAIL, exercise_name="pull-up")

    assert result == PictureFailed(step="brief", reason="timeout", spent_usd=Decimal("0.00021"))
    assert run.images.prompts == []


async def test_a_failed_image_fails_the_run() -> None:
    run = _run()
    run.images.reply = ImageGenerationFailed(reason="error")
    run.images.cost = None

    result = await run.service.generate(admin_email=_ADMIN_EMAIL, exercise_name="pull-up")

    assert result == PictureFailed(step="image", reason="error", spent_usd=Decimal("0.00021"))


async def test_run_spend_passes_rows_through_and_reads_them_back() -> None:
    calls = FakeCalls()
    spend = RunSpend(calls)
    user_id = uuid4()

    await spend.record(_row(user_id, Decimal("0.5")))

    assert [row.cost for row in await spend.list_for_user(user_id)] == [Decimal("0.5")]
    assert spend.spent_usd == Decimal("0.5")
    assert spend.cost_unknown is False
