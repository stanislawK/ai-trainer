"""`ConversationModelPort` on the prompt registry and the gateway: the router, the chitchat
specialist, each sport's extraction template, and the fixed replies (ADR-0008)."""

import hashlib
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from uuid import UUID

from ai_trainer.application.ports.conversation import (
    Extraction,
    FixedReply,
    ReplyContext,
    Routing,
    TemplateRef,
)
from ai_trainer.application.ports.llm_gateway import LlmGatewayPort, LlmStreamEvent
from ai_trainer.domain.chat import ChatMessage
from ai_trainer.domain.clarification import SPORT_QUESTION_TEMPLATE_ID
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.llm_calls import ProviderPreferences, ReasoningEffort
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.domain.training import DRAFT_FAILED_TEMPLATE_ID
from ai_trainer.llm.chitchat import build_chitchat_deps, chitchat_template
from ai_trainer.llm.extract import build_extract_deps, extract_template, extract_templates
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.router import ROUTER_VERSIONS, ChatTurn, build_router_deps, router_template

NOT_YET_REPLY_ID = "not_yet"
_NOT_YET_VERSION = 1
# The line above a draft card, sent as written (ADR-0008 fixed replies).
DRAFT_READY_REPLY_ID = "draft_ready"
_DRAFT_READY_VERSION = 1
# The line in place of a draft whose extraction failed, beside Retry (ticket #161).
DRAFT_FAILED_REPLY_ID = DRAFT_FAILED_TEMPLATE_ID
_DRAFT_FAILED_VERSION = 1
# The question above the choice card that asks which sport a log was, sent as written (ADR-0015).
SPORT_QUESTION_REPLY_ID = SPORT_QUESTION_TEMPLATE_ID
_SPORT_QUESTION_VERSION = 1
# The question on a draft's card that asks whether it is the same session as an earlier one that
# day, sent as written (B16, ticket #166).
SAME_SESSION_QUESTION_REPLY_ID = "same_session_question"
_SAME_SESSION_QUESTION_VERSION = 1


def build_prompt_registry(root: Path, sports: SportRegistry) -> PromptRegistry:
    """Every template the app runs, each at its active version (ADR-0008 invariant 2)."""
    registry = PromptRegistry(root=root)
    for version in ROUTER_VERSIONS:
        registry.register(router_template(sports, version=version))
    registry.register(chitchat_template())
    for template in extract_templates(sports):
        registry.register(template)
    return registry


def session_id(conversation_id: UUID) -> str:
    """OpenRouter's sticky-routing key for one conversation (ADR-0026 step 4): a hash of the
    conversation's random ID, so it is stable across the conversation's calls, names no user,
    and links back to a conversation only for someone holding our database (invariant 4)."""
    return hashlib.sha256(f"ai-trainer-session:{conversation_id}".encode()).hexdigest()


def _turns(history: Sequence[ChatMessage]) -> list[ChatTurn]:
    return [
        ChatTurn.model_validate({"role": message.role.value, "text": message.text})
        for message in history
    ]


