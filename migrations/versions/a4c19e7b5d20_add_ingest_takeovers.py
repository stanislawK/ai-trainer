"""add ingest takeovers

Revision ID: a4c19e7b5d20
Revises: 2296d6928690
Create Date: 2026-11-06 15:00:00.000000

Expand only (ADR-0024), for #462 (ADR-0036's #462 amendment):
- `knowledge_sources` gains `ingest_takeovers`, how many times a worker took the source over
  after the worker before it stopped (its lease lapsed). A claim from `uploaded` sets it to 0
  and a takeover adds 1, so a file that keeps killing the `ingest` container fails as
  `worker_crashed` instead of holding the queue.
- `bigint NOT NULL DEFAULT 0` (squawk's prefer-bigint-over-smallint): a constant default is
  stored in the catalogue (PostgreSQL 11+), so the table isn't rewritten. Every existing row
  reads 0, which is right: none was taken over under the guard. No backfill.

Release N's code never reads it, so an app rollback stays safe (invariant 4).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c19e7b5d20"
down_revision: str | Sequence[str] | None = "2296d6928690"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_sources",
        sa.Column("ingest_takeovers", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("knowledge_sources", "ingest_takeovers")
