"""Tests for manual adjudication of same-second position conflicts.

Covers the full review workflow: picking one original fix for a contested
second, the 60 kn gate on the chosen point, stale-page rejection (version and
record-set fingerprint), automatic invalidation by late telegrams,
concurrency against uploads, and restart persistence.
"""

from __future__ import annotations

import io
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ais, db as dbmod
from app import main as mainmod
from app.store import Store

BASE = 1767225600
HEADER = "station,mmsi,utc,lat,lon\n"


# --------------------------------------------------------------------------
# Pure evaluator rules
# --------------------------------------------------------------------------


def cand(row_id, utc, lat, lon, station="SZ01", batch_id="b1"):
    return ais.Candidate(
        row_id=row_id, batch_id=batch_id, station=station, utc=utc, lat=lat, lon=lon
    )


def test_adjudication_promotes_chosen_candidate_and_keeps_others_isolated():
    rows = [
        cand(1, 0, 22.0, 114.0),
        cand(2, 60, 22.001, 114.001, "SZ01"),  # ~0.085 nm -> well under 60 kn
        cand(3, 60, 22.9, 114.9, "HK02"),
        cand(4, 120, 22.002, 114.002),
    ]
    ev = ais.evaluate_track(rows, adjudications={60: 2})
    assert ev.candidate_status[2][0] == "trusted"
    assert ev.candidate_status[3][0] == "isolated"
    assert ev.candidate_status[3][1] == "conflict"
    assert [p.row_id for p in ev.trusted_points] == [1, 2, 4]
    report = next(r for r in ev.seconds if r["utc"] == 60)
    assert report["status"] == "adjudicated" and report["selected_row_id"] == 2


def test_adjudicated_point_still_subject_to_speed_gate():
    rows = [
        cand(1, 0, 22.0, 114.0),
        cand(2, 60, 23.0, 114.0, "SZ01"),  # ~60 nm in 60 s -> far over 60 kn
        cand(3, 60, 22.001, 114.001, "HK02"),  # ~0.085 nm -> passes
    ]
    ev = ais.evaluate_track(rows, adjudications={60: 2})
    # Chosen point fails the gate and is isolated; the other side stays conflict.
    assert ev.candidate_status[2][0] == "isolated"
    assert ev.candidate_status[2][1] == "speed_exceeded"
    assert ev.candidate_status[3][1] == "conflict"
    assert [p.row_id for p in ev.trusted_points] == [1]

    # A close-by chosen point passes instead.
    ev2 = ais.evaluate_track(rows, adjudications={60: 3})
    assert ev2.candidate_status[3][0] == "trusted"
    assert [p.row_id for p in ev2.trusted_points] == [1, 3]


def test_unknown_adjucated_row_falls_back_to_conflict_isolation():
    rows = [cand(1, 60, 22.1, 114.1), cand(2, 60, 22.9, 114.9, "HK02")]
    ev = ais.evaluate_track(rows, adjudications={60: 999})
    assert all(s[0] == "isolated" and s[1] == "conflict" for s in ev.candidate_status.values())


def test_set_fingerprint_is_order_independent_and_changes_with_set():
    fp1 = Store.set_fingerprint([1, 2, 3])
    fp2 = Store.set_fingerprint([3, 1, 2])
    fp3 = Store.set_fingerprint([1, 2, 4])
    assert fp1 == fp2
    assert fp1 != fp3


# --------------------------------------------------------------------------
# End-to-end HTTP / store workflow
# --------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(mainmod, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(mainmod, "DB_PATH", tmp_path / "data" / "ais.sqlite3")
    mainmod.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with TestClient(mainmod.app) as c:
        c.tmp_path = tmp_path
        yield c


def upload(client, name, body: bytes | str):
    payload = body.encode() if isinstance(body, str) else body
    return client.post(
        "/upload",
        files={"file": (name, io.BytesIO(payload), "text/csv")},
        follow_redirects=False,
    )


def csv_text(*rows: str) -> str:
    return HEADER + "".join(r + "\n" for r in rows)


