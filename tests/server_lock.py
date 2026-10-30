"""One advisory lock for the whole Postgres server, held by every test module that creates
databases or drops and recreates cluster-wide roles (migration e8b1f4c7a2d9's
`ai_trainer_monitor`). Those share the server with every other checkout's run, which
`xdist_group` can't see: two worktrees running the suite at once raced on that role (ADR-0035
invariant 6, #351). It is taken in the `postgres` database because advisory locks are scoped to
a database, and each checkout has its own."""

from sqlalchemy.engine import make_url

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

SERVER_WIDE_MODULES = (
    "tests/integration/migrations/",
    "tests/migrations/",
    "tests/integration/test_worktree_databases.py",
)
SERVER_WIDE_LOCK_KEY = 3510035


def server_wide_lock_dsn() -> str:
    url = make_url(str(Settings().database_url)).set(database="postgres")
    return to_psycopg_dsn(url.render_as_string(hide_password=False))
