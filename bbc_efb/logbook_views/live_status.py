"""Pilot-facing wording for the live Flight Logging status on the Black Box page.

No Qt and no storage. ``describe_recorder_status`` accepts whatever the shell polls from
the Recorder and turns it into plain English, without guessing:

* the round-3 ``RecorderStatus`` contract (``state``, ``phase``, ``detail``, ``flight_id``,
  ``started_utc``, ``aircraft``, ``departure``, ``block_time_s``, ``airborne_time_s``,
  ``simulated``);
* the current ``bbc_recorder.service.RecorderStatus`` (``state``, ``label``, ``detail``,
  ``reason``, ``phase``, ``flight: CurrentFlight``);
* the footer's ``LoggingStatus`` (``state``, ``detail``, ``flight`` as label/value pairs,
  ``simulated``, ``label``);
* ``None`` (nothing received yet) or a plain string.

Only the Recorder decides whether Flight Logging is available. The page says
"not available" only when the status itself says ``unavailable``; any running state
(armed, recording, paused, gap, saving, error ...) is described as it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

#: States in which a flight is open (the Recorder is capturing or holding one).
OPEN_STATES = frozenset({"recording", "active", "paused", "gap", "interrupted", "saving"})

DEFAULT_HEADLINES = {
    "unavailable": "Flight Logging not available",
    "inactive": "Flight Logging inactive",
    "armed": "Ready",
    "ready": "Ready",
    "recording": "Logging active",
    "active": "Logging active",
    "paused": "Logging paused (simulator paused)",
    "gap": "Logging interrupted: data is missing",
    "interrupted": "Logging interrupted: data is missing",
    "saving": "Saving the flight",
    "logged": "Flight logged",
    "save_failed": "Save failed",
    "error": "Flight Logging error",
}
DEFAULT_DETAILS = {
    "unavailable": "The Recorder did not say why.",
    "inactive": "The Recorder is running but is not logging now (for example, no simulator connection).",
    "armed": "The Recorder is running and waiting for a flight to start.",
    "ready": "The Recorder is running and waiting for a flight to start.",
    "recording": "The Recorder is capturing this flight from simulator telemetry.",
    "active": "The Recorder is capturing this flight from simulator telemetry.",
    "paused": "The flight stays open while the simulator is paused.",
    "gap": "The flight stays open; the missing period will be listed as a data gap.",
    "interrupted": "The flight stays open; the missing period will be listed as a data gap.",
    "saving": "The Recorder is saving the flight to the Logbook.",
}
TONES = {
    "recording": "ok", "active": "ok", "logged": "ok",
    "paused": "busy", "saving": "busy", "armed": "off", "ready": "off", "inactive": "off",
    "unavailable": "off", "gap": "error", "interrupted": "error", "error": "error", "save_failed": "error",
}
NOT_RECEIVED = "Flight Logging status not received yet"


@dataclass(frozen=True, slots=True)
class LiveStatus:
    """What the Black Box page shows about Flight Logging right now."""

    state: str
    headline: str
    detail: str
    facts: tuple[tuple[str, str], ...] = ()
    simulated: bool = False
    flight_open: bool = False
    tone: str = "off"

    @property
    def text(self) -> str:
        parts = [self.headline + (" (SIMULATED)" if self.simulated and "SIMULATED" not in self.headline else "")]
        if self.detail:
            parts.append(self.detail)
        return ". ".join(part.rstrip(".") for part in parts) + "."


def _state_of(status: Any) -> str:
    state = getattr(status, "state", None)
    state = getattr(state, "value", state)
    return str(state).strip().lower() if state is not None else ""


def _text(value: Any) -> str:
    if value is None:
        return ""
    value = getattr(value, "value", value) if not isinstance(value, str) else value
    return str(value).strip()


def _first(*values: Any) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _duration(seconds: Any) -> str:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds != seconds or seconds < 0:
        return ""
    minutes = int(round(seconds / 60))
    return f"{minutes // 60}h {minutes % 60:02}m"


def _moment(value: Any, basis: str) -> str:
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return f"{moment.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC ({basis})"
    text = _text(value)
    return f"{text} ({basis})" if text else ""


def _phase(status: Any, flight: Any) -> str:
    label = _first(getattr(flight, "phase_label", None), getattr(status, "phase_label", None))
    if label:
        return label
    phase = getattr(status, "phase", None)
    if phase is None and flight is not None:
        phase = getattr(flight, "phase", None)
    text = _text(phase)
    return text.replace("_", " ").capitalize() if text else ""


def _facts(status: Any) -> tuple[tuple[str, str], ...]:
    flight = getattr(status, "flight", None)
    if isinstance(flight, (tuple, list)):
        # Footer LoggingStatus: already-worded (label, value) pairs.
        pairs = tuple((str(name), str(value)) for name, value in flight
                      if isinstance(name, str) and value not in (None, ""))
        flight = None
        if pairs:
            return pairs
    rows: list[tuple[str, str]] = []

    def add(name: str, value: str) -> None:
        if value:
            rows.append((name, value))

    add("Flight", _first(getattr(status, "flight_id", None), getattr(flight, "flight_id", None)))
    add("Aircraft", _first(getattr(status, "aircraft", None), getattr(flight, "aircraft_title", None),
                           getattr(flight, "atc_model", None)))
    add("Callsign", _first(getattr(flight, "atc_id", None)))
    add("From", _first(getattr(status, "departure", None)))
    add("Phase", _phase(status, flight))
    add("Started", _first(_moment(getattr(status, "started_utc", None), "simulator UTC"),
                          _moment(getattr(flight, "sim_started_utc", None), "simulator UTC"),
                          _moment(getattr(flight, "started_at", None), "computer clock")))
    add("Block time", _duration(getattr(status, "block_time_s", None)))
    add("Airborne time", _duration(getattr(status, "airborne_time_s", None)))
    for name, attr in (("Touchdowns", "touchdowns"), ("Data gaps", "gaps")):
        value = getattr(flight, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            add(name, f"{value:,}")
    if flight is not None and getattr(flight, "journal_ok", True) is False:
        add("Journal", "Not being written: see the footer and the log file")
    return tuple(rows)


def describe_recorder_status(status: Any) -> LiveStatus:
    """Plain-English live status. Never invents availability the status does not state."""
    if status is None:
        return LiveStatus("", NOT_RECEIVED, "The footer shows the current Flight Logging status.")
    if isinstance(status, str):
        text = status.strip()
        return LiveStatus("", text or NOT_RECEIVED, "")
    state = _state_of(status)
    label = getattr(status, "label", None)
    headline = _first(label if isinstance(label, str) else None, getattr(status, "text", None),
                      DEFAULT_HEADLINES.get(state), state.capitalize())
    if state != "unavailable" and "not available" in headline.lower():
        headline = DEFAULT_HEADLINES.get(state, headline)
    detail = _first(getattr(status, "detail", None), getattr(status, "reason", None), DEFAULT_DETAILS.get(state))
    flight_open = getattr(status, "flight_open", None)
    if not isinstance(flight_open, bool):
        flight_open = state in OPEN_STATES
    simulated = getattr(status, "simulated", False) is True
    return LiveStatus(state, headline or NOT_RECEIVED, detail, _facts(status), simulated, flight_open,
                      TONES.get(state, "off"))


__all__ = ["LiveStatus", "OPEN_STATES", "describe_recorder_status"]
