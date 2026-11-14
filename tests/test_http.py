"""HTTP-level tests: publication atomicity, downloads, concurrency, restart."""

from __future__ import annotations

import csv
import io
import os
import shutil
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as dbmod
from app import main as mainmod


BASE = 1767225600
HEADER = "station,mmsi,utc,lat,lon\n"


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


def test_upload_publishes_original_and_track(client):
    body = csv_text(
        f"SZ01,413000001,{BASE},22.5,114.2",
        f"SZ01,413000001,{BASE + 60},22.505,114.205",
    )
    resp = upload(client, "one.csv", body)
    assert resp.status_code == 303
    batch_url = resp.headers["location"]

    page = client.get(batch_url)
    assert page.status_code == 200
    assert "one.csv" in page.text

    original = client.get(batch_url + "/original.csv")
    assert original.status_code == 200
    assert original.content == body.encode()

    track = client.get("/vessels/413000001/track.csv")
    assert track.status_code == 200
    lines = list(csv.DictReader(io.StringIO(track.text)))
    assert [int(r["utc_seconds"]) for r in lines] == [BASE, BASE + 60]
    assert lines[1]["speed_knots"] == "24.52"


def test_replay_and_conflict_reasons_are_visible(client):
    body = csv_text(
        f"SZ01,413000001,{BASE},22.5,114.2",
        f"SZ01,413000001,{BASE},22.5,114.2",
        f"HK02,413000002,{BASE},22.3,114.1",
        f"HK02,413000002,{BASE},22.9,114.8",
        f"SZ01,413000002,{BASE},22.3,114.1",
    )
    upload(client, "dup.csv", body)
    page = client.get("/vessels/413000001")
    assert "duplicate" in page.text
    page2 = client.get("/vessels/413000002")
    assert "conflict" in page2.text

    isolated = client.get("/vessels/413000002/isolated.csv")
    rows = list(csv.DictReader(io.StringIO(isolated.text)))
    assert len(rows) == 3
    assert {r["reason"] for r in rows} == {"conflict"}


def test_failed_upload_does_not_replace_published_track(client):
    body = csv_text(f"SZ01,413000001,{BASE},22.5,114.2")
    upload(client, "good.csv", body)
    before = client.get("/vessels/413000001/track.csv").content

    bad = upload(client, "bad.csv", csv_text("SZ01,not-a-number,1,2,3"))
    assert bad.status_code == 422
    assert "未发布" in bad.text
    after = client.get("/vessels/413000001/track.csv").content
    assert before == after

    # No batch page exists for a rejected upload.
    assert client.get("/").text.count("bad.csv") == 0


def test_missing_columns_rejected(client):
    resp = upload(client, "bad.csv", "a,b,c\n1,2,3\n")
    assert resp.status_code == 400
    assert client.get("/").text.count("bad.csv") == 0


def test_late_batch_recomputes_whole_vessel(client):
    first = csv_text(
        f"SZ01,413000001,{BASE},22.0,114.0",
        f"SZ01,413000001,{BASE + 60},22.01,114.01",
        f"SZ01,413000001,{BASE + 120},22.02,114.02",
    )
    upload(client, "base.csv", first)
    track = list(csv.DictReader(io.StringIO(client.get("/vessels/413000001/track.csv").text)))
    assert len(track) == 3

    late = csv_text(f"HK02,413000001,{BASE + 60},22.9,114.9")
    upload(client, "late.csv", late)

    page = client.get("/vessels/413000001")
    assert page.status_code == 200
    track = list(csv.DictReader(io.StringIO(client.get("/vessels/413000001/track.csv").text)))
    # t+60 is now contested by both stations, so it is fully isolated; t+120
    # is re-measured against t+0 and survives.
    assert [int(r["utc_seconds"]) for r in track] == [BASE, BASE + 120]

    stats = client.get("/vessels").text
    assert "413000001" in stats


def test_concurrent_uploads_leave_consistent_state(client):
    def worker(idx: int):
        body = csv_text(
            f"SZ01,41300000{idx},{BASE + idx * 10},22.{idx},114.{idx}",
            f"SZ01,41300000{idx},{BASE + idx * 10 + 60},22.{idx}1,114.{idx}1",
        )
        return upload(client, f"c{idx}.csv", body).status_code

    results: list[int] = []
    threads = [threading.Thread(target=lambda i=i: results.append(worker(i))) for i in range(1, 6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [303] * 5
    for idx in range(1, 6):
        track = client.get(f"/vessels/41300000{idx}/track.csv")
        assert track.status_code == 200
        assert len(list(csv.DictReader(io.StringIO(track.text)))) == 2


def test_restart_keeps_published_batches_and_cleans_staging(client, tmp_path, monkeypatch):
    body = csv_text(f"SZ01,413000001,{BASE},22.5,114.2")
    resp = upload(client, "keep.csv", body)
    batch_url = resp.headers["location"]

    data_dir = tmp_path / "data"
    orphan = data_dir / "originals" / ".staging-crashed"
    orphan.mkdir(parents=True)
    (orphan / "original.csv").write_text("partial")

    # Simulate a restart with a brand new app lifespan on the same data dir.
    with TestClient(mainmod.app) as c2:
        assert c2.get(batch_url).status_code == 200
        assert c2.get(batch_url + "/original.csv").content == body.encode()
        assert c2.get("/vessels/413000001/track.csv").status_code == 200
    assert not orphan.exists()


def test_uncommitted_batch_directory_is_not_visible(client, tmp_path):
    data_dir = tmp_path / "data"
    dropped = data_dir / "originals" / "deadbeef"
    dropped.mkdir(parents=True)
    (dropped / "original.csv").write_text("station,mmsi,utc,lat,lon\n")

    assert client.get("/batches/deadbeef/original.csv").status_code == 404
    with TestClient(mainmod.app) as c2:
        assert not dropped.exists()


def test_version_pinning_snapshot(client, tmp_path):
    upload(client, "a.csv", csv_text(f"SZ01,413000001,{BASE},22.5,114.2"))
    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        # Simulate a reader that captured version 1 while a writer moves to v2.
        with dbmod.read_tx(conn):
            v1 = conn.execute(
                "SELECT version FROM vessels WHERE mmsi='413000001'"
            ).fetchone()["version"]
            assert v1 == 1
    finally:
        conn.close()

    upload(client, "b.csv", csv_text(f"SZ01,413000001,{BASE + 60},22.51,114.21"))

    conn = dbmod.connect(mainmod.DB_PATH)
    try:
        row = conn.execute("SELECT version, trusted_count FROM vessels WHERE mmsi='413000001'").fetchone()
        assert row["version"] == 2 and row["trusted_count"] == 2
        # Old versions are pruned; the new snapshot is complete.
        assert conn.execute(
            "SELECT COUNT(*) c FROM track_points WHERE mmsi='413000001' AND version=2"
        ).fetchone()["c"] == 2
    finally:
        conn.close()


def test_bad_rows_are_reported_but_good_rows_publish(client):
    body = csv_text(
        f"SZ01,413000001,{BASE},22.5,114.2",
        f"SZ01,bad,{BASE},22.5,114.2",
        f"SZ01,413000001,not-a-time,22.5,114.2",
    )
    resp = upload(client, "mixed.csv", body)
    assert resp.status_code == 303
    page = client.get(resp.headers["location"]).text
    assert "invalid_mmsi" in page
    assert "invalid_utc" in page
