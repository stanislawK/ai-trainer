import jinja2
import pytest

from ai_trainer.web.templating import build_templates


def test_undefined_variable_raises_instead_of_rendering_empty() -> None:
    templates = build_templates()

    template = templates.env.from_string("{{ does_not_exist }}")

    with pytest.raises(jinja2.exceptions.UndefinedError):
        template.render()


def test_defined_variable_still_renders() -> None:
    templates = build_templates()

    template = templates.env.from_string("{{ greeting }}")

    assert template.render(greeting="hi") == "hi"
