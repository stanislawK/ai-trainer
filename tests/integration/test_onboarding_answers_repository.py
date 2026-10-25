"""`onboarding_answers` and the summary's confirm on PostgreSQL (ADR-0028, ticket #254): a send
bumps the revision, a read writes only while its revision matches, and confirm writes the profile
and deletes the answers in one transaction. Every method is scoped to one athlete."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from uuid import UUID

import pytest
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.onboarding_repository import SqlAlchemyOnboardingRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.athlete_profile import AthleteProfile
from ai_trainer.domain.goals import Goal
from ai_trainer.domain.onboarding import AnswerStatus, OnboardingAnswer, OnboardingStep
from ai_trainer.domain.sport_profile import SportProfile
from ai_trainer.domain.users import NewUser, UserStatus

ABOUT_YOU = OnboardingStep.ABOUT_YOU
WHEN = datetime(2026, 10, 24, 18, 0, tzinfo=UTC)
PARSED: dict[str, JsonValue] = {
    "age": 31,
    "height_cm": 178,
    "weight_kg": 73.0,
    "limits": "Sore left shoulder",
}

type Factory = Callable[[], AsyncSession]


async def _user(factory: Factory, sub: str) -> UUID:
    created = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=None, locale="en", status=UserStatus.ACTIVE
        )
    )
    return created.id


@pytest.fixture
def repository(db_session_factory: Factory) -> SqlAlchemyOnboardingRepository:
    return SqlAlchemyOnboardingRepository(db_session_factory)


async def test_the_pick_makes_the_sport_the_only_one_and_stores_the_zone(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    user = await _user(db_session_factory, "pick-a")
    await repository.replace_sports(user, ["gym", "cycling"])

    await repository.start(user, "climbing", "Europe/Warsaw")

    assert list(await repository.list_sports(user)) == ["climbing"]
    stored = await SqlAlchemyUsersRepository(db_session_factory).get(user)
    assert stored is not None
    assert stored.timezone == "Europe/Warsaw"
    assert stored.onboarded_at is None


async def test_a_new_pick_drops_the_answers_of_the_sport_it_replaced(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    user = await _user(db_session_factory, "pick-b")
    await repository.start(user, "climbing", "UTC")
    await repository.store_answer(user, ABOUT_YOU, None, "31")
    await repository.store_answer(user, OnboardingStep.GOALS, "climbing", "7c")

    await repository.start(user, "gym", "UTC")

    assert [answer.step for answer in await repository.list_answers(user)] == [ABOUT_YOU]


async def test_another_sport_is_added_after_the_first_and_keeps_every_answer(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    """#256: "Add another sport?" appends the sport; the flow asks the sports in this order."""
    user = await _user(db_session_factory, "add-a")
    await repository.start(user, "gym", "UTC")
    await repository.skip_answer(user, OnboardingStep.GOALS, "gym")

    await repository.add_sport(user, "climbing")
    await repository.add_sport(user, "climbing")

    assert list(await repository.list_sports(user)) == ["gym", "climbing"]
    assert [a.sport_id for a in await repository.list_answers(user)] == ["gym"]