def conflict_payload(mmsi="413000002"):
    return csv_text(
        f"SZ01,{mmsi},{BASE - 60},22.299,114.099",     # prior trusted reference point
        f"SZ01,{mmsi},{BASE},22.3,114.1",          # near fix, passes the 60 kn gate
        f"HK02,{mmsi},{BASE},22.9,114.8",          # ~46 nm away, fails the gate
        f"SZ01,{mmsi},{BASE},22.3,114.1",          # re-sent duplicate of the near fix
        f"SZ01,{mmsi},{BASE + 120},22.301,114.101",
    )


def get_conflict_form(client, mmsi="413000002"):
    page = client.get(f"/vessels/{mmsi}")
    assert page.status_code == 200
    import re

    version = int(re.search(r'name="expected_version" value="(\d+)"', page.text).group(1))
    fingerprint = re.search(r'name="set_fingerprint" value="([0-9a-f]+)"', page.text).group(1)
    row_ids = [int(x) for x in re.findall(r'name="selected_row_id" value="(\d+)"', page.text)]
    return version, fingerprint, row_ids


def adjudicate(client, mmsi, utc, row_id, version, fingerprint):
    return client.post(
        f"/vessels/{mmsi}/adjudicate",
        data={
            "utc": utc,
            "selected_row_id": row_id,
            "expected_version": version,
            "set_fingerprint": fingerprint,
        },
        follow_redirects=False,
    )


def test_full_adjudication_workflow_publishes_chosen_track(client):
    upload(client, "conflict.csv", conflict_payload())
    version, fingerprint, row_ids = get_conflict_form(client)
    # Whole second isolated first: no trusted point at BASE.
    track = client.get("/vessels/413000002/track.csv")
    assert [line for line in track.text.splitlines()[1:] if line]
    import csv as _csv

    utcs = [int(r["utc_seconds"]) for r in _csv.DictReader(io.StringIO(track.text))]
    assert BASE not in utcs

    chosen = row_ids[0]  # SZ01 fix at 22.3/114.1, close to the later trusted point
    resp = adjudicate(client, "413000002", BASE, chosen, version, fingerprint)
    assert resp.status_code == 303, resp.text

    page = client.get("/vessels/413000002")
    assert "已采信" in page.text
    track = list(_csv.DictReader(io.StringIO(client.get("/vessels/413000002/track.csv").text)))
    utcs = [int(r["utc_seconds"]) for r in track]
    assert BASE in utcs  # the contested second now feeds the trusted track
    # The other two conflict rows stay isolated.
    isolated = list(
        _csv.DictReader(io.StringIO(client.get("/vessels/413000002/isolated.csv").text))
    )
    reasons = {r["reason"] for r in isolated}
    assert "conflict" in reasons


def test_speed_gate_rejection_rolls_back_everything(client):
    # Chosen fix is ~46 nm away from the next trusted point 120 s later; the
    # near fix passes, the far fix implies >60 kn and must be refused.
    upload(client, "conflict.csv", conflict_payload())
    version, fingerprint, row_ids = get_conflict_form(client)
    far_row = row_ids[1]  # HK02 fix 22.9/114.8
    near_row = row_ids[0]

    resp = adjudicate(client, "413000002", BASE, far_row, version, fingerprint)
    assert resp.status_code == 409
    assert "60 节" in resp.text

    # Nothing was published: vessel version unchanged, still fully isolated.
    page = client.get("/vessels/413000002")
    assert "已采信" not in page.text
    assert "未采信" in page.text
    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        assert conn.execute(
            "SELECT version FROM vessels WHERE mmsi='413000002'"
        ).fetchone()["version"] == version
        assert conn.execute("SELECT COUNT(*) c FROM adjudications").fetchone()["c"] == 0
    finally:
        conn.close()

    # The same page can then pick the near fix and succeed.
    resp2 = adjudicate(client, "413000002", BASE, near_row, version, fingerprint)
    assert resp2.status_code == 303


def test_stale_version_submission_is_rejected(client):
    upload(client, "conflict.csv", conflict_payload())
    version, fingerprint, row_ids = get_conflict_form(client)

    # A new batch moves the vessel to a new version while the page stays open.
    upload(
        client,
        "more.csv",
        csv_text(f"SZ01,413000002,{BASE + 300},22.302,114.102"),
    )
    resp = adjudicate(client, "413000002", BASE, row_ids[0], version, fingerprint)
    assert resp.status_code == 409
    assert "版本" in resp.text
    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM adjudications").fetchone()["c"] == 0
    finally:
        conn.close()


