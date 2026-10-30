import subprocess
from pathlib import Path

import pytest

from scripts import squawk_migrations as sq

REVISION = '''"""scratch"""

revision = "aaa111"
down_revision = {down}
'''

# Alembic's offline output wraps a migration in a transaction; squawk judges it as one.
DROP_SQL = "BEGIN;\nALTER TABLE users DROP COLUMN nickname;\nCOMMIT;\n"
INDEX_SQL = "BEGIN;\nCREATE INDEX ix_users_nick ON users (nickname);\nCOMMIT;\n"
BOTH_SQL = (
    "BEGIN;\nALTER TABLE users DROP COLUMN nickname;\nCREATE INDEX ix_a ON users (a);\nCOMMIT;\n"
)


def _revision_file(tmp_path: Path, *, down: str = '"d3a7e5c1b942"', extra: str = "") -> Path:
    path = tmp_path / "aaa111_scratch.py"
    path.write_text(REVISION.format(down=down) + extra)
    return path


def _fake_render(sql: str) -> sq.Renderer:
    def render(down_revision: str, revision: str) -> str:
        return sql

    return render


def test_branch_with_no_new_revision_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert sq.lint_revisions([], _fake_render(DROP_SQL)) == 0
    assert "No new migration revisions" in capsys.readouterr().out


def test_drop_column_fails_naming_the_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = sq.lint_revisions([_revision_file(tmp_path)], _fake_render(DROP_SQL))

    assert code == 1
    assert "ban-drop-column" in capsys.readouterr().out


def test_non_concurrent_index_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = sq.lint_revisions([_revision_file(tmp_path)], _fake_render(INDEX_SQL))

    assert code == 1
    assert "require-concurrent-index-creation" in capsys.readouterr().out


def test_ignore_comment_with_reason_and_ticket_passes(tmp_path: Path) -> None:
    extra = "# squawk-ignore ban-drop-column: the app stopped reading it in v1.2 (#123)\n"

    code = sq.lint_revisions([_revision_file(tmp_path, extra=extra)], _fake_render(DROP_SQL))

    assert code == 0


def test_ignore_comment_covers_only_the_listed_rules(tmp_path: Path) -> None:
    extra = "# squawk-ignore ban-drop-column: stopped reading it (#123)\n"

    code = sq.lint_revisions([_revision_file(tmp_path, extra=extra)], _fake_render(BOTH_SQL))

    assert code == 1


@pytest.mark.parametrize(
    "comment",
    [
        "# squawk-ignore ban-drop-column\n",
        "# squawk-ignore ban-drop-column: no ticket here\n",
        "# squawk-ignore ban-drop-column (#123)\n",
        "# squawk-ignore: reason (#123)\n",
    ],
)
def test_ignore_comment_without_reason_or_ticket_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], comment: str
) -> None:
    code = sq.lint_revisions([_revision_file(tmp_path, extra=comment)], _fake_render(DROP_SQL))

    assert code == 1
    assert "squawk-ignore" in capsys.readouterr().err


def test_revision_that_cannot_render_offline_fails_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(down_revision: str, revision: str) -> str:
        raise sq.RenderError("cannot render offline: get_bind")

    code = sq.lint_revisions([_revision_file(tmp_path)], broken)

    assert code == 1
    err = capsys.readouterr().err
    assert "aaa111" in err
    assert "cannot render offline" in err


def test_merge_revision_is_refused_with_a_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _revision_file(tmp_path, down='("x1", "x2")')

    assert sq.lint_revisions([path], _fake_render("")) == 1
    assert "single down_revision" in capsys.readouterr().err


