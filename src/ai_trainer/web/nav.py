"""The signed-in shell's sections (ADR-0019, ticket #40): one list in code, so `layouts/app.html`
never branches on the current route by hand. A section appears here only once its page exists;
M0 ships Chat and, for admins, Admin (ADR-0005 — admin rights are derived per request)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class NavSection:
    id: str
    label: str
    href: str
    icon: str
    admin_only: bool = False


NAV_SECTIONS: tuple[NavSection, ...] = (
    NavSection(id="chat", label="Chat", href="/", icon="message-circle"),
    NavSection(id="admin", label="Admin", href="/admin", icon="shield-check", admin_only=True),
)


def visible_sections(*, is_admin: bool) -> tuple[NavSection, ...]:
    return tuple(section for section in NAV_SECTIONS if not section.admin_only or is_admin)
