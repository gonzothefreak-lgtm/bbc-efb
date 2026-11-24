"""BBC Flight Recorder: pure flight-state logic fed by Core observations. No Qt, no SimConnect.

Importing the package does not import Core: ``bbc_recorder.service`` (the Core-facing service)
and ``bbc_recorder.tape`` are imported explicitly by the app and the tests.
"""

import logging as _logging

# Library logging: the app configures handlers; without them, Recorder warnings stay quiet.
_logging.getLogger(__name__).addHandler(_logging.NullHandler())

from .model import EventKind, Phase, RecorderEvent
from .rules import RECORDER_FIELDS, RULES_VERSION, BlockOnPolicy, PausePolicy, RecorderRules, snapshot_simvars
from .state_machine import (
    EventJournal,
    FlightRecorder,
    FlightSink,
    FlightSummary,
    Lifecycle,
    LifecycleKind,
    Sample,
    SamplingHint,
    build_flight_record,
    haversine_nm,
    lifecycle_from_update,
    sample_from_snapshot,
)

__all__ = [
    "BlockOnPolicy", "EventJournal", "EventKind", "FlightRecorder", "FlightSink", "FlightSummary",
    "Lifecycle", "LifecycleKind", "PausePolicy", "Phase", "RECORDER_FIELDS", "RULES_VERSION",
    "RecorderEvent", "RecorderRules", "Sample", "SamplingHint", "build_flight_record", "haversine_nm",
    "lifecycle_from_update", "sample_from_snapshot", "snapshot_simvars",
]
