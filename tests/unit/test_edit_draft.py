"""`edit_draft` and `draft_to_edit` (PRD-0004 F2, ADR-0014 invariant 4, ticket #158): the
athlete's edit replaces the pending draft's date, duration, RPE, feeling and the sport's own
fields; a bad value names its field and changes nothing; only a pending draft can be edited."""

from datetime import date
from uuid import uuid4

import pytest
from pydantic import JsonValue

from ai_trainer.application.drafts import confirm_draft, draft_to_edit, edit_draft, find_draft
from ai_trainer.domain.sports.climbing.payload import ClimbingPayload, Grade
from ai_trainer.domain.sports.registry import SportRegistry, default_sport_registry
from ai_trainer.domain.training import (
    DraftConfirmedError,
    DraftDiscardedError,
    DraftEditError,
    DraftNotFoundError,
)
from tests.chat_support import FixedClock, InMemoryDraftRepository, pending_draft

_SPORTS = default_sport_registry()
_TODAY = date(2026, 10, 2)
_YESTERDAY = date(2026, 10, 1)


def _form(**fields: JsonValue) -> dict[str, JsonValue]:
    """The edit form as the card posts it for `THREE_6A_TWO_6B`, with `fields` changed."""
    form: dict[str, JsonValue] = {
        "occurred_on": _TODAY.isoformat(),
        "duration_min": "",
        "rpe": "",
        "feeling": "",
        "activities": [
            {"count": "3", "grade": "6A", "style": ""},
            {"count": "2", "grade": "6B", "style": ""},
        ],
    }
    form.update(fields)
    return form


async def test_a_changed_date_is_saved_as_stated_and_confirmed_on_that_day() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id)

    edited = await edit_draft(
        user_id, draft.id, _form(occurred_on="2026-10-01"), drafts=drafts, sports=_SPORTS
    )
    confirmed = await confirm_draft(user_id, draft.id, drafts=drafts, clock=FixedClock())

    assert (edited.status, edited.draft.occurred_on) == ("pending", _YESTERDAY)
    assert edited.draft.date_source == "stated"
    assert confirmed.draft.occurred_on == _YESTERDAY


async def test_an_unchanged_date_keeps_how_it_was_found() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id)

    edited = await edit_draft(user_id, draft.id, _form(rpe="7"), drafts=drafts, sports=_SPORTS)

    assert (edited.draft.occurred_on, edited.draft.date_source) == (_TODAY, "assumed_today")


async def test_the_envelope_and_the_sport_fields_are_replaced() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id)
    form = _form(
        duration_min="90",
        rpe="8",
        feeling="  strong  ",
        activities=[
            {"count": "4", "grade": "6A", "style": "flash"},
            {"count": "2", "grade": "6C", "style": ""},
        ],
    )

    edited = await edit_draft(user_id, draft.id, form, drafts=drafts, sports=_SPORTS)

    assert (edited.draft.duration_min, edited.draft.rpe, edited.draft.feeling) == (
        90,
        8,
        "strong",
    )
    payload = ClimbingPayload.model_validate(edited.draft.payload)
    assert [(a.kind, a.count, a.grade, a.style) for a in payload.activities] == [
        ("boulder", 4, Grade(original="6A", scale="font"), "flash"),
        ("boulder", 2, Grade(original="6C", scale="font"), None),
    ]
    assert edited.payload_version == 1
    assert drafts.all_of(user_id) == [edited]


async def test_blank_optional_fields_clear_them() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id)
    await edit_draft(user_id, draft.id, _form(rpe="7", feeling="ok"), drafts=drafts, sports=_SPORTS)

    edited = await edit_draft(user_id, draft.id, _form(), drafts=drafts, sports=_SPORTS)

    assert (edited.draft.rpe, edited.draft.feeling, edited.draft.duration_min) == (None, None, None)