def test_stale_fingerprint_submission_is_rejected_after_set_change(client):
    upload(client, "conflict.csv", conflict_payload())
    version, fingerprint, row_ids = get_conflict_form(client)

    # A late telegram adds a third position to the same contested second: the
    # fingerprint embedded in the open page is now stale (and the version moved).
    upload(
        client,
        "late.csv",
        csv_text(f"TW03,413000002,{BASE},22.0,113.5"),
    )
    resp = adjudicate(client, "413000002", BASE, row_ids[0], version, fingerprint)
    assert resp.status_code == 409  # stale page rejected, nothing published

    # The fingerprint guard itself: bypassing the version check still refuses
    # the old set hash at the store boundary.
    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        current_version = conn.execute(
            "SELECT version FROM vessels WHERE mmsi='413000002'"
        ).fetchone()["version"]
        store = Store(mainmod.DATA_DIR, conn)
        with pytest.raises(Exception) as exc:
            store.adjudicate("413000002", BASE, row_ids[0], current_version, fingerprint)
        assert "记录集合" in exc.value.message
    finally:
        conn.close()

    # The fresh page carries the new fingerprint and can adjudicate the set.
    version2, fingerprint2, row_ids2 = get_conflict_form(client)
    assert fingerprint2 != fingerprint and version2 != version
    near = min(row_ids2)
    ok = adjudicate(client, "413000002", BASE, near, version2, fingerprint2)
    assert ok.status_code == 303


def test_late_telegram_invalidates_active_adjudication_to_pending_review(client):
    upload(client, "conflict.csv", conflict_payload())
    version, fingerprint, row_ids = get_conflict_form(client)
    assert adjudicate(client, "413000002", BASE, row_ids[0], version, fingerprint).status_code == 303

    # Active adjudication feeds the track.
    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        adj = conn.execute("SELECT * FROM adjudications WHERE mmsi='413000002'").fetchone()
        assert adj["state"] == "active"
    finally:
        conn.close()

    # Late telegram changes the contested second's set.
    upload(client, "late.csv", csv_text(f"TW03,413000002,{BASE},22.0,113.5"))

    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        adj = conn.execute("SELECT * FROM adjudications WHERE mmsi='413000002'").fetchone()
        assert adj["state"] == "pending_review"
        assert adj["reason"] == "set_changed"
        # Track reverts to full conflict isolation at BASE.
        import csv as _csv

        track = list(
            _csv.DictReader(io.StringIO(client.get("/vessels/413000002/track.csv").text))
        )
        assert all(int(r["utc_seconds"]) != BASE for r in track)
    finally:
        conn.close()

    page = client.get("/vessels/413000002")
    assert "待复核" in page.text
    # Re-adjudication on the new set restores an active choice.
    version2, fingerprint2, row_ids2 = get_conflict_form(client)
    resp = adjudicate(client, "413000002", BASE, min(row_ids2), version2, fingerprint2)
    assert resp.status_code == 303
    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        assert conn.execute(
            "SELECT state FROM adjudications WHERE mmsi='413000002'"
        ).fetchone()["state"] == "active"
    finally:
        conn.close()


def test_selected_row_outside_the_set_is_rejected(client):
    upload(client, "conflict.csv", conflict_payload())
    version, fingerprint, row_ids = get_conflict_form(client)
    other = max(row_ids) + 100
    resp = adjudicate(client, "413000002", BASE, other, version, fingerprint)
    assert resp.status_code == 400


