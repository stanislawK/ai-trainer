"""`ConversationModelPort` on the prompt registry and the gateway: the router, the chitchat
specialist and the "not yet" reply (ADR-0008 skeleton routing)."""

from collections.abc import AsyncGenerator, Sequence
from pathlib import Path

from ai_trainer.application.ports.conversation import (
    FixedReply,
    ReplyContext,
    Routing,
    TemplateRef,
)
from ai_trainer.application.ports.llm_gateway import LlmGatewayPort, LlmStreamEvent
from ai_trainer.domain.chat import ChatMessage
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.llm.chitchat import build_chitchat_deps, chitchat_template
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.router import ChatTurn, build_router_deps, router_template

NOT_YET_REPLY_ID = "not_yet"
_NOT_YET_VERSION = 1


def build_prompt_registry(root: Path, sports: SportRegistry) -> PromptRegistry:
    """Every template the app runs, each at its active version (ADR-0008 invariant 2)."""
    registry = PromptRegistry(root=root)
    registry.register(router_template(sports))
    registry.register(chitchat_template())
    return registry


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
        history_turns: int,
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        self._router = router_template(sports)
        self._chitchat = chitchat_template()
        self._router_model = router_model
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

    def not_yet(self) -> FixedReply:
        return FixedReply(
            text=self._registry.load_fixed_reply(NOT_YET_REPLY_ID, _NOT_YET_VERSION),
            template=TemplateRef(id=NOT_YET_REPLY_ID, version=_NOT_YET_VERSION),
        )
