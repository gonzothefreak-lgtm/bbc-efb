"""Shared fixtures for the logviews tests (record dictionaries in storage format 2)."""

from __future__ import annotations

import os
import random
import uuid
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

TODAY = date(2026, 11, 24)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def fields(**values):
    base = {
        "flight_date": "2026-11-20", "aircraft": "B738", "departure": "YSSY", "arrival": "YBBN",
        "callsign": "", "registration": "", "minutes": None, "notes": "", "crew": None,
        "passengers": None, "baggage_kg": None, "freight_kg": None,
    }
    base.update(values)
    return base


def manual(flight_id=None, saved_at="2026-11-20T10:00:00.000+00:00", extra=None, **values):
    record = {
        "schema": 2, "id": flight_id or str(uuid.uuid4()), "source": "manual",
        "revisions": [{"saved_at": saved_at, "by": "pilot", "reason": "", "fields": fields(**values)}],
    }
    if extra is not None:
        record["extra"] = extra
    return record


def evidence(**values):
    base = {
        "journal": "recorder/archive/x.jsonl", "summary": "Normal flight", "time_basis": "simulator",
        "simulated": False, "off_block": "2026-11-20T08:00:00.000+00:00",
        "takeoff": "2026-11-20T08:15:00.000+00:00", "landing": "2026-11-20T09:20:00.000+00:00",
        "on_block": "2026-11-20T09:30:00.000+00:00", "block_s": 5400.0, "airborne_s": 3900.0,
        "touchdown_rate_fpm": -180.0, "gaps": [], "rules_version": "r1",
    }
    base.update(values)
    return base


def recorded(flight_id=None, ev=None, acks=(), saved_at="2026-11-20T09:31:00.000+00:00", **values):
    record = {
        "schema": 2, "id": flight_id or str(uuid.uuid4()), "source": "recorder",
        "recorder": evidence() if ev is None else ev,
        "revisions": [{"saved_at": saved_at, "by": "recorder", "reason": "",
                       "origins": {"aircraft": "observed", "departure": "derived"},
                       "fields": fields(**{"minutes": 90, **values})}],
    }
    if acks:
        record["acknowledgements"] = [
            {"gap": index, "at": "2026-11-21T10:00:00.000+00:00", "by": "pilot", "note": note}
            for index, note in acks
        ]
    return record


def imported(flight_id=None, extra=None, **values):
    record = {
        "schema": 2, "id": flight_id or str(uuid.uuid4()), "source": "imported",
        "import": {"importer": "CSV", "reference": "log.csv line 4"},
        "revisions": [{"saved_at": "2026-11-19T10:00:00.000+00:00", "by": "import", "reason": "",
                       "fields": fields(**values)}],
    }
    if extra is not None:
        record["extra"] = extra
    return record


AIRCRAFT = ("B738", "A320", "B77W", "DH8D", "C172", "E190", "CRJ9", "A333", "PA28", "EC135", "XYZ9")
AIRPORTS = ("YSSY", "YMML", "YBBN", "YPPH", "EGLL", "LFPG", "KJFK", "NZAA", "VHHH", "YBCS")
OPERATIONS = ("airline", "freight", "charter", "ga", "")


def many(count=2000, seed=7):
    """A deterministic mixed Logbook of ``count`` record dictionaries."""
    rng = random.Random(seed)
    records = []
    start = date(2024, 1, 1)
    for index in range(count):
        day = start + timedelta(days=rng.randrange(0, 1060))
        dep, arr = rng.sample(AIRPORTS, 2)
        common = dict(
            flight_date=day.isoformat(), aircraft=rng.choice(AIRCRAFT), departure=dep, arrival=arr,
            callsign=f"BBC{index:04d}", registration=f"VH-{index % 900:03d}",
        )
        kind = index % 5
        op = rng.choice(OPERATIONS)
        extra = {"operation": op} if op else None
        if kind in (0, 1):
            common["minutes"] = rng.choice((None, rng.randrange(30, 600)))
            records.append(manual(extra=extra, **common))
        elif kind in (2, 3):
            gaps = [{"reason": "Simulator disconnected", "start": None, "end": None}] if index % 7 == 0 else []
            ev = evidence(touchdown_rate_fpm=rng.choice((None, -rng.randrange(40, 600))), gaps=gaps,
                          simulated=(index % 50 == 2), distance_nm=rng.choice((None, float(rng.randrange(80, 4000)))))
            rec = recorded(ev=ev, acks=((0, "ok"),) if gaps and index % 2 == 0 else (), **common)
            if extra:
                rec["extra"] = extra
            records.append(rec)
        else:
            common["minutes"] = rng.randrange(30, 600)
            records.append(imported(extra=extra, **common))
    return records


def app():
    from PySide6.QtWidgets import QApplication
    instance = QApplication.instance() or QApplication([])
    return instance


def process(ms=0):
    from PySide6.QtCore import QCoreApplication, QEventLoop, QElapsedTimer
    timer = QElapsedTimer()
    timer.start()
    QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
    while timer.elapsed() < ms:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
