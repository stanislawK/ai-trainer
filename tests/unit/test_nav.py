"""The signed-in shell's section list (ADR-0019, tickets #40 and #41): one list in code,
filtered by admin rights derived per request (ADR-0005)."""

from ai_trainer.web.nav import NAV_SECTIONS, visible_sections


def test_nav_sections_holds_chat_settings_and_admin_only() -> None:
    assert {section.id for section in NAV_SECTIONS} == {"chat", "settings", "admin"}


def test_chat_section_is_not_admin_only() -> None:
    chat = next(section for section in NAV_SECTIONS if section.id == "chat")

    assert chat.admin_only is False
    assert chat.href == "/"


def test_settings_section_is_not_admin_only() -> None:
    settings = next(section for section in NAV_SECTIONS if section.id == "settings")

    assert settings.admin_only is False
    assert settings.href == "/settings"
    assert settings.icon == "settings"


def test_admin_section_is_admin_only() -> None:
    admin = next(section for section in NAV_SECTIONS if section.id == "admin")

    assert admin.admin_only is True
    assert admin.href == "/admin"


def test_non_admin_never_sees_the_admin_section() -> None:
    sections = visible_sections(is_admin=False)

    assert {section.id for section in sections} == {"chat", "settings"}


def test_admin_sees_every_section() -> None:
    sections = visible_sections(is_admin=True)

    assert {section.id for section in sections} == {"chat", "settings", "admin"}
