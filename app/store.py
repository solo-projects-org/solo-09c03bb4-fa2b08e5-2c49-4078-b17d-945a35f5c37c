"""Atomic publication of uploads and per-vessel recomputation.

Key invariants
--------------
1. The original file is written to a staging directory and fsynced *before*
   any database write.  It is renamed into ``originals/<batch_id>/`` before
   the batch row is committed.
2. Everything a reader can see (raw rows, verdicts, trusted points) is
   committed in one transaction.  A failed upload therefore rolls back cleanly
   and leaves the previous published track untouched.
3. Each vessel has a monotonic ``version``.  Readers pin one version for a
   whole request so a concurrent recomputation cannot mix old and new points.
4. Recomputing a vessel is always a full replay of every committed row for
   that MMSI across all published batches, so a late telegram can move an
   earlier point from trusted to isolated (or vice versa).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import ais
from .db import write_tx

STAGING_PREFIX = ".staging-"


class UploadError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class AdjudicationError(UploadError):
    """A manual adjudication submission cannot be published (409/400)."""


@dataclass
class PublishedBatch:
    batch_id: str
    filename: str
    accepted: int
    rejected: int
    vessels: int
    rows_total: int


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, root: Path, conn: sqlite3.Connection):
        self.root = Path(root)
        self.originals_dir = self.root / "originals"
        self.conn = conn
        self.originals_dir.mkdir(parents=True, exist_ok=True)

    # -- startup recovery -------------------------------------------------

    def cleanup_staging(self) -> None:
        """Remove staging/orphan directories left by a crash.

        Only directories never referenced by a committed batch are touched;
        published batches are immutable and always survive a restart.
        """

        for entry in self.originals_dir.iterdir():
            if not entry.is_dir():
                continue
            if entry.name.startswith(STAGING_PREFIX):
                shutil.rmtree(entry, ignore_errors=True)
                continue
            row = self.conn.execute(
                "SELECT 1 FROM batches WHERE id = ?", (entry.name,)
            ).fetchone()
            if row is None:
                shutil.rmtree(entry, ignore_errors=True)

    # -- publication ------------------------------------------------------

    def publish(self, filename: str, payload: bytes) -> PublishedBatch:
        batch_id = uuid.uuid4().hex
        digest = hashlib.sha256(payload).hexdigest()

        try:
            outcome = ais.parse_ais_csv(payload)
        except ais.CsvFormatError as exc:
            raise UploadError(400, str(exc)) from exc

        if not outcome.rows and not outcome.rejected:
            raise UploadError(400, "文件没有可解析的数据行")
        if not outcome.rows:
            raise UploadError(
                422,
                "文件中没有任何通过校验的记录，已整体拒绝且未影响既有航迹",
            )

        staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=self.originals_dir))
        final_dir = self.originals_dir / batch_id
        try:
            staged_file = staging / "original.csv"
            with open(staged_file, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            _fsync_dir(staging)
            os.rename(staging, final_dir)
            _fsync_dir(self.originals_dir)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(final_dir, ignore_errors=True)
            raise UploadError(503, f"原档写入失败: {exc}") from exc

        relative = str(Path("originals") / batch_id / "original.csv")
        mmsis = sorted({row.mmsi for row in outcome.rows})

        try:
            with write_tx(self.conn) as conn:
                self._recheck_batch_id(conn, batch_id)
                conn.execute(
                    """
                    INSERT INTO batches (id, filename, sha256, byte_size, uploaded_at,
                                         status, rows_total, accepted_count, rejected_count,
                                         vessel_count, original_path)
                    VALUES (?, ?, ?, ?, ?, 'published', ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        filename[:255] or "upload.csv",
                        digest,
                        len(payload),
                        _utcnow(),
                        len(outcome.rows) + len(outcome.rejected),
                        len(outcome.rows),
                        len(outcome.rejected),
                        len(mmsis),
                        relative,
                    ),
                )
                self._insert_rows(conn, batch_id, outcome.rows)
                self._insert_rejects(conn, batch_id, outcome.rejected)
                for mmsi in mmsis:
                    self._recompute_vessel(conn, mmsi, batch_id)
        except Exception as exc:
            # The batch row was rolled back; its directory is now unreferenced
            # and is removed immediately (cleanup_staging covers a hard crash).
            shutil.rmtree(final_dir, ignore_errors=True)
            if isinstance(exc, UploadError):
                raise
            raise UploadError(503, f"判定写入失败，本批次未发布: {exc}") from exc

        return PublishedBatch(
            batch_id=batch_id,
            filename=filename,
            accepted=len(outcome.rows),
            rejected=len(outcome.rejected),
            vessels=len(mmsis),
            rows_total=len(outcome.rows) + len(outcome.rejected),
        )

    def _recheck_batch_id(self, conn: sqlite3.Connection, batch_id: str) -> None:
        if conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone():
            raise sqlite3.IntegrityError(f"批次 {batch_id} 已存在")

    def _insert_rows(self, conn: sqlite3.Connection, batch_id: str, rows) -> None:
        conn.executemany(
            "INSERT INTO raw_rows (batch_id, line_no, station, mmsi, utc, lat, lon) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (batch_id, r.line_no, r.station, r.mmsi, r.utc, r.lat, r.lon)
                for r in rows
            ],
        )

    def _insert_rejects(self, conn: sqlite3.Connection, batch_id: str, rejects) -> None:
        conn.executemany(
            "INSERT INTO rejects (batch_id, line_no, mmsi, station, utc, reason, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    batch_id,
                    item["line"],
                    item.get("mmsi"),
                    item.get("station"),
                    item.get("utc"),
                    item["reason"],
                    item["detail"],
                )
                for item in rejects
            ],
        )

    @staticmethod
    def set_fingerprint(row_ids) -> str:
        """Stable hash of the *complete* record set of one contested second.

        A page rendered before the set changed submits the fingerprint it was
        rendered with; publication only proceeds when it still matches.
        """

        joined = ",".join(str(i) for i in sorted(row_ids))
        return hashlib.sha256(joined.encode("ascii")).hexdigest()

    def _load_conflict_seconds(self, conn: sqlite3.Connection, mmsi: str) -> dict[int, list[int]]:
        """Return the current per-second sets of raw row ids for seconds whose
        candidates carry at least two distinct positions."""

        rows = conn.execute(
            "SELECT id, utc, lat, lon FROM raw_rows WHERE mmsi = ? ORDER BY utc, id",
            (mmsi,),
        ).fetchall()
        seconds: dict[int, list[sqlite3.Row]] = {}
        for r in rows:
            seconds.setdefault(r["utc"], []).append(r)
        return {
            utc: [r["id"] for r in group]
            for utc, group in seconds.items()
            if len({(r["lat"], r["lon"]) for r in group}) > 1
        }

    def _sync_adjudication_state(
        self, conn: sqlite3.Connection, mmsi: str, conflict_seconds: dict[int, list[int]]
    ) -> None:
        """Invalidate adjudications whose underlying record set has changed.

        A late telegram that adds/removes a row for a contested second changes
        the set the officer originally saw: the old choice can no longer stand
        and is marked ``pending_review`` so the track reverts to full
        conflict isolation until a human re-decides.
        """

        for adj in conn.execute(
            "SELECT * FROM adjudications WHERE mmsi = ? AND state = 'active'", (mmsi,)
        ).fetchall():
            current = conflict_seconds.get(adj["utc"])
            if current is None or self.set_fingerprint(current) != adj["set_fingerprint"]:
                conn.execute(
                    "UPDATE adjudications SET state = 'pending_review',"
                    " reason = 'set_changed',"
                    " detail = '迟到电文改变了该秒的完整记录集合，旧采信失效，待人工复核' "
                    "WHERE id = ?",
                    (adj["id"],),
                )

    def _active_adjudications(
        self, conn: sqlite3.Connection, mmsi: str
    ) -> dict[int, int]:
        return {
            r["utc"]: r["selected_row_id"]
            for r in conn.execute(
                "SELECT utc, selected_row_id FROM adjudications "
                "WHERE mmsi = ? AND state = 'active'",
                (mmsi,),
            ).fetchall()
        }

    def adjudicate(
        self,
        mmsi: str,
        utc: int,
        selected_row_id: int,
        expected_version: int,
        expected_fingerprint: str,
    ) -> int:
        """Publish one manual adjudication inside a single write transaction.

        The submission is refused when the page is stale (vessel version or the
        contested second's record-set fingerprint no longer match) or when the
        chosen fix itself violates the 60 kn gate.  On refusal the whole
        transaction rolls back, so the previously published track is untouched.
        Returns the new vessel version.
        """

        with write_tx(self.conn) as conn:
            vessel = conn.execute(
                "SELECT version FROM vessels WHERE mmsi = ?", (mmsi,)
            ).fetchone()
            if vessel is None:
                raise AdjudicationError(404, "没有该船的已发布数据")
            if vessel["version"] != expected_version:
                raise AdjudicationError(
                    409,
                    f"页面基于判定版本 v{expected_version}，当前已是 v{vessel['version']}，"
                    "请刷新页面后重新采信",
                )

            conflict_seconds = self._load_conflict_seconds(conn, mmsi)
            current_ids = conflict_seconds.get(utc)
            if current_ids is None:
                raise AdjudicationError(409, "该秒已不是冲突秒，无需采信，请刷新页面")
            fingerprint = self.set_fingerprint(current_ids)
            if fingerprint != expected_fingerprint:
                raise AdjudicationError(
                    409, "页面打开后该秒的完整记录集合已变化（迟到电文），请刷新页面后重新采信"
                )
            if selected_row_id not in current_ids:
                raise AdjudicationError(400, "所选记录不属于该冲突秒的原始记录集合")

            conn.execute(
                """
                INSERT INTO adjudications
                    (mmsi, utc, selected_row_id, state, set_fingerprint, vessel_version,
                     decided_batch_id, reason, detail, decided_at)
                VALUES (?, ?, ?, 'active', ?, ?, ?, NULL, NULL, ?)
                ON CONFLICT(mmsi, utc) DO UPDATE SET
                    selected_row_id = excluded.selected_row_id,
                    state = 'active',
                    set_fingerprint = excluded.set_fingerprint,
                    vessel_version = excluded.vessel_version,
                    decided_batch_id = excluded.decided_batch_id,
                    reason = NULL,
                    detail = NULL,
                    decided_at = excluded.decided_at
                """,
                (
                    mmsi,
                    utc,
                    selected_row_id,
                    fingerprint,
                    expected_version,
                    conn.execute(
                        "SELECT batch_id FROM raw_rows WHERE id = ?", (selected_row_id,)
                    ).fetchone()["batch_id"],
                    _utcnow(),
                ),
            )

            new_version = self._recompute_vessel(conn, mmsi, None)

            chosen = conn.execute(
                "SELECT status, reason FROM verdicts WHERE mmsi = ? AND version = ? "
                "AND raw_row_id = ?",
                (mmsi, new_version, selected_row_id),
            ).fetchone()
            if chosen["status"] != "trusted":
                # The chosen fix cannot join the trusted track (60 kn gate).
                # Roll back *everything*, including the adjudication row, so a
                # failed decision never publishes a contradictory state.
                raise AdjudicationError(
                    409,
                    "所选原始定位未通过 60 节速度门限"
                    + (f"（{chosen['reason']}），本次采信未发布，隔离状态保持不变" if chosen["reason"] else ""),
                )
            return new_version

    def _recompute_vessel(
        self, conn: sqlite3.Connection, mmsi: str, batch_id: str | None
    ) -> int:
        """Full replay of one vessel.  Returns the new vessel version."""

        rows = conn.execute(
            "SELECT id, batch_id, station, utc, lat, lon FROM raw_rows WHERE mmsi = ? "
            "ORDER BY utc, id",
            (mmsi,),
        ).fetchall()
        candidates = [
            ais.Candidate(
                row_id=r["id"],
                batch_id=r["batch_id"],
                station=r["station"],
                utc=r["utc"],
                lat=r["lat"],
                lon=r["lon"],
            )
            for r in rows
        ]

        # A record set changed by a late telegram invalidates any active
        # adjudication for that contested second before the replay.
        conflict_seconds = self._load_conflict_seconds(conn, mmsi)
        self._sync_adjudication_state(conn, mmsi, conflict_seconds)
        adjudications = self._active_adjudications(conn, mmsi)

        evaluation = ais.evaluate_track(candidates, adjudications)

        # The chosen point of an active adjudication may itself fail the speed
        # gate: the adjudication cannot stand and goes back to pending review.
        for adj in conn.execute(
            "SELECT id, utc, selected_row_id FROM adjudications "
            "WHERE mmsi = ? AND state = 'active'",
            (mmsi,),
        ).fetchall():
            status, reason, detail = evaluation.candidate_status.get(
                adj["selected_row_id"], ("isolated", None, None)
            )
            if status != "trusted":
                conn.execute(
                    "UPDATE adjudications SET state = 'pending_review', reason = ?, detail = ? "
                    "WHERE id = ?",
                    (
                        reason or "gate_failed",
                        (detail or "") + "（采信点未通过门限，航迹恢复冲突隔离）",
                        adj["id"],
                    ),
                )

        current = conn.execute(
            "SELECT version FROM vessels WHERE mmsi = ?", (mmsi,)
        ).fetchone()
        version = (current["version"] + 1) if current else 1

        conn.execute(
            """
            INSERT INTO vessels (mmsi, version, row_count, trusted_count, isolated_count,
                                 first_utc, last_utc, updated_at, last_batch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mmsi) DO UPDATE SET
                version = excluded.version,
                row_count = excluded.row_count,
                trusted_count = excluded.trusted_count,
                isolated_count = excluded.isolated_count,
                first_utc = excluded.first_utc,
                last_utc = excluded.last_utc,
                updated_at = excluded.updated_at,
                last_batch_id = COALESCE(excluded.last_batch_id, vessels.last_batch_id)
            """,
            (
                mmsi,
                version,
                len(candidates),
                len(evaluation.trusted_points),
                sum(1 for c in candidates if c.status == "isolated"),
                evaluation.trusted_points[0].utc if evaluation.trusted_points else None,
                evaluation.trusted_points[-1].utc if evaluation.trusted_points else None,
                _utcnow(),
                batch_id,
            ),
        )

        conn.execute(
            "DELETE FROM track_points WHERE mmsi = ? AND version = ?", (mmsi, version)
        )
        conn.execute("DELETE FROM verdicts WHERE mmsi = ? AND version = ?", (mmsi, version))

        conn.executemany(
            "INSERT INTO track_points (mmsi, version, seq, raw_row_id, batch_id, station,"
            " utc, lat, lon, speed_knots) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    mmsi,
                    version,
                    seq,
                    point.row_id,
                    point.batch_id,
                    point.station,
                    point.utc,
                    point.lat,
                    point.lon,
                    point.speed_knots,
                )
                for seq, point in enumerate(evaluation.trusted_points)
            ],
        )

        conn.executemany(
            "INSERT INTO verdicts (mmsi, version, raw_row_id, status, reason, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (mmsi, version, row_id, status, reason, detail)
                for row_id, (status, reason, detail) in evaluation.candidate_status.items()
            ],
        )

        # Drop all superseded versions; readers are pinned by version and any
        # in-flight read is on a snapshot that still sees them.
        conn.execute(
            "DELETE FROM track_points WHERE mmsi = ? AND version < ?", (mmsi, version)
        )
        conn.execute("DELETE FROM verdicts WHERE mmsi = ? AND version < ?", (mmsi, version))

        # Remember which vessel version an adjudication was decided against.
        conn.execute(
            "UPDATE adjudications SET vessel_version = ? WHERE mmsi = ? AND state = 'active'",
            (version, mmsi),
        )
        return version


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
