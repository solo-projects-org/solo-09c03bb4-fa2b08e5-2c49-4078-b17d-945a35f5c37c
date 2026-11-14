"""AIS CSV parsing, validation and per-vessel track evaluation.

The rules implemented here are the ones agreed with the duty officer:

* a row is identified by (station, MMSI, UTC second, lat, lon) - exact
  duplicates from a re-sent telegram are *collapsed* to one candidate;
* within one vessel and one UTC second, if every candidate has the same
  position the second stays eligible, otherwise the whole second is
  quarantined;
* candidates are ordered by UTC second and a candidate is quarantined when
  the speed computed against the previous trusted point exceeds 60 kn;
* because candidates may arrive out of order, the evaluation is a pure
  function of the full set of rows for a vessel.  A late telegram therefore
  re-evaluates the whole vessel and can change earlier verdicts.
"""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Iterator

EARTH_RADIUS_NM = 3440.065
MAX_SPEED_KNOTS = 60.0
SECONDS_PER_HOUR = 3600.0

# Retries of the very same telegram can carry tiny coordinate jitter.  Six
# decimal places is ~0.11 m, i.e. the resolution at which a position is still
# the same place; anything beyond that is treated as a different position.
POSITION_DECIMALS = 6

MERGE_WINDOW_SECONDS = 2.0

REQUIRED_COLUMNS = ("station", "mmsi", "utc", "lat", "lon")

ALIASES = {
    "station": {"station", "station_id", "shore_station", "site", "receiver", "岸站", "岸站号"},
    "mmsi": {"mmsi", "vessel_mmsi", "船", "船舶", "mmsi号"},
    "utc": {
        "utc",
        "utc_time",
        "utc_seconds",
        "utc_sec",
        "timestamp",
        "time",
        "epoch",
        "ts",
        "utc秒",
        "时间",
    },
    "lat": {"lat", "latitude", "纬度"},
    "lon": {"lon", "lng", "long", "longitude", "经度"},
}

MMSI_MIN, MMSI_MAX = 1, 999_999_999
LAT_MIN, LAT_MAX = -90.0, 90.0
LON_MIN, LON_MAX = -180.0, 180.0
EARLIEST_UTC = int(datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp())
LATEST_UTC = int(datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp())


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


class CsvFormatError(Exception):
    """The upload cannot be interpreted as an AIS CSV at all."""


@dataclass(frozen=True)
class RawRow:
    line_no: int
    station: str
    mmsi: str
    utc: int
    lat: float
    lon: float


@dataclass
class ParseOutcome:
    rows: list[RawRow] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)


