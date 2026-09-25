from datetime import UTC, datetime

import jinja2
import pytest

from ai_trainer.web.templating import build_templates


def test_undefined_variable_raises_instead_of_rendering_empty() -> None:
    templates = build_templates()

    template = templates.env.from_string("{{ does_not_exist }}")

    with pytest.raises(jinja2.exceptions.UndefinedError):
        template.render()


def test_missing_template_name_raises_instead_of_a_silent_404() -> None:
    templates = build_templates()

    with pytest.raises(jinja2.exceptions.TemplateNotFound):
        templates.get_template("pages/does-not-exist/nope.html")


def test_defined_variable_still_renders() -> None:
    templates = build_templates()

    template = templates.env.from_string("{{ greeting }}")

    assert template.render(greeting="hi") == "hi"


def test_dateformat_filter_renders_day_month_year() -> None:
    templates = build_templates()
    template = templates.env.from_string("{{ value | dateformat }}")

    rendered = template.render(value=datetime(2025, 9, 2, tzinfo=UTC))

    assert rendered == "2 Sep 2025"