def test_first_revision_renders_from_base(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    def render(down_revision: str, revision: str) -> str:
        seen.append((down_revision, revision))
        return "SELECT 1;\n"

    assert sq.lint_revisions([_revision_file(tmp_path, down="None")], render) == 0
    assert seen == [("base", "aaa111")]


def test_real_render_of_an_existing_revision_works() -> None:
    sql = sq.render_offline("base", "fde2eaf4fd78")

    assert "CREATE TABLE users" in sql


def test_real_render_failure_raises_a_clear_error() -> None:
    with pytest.raises(sq.RenderError, match="nonexistent"):
        sq.render_offline("base", "nonexistent")


def test_new_revision_files_lists_only_added_versions(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    versions = tmp_path / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / "old.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-m", "base")
    git("checkout", "-b", "feature")
    (versions / "new.py").write_text("x = 2\n")
    (versions / "old.py").write_text("x = 3\n")
    (tmp_path / "other.py").write_text("y = 1\n")
    git("add", ".")
    git("commit", "-m", "feature")

    assert sq.new_revision_files("main", tmp_path) == [versions / "new.py"]


def test_main_uses_the_base_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_files(base: str) -> list[Path]:
        seen.append(base)
        return []

    monkeypatch.setattr(sq, "new_revision_files", fake_files)

    assert sq.main(["--base", "origin/dev"]) == 0
    assert sq.main([]) == 0
    assert seen == ["origin/dev", "origin/main"]


def test_annotated_revision_ids_are_read(tmp_path: Path) -> None:
    path = tmp_path / "x.py"
    path.write_text('revision: str = "bbb"\ndown_revision: str | None = "aaa"\n')

    assert sq.revision_ids(path) == ("aaa", "bbb")


def test_annotation_without_a_value_counts_as_no_parent(tmp_path: Path) -> None:
    path = tmp_path / "x.py"
    path.write_text('revision = "bbb"\ndown_revision: str | None\n')

    assert sq.revision_ids(path) == ("base", "bbb")


def test_file_without_a_revision_id_fails_with_a_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "x.py"
    path.write_text("down_revision = None\n")

    assert sq.lint_revisions([path], _fake_render("")) == 1
    assert "no string `revision`" in capsys.readouterr().err


def test_unparseable_revision_fails_with_a_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("revision = (\n")
    dynamic = tmp_path / "dynamic.py"
    dynamic.write_text("revision = make_id()\n")

    assert sq.lint_revisions([broken, dynamic], _fake_render("")) == 1
    err = capsys.readouterr().err
    assert "broken.py" in err
    assert "dynamic.py" in err


def test_bookkeeping_only_output_is_not_schema_sql() -> None:
    sql = (
        "BEGIN;\n\nCREATE TABLE alembic_version (\n    version_num VARCHAR(32) NOT NULL\n);\n\n"
        "-- Running upgrade a -> b\n\nUPDATE alembic_version SET version_num='b';\n\nCOMMIT;\n"
    )

    assert not sq._has_schema_sql(sql)
    assert sq._has_schema_sql(sql.replace("COMMIT;", "ALTER TABLE users ADD x int;\nCOMMIT;"))


def test_missing_squawk_binary_fails_with_a_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)

    assert sq._squawk("SELECT 1;", "x.sql") == 1
    assert "squawk is not on PATH" in capsys.readouterr().err


def test_unknown_base_ref_fails_with_a_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(base: str) -> list[Path]:
        raise subprocess.CalledProcessError(128, "git", stderr="bad revision")

    monkeypatch.setattr(sq, "new_revision_files", boom)

    assert sq.main(["--base", "nope"]) == 1
    assert "cannot diff against 'nope'" in capsys.readouterr().err


def test_renamed_revision_counts_as_new(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    versions = tmp_path / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / "old.py").write_text("revision = 'a'\n" * 20)
    git("add", ".")
    git("commit", "-m", "base")
    git("checkout", "-b", "feature")
    git("mv", "migrations/versions/old.py", "migrations/versions/renamed.py")
    git("commit", "-am", "rename")

    assert sq.new_revision_files("main", tmp_path) == [versions / "renamed.py"]


def test_squawk_ignore_inside_a_string_is_not_a_comment(
    tmp_path: Path,
) -> None:
    extra = 'NOTE = "# squawk-ignore ban-drop-column: sneaky (#1)"\n'

    code = sq.lint_revisions([_revision_file(tmp_path, extra=extra)], _fake_render(DROP_SQL))

    assert code == 1


def test_unrelated_comments_are_not_ignores(tmp_path: Path) -> None:
    path = _revision_file(tmp_path, extra="# just a note\n")

    assert sq.ignored_rules(path) == []


def test_revision_rendering_only_bookkeeping_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done = subprocess.CompletedProcess(
        [],
        0,
        stdout="BEGIN;\n\nUPDATE alembic_version SET version_num='b';\n\nCOMMIT;\n",
        stderr="",
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: done)

    with pytest.raises(sq.RenderError, match="no SQL"):
        sq.render_offline("a", "b")