@pytest.mark.parametrize(
    ("fields", "invalid"),
    [
        ({"rpe": "11"}, {"rpe"}),
        ({"rpe": "0"}, {"rpe"}),
        ({"rpe": "hard"}, {"rpe"}),
        ({"duration_min": "1441"}, {"duration_min"}),
        ({"feeling": "x" * 201}, {"feeling"}),
        ({"occurred_on": ""}, {"occurred_on"}),
        ({"occurred_on": "yesterday"}, {"occurred_on"}),
        (
            {"rpe": "11", "activities": [{"count": "0", "grade": "6A", "style": "dyno"}]},
            {"rpe", "activities.0.count", "activities.0.style"},
        ),
    ],
)
async def test_a_bad_value_names_its_field_and_the_draft_is_unchanged(
    fields: dict[str, JsonValue], invalid: set[str]
) -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id)

    with pytest.raises(DraftEditError) as caught:
        await edit_draft(user_id, draft.id, _form(**fields), drafts=drafts, sports=_SPORTS)

    assert caught.value.fields == frozenset(invalid)
    assert drafts.all_of(user_id) == [draft]


async def test_editing_a_confirmed_draft_is_refused_and_it_stays_as_it_was() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id, status="confirmed")

    with pytest.raises(DraftConfirmedError):
        await draft_to_edit(user_id, draft.id, drafts=drafts)
    with pytest.raises(DraftConfirmedError):
        await edit_draft(
            user_id, draft.id, _form(occurred_on="2026-10-01"), drafts=drafts, sports=_SPORTS
        )

    assert drafts.all_of(user_id) == [draft]


async def test_editing_a_discarded_draft_is_refused() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id, status="discarded")

    with pytest.raises(DraftDiscardedError):
        await draft_to_edit(user_id, draft.id, drafts=drafts)
    with pytest.raises(DraftDiscardedError):
        await edit_draft(user_id, draft.id, _form(), drafts=drafts, sports=_SPORTS)

    assert drafts.all_of(user_id) == [draft]


async def test_another_users_draft_is_not_found_and_unchanged() -> None:
    drafts = InMemoryDraftRepository()
    owner = uuid4()
    draft = await pending_draft(drafts, owner)
    stranger = uuid4()

    with pytest.raises(DraftNotFoundError):
        await find_draft(stranger, draft.id, drafts=drafts)
    with pytest.raises(DraftNotFoundError):
        await draft_to_edit(stranger, draft.id, drafts=drafts)
    with pytest.raises(DraftNotFoundError):
        await edit_draft(stranger, draft.id, _form(rpe="5"), drafts=drafts, sports=_SPORTS)

    assert drafts.all_of(owner) == [draft]


async def test_a_pending_draft_is_found_and_open_to_edit() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id)

    assert await find_draft(user_id, draft.id, drafts=drafts) == draft
    assert await draft_to_edit(user_id, draft.id, drafts=drafts) == draft


async def test_a_draft_whose_sport_cant_be_edited_is_refused_as_not_found() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id)
    no_extraction = SportRegistry(
        plugin.model_copy(update={"extraction": None}) for plugin in _SPORTS.all()
    )

    with pytest.raises(DraftNotFoundError):
        await edit_draft(user_id, draft.id, _form(), drafts=drafts, sports=no_extraction)
    with pytest.raises(DraftNotFoundError):
        await edit_draft(user_id, draft.id, _form(), drafts=drafts, sports=SportRegistry([]))

    assert drafts.all_of(user_id) == [draft]


async def test_values_that_are_already_typed_are_taken_as_they_are() -> None:
    drafts = InMemoryDraftRepository()
    user_id = uuid4()
    draft = await pending_draft(drafts, user_id)

    edited = await edit_draft(
        user_id, draft.id, {"rpe": 6, "duration_min": 45}, drafts=drafts, sports=_SPORTS
    )

    assert (edited.draft.rpe, edited.draft.duration_min) == (6, 45)
    assert edited.draft.payload == draft.draft.payload
