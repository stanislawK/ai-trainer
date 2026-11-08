import uuid

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from ai_trainer.adapters.orm.base import Base


class MediaAssetOrm(Base):
    """One catalogue picture or video, with its source, licence and attribution (ADR-0031, F24).
    It holds no user data, and is kept when its links are removed, for audit."""

    __tablename__ = "media_assets"
    __table_args__ = (
        CheckConstraint("kind IN ('gif', 'image', 'drawing', 'video')", name="media_assets_kind"),
        CheckConstraint("rights IN ('licensed', 'unstated')", name="media_assets_rights"),
        CheckConstraint("delivery IN ('stored', 'embedded')", name="media_assets_delivery"),
        CheckConstraint(
            "delivery <> 'stored' OR "
            "(storage_key IS NOT NULL AND sha256 IS NOT NULL AND embed_url IS NULL)",
            name="media_assets_stored",
        ),
        CheckConstraint(
            "delivery <> 'embedded' OR (embed_url IS NOT NULL AND storage_key IS NULL)",
            name="media_assets_embedded",
        ),
        CheckConstraint(
            "rights <> 'licensed' OR (license IS NOT NULL AND attribution IS NOT NULL)",
            name="media_assets_licensed",
        ),
        CheckConstraint(
            "rights <> 'unstated' OR delivery = 'embedded'", name="media_assets_unstated"
        ),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="media_assets_sha256_format"),
        CheckConstraint("width > 0 AND height > 0", name="media_assets_size"),
        CheckConstraint("source <> '' AND source_ref <> ''", name="media_assets_source_present"),
        Index(
            "media_assets_sha256_key",
            "sha256",
            unique=True,
            postgresql_where=text("sha256 IS NOT NULL"),
        ),
        Index(
            "media_assets_embed_url_key",
            "embed_url",
            unique=True,
            postgresql_where=text("embed_url IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    rights: Mapped[str] = mapped_column(Text, nullable=False)
    delivery: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    embed_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    license: Mapped[str | None] = mapped_column(Text, nullable=True)
    attribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    width: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    height: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class ExerciseMediaOrm(Base):
    """A media asset on a catalogue exercise, never on a name (ADR-0031). Only `approved` is
    shown; `removed` hides it at once and keeps the row."""

    __tablename__ = "exercise_media"
    __table_args__ = (
        # Its first column indexes `exercise_id` for the card query and the cascade.
        UniqueConstraint("exercise_id", "asset_id", name="exercise_media_exercise_asset_key"),
        CheckConstraint(
            "status IN ('suggested', 'approved', 'removed')", name="exercise_media_status"
        ),
        CheckConstraint("position >= 0", name="exercise_media_position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    exercise_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exercises.id", ondelete="CASCADE"), nullable=False
    )
    # RESTRICT: an asset row is kept for audit, so it is never deleted from under a link.
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
