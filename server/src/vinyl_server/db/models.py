"""SQLAlchemy ORM for the vinyl service.

Six tables and a view::

    collection --< accession --< disc_side --< capture
                       |             |
                       +------< asset +          v_side_status (read model)
    release -----------+

Three design choices worth knowing before reading the code:

**Natural keys.** ``accession.accession_id`` (``GM-0142``) and
``disc_side.side_id`` (``GM-0142-D1-A``) are the primary keys, not surrogate
UUIDs. Those strings are printed on the 4x6 card, encoded in its barcode, and
are the FLAC filename stems — a surrogate key would be one nobody ever scans or
types. CHECK constraints keep each display key and its component columns from
drifting apart. ``capture`` and ``asset`` keep UUIDs; they have no natural key.

**Derived side state.** ``disc_side.status`` stores only what captures cannot
express (``pending`` / ``skipped``). Whether a side is captured, uploaded, or
QC-passed is read from its *current* capture through ``v_side_status``, so there
is no second copy of the truth to drift.

**One current capture, enforced by an index.** ``uq_capture_current_per_side`` is
a partial unique index over ``is_current``, which makes "which file is the
master" a fact the database guarantees rather than a recursive ``supersedes``
walk that application code has to get right every time.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, REAL, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from vinyl_shared.states import (
    AccessionStatus,
    AssetKind,
    CaptureStatus,
    ConditionGrade,
    QCVerdict,
    SideStatus,
    Speed,
)

from vinyl_server.db.types import IntEnumType, StrEnumType

# Stable, predictable constraint/index names so Alembic autogenerate diffs cleanly.
_NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=_NAMING)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    # Maintained by a DB trigger (see migration 0001), not SQLAlchemy's onupdate:
    # three different clients write to these tables (the API, the capture worker,
    # and the QC job), and the trigger holds for all of them.
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Collection(Base):
    """One physical collection.

    Exists chiefly to make accession-number allocation atomic::

        UPDATE collection SET next_number = next_number + 1
         WHERE prefix = 'GM' RETURNING next_number - 1;

    The ``UPDATE ... RETURNING`` takes a row lock, so two inventory sessions can
    never mint the same number — which ``SELECT max(number) + 1`` would allow.
    """

    __tablename__ = "collection"

    prefix: Mapped[str] = mapped_column(String(4), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    next_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("prefix ~ '^[A-Z]{2,4}$'", name="prefix_format"),
        CheckConstraint("next_number >= 1", name="next_number_positive"),
    )


class Release(Base):
    """A Discogs release.

    Separate from :class:`Accession` for two reasons: two accessions can be the
    *same* release (duplicate copies are ordinary in an inherited collection),
    and an accession may never be identified at all.
    """

    __tablename__ = "release"

    #: autoincrement=False: this id comes FROM Discogs. Left to its default,
    #: SQLAlchemy makes an integer primary key a BIGSERIAL and the database would
    #: happily invent release ids of its own.
    discogs_release_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    musicbrainz_release_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )

    artist: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    catalog_number: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    country: Mapped[str | None] = mapped_column(Text, nullable=True)
    year: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    #: Normalized tracklist, stored ONCE here and never duplicated per side:
    #: ``[{"position": "A1", "title": "...", "duration_sec": 372}, ...]``
    tracklist: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    #: Raw Discogs API response, cached so re-deriving tracklists and runtimes
    #: later never re-hits the rate-limited API.
    discogs_raw: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = _created_at()

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    accessions: Mapped[list[Accession]] = relationship(back_populates="release")

    __table_args__ = (
        CheckConstraint("year BETWEEN 1880 AND 2100", name="year_plausible"),
    )


class Accession(Base):
    """One jacket / one set. The unit the accession number identifies."""

    __tablename__ = "accession"

    accession_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    collection: Mapped[str] = mapped_column(
        String(4), ForeignKey("collection.prefix"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)

    disc_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    has_jacket: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    has_inner_sleeve: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Both NULLABLE: sets inventoried before grading existed stay ungraded
    #: until someone happens to open them; notes are overflow for the weird.
    condition_grade: Mapped[ConditionGrade | None] = mapped_column(
        StrEnumType(ConditionGrade, length=12), nullable=True
    )
    condition_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: NULLABLE on purpose: inventory produces discs that cannot be matched.
    release_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("release.discogs_release_id"), nullable=True, index=True
    )

    status: Mapped[AccessionStatus] = mapped_column(
        StrEnumType(AccessionStatus, length=20),
        nullable=False,
        default=AccessionStatus.INVENTORIED,
        index=True,
    )

    inventoried_at: Mapped[datetime] = _created_at()
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    release: Mapped[Release | None] = relationship(back_populates="accessions")
    sides: Mapped[list[DiscSide]] = relationship(
        back_populates="accession", cascade="all, delete-orphan"
    )
    assets: Mapped[list[Asset]] = relationship(
        back_populates="accession", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("collection", "number", name="uq_accession_collection_number"),
        CheckConstraint("disc_count >= 1", name="disc_count_positive"),
        # 4-digit zero-padding is the printed card format; exceeding it is a
        # migration and a new card layout, not something to absorb silently.
        CheckConstraint("number BETWEEN 1 AND 9999", name="number_in_range"),
        CheckConstraint(
            "accession_id = collection || '-' || lpad(number::text, 4, '0')",
            name="id_matches_parts",
        ),
        CheckConstraint(
            "(status = 'identified' AND release_id IS NOT NULL) "
            "OR (status = 'unidentifiable' AND release_id IS NULL) "
            "OR (status IN ('inventoried', 'excluded'))",
            name="status_agrees_with_release",
        ),
    )


class DiscSide(Base):
    """The unit of capture: one row = one physical action = one filename.

    Rows are created at inventory time (``2 * disc_count`` of them), so the
    status grid and the capture UI's "next uncaptured side" have something to
    read the moment a barcode is scanned.
    """

    __tablename__ = "disc_side"

    side_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    accession_id: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("accession.accession_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    disc: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    side: Mapped[str] = mapped_column(String(1), nullable=False)

    #: On the physical unit, never the set. 33 means 33-1/3.
    speed_rpm: Mapped[Speed] = mapped_column(IntEnumType(Speed), nullable=False)

    #: WHICH Discogs positions live on this side: ``["A1", "A2", "A3"]``.
    #: Human-editable, because position -> physical side is genuinely ambiguous
    #: on multi-disc sets (A/B/C/D vs 1-1/1-2 numbering, works split across sides).
    track_positions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    expected_runtime_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[SideStatus] = mapped_column(
        StrEnumType(SideStatus, length=20), nullable=False, default=SideStatus.PENDING
    )
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    accession: Mapped[Accession] = relationship(back_populates="sides")
    captures: Mapped[list[Capture]] = relationship(
        back_populates="disc_side", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("accession_id", "disc", "side", name="uq_disc_side_coordinates"),
        CheckConstraint("disc >= 1", name="disc_positive"),
        CheckConstraint("side IN ('A', 'B')", name="side_is_a_or_b"),
        CheckConstraint("speed_rpm IN (33, 45, 78)", name="speed_known"),
        CheckConstraint("expected_runtime_sec > 0", name="expected_runtime_positive"),
        CheckConstraint(
            "side_id = accession_id || '-D' || disc::text || '-' || side",
            name="id_matches_parts",
        ),
        # The phase-one capture queue: pending sides, 78s filtered out downstream.
        Index("ix_disc_side_queue", "speed_rpm", "status", postgresql_where="status = 'pending'"),
    )


class Capture(Base):
    """One capture attempt of one side.

    The row is INSERTed when recording *starts*, not when upload succeeds: a
    failed upload must never erase the record that the attempt happened.
    """

    __tablename__ = "capture"

    id: Mapped[uuid.UUID] = _uuid_pk()
    side_id: Mapped[str] = mapped_column(
        String(24),
        ForeignKey("disc_side.side_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: "Which file is the master" as a single fact, guaranteed unique per side by
    #: ``uq_capture_current_per_side``.
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Provenance only, advisory — NOT how the master is determined.
    supersedes: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("capture.id"), nullable=True
    )

    #: ``failed`` means exactly one thing: this capture is no good, redo it at the
    #: turntable. Upload trouble is a retry loop below, not a lifecycle state.
    status: Mapped[CaptureStatus] = mapped_column(
        StrEnumType(CaptureStatus, length=20), nullable=False, default=CaptureStatus.RECORDING
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: RECORDING INTEGRITY — laptop, seconds, judges the capture CHAIN. Kept for
    #: pass and fail alike: the diagnosis lives in the pattern across attempts
    #: (clipping every time = gain knob; xruns every time = the machine was busy).
    integrity: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    #: QUALITY — cluster, authoritative, judges the RECORD. Not fixable by redoing.
    qc_verdict: Mapped[QCVerdict | None] = mapped_column(
        StrEnumType(QCVerdict, length=20), nullable=True
    )
    qc: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Promoted out of the QC blob because these are sorted and thresholded ACROSS
    # THE COLLECTION, which JSONB does poorly:
    #   noise floor + clicks -> the "buy a commercial copy instead" flag
    #   peak                 -> is the gain setting consistent session to session
    #   duration delta       -> systematic bias across sides = platter speed error
    lead_in_noise_floor_dbfs: Mapped[float | None] = mapped_column(REAL, nullable=True)
    clicks_per_min: Mapped[float | None] = mapped_column(REAL, nullable=True)
    peak_dbfs: Mapped[float | None] = mapped_column(REAL, nullable=True)
    duration_delta_sec: Mapped[float | None] = mapped_column(REAL, nullable=True)

    # The artifact.
    s3_bucket: Mapped[str | None] = mapped_column(Text, nullable=True)
    s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Laptop staging path, kept for debugging uploads that never landed.
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: Recorded at capture time. These are archival masters headed for decades of
    #: cold storage; a checksum taken now is what proves bitrot later.
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Recorded facts, so the archival invariants (96 kHz / 24-bit) are provable.
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bit_depth: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    duration_sec: Mapped[float | None] = mapped_column(REAL, nullable=True)

    #: Upload is a RETRY LOOP, not a lifecycle state: the file is fine and sits on
    #: the laptop until the cluster is reachable (same self-healing shape as the
    #: recipe-pipeline worker). The row stays ``captured`` throughout.
    upload_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    upload_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    disc_side: Mapped[DiscSide] = relationship(back_populates="captures")

    __table_args__ = (
        CheckConstraint("upload_attempts >= 0", name="upload_attempts_non_negative"),
        CheckConstraint("bytes > 0", name="bytes_positive"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
        CheckConstraint(
            "status <> 'uploaded' OR (s3_bucket IS NOT NULL AND s3_key IS NOT NULL)",
            name="uploaded_has_object",
        ),
        # Exactly one current capture per side — the whole "which is the master"
        # question, answered by the database rather than by application logic.
        Index(
            "uq_capture_current_per_side",
            "side_id",
            unique=True,
            postgresql_where="is_current",
        ),
        # Upload queue (laptop): captured but not yet in S3. Failed captures never upload.
        Index(
            "ix_capture_upload_queue",
            "started_at",
            postgresql_where="status = 'captured'",
        ),
        # Review queue.
        Index("ix_capture_qc_queue", "qc_verdict", postgresql_where="is_current"),
    )


class Asset(Base):
    """Photos and generated documents in the Garage ``vinyl`` bucket.

    ``side_id`` is nullable: a jacket photo belongs to the set, but a runout
    etching photo belongs to ONE SIDE of ONE DISC — on a multi-disc set "the
    runout photo" is meaningless without knowing which.
    """

    __tablename__ = "asset"

    id: Mapped[uuid.UUID] = _uuid_pk()
    accession_id: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("accession.accession_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    side_id: Mapped[str | None] = mapped_column(
        String(24),
        ForeignKey("disc_side.side_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    kind: Mapped[AssetKind] = mapped_column(StrEnumType(AssetKind, length=20), nullable=False)

    s3_bucket: Mapped[str] = mapped_column(Text, nullable=False)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = _created_at()

    accession: Mapped[Accession] = relationship(back_populates="assets")

    __table_args__ = (
        CheckConstraint("bytes > 0", name="bytes_positive"),
        CheckConstraint(
            "kind <> 'runout' OR side_id IS NOT NULL", name="runout_names_a_side"
        ),
    )
