"""Structural checks for the layouts/pages/partials template layout (ADR-0019, ticket #36)."""

from ai_trainer.web.templating import TEMPLATES_DIR


def test_no_template_path_ends_in_content_suffix() -> None:
    offenders = [path for path in TEMPLATES_DIR.rglob("*_content.html") if path.is_file()]

    assert offenders == []


def test_every_page_and_partial_sits_exactly_one_feature_folder_deep() -> None:
    """ADR-0019: `pages/<feature>/<view>.html` and `partials/<feature>/<fragment>.html` —
    neither directly under the type folder nor nested inside an extra subfolder."""
    for type_dir_name in ("pages", "partials"):
        type_dir = TEMPLATES_DIR / type_dir_name
        misplaced = [
            path for path in type_dir.rglob("*.html") if len(path.relative_to(type_dir).parts) != 2
        ]

        assert misplaced == [], (
            f"{type_dir} has files not exactly one feature folder deep: {misplaced}"
        )


def test_base_layout_lives_under_layouts_not_pages() -> None:
    assert (TEMPLATES_DIR / "layouts" / "base.html").is_file()
    assert not (TEMPLATES_DIR / "pages" / "base.html").exists()


def test_templates_root_holds_only_the_type_folders() -> None:
    """Type-then-feature (ADR-0019): nothing sits loose at the templates root, and the
    template types are limited to the four the ADR names."""
    top_level = {path.name for path in TEMPLATES_DIR.iterdir()}

    assert top_level == {"layouts", "pages", "partials", "components"}
