"""add the ai_trainer_monitor role for Alloy's Postgres exporter

Revision ID: e8b1f4c7a2d9
Revises: 7d2c9e4a1f63
Create Date: 2026-10-26 10:12:40.118230

Expand-only (ADR-0024, ticket #273): a login role holding `pg_monitor` alone, so it reads the
statistics views and no app table (ADR-0030). It has no password here, because no secret is in
the repo: `deploy/monitor-password.sh` sets it from alloy.env. A role belongs to the cluster, so
it may exist already from another database migrated on it: the upgrade keeps it, with its
attributes reset to these.

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8b1f4c7a2d9"
down_revision: str | Sequence[str] | None = "7d2c9e4a1f63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ai_trainer_monitor') THEN
                CREATE ROLE ai_trainer_monitor LOGIN;
            END IF;
        END
        $$
        """
    )
    # One left from elsewhere keeps its name, never more rights than these.
    op.execute(
        "ALTER ROLE ai_trainer_monitor"
        " LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT"
    )
    op.execute("GRANT pg_monitor TO ai_trainer_monitor")


def downgrade() -> None:
    op.execute("DROP ROLE IF EXISTS ai_trainer_monitor")