async def test_adding_a_sport_only_touches_the_given_user(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    mine = await _user(db_session_factory, "add-b")
    other = await _user(db_session_factory, "add-c")
    await repository.start(mine, "climbing", "UTC")
    await repository.start(other, "climbing", "UTC")

    await repository.add_sport(mine, "cycling")

    assert list(await repository.list_sports(other)) == ["climbing"]


async def test_a_send_stores_the_answer_pending_at_the_first_revision(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    user = await _user(db_session_factory, "send-a")

    revision = await repository.store_answer(user, ABOUT_YOU, None, "31, 178 cm")

    assert revision == 1
    assert list(await repository.list_answers(user)) == [
        OnboardingAnswer(
            step=ABOUT_YOU,
            sport_id=None,
            text="31, 178 cm",
            revision=1,
            status=AnswerStatus.PENDING,
        )
    ]


async def test_a_second_send_replaces_the_answer_and_bumps_the_revision(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    user = await _user(db_session_factory, "send-b")
    first = await repository.store_answer(user, ABOUT_YOU, None, "31")
    await repository.save_read(user, ABOUT_YOU, None, first, PARSED)

    second = await repository.store_answer(user, ABOUT_YOU, None, "32")

    assert second == first + 1
    [answer] = await repository.list_answers(user)
    assert (answer.text, answer.status, answer.parsed) == ("32", AnswerStatus.PENDING, None)


async def test_a_read_at_the_current_revision_is_saved(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    user = await _user(db_session_factory, "read-a")
    revision = await repository.store_answer(user, ABOUT_YOU, None, "31")

    assert await repository.save_read(user, ABOUT_YOU, None, revision, PARSED) is True

    [answer] = await repository.list_answers(user)
    assert (answer.status, answer.parsed) == (AnswerStatus.PARSED, PARSED)


async def test_a_read_of_an_older_revision_is_dropped(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    user = await _user(db_session_factory, "read-b")
    older = await repository.store_answer(user, ABOUT_YOU, None, "31")
    await repository.store_answer(user, ABOUT_YOU, None, "32")

    assert await repository.save_read(user, ABOUT_YOU, None, older, PARSED) is False
    assert await repository.mark_failed(user, ABOUT_YOU, None, older) is False

    [answer] = await repository.list_answers(user)
    assert (answer.status, answer.parsed) == (AnswerStatus.PENDING, None)


async def test_a_failed_read_marks_the_answer_and_a_later_read_still_lands(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    user = await _user(db_session_factory, "read-c")
    revision = await repository.store_answer(user, ABOUT_YOU, None, "31")

    assert await repository.mark_failed(user, ABOUT_YOU, None, revision) is True
    [failed] = await repository.list_answers(user)
    assert failed.status is AnswerStatus.FAILED

    assert await repository.save_read(user, ABOUT_YOU, None, revision, PARSED) is True
    [answer] = await repository.list_answers(user)
    assert answer.status is AnswerStatus.PARSED


async def test_a_skip_clears_the_text_and_any_read(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    user = await _user(db_session_factory, "skip-a")
    revision = await repository.store_answer(user, ABOUT_YOU, None, "31")
    await repository.save_read(user, ABOUT_YOU, None, revision, PARSED)

    skipped = await repository.skip_answer(user, ABOUT_YOU, None)

    assert skipped == revision + 1
    [answer] = await repository.list_answers(user)
    assert (answer.text, answer.status, answer.parsed) == (None, AnswerStatus.SKIPPED, None)
    # A read still in flight for the skipped answer cannot land.
    assert await repository.save_read(user, ABOUT_YOU, None, skipped, PARSED) is False


async def test_confirm_writes_the_profile_sports_zone_and_date_and_deletes_the_answers(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    user = await _user(db_session_factory, "confirm-a")
    await repository.start(user, "climbing", "UTC")
    await repository.store_answer(user, ABOUT_YOU, None, "31")
    profile = AthleteProfile(birth_year=1995, height_cm=178, weight_kg=72.5, limits="Shoulder")

    await repository.confirm(
        user,
        profile=profile,
        sport_ids=["climbing"],
        sport_profiles={},
        goals=[],
        timezone="Europe/Warsaw",
        when=WHEN,
    )

    assert await repository.get_profile(user) == profile
    assert list(await repository.list_sports(user)) == ["climbing"]
    assert list(await repository.list_answers(user)) == []
    stored = await SqlAlchemyUsersRepository(db_session_factory).get(user)
    assert stored is not None
    assert (stored.timezone, stored.onboarded_at) == ("Europe/Warsaw", WHEN)


async def test_confirming_again_replaces_the_profile(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    user = await _user(db_session_factory, "confirm-b")
    await repository.confirm(
        user,
        profile=AthleteProfile(height_cm=178),
        sport_ids=["climbing"],
        sport_profiles={},
        goals=[],
        timezone="UTC",
        when=WHEN,
    )

    await repository.confirm(
        user,
        profile=AthleteProfile(limits="Knee"),
        sport_ids=["gym"],
        sport_profiles={},
        goals=[],
        timezone="UTC",
        when=WHEN,
    )

    assert await repository.get_profile(user) == AthleteProfile(limits="Knee")
    assert list(await repository.list_sports(user)) == ["gym"]


async def test_a_confirm_that_fails_writes_nothing(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    user = await _user(db_session_factory, "confirm-c")
    await repository.start(user, "climbing", "UTC")
    await repository.store_answer(user, ABOUT_YOU, None, "31")

    with pytest.raises(Exception, match="athlete_profiles_height_range"):
        await repository.confirm(
            user,
            profile=AthleteProfile(height_cm=20),
            sport_ids=["gym"],
            sport_profiles={},
            goals=[],
            timezone="Europe/Warsaw",
            when=WHEN,
        )

    assert await repository.get_profile(user) is None
    assert list(await repository.list_sports(user)) == ["climbing"]
    assert len(await repository.list_answers(user)) == 1
    stored = await SqlAlchemyUsersRepository(db_session_factory).get(user)
    assert stored is not None
    assert (stored.timezone, stored.onboarded_at) == ("UTC", None)


async def test_one_athletes_answers_and_profile_never_reach_another(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    first = await _user(db_session_factory, "tenant-a")
    second = await _user(db_session_factory, "tenant-b")
    revision = await repository.store_answer(first, ABOUT_YOU, None, "31")
    await repository.confirm(
        first,
        profile=AthleteProfile(height_cm=178),
        sport_ids=[],
        sport_profiles={},
        goals=[],
        timezone="UTC",
        when=WHEN,
    )
    await repository.store_answer(first, ABOUT_YOU, None, "31")

    assert list(await repository.list_answers(second)) == []
    assert await repository.get_profile(second) is None
    assert await repository.save_read(second, ABOUT_YOU, None, revision, PARSED) is False
    assert await repository.mark_failed(second, ABOUT_YOU, None, revision) is False
    # Another athlete's send starts at their own first revision and leaves the first's alone.
    assert await repository.store_answer(second, ABOUT_YOU, None, "40") == 1
    [answer] = await repository.list_answers(first)
    assert answer.text == "31"

    await repository.confirm(
        second,
        profile=AthleteProfile(),
        sport_ids=[],
        sport_profiles={},
        goals=[],
        timezone="UTC",
        when=WHEN,
    )
    assert len(await repository.list_answers(first)) == 1
    assert await repository.get_profile(first) == AthleteProfile(height_cm=178)


async def test_answers_are_listed_in_step_order(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    user = await _user(db_session_factory, "order-a")
    await repository.store_answer(user, OnboardingStep.GOALS, "climbing", "7c")
    await repository.store_answer(user, ABOUT_YOU, None, "31")

    assert [answer.step for answer in await repository.list_answers(user)] == [
        ABOUT_YOU,
        OnboardingStep.GOALS,
    ]


async def test_discarding_the_answers_starts_onboarding_over_for_that_athlete_only(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    first = await _user(db_session_factory, "discard-a")
    second = await _user(db_session_factory, "discard-b")
    await repository.store_answer(first, ABOUT_YOU, None, "31")
    await repository.store_answer(second, ABOUT_YOU, None, "40")

    await repository.discard_answers(first)

    assert list(await repository.list_answers(first)) == []
    assert len(await repository.list_answers(second)) == 1


async def test_one_athletes_pick_leaves_anothers_sports_and_answers_alone(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    first = await _user(db_session_factory, "pick-tenant-a")
    second = await _user(db_session_factory, "pick-tenant-b")
    await repository.start(second, "climbing", "UTC")
    await repository.store_answer(second, OnboardingStep.GOALS, "climbing", "7c")

    await repository.start(first, "gym", "Europe/Warsaw")

    assert list(await repository.list_sports(second)) == ["climbing"]
    assert len(await repository.list_answers(second)) == 1


CLIMBING = SportProfile(
    level="Projecting 7b",
    exercises="Board, hangboard",
    sessions_per_week=4,
    session_minutes=120,
    preferred_days=(0, 2, 4, 5),
)
FIRST_7C = Goal(
    text="Send my first 7c outdoors", target_date=date(2027, 6, 30), sport_id="climbing"
)


async def test_confirm_writes_the_sports_profile_and_goals_with_the_rest(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    """Ticket #255: the sport's profile and goals are written in confirm's one transaction."""
    user = await _user(db_session_factory, "confirm-sport-a")
    await repository.start(user, "climbing", "UTC")

    await repository.confirm(
        user,
        profile=AthleteProfile(),
        sport_ids=["climbing"],
        sport_profiles={"climbing": CLIMBING},
        goals=[FIRST_7C, Goal(text="Climb outside more", sport_id="climbing")],
        timezone="UTC",
        when=WHEN,
    )

    assert await repository.list_sport_profiles(user) == {"climbing": CLIMBING}
    assert list(await repository.list_goals(user)) == [
        FIRST_7C,
        Goal(text="Climb outside more", sport_id="climbing"),
    ]


async def test_confirming_again_replaces_the_sport_profiles_and_goals(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    user = await _user(db_session_factory, "confirm-sport-b")
    await repository.replace_goals(user, [Goal(text="An old goal")])
    for sport_profiles, goals in (
        ({"climbing": CLIMBING}, [FIRST_7C]),
        ({"gym": SportProfile()}, []),
    ):
        await repository.confirm(
            user,
            profile=AthleteProfile(),
            sport_ids=list(sport_profiles),
            sport_profiles=sport_profiles,
            goals=goals,
            timezone="UTC",
            when=WHEN,
        )

    assert await repository.list_sport_profiles(user) == {"gym": SportProfile()}
    assert list(await repository.list_goals(user)) == []


async def test_a_confirm_with_a_sport_out_of_bounds_writes_nothing(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    user = await _user(db_session_factory, "confirm-sport-c")
    await repository.start(user, "climbing", "UTC")

    with pytest.raises(Exception, match="sport_profiles_sessions_range"):
        await repository.confirm(
            user,
            profile=AthleteProfile(height_cm=178),
            sport_ids=["climbing"],
            sport_profiles={"climbing": SportProfile(sessions_per_week=20)},
            goals=[FIRST_7C],
            timezone="UTC",
            when=WHEN,
        )

    assert await repository.get_profile(user) is None
    assert await repository.list_sport_profiles(user) == {}
    assert list(await repository.list_goals(user)) == []


async def test_one_athletes_sport_profiles_and_goals_never_reach_another(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    first = await _user(db_session_factory, "tenant-sport-a")
    second = await _user(db_session_factory, "tenant-sport-b")
    await repository.confirm(
        first,
        profile=AthleteProfile(),
        sport_ids=["climbing"],
        sport_profiles={"climbing": CLIMBING},
        goals=[FIRST_7C],
        timezone="UTC",
        when=WHEN,
    )

    await repository.confirm(
        second,
        profile=AthleteProfile(),
        sport_ids=["climbing"],
        sport_profiles={"climbing": SportProfile()},
        goals=[],
        timezone="UTC",
        when=WHEN,
    )

    assert await repository.list_sport_profiles(first) == {"climbing": CLIMBING}
    assert list(await repository.list_goals(first)) == [FIRST_7C]
    assert list(await repository.list_goals(second)) == []


async def test_confirm_saves_two_sports_profiles_and_goals(
    db_session_factory: Factory, repository: SqlAlchemyOnboardingRepository
) -> None:
    """#256: a second sport's section is saved alongside the first, each goal under its sport."""
    user = await _user(db_session_factory, "two-a")
    await repository.start(user, "climbing", "UTC")
    await repository.add_sport(user, "gym")
    goals = [
        Goal(text="First 7c", target_date=date(2027, 6, 30), sport_id="climbing"),
        Goal(text="Deadlift 150 kg", sport_id="gym"),
    ]

    await repository.confirm(
        user,
        profile=AthleteProfile(),
        sport_ids=["climbing", "gym"],
        sport_profiles={
            "climbing": SportProfile(level="Projecting 7b"),
            "gym": SportProfile(sessions_per_week=2, session_minutes=60),
        },
        goals=goals,
        timezone="Europe/Warsaw",
        when=WHEN,
    )

    assert sorted(await repository.list_sports(user)) == ["climbing", "gym"]
    assert list(await repository.list_goals(user)) == goals
    assert dict(await repository.list_sport_profiles(user)) == {
        "climbing": SportProfile(level="Projecting 7b"),
        "gym": SportProfile(sessions_per_week=2, session_minutes=60),
    }
