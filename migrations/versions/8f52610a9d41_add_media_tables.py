"""add media tables

Revision ID: 8f52610a9d41
Revises: a4c19e7b5d20
Create Date: 2026-11-08 19:44:57.845963

Expand only (ADR-0024), for #530 (ADR-0031 "Media links (F24)"):
- `media_assets`: one catalogue picture or video, with its source, licence and attribution.
  Its CHECKs hold ADR-0031's rules: `stored` has `storage_key` and `sha256` and no `embed_url`;
  `embedded` has `embed_url` and no `storage_key`; `licensed` has `license` and `attribution`;
  `unstated` is always `embedded`. `sha256` and `embed_url` are each unique where set.
- `exercise_media`: an asset on a catalogue exercise, `(exercise_id, asset_id)` unique, its
  `status` one of `suggested`, `approved`, `removed`. Deleting an exercise (an athlete's custom
  one, with their account) cascades; an asset is kept for audit, so it is RESTRICT.

Two new, empty tables: nothing existing is altered and nothing is backfilled. Neither holds user
data. Release N's code never reads them, so an app rollback stays safe (invariant 4).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f52610a9d41"
down_revision: str | Sequence[str] | None = "a4c19e7b5d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("rights", sa.Text(), nullable=False),
        sa.Column("delivery", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("embed_url", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("license", sa.Text(), nullable=True),
        sa.Column("attribution", sa.Text(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("width", sa.BigInteger(), nullable=True),
        sa.Column("height", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('gif', 'image', 'drawing', 'video')", name="media_assets_kind"
        ),
        sa.CheckConstraint("rights IN ('licensed', 'unstated')", name="media_assets_rights"),
        sa.CheckConstraint("delivery IN ('stored', 'embedded')", name="media_assets_delivery"),
        sa.CheckConstraint(
            "delivery <> 'stored' OR "
            "(storage_key IS NOT NULL AND sha256 IS NOT NULL AND embed_url IS NULL)",
            name="media_assets_stored",
        ),
        sa.CheckConstraint(
            "delivery <> 'embedded' OR (embed_url IS NOT NULL AND storage_key IS NULL)",
            name="media_assets_embedded",
        ),
        sa.CheckConstraint(
            "rights <> 'licensed' OR (license IS NOT NULL AND attribution IS NOT NULL)",
            name="media_assets_licensed",
        ),
        sa.CheckConstraint(
            "rights <> 'unstated' OR delivery = 'embedded'", name="media_assets_unstated"
        ),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="media_assets_sha256_format"),
        sa.CheckConstraint("width > 0 AND height > 0", name="media_assets_size"),
        sa.CheckConstraint("source <> '' AND source_ref <> ''", name="media_assets_source_present"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "media_assets_sha256_key",
        "media_assets",
        ["sha256"],
        unique=True,
        postgresql_where=sa.text("sha256 IS NOT NULL"),
    )
    op.create_index(
        "media_assets_embed_url_key",
        "media_assets",
        ["embed_url"],
        unique=True,
        postgresql_where=sa.text("embed_url IS NOT NULL"),
    )
    op.create_table(
        "exercise_media",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("exercise_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('suggested', 'approved', 'removed')", name="exercise_media_status"
        ),
        sa.CheckConstraint("position >= 0", name="exercise_media_position"),
        sa.ForeignKeyConstraint(["exercise_id"], ["exercises.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["media_assets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exercise_id", "asset_id", name="exercise_media_exercise_asset_key"),
    )
    op.create_index("ix_exercise_media_asset_id", "exercise_media", ["asset_id"])


def downgrade() -> None:
    op.drop_index("ix_exercise_media_asset_id", table_name="exercise_media")
    op.drop_table("exercise_media")
    op.drop_index("media_assets_embed_url_key", table_name="media_assets")
    op.drop_index("media_assets_sha256_key", table_name="media_assets")
    op.drop_table("media_assets")
