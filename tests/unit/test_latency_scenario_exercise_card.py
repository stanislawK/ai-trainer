"""`make latency-scenario sport=gym` on the exercise card (ADR-0031, F23, tickets #402 and #415): an
unsettled name asks in the draft card's place, on a card with no draft ID. These run the real chat
router and templates, so the script can't drift from that markup unnoticed, as it did at #402."""

import httpx2

from scripts.latency_scenario import Turn, play, summarize
from tests.unit.test_chat_exercise_question import _App
from tests.unit.test_latency_scenario import (
    _BASE,
    FakeLlmCalls,
    SteppingTimer,
    _play_stub,
    _stub_app,
)

_QUESTION = "22222222-2222-2222-2222-222222222222"
_MESSAGE_ID = "7f1c3a52-9a7e-4b39-9d5e-0c0a8e6b2f11"


def _client(gym: _App) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=gym.client.app), base_url=_BASE)


async def test_a_log_asking_on_the_exercise_card_is_settled_and_confirmed() -> None:
    gym = _App("bench press", "squat")
    timer = SteppingTimer(0.5)

    async with _client(gym) as client:
        [result] = await play(
            client,
            FakeLlmCalls(gym.user.id),
            gym.user.id,
            csrf_token="token",
            turns=(Turn("bench press and squat", log=True, confirm=True),),
            timer=timer,
        )

    [draft] = gym.drafts.all_of(gym.user.id)
    assert result.outcome == "ok"
    assert result.draft_id == draft.id
    assert draft.status == "confirmed"
    assert gym.open_questions() == []
    # Timed to the exercise card: the taps and the confirm come after the turn's two readings.
    assert result.ms == 500


async def test_a_log_left_unconfirmed_on_the_exercise_card_keeps_its_question_open() -> None:
    gym = _App("bench press")

    async with _client(gym) as client:
        [result] = await play(
            client,
            FakeLlmCalls(gym.user.id),
            gym.user.id,
            csrf_token="token",
            turns=(Turn("bench press", log=True, confirm=False),),
            timer=SteppingTimer(0.5),
        )

    [draft] = gym.drafts.all_of(gym.user.id)
    assert result.outcome == "ok"
    assert draft.status == "pending"
    assert len(gym.open_questions()) == 1


async def test_answers_that_never_reach_a_draft_are_a_failure() -> None:
    option = (
        f"""<button hx-post="/clarifications/{_QUESTION}/answer" """
        """hx-vals='{"option_ids": "keep"}'></button>"""
    )
    final = f'data: <template hx-target="#reply-{_MESSAGE_ID}">{option}</template>\n\n'

    def app(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/reply"):
            return httpx2.Response(200, text=final)
        return httpx2.Response(200, text="<div data-choice-answer></div>")

    result = await _play_stub(_stub_app(app))

    assert result.outcome == "confirm_no_draft"
    assert result.draft_id is None
    assert summarize([result]).failures == [1]
