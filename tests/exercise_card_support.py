"""The exercise card rendered on its own (#581): an open exercise question with the given candidates
and pictures, through the app's Jinja environment, for the unit and e2e tests of its More."""

from collections.abc import Mapping, Sequence
from uuid import uuid4

from ai_trainer.application.exercise_card_media import CardPicture, CardSlot
from ai_trainer.application.media_credit import WgerCredit
from ai_trainer.domain.clarification import (
    EXERCISE_PURPOSE,
    KEEP_NAME_OPTION,
    ChoiceOption,
    Clarification,
    StoredClarification,
)
from ai_trainer.domain.exercise_simple_texts import CardHowto
from ai_trainer.domain.exercises import ExerciseCandidate
from ai_trainer.web.templating import build_templates
from tests.chat_support import FROZEN_NOW

LONG_HOW = (
    "Stand with your feet shoulder-width apart, a dumbbell in each hand at your sides. Keep a"
    " slight bend in your elbows and lift the dumbbells out to shoulder height, then lower them"
    " slowly. Breathe out as you lift, keep your shoulders down and relaxed, and don't swing your"
    " torso to get the weights up. Pause for a moment at the top before you lower them again."
)
SHORT_HOW = "Lift the band's ends out to shoulder height."

WGER_PICTURE = CardPicture(
    public_url="/static/icons/icon-192.png",
    origin="wger",
    credit=WgerCredit(
        author="K. Brandt",
        licence="CC BY-SA 4.0",
        licence_url="https://creativecommons.org/licenses/by-sa/4.0/",
    ),
    page_url="https://wger.de/api/v2/exerciseimage/7/",
    attribution="K. Brandt · CC BY-SA 4.0 — via wger.de",
    ai_flagged=False,
)


def exercise_question(
    candidates: Sequence[ExerciseCandidate], subject: str = "lateral rises"
) -> StoredClarification:
    options = (
        *(
            ChoiceOption(
                id=str(candidate.id),
                copy_key="gym_exercise_candidate",
                params={"name": candidate.name, "how": candidate.how or ""},
            )
            for candidate in candidates
        ),
        ChoiceOption(id=KEEP_NAME_OPTION, copy_key="gym_exercise_keep", params={"name": subject}),
    )
    return StoredClarification(
        id=uuid4(),
        conversation_id=uuid4(),
        message_id=uuid4(),
        purpose=EXERCISE_PURPOSE,
        clarification=Clarification(
            question="Which exercise is it?",
            options=options,
            pending="log_session",
            sport="gym",
            subject=subject,
        ),
        status="open",
        created_at=FROZEN_NOW,
    )


def render_exercise_card(
    question: StoredClarification,
    pictures: Mapping[str, CardSlot] | None = None,
    howtos: Mapping[str, CardHowto] | None = None,
) -> str:
    template = build_templates().env.from_string(
        '{% from "components/choice_card.html" import exercise_card %}'
        "{{ exercise_card(question, pictures, howtos) }}"
    )
    return template.render(question=question, pictures=pictures or {}, howtos=howtos or {})
