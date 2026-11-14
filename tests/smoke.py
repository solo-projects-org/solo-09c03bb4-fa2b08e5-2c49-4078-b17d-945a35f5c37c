"""End-to-end smoke run over the bundled samples (filesystem + SQLite, no HTTP).

Usage: python -m tests.smoke            (from an activated venv)
       python tests/smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ais, db
from app.store import Store


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db.init_db(root / "ais.sqlite3")
        conn = db.connect(root / "ais.sqlite3")
        store = Store(root, conn)

        b1 = store.publish("batch1_base.csv", Path("samples/batch1_base.csv").read_bytes())
        print(f"batch1: accepted={b1.accepted} rejected={b1.rejected} vessels={b1.vessels}")

        b2 = store.publish(
            "batch2_late_arrivals.csv",
            Path("samples/batch2_late_arrivals.csv").read_bytes(),
        )
        print(f"batch2: accepted={b2.accepted} rejected={b2.rejected} vessels={b2.vessels}")

        flipped = conn.execute(
            "SELECT isolated_count FROM vessels WHERE mmsi = '413000006'"
        ).fetchone()
        isolated_416 = conn.execute(
            """
            SELECT COUNT(*) c FROM verdicts v
            WHERE v.mmsi = '413000006' AND v.version = 2 AND v.status = 'isolated'
            """
        ).fetchone()["c"]
        assert isolated_416 == 1, "late point did not change the earlier verdict"
        print(f"late point flipped an earlier point: isolated={isolated_416}")

        for mmsi in ("413000001", "413000002", "413000003", "413000005", "413000006"):
            row = conn.execute("SELECT * FROM vessels WHERE mmsi = ?", (mmsi,)).fetchone()
            print(
                f"\nMMSI {mmsi}: v{row['version']} rows={row['row_count']} "
                f"trusted={row['trusted_count']} isolated={row['isolated_count']}"
            )
            for p in conn.execute(
                "SELECT * FROM track_points WHERE mmsi = ? AND version = ? ORDER BY seq",
                (mmsi, row["version"]),
            ):
                speed = "" if p["speed_knots"] is None else f"{p['speed_knots']:.1f} kn"
                print(f"  trusted {p['utc']} {p['lat']:.6f},{p['lon']:.6f} {speed}")
            for v in conn.execute(
                """
                SELECT r.utc, r.station, v.reason, v.detail
                FROM verdicts v JOIN raw_rows r ON r.id = v.raw_row_id
                WHERE v.mmsi = ? AND v.version = ? AND v.status = 'isolated'
                ORDER BY r.utc, r.id
                """,
                (mmsi, row["version"]),
            ):
                print(f"  isolated {v['utc']} [{v['station']}] {v['reason']}: {v['detail'] or ''}")

        # An all-bad upload is rejected wholesale and leaves tracks untouched.
        before = conn.execute(
            "SELECT version, trusted_count FROM vessels WHERE mmsi = '413000003'"
        ).fetchone()
        try:
            store.publish("bad.csv", b"station,mmsi,utc,lat,lon\nSZ01,x,1,2,3\n")
        except Exception as exc:  # noqa: BLE001 - expected
            print(f"\nall-bad upload rejected: {type(exc).__name__}: {exc}")
        after = conn.execute(
            "SELECT version, trusted_count FROM vessels WHERE mmsi = '413000003'"
        ).fetchone()
        assert tuple(before) == tuple(after), "failed upload changed a published track"
        print("published tracks unchanged after failed upload")

        # The pure evaluator agrees with the persisted state.
        rows = conn.execute(
            "SELECT id, batch_id, station, utc, lat, lon FROM raw_rows WHERE mmsi='413000003'"
        ).fetchall()
        ev = ais.evaluate_track(
            [
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
        )
        persisted = conn.execute(
            "SELECT utc FROM track_points WHERE mmsi='413000003' AND version=1 ORDER BY seq"
        ).fetchall()
        assert [p.utc for p in ev.trusted_points] == [r["utc"] for r in persisted]
        print("evaluator matches persisted track")

        conn.close()
    print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