def _decode(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvFormatError("文件既不是 UTF-8 也不是 GBK/Latin-1 编码")


def _normalise_header(name: str) -> str:
    return re.sub(r"[\s\-]+", "_", name.strip().lower())


def _map_header(header: list[str]) -> dict[str, int]:
    normalised = [_normalise_header(h) for h in header]
    mapping: dict[str, int] = {}
    for canonical, aliases in ALIASES.items():
        for idx, value in enumerate(normalised):
            if value in aliases:
                if canonical in mapping:
                    break
                mapping[canonical] = idx
                break
    missing = [c for c in REQUIRED_COLUMNS if c not in mapping]
    if missing:
        raise CsvFormatError(
            "缺少必需列: " + ", ".join(missing) + "；需要包含岸站号、MMSI、UTC 秒、纬度、经度"
        )
    return mapping


def _parse_int(text: str) -> int:
    return int(float(text.strip()))


def _parse_float(text: str) -> float:
    value = float(text.strip())
    if not math.isfinite(value):
        raise ValueError("坐标不是有限数值")
    return value


def parse_ais_csv(payload: bytes) -> ParseOutcome:
    """Parse the uploaded archive, keeping per-row rejection reasons."""

    if not payload.strip():
        raise CsvFormatError("上传文件为空")

    text = _decode(payload)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:  # pragma: no cover - empty payload handled above
        raise CsvFormatError("上传文件为空")

    header = [h.strip() for h in header]
    mapping = _map_header(header)
    outcome = ParseOutcome()

    for row in reader:
        line_no = reader.line_num
        if not row or all(not cell.strip() for cell in row):
            continue
        try:
            station = row[mapping["station"]].strip()
            mmsi = row[mapping["mmsi"]].strip()
            utc_text = row[mapping["utc"]]
            lat_text = row[mapping["lat"]]
            lon_text = row[mapping["lon"]]
        except IndexError:
            outcome.rejected.append(
                {"line": line_no, "reason": "malformed_row", "detail": "字段数量少于表头"}
            )
            continue

        if not station:
            outcome.rejected.append(
                {"line": line_no, "reason": "missing_station", "detail": "岸站号为空"}
            )
            continue
        if len(station) > 64:
            outcome.rejected.append(
                {"line": line_no, "reason": "invalid_station", "detail": "岸站号超过 64 字符"}
            )
            continue

        if not mmsi.isdigit() or not (MMSI_MIN <= int(mmsi) <= MMSI_MAX):
            outcome.rejected.append(
                {"line": line_no, "reason": "invalid_mmsi", "detail": f"MMSI 非法: {mmsi!r}"}
            )
            continue

        try:
            utc = _parse_int(utc_text)
        except (TypeError, ValueError):
            outcome.rejected.append(
                {"line": line_no, "reason": "invalid_utc", "detail": f"UTC 秒非法: {utc_text!r}"}
            )
            continue
        if not (EARLIEST_UTC <= utc <= LATEST_UTC):
            outcome.rejected.append(
                {
                    "line": line_no,
                    "reason": "utc_out_of_range",
                    "detail": f"UTC 秒超出 2000-2100 年范围: {utc}",
                }
            )
            continue

        try:
            lat = _parse_float(lat_text)
            lon = _parse_float(lon_text)
        except (TypeError, ValueError):
            outcome.rejected.append(
                {
                    "line": line_no,
                    "reason": "invalid_position",
                    "detail": f"经纬度非法: lat={lat_text!r} lon={lon_text!r}",
                }
            )
            continue
        if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
            outcome.rejected.append(
                {
                    "line": line_no,
                    "reason": "position_out_of_range",
                    "detail": f"经纬度越界: lat={lat} lon={lon}",
                }
            )
            continue
        if lat == 0.0 and lon == 0.0:
            outcome.rejected.append(
                {"line": line_no, "reason": "position_null_island", "detail": "0,0 常为无效定位"}
            )
            continue

        outcome.rows.append(
            RawRow(
                line_no=line_no,
                station=station,
                mmsi=mmsi.lstrip("0") or "0",
                utc=utc,
                lat=round(lat, POSITION_DECIMALS),
                lon=round(lon, POSITION_DECIMALS),
            )
        )
    return outcome


# --------------------------------------------------------------------------
# Track evaluation
# --------------------------------------------------------------------------


@dataclass
class Candidate:
    """One parsed row inside a vessel evaluation."""

    row_id: int
    station: str
    utc: int
    lat: float
    lon: float
    batch_id: str = ""
    status: str = "trusted"  # trusted | isolated
    reason: str | None = None
    detail: str | None = None


@dataclass
class TrustedPoint:
    row_id: int
    batch_id: str
    station: str
    utc: int
    lat: float
    lon: float
    speed_knots: float | None


@dataclass
class Evaluation:
    trusted_points: list[TrustedPoint]
    candidate_status: dict[int, tuple[str, str | None, str | None]]
    seconds: list[dict]


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(a)))


def speed_knots(lat1: float, lon1: float, lat2: float, lon2: float, dt: float) -> float:
    if dt <= 0:
        return math.inf
    return haversine_nm(lat1, lon1, lat2, lon2) / dt * SECONDS_PER_HOUR


