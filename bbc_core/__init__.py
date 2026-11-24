"""Fresh Black Box Core implementation."""

from __future__ import annotations

from .client_data import ClientDataArea, ClientDataMode, ClientDataRegion
from .contract import (
    API_VERSION, AircraftInput, Capability, CapabilityStatus, ClientEventData, CommandParameter, CommandResult,
    CommandState, Connection, ConnectionState, CoreRequestError, Discovery, Event, FrameSample, Gap,
    InputCatalog, InputCatalogState, InputId, InputParameters, Kind, MissionAction, Observation, PauseState,
    Quality, SignalError, SimObjectChange, Snapshot, SnapshotPeriod, Source, StrEnum, Subscription,
    SubscriptionEnded, Update, ValueType, simulator_utc,
)
from .core import ConsumerAPI, Core, known_control_groups
from .facilities import (
    AirportDetails, AirportFilter, AirportFilterField, AirportJetway, AirportReference,
    FacilityCacheSubscription, FacilityCacheUpdate,
    FacilityChange, FacilityIdentity, FacilityKind, FacilityResult, FacilityState, JetwayState,
    NavaidDetails, NavaidKind, NavaidReference, ParkingPosition, Runway,
)
from .sdk_simvar import SimVar, SimVarFormat
from .simulator_ops import (
    ControllerInfo, ControllerInput, HardwareSample, InitialPosition, ObjectField, ObjectFormat, ObjectKind,
    ObjectSample, SimObjectRef, SimulatorOperation, SimulatorReadState, SimulatorResult,
)

__all__ = [
    "API_VERSION", "Capability", "CapabilityStatus", "CommandParameter", "CommandResult", "CommandState",
    "Connection", "ConnectionState", "ConsumerAPI", "Core", "CoreRequestError", "Discovery",
    "Event", "Gap", "Kind", "Observation", "Quality", "Source", "Subscription", "Update",
    "ValueType", "AircraftInput", "InputCatalog", "InputCatalogState", "InputId", "InputParameters",
    "ClientDataArea", "ClientDataRegion", "FrameSample", "SimObjectChange", "MissionAction",
    "ClientDataMode",
    "SimVar", "SimVarFormat",
    "AirportDetails", "AirportFilter", "AirportFilterField", "AirportJetway", "AirportReference",
    "FacilityResult", "FacilityState",
    "ParkingPosition", "Runway", "JetwayState", "NavaidDetails", "NavaidKind",
    "FacilityCacheSubscription", "FacilityCacheUpdate", "FacilityChange", "FacilityIdentity", "FacilityKind",
    "NavaidReference",
    "ClientEventData",
    "ControllerInfo", "ControllerInput", "HardwareSample", "InitialPosition", "ObjectField", "ObjectFormat",
    "ObjectKind", "ObjectSample", "SimObjectRef", "SimulatorOperation", "SimulatorReadState", "SimulatorResult",
    "PauseState", "SignalError", "Snapshot", "SnapshotPeriod", "StrEnum", "SubscriptionEnded", "simulator_utc",
    "known_control_groups",
]
