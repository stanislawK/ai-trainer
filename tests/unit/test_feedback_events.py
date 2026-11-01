"""Feedback events from the draft actions (ADR-0034, PRD-0010 G6, ticket #394): an edit, a
discard and an undo each write one content-free row, an edit names only the fields it changed,
and a failing store never fails the athlete's action."""

from uuid import uuid4

import pytest
from pydantic import JsonValue

from ai_trainer.application.drafts import discard_draft, edit_draft, undo_log
from ai_trainer.application.feedback import FeedbackRecorder, changed_fields, count_signals
from ai_trainer.domain.feedback import FeedbackKind, FeedbackSignal
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.training import SessionDraft
from tests.chat_support import FROZEN_NOW, FixedClock, InMemoryDraftRepository, pending_draft
from tests.feedback_support import InMemoryFeedbackEvents

_SPORTS = default_sport_registry()
_TYPED = "dropped the bar on my foot, 187 kg"


async def _gym_draft(drafts: InMemoryDraftRepository, user_id):  # type: ignore[no-untyped-def]
    return await drafts.add(
        user_id,
        uuid4(),
        uuid4(),
        "gym",
        SessionDraft(
            occurred_on=FROZEN_NOW.date(),
            date_source="assumed_today",
            timezone="Europe/Warsaw",
            payload={
                "sport": "gym",
                "activities": [{"name": "squat", "sets": 5, "reps": 5, "load": None, "rir": None}],
            },
        ),
        payload_version=1,
        created_at=FROZEN_NOW,
        template_id="gym.extract",
        template_version=3,
    )


def _form(**row: str) -> dict:  # type: ignore[type-arg]
    """The edit form for one squat exercise, as shown: `sets` is the number of set rows it posts
    (#398), each with `reps` and `load`."""
    one_set = {
        "reps": row.get("reps", "5"),
        "load": row.get("load", ""),
        "duration": "",
        "distance": "",
        "band": "",
    }
    return {
        "occurred_on": FROZEN_NOW.date().isoformat(),
        "duration_min": "",
        "rpe": "",
        "feeling": "",
        "activities": [
            {
                "name": row.get("name", "squat"),
                "rir": row.get("rir", ""),
                "sets": [one_set] * int(row.get("sets", "5")),
            }
        ],
    }


def _recorder(events: InMemoryFeedbackEvents) -> FeedbackRecorder:
    return FeedbackRecorder(events, clock=FixedClock())


async def test_editing_reps_records_one_draft_edited_with_the_field_name_and_no_value() -> None:
    drafts, events, user_id = InMemoryDraftRepository(), InMemoryFeedbackEvents(), uuid4()
    draft = await _gym_draft(drafts, user_id)

    await edit_draft(
        user_id,
        draft.id,
        _form(reps="8"),
        drafts=drafts,
        sports=_SPORTS,
        feedback=_recorder(events),
    )

    [event] = events.events
    assert event.kind is FeedbackKind.DRAFT_EDITED
    assert set(event.fields) == {"reps"}
    assert (event.template_id, event.template_version) == ("gym.extract", 3)
    assert event.created_at == FROZEN_NOW
    assert event.user_id == user_id


async def test_editing_sets_records_sets() -> None:
    drafts, events, user_id = InMemoryDraftRepository(), InMemoryFeedbackEvents(), uuid4()
    draft = await _gym_draft(drafts, user_id)

    await edit_draft(
        user_id,
        draft.id,
        _form(sets="4"),
        drafts=drafts,
        sports=_SPORTS,
        feedback=_recorder(events),
    )

    assert [set(e.fields) for e in events.events] == [{"sets"}]


async def test_an_envelope_edit_and_a_payload_edit_name_both_fields() -> None:
    drafts, events, user_id = InMemoryDraftRepository(), InMemoryFeedbackEvents(), uuid4()
    draft = await _gym_draft(drafts, user_id)
    form = _form(name="front squat")
    form["rpe"] = "8"

    await edit_draft(
        user_id, draft.id, form, drafts=drafts, sports=_SPORTS, feedback=_recorder(events)
    )

    assert [set(e.fields) for e in events.events] == [{"name_as_typed", "rpe"}]


async def test_an_edit_that_changes_nothing_writes_no_row() -> None:
    drafts, events, user_id = InMemoryDraftRepository(), InMemoryFeedbackEvents(), uuid4()
    draft = await _gym_draft(drafts, user_id)

    await edit_draft(
        user_id, draft.id, _form(), drafts=drafts, sports=_SPORTS, feedback=_recorder(events)
    )

    assert events.events == []


async def test_a_typed_value_never_reaches_the_row() -> None:
    drafts, events, user_id = InMemoryDraftRepository(), InMemoryFeedbackEvents(), uuid4()
    draft = await _gym_draft(drafts, user_id)

    await edit_draft(
        user_id,
        draft.id,
        _form(name=_TYPED),
        drafts=drafts,
        sports=_SPORTS,
        feedback=_recorder(events),
    )

    assert _TYPED not in events.events[0].model_dump_json()


async def test_discard_records_one_draft_discarded_and_a_second_discard_none() -> None:
    drafts, events, user_id = InMemoryDraftRepository(), InMemoryFeedbackEvents(), uuid4()
    draft = await _gym_draft(drafts, user_id)

    for _ in range(2):
        await discard_draft(
            user_id, draft.id, drafts=drafts, clock=FixedClock(), feedback=_recorder(events)
        )

    assert [(e.kind, e.template_id, e.template_version) for e in events.events] == [
        (FeedbackKind.DRAFT_DISCARDED, "gym.extract", 3)
    ]