class LlmConversation:
    """Implements `ConversationModelPort`. Instructions are rendered exactly as the templates'
    eval runs rendered them (`PromptRegistry.render_instructions`), and every model call goes
    through the gateway, which writes its `llm_calls` row (ADR-0018)."""

    def __init__(
        self,
        *,
        registry: PromptRegistry,
        gateway: LlmGatewayPort,
        sports: SportRegistry,
        router_model: str,
        chitchat_model: str,
        extract_model: str,
        history_turns: int,
        router_provider: ProviderPreferences | None = None,
        extract_provider: ProviderPreferences | None = None,
        router_timeout_seconds: float | None = None,
        extract_timeout_seconds: float | None = None,
        router_reasoning_effort: ReasoningEffort | None = None,
        extract_reasoning_effort: ReasoningEffort | None = None,
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        self._sports = sports
        self._extract_model = extract_model
        self._extract_provider = extract_provider
        self._extract_reasoning_effort = extract_reasoning_effort
        self._extract_timeout_seconds = extract_timeout_seconds
        self._router = router_template(sports)
        self._chitchat = chitchat_template()
        self._router_model = router_model
        self._router_provider = router_provider
        self._router_reasoning_effort = router_reasoning_effort
        self._router_timeout_seconds = router_timeout_seconds
        self._chitchat_model = chitchat_model
        self._history_turns = history_turns

    @property
    def chitchat_template(self) -> TemplateRef:
        return TemplateRef(id=self._chitchat.id, version=self._chitchat.version)

    async def route(self, context: ReplyContext) -> Routing:
        deps = build_router_deps(
            message=context.message,
            history=_turns(context.history),
            athlete_sports=list(context.athlete_sports),
            now=context.now,
            timezone=context.timezone,
            history_turns=self._history_turns,
        )
        result = await self._gateway.run(
            user_id=context.user_id,
            template_id=self._router.id,
            template_version=self._router.version,
            model_id=self._router_model,
            output_type=self._router.output_type,
            instructions=self._registry.render_instructions(
                self._router.id, self._router.version, deps
            ),
            prompt=context.message,
            temperature=self._router.temperature,
            output_retries=self._router.output_retries,
            provider=self._router_provider,
            reasoning_effort=self._router_reasoning_effort,
            session_id=session_id(context.conversation_id),
            timeout_seconds=self._router_timeout_seconds,
        )
        if result.output is None:
            return Routing(intents=(), outcome=result.outcome)
        intents = [
            Intent(kind=intent.kind, sport=getattr(intent, "sport", None))
            for intent in getattr(result.output, "intents")  # noqa: B009 -- built at runtime
        ]
        return Routing(intents=intents, outcome=result.outcome)

    def stream_chitchat(self, context: ReplyContext) -> AsyncGenerator[LlmStreamEvent]:
        deps = build_chitchat_deps(
            message=context.message,
            history=_turns(context.history),
            athlete_sports=list(context.athlete_sports),
            goals=list(context.goals),
            now=context.now,
            timezone=context.timezone,
            history_turns=self._history_turns,
        )
        return self._gateway.stream(
            user_id=context.user_id,
            template_id=self._chitchat.id,
            template_version=self._chitchat.version,
            model_id=self._chitchat_model,
            instructions=self._registry.render_instructions(
                self._chitchat.id, self._chitchat.version, deps
            ),
            prompt=context.message,
        )

    async def extract(self, context: ReplyContext, sport_id: str) -> Extraction:
        spec = self._sports.get(sport_id).extraction
        if spec is None:
            raise ValueError(f"sport {sport_id!r} names no extraction template")
        template = extract_template(spec)
        deps = build_extract_deps(
            message=context.message,
            history=_turns(context.history),
            now=context.now,
            timezone=context.timezone,
            history_turns=self._history_turns,
        )
        result = await self._gateway.run(
            user_id=context.user_id,
            template_id=template.id,
            template_version=template.version,
            model_id=self._extract_model,
            output_type=spec.output_type,
            instructions=self._registry.render_instructions(template.id, template.version, deps),
            prompt=context.message,
            temperature=template.temperature,
            output_retries=template.output_retries,
            provider=self._extract_provider,
            reasoning_effort=self._extract_reasoning_effort,
            session_id=session_id(context.conversation_id),
            timeout_seconds=self._extract_timeout_seconds,
        )
        return Extraction(
            output=result.output,
            outcome=result.outcome,
            template=TemplateRef(id=template.id, version=template.version),
        )

    def draft_ready(self) -> FixedReply:
        return FixedReply(
            text=self._registry.load_fixed_reply(DRAFT_READY_REPLY_ID, _DRAFT_READY_VERSION),
            template=TemplateRef(id=DRAFT_READY_REPLY_ID, version=_DRAFT_READY_VERSION),
        )

    def draft_failed(self) -> FixedReply:
        return FixedReply(
            text=self._registry.load_fixed_reply(DRAFT_FAILED_REPLY_ID, _DRAFT_FAILED_VERSION),
            template=TemplateRef(id=DRAFT_FAILED_REPLY_ID, version=_DRAFT_FAILED_VERSION),
        )

    def not_yet(self) -> FixedReply:
        return FixedReply(
            text=self._registry.load_fixed_reply(NOT_YET_REPLY_ID, _NOT_YET_VERSION),
            template=TemplateRef(id=NOT_YET_REPLY_ID, version=_NOT_YET_VERSION),
        )

    def sport_question(self) -> FixedReply:
        return FixedReply(
            text=self._registry.load_fixed_reply(SPORT_QUESTION_REPLY_ID, _SPORT_QUESTION_VERSION),
            template=TemplateRef(id=SPORT_QUESTION_REPLY_ID, version=_SPORT_QUESTION_VERSION),
        )

    def same_session_question(self) -> FixedReply:
        return FixedReply(
            text=self._registry.load_fixed_reply(
                SAME_SESSION_QUESTION_REPLY_ID, _SAME_SESSION_QUESTION_VERSION
            ),
            template=TemplateRef(
                id=SAME_SESSION_QUESTION_REPLY_ID, version=_SAME_SESSION_QUESTION_VERSION
            ),
        )