def evaluate_track(rows: Iterable[Candidate]) -> Evaluation:
    """Recompute the whole vessel from its complete candidate set.

    Pure function; no database access so the semantics are easy to test.
    """

    rows = sorted(rows, key=lambda c: (c.utc, c.station, c.lat, c.lon, c.row_id))
    by_second: dict[int, list[Candidate]] = {}
    for row in rows:
        by_second.setdefault(row.utc, []).append(row)

    statuses: dict[int, tuple[str, str | None, str | None]] = {}
    second_reports: list[dict] = []
    positions_by_second: dict[int, list[Candidate]] = {}

    for utc in sorted(by_second):
        group = by_second[utc]
        distinct = {(c.lat, c.lon) for c in group}
        if len(distinct) > 1:
            for c in group:
                statuses[c.row_id] = ("isolated", "conflict", None)
            second_reports.append(
                {
                    "utc": utc,
                    "status": "conflict",
                    "candidate_count": len(group),
                    "distinct_positions": len(distinct),
                    "max_drift_nm": max(
                        haversine_nm(group[0].lat, group[0].lon, c.lat, c.lon) for c in group
                    )
                    if len(group) > 1
                    else 0.0,
                }
            )
        else:
            # Identical duplicates collapse: the earliest-arriving row wins so
            # that re-sent telegrams do not create pseudo-tracks.  The winner
            # is chosen by row id (insertion order) - station ordering would
            # make the result depend on something the operator cannot see.
            winner = min(group, key=lambda c: c.row_id)
            for c in group:
                if c is winner:
                    continue
                statuses[c.row_id] = (
                    "isolated",
                    "duplicate",
                    f"与候选 #{winner.row_id} 同秒同位置，仅保留该候选",
                )
            positions_by_second[utc] = [winner]
            second_reports.append(
                {
                    "utc": utc,
                    "status": "duplicate" if len(group) > 1 else "ok",
                    "candidate_count": len(group),
                    "distinct_positions": 1,
                    "max_drift_nm": 0.0,
                }
            )

    # Now walk the surviving, unique-per-second candidates in time order and
    # isolate those that imply an impossible speed from the previous trusted
    # point.
    ordered = sorted(positions_by_second.items())
    trusted: list[TrustedPoint] = []
    prev: TrustedPoint | None = None
    for utc, (candidate,) in ordered:
        if prev is None:
            candidate.status, candidate.reason, candidate.detail = "trusted", None, None
            point = TrustedPoint(
                row_id=candidate.row_id,
                batch_id=candidate.batch_id,
                station=candidate.station,
                utc=utc,
                lat=candidate.lat,
                lon=candidate.lon,
                speed_knots=None,
            )
            trusted.append(point)
            prev = point
            continue

        dt = utc - prev.utc
        knots = speed_knots(prev.lat, prev.lon, candidate.lat, candidate.lon, dt)
        if knots > MAX_SPEED_KNOTS:
            distance = haversine_nm(prev.lat, prev.lon, candidate.lat, candidate.lon)
            candidate.status = "isolated"
            candidate.reason = "speed_exceeded"
            candidate.detail = (
                f"相对上一可信点 {prev.utc} 的速度 {knots:.1f} 节超过 {MAX_SPEED_KNOTS:.0f} 节"
                f"（间隔 {dt} 秒，距离 {distance:.2f} 海里）"
            )
            for report in second_reports:
                if report["utc"] == utc:
                    report["status"] = "speed_exceeded"
                    report["speed_knots"] = round(knots, 2)
            continue

        candidate.status, candidate.reason, candidate.detail = "trusted", None, None
        point = TrustedPoint(
            row_id=candidate.row_id,
            batch_id=candidate.batch_id,
            station=candidate.station,
            utc=utc,
            lat=candidate.lat,
            lon=candidate.lon,
            speed_knots=knots,
        )
        trusted.append(point)
        prev = point
        for report in second_reports:
            if report["utc"] == utc:
                report["speed_knots"] = round(knots, 2)

    for candidate in rows:
        status = statuses.get(candidate.row_id)
        if status is not None:
            candidate.status, candidate.reason, candidate.detail = status
        # else: set by the speed pass above.

    final_status = {c.row_id: (c.status, c.reason, c.detail) for c in rows}
    return Evaluation(
        trusted_points=trusted,
        candidate_status=final_status,
        seconds=sorted(second_reports, key=lambda r: r["utc"]),
    )