async def test_undo_records_one_session_undone() -> None:
    drafts, events, user_id = InMemoryDraftRepository(), InMemoryFeedbackEvents(), uuid4()
    draft = await pending_draft(drafts, user_id, status="confirmed")

    await undo_log(user_id, draft.id, drafts=drafts, clock=FixedClock(), feedback=_recorder(events))

    assert [e.kind for e in events.events] == [FeedbackKind.SESSION_UNDONE]


async def test_a_refused_action_records_nothing() -> None:
    drafts, events, user_id = InMemoryDraftRepository(), InMemoryFeedbackEvents(), uuid4()
    confirmed = await pending_draft(drafts, user_id, status="confirmed")

    with pytest.raises(Exception):  # noqa: B017, PT011 -- any refusal; the point is no row
        await discard_draft(
            user_id, confirmed.id, drafts=drafts, clock=FixedClock(), feedback=_recorder(events)
        )

    assert events.events == []


async def test_a_failing_store_does_not_fail_the_discard() -> None:
    drafts, user_id = InMemoryDraftRepository(), uuid4()
    draft = await _gym_draft(drafts, user_id)
    broken = _recorder(InMemoryFeedbackEvents(fail=True))

    discarded = await discard_draft(
        user_id, draft.id, drafts=drafts, clock=FixedClock(), feedback=broken
    )

    assert discarded.status == "discarded"


def _draft_with(payload: dict[str, JsonValue]) -> SessionDraft:
    return SessionDraft(
        occurred_on=FROZEN_NOW.date(),
        date_source="assumed_today",
        timezone="Europe/Warsaw",
        payload=payload,
    )


def test_a_changed_scalar_or_list_of_scalars_names_its_own_key() -> None:
    before = _draft_with({"distance_km": 40, "tags": ["easy"], "rows": [{"a": 1}]})
    after = _draft_with({"distance_km": 45, "tags": ["hard"], "rows": [{"a": 1}]})

    assert changed_fields(before, after) == ["distance_km", "tags"]


def test_a_changed_row_names_the_keys_that_differ_in_it_not_the_list() -> None:
    before = _draft_with({"rows": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]})
    after = _draft_with({"rows": [{"a": 1, "b": 2}, {"a": 3, "b": 9}]})

    assert changed_fields(before, after) == ["b"]


def test_counts_group_by_kind_template_and_field() -> None:
    edited = FeedbackKind.DRAFT_EDITED
    signals = [
        FeedbackSignal(kind=edited, template_id="gym.extract", fields=["reps", "sets"]),
        FeedbackSignal(kind=edited, template_id="gym.extract", fields=["reps"]),
        FeedbackSignal(kind=FeedbackKind.DRAFT_DISCARDED, template_id="gym.extract"),
    ]

    counts = [(c.kind, c.template_id, c.field, c.count) for c in count_signals(signals)]

    assert counts == [
        (edited, "gym.extract", "reps", 2),
        (FeedbackKind.DRAFT_DISCARDED, "gym.extract", None, 1),
        (edited, "gym.extract", "sets", 1),
    ]


# --- #395: gym payload v2 holds reps and load inside each exercise's list of sets ------------


async def _v2_draft(drafts: InMemoryDraftRepository, user_id):  # type: ignore[no-untyped-def]
    payload = GymExtraction.model_validate(
        {
            "occurred_on": FROZEN_NOW.date(),
            "date_source": "assumed_today",
            "activities": [
                {
                    "name": "squat",
                    "sets": 5,
                    "reps": 5,
                    "load": {"original": "100kg", "value": 100, "unit": "kg"},
                }
            ],
        }
    ).to_payload()
    return await drafts.add(
        user_id,
        uuid4(),
        uuid4(),
        "gym",
        SessionDraft(
            occurred_on=FROZEN_NOW.date(),
            date_source="assumed_today",
            timezone="Europe/Warsaw",
            payload=payload.model_dump(mode="json"),
        ),
        payload_version=2,
        created_at=FROZEN_NOW,
        template_id="gym.extract",
        template_version=5,
    )


@pytest.mark.parametrize(
    ("row", "fields"),
    [
        ({"reps": "8"}, {"reps"}),
        ({"load": "120"}, {"load"}),
        ({"sets": "4"}, {"sets"}),
        ({"rir": "2"}, {"rir"}),
        ({"name": "front squat"}, {"name_as_typed"}),
        ({"reps": "8", "load": "120"}, {"reps", "load"}),
        # Code reads `measure` from the set, so it follows a load the athlete cleared.
        ({"load": ""}, {"load", "measure"}),
        ({}, None),
    ],
)
async def test_a_v2_edit_names_the_field_inside_its_sets(
    row: dict[str, str], fields: set[str] | None
) -> None:
    drafts, events, user_id = InMemoryDraftRepository(), InMemoryFeedbackEvents(), uuid4()
    draft = await _v2_draft(drafts, user_id)

    await edit_draft(
        user_id,
        draft.id,
        _form(**{"load": "100", **row}),
        drafts=drafts,
        sports=_SPORTS,
        feedback=_recorder(events),
    )

    assert [set(e.fields) for e in events.events] == ([] if fields is None else [fields])


def test_lists_of_different_lengths_name_their_own_key() -> None:
    before = _draft_with({"rows": [{"sets": [{"a": 1}, {"a": 1}]}]})
    after = _draft_with({"rows": [{"sets": [{"a": 1}]}]})

    assert changed_fields(before, after) == ["sets"]