def test_concurrent_upload_and_adjudication_never_publish_contradiction(client):
    upload(client, "conflict.csv", conflict_payload())
    version, fingerprint, row_ids = get_conflict_form(client)
    outcomes: list[str] = []

    def adjudicate_worker():
        resp = adjudicate(client, "413000002", BASE, row_ids[0], version, fingerprint)
        outcomes.append(f"adj:{resp.status_code}")

    def upload_worker():
        resp = upload(
            client,
            "late.csv",
            csv_text(f"TW03,413000002,{BASE},22.0,113.5"),
        )
        outcomes.append(f"upload:{resp.status_code}")

    threads = [threading.Thread(target=adjudicate_worker), threading.Thread(target=upload_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Both operations serialise on BEGIN IMMEDIATE; the upload always commits.
    # Whatever the order, a surviving active adjudication must match the set
    # that was visible on the submitting page - never the changed set.
    assert "upload:303" in outcomes
    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        adj = conn.execute("SELECT * FROM adjudications WHERE mmsi='413000002'").fetchone()
        if adj is not None and adj["state"] == "active":
            assert adj["set_fingerprint"] == fingerprint
            # An active adjudication means it committed before the upload; the
            # upload's set change then forces it to pending review on recompute.
        # Whatever happened, the published track matches the stored verdicts.
        vessel = conn.execute(
            "SELECT version, trusted_count FROM vessels WHERE mmsi='413000002'"
        ).fetchone()
        n_points = conn.execute(
            "SELECT COUNT(*) c FROM track_points WHERE mmsi='413000002' AND version=?",
            (vessel["version"],),
        ).fetchone()["c"]
        assert n_points == vessel["trusted_count"]
        # No verdict can both be trusted and reference an isolated second
        # inconsistently: every active adjudication's chosen row is trusted.
        if adj is not None and adj["state"] == "active":
            chosen = conn.execute(
                "SELECT status FROM verdicts WHERE mmsi='413000002' AND version=? "
                "AND raw_row_id=?",
                (vessel["version"], adj["selected_row_id"]),
            ).fetchone()
            assert chosen["status"] == "trusted"
    finally:
        conn.close()


def test_adjudication_survives_restart(client):
    upload(client, "conflict.csv", conflict_payload())
    version, fingerprint, row_ids = get_conflict_form(client)
    assert adjudicate(client, "413000002", BASE, row_ids[0], version, fingerprint).status_code == 303

    with TestClient(mainmod.app) as c2:
        page = c2.get("/vessels/413000002")
        assert "已采信" in page.text
        conn = dbmod.connect(mainmod.DB_PATH)
        try:
            adj = conn.execute(
                "SELECT state, set_fingerprint FROM adjudications WHERE mmsi='413000002'"
            ).fetchone()
            assert adj["state"] == "active" and adj["set_fingerprint"] == fingerprint
        finally:
            conn.close()


def test_pending_review_survives_restart(client):
    upload(client, "conflict.csv", conflict_payload())
    version, fingerprint, row_ids = get_conflict_form(client)
    adjudicate(client, "413000002", BASE, row_ids[0], version, fingerprint)
    upload(client, "late.csv", csv_text(f"TW03,413000002,{BASE},22.0,113.5"))

    with TestClient(mainmod.app) as c2:
        page = c2.get("/vessels/413000002")
        assert "待复核" in page.text
        conn = dbmod.connect(mainmod.DB_PATH)
        try:
            assert conn.execute(
                "SELECT state FROM adjudications WHERE mmsi='413000002'"
            ).fetchone()["state"] == "pending_review"
        finally:
            conn.close()


def test_re_adjudicating_replaces_the_old_choice_with_one_row(client):
    # Two distinct but both nearby positions so either choice passes the gate.
    payload = csv_text(
        f"SZ01,413000007,{BASE - 60},22.3,114.1",
        f"SZ01,413000007,{BASE},22.301,114.101",
        f"HK02,413000007,{BASE},22.302,114.102",
    )
    upload(client, "conflict7.csv", payload)
    version, fingerprint, row_ids = get_conflict_form(client, "413000007")
    adjudicate(client, "413000007", BASE, row_ids[0], version, fingerprint)
    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        first = conn.execute(
            "SELECT id, selected_row_id FROM adjudications WHERE mmsi='413000007'"
        ).fetchone()
    finally:
        conn.close()

    # Pick the other candidate of the same second.
    version2, fingerprint2, _ = get_conflict_form(client, "413000007")
    other = next(r for r in row_ids if r != first["selected_row_id"])
    resp = adjudicate(client, "413000007", BASE, other, version2, fingerprint2)
    assert resp.status_code == 303
    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        rows = conn.execute(
            "SELECT * FROM adjudications WHERE mmsi='413000007'"
        ).fetchall()
        assert len(rows) == 1  # upsert, no history pile-up
        assert rows[0]["selected_row_id"] == other and rows[0]["state"] == "active"
    finally:
        conn.close()
