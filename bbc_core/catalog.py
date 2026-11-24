"""Starter data dictionary and SimConnect translation for the offline slice."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite

from .contract import (
    Capability, ClientEventData, CommandParameter, CoreRequestError, FrameSample, Kind, MissionAction, Scalar,
    SimObjectChange, ValueType,
)
from .provider import Signal
from .simulator_ops import HardwareSample


@dataclass(frozen=True, slots=True)
class ReadMapping:
    capability: Capability
    signal: Signal


def _value(id, name, value_type, unit, provider_unit, description):
    return ReadMapping(
        Capability(id, Kind.VALUE, value_type, unit, description),
        Signal(name, provider_unit, value_type),
    )


def _system(id, name, value_type, description, *, event=False):
    return ReadMapping(
        Capability(id, Kind.EVENT if event else Kind.VALUE, value_type, None, description),
        Signal(name, None, value_type, event=event, system_state=not event),
    )


COM1_ACTIVE = Signal("COM ACTIVE FREQUENCY:1", "Hz", ValueType.INTEGER)
COM1_STANDBY = Signal("COM STANDBY FREQUENCY:1", "Hz", ValueType.INTEGER)
ENGINE1_THROTTLE = Signal("GENERAL ENG THROTTLE LEVER POSITION:1", "percent", ValueType.NUMBER)

_READS = (
    _value("aircraft.title", "TITLE", ValueType.TEXT, None, None, "Loaded aircraft title."),
    _value("aircraft.ground_speed", "GROUND VELOCITY", ValueType.NUMBER,
           "knots", "knots", "Speed relative to the earth's surface."),
    _value("aircraft.on_ground", "SIM ON GROUND", ValueType.BOOLEAN, None, "bool",
           "Observed air or ground state."),
    _value("aircraft.latitude", "PLANE LATITUDE", ValueType.NUMBER,
           "degrees", "degrees", "Latitude, north positive."),
    _value("aircraft.longitude", "PLANE LONGITUDE", ValueType.NUMBER,
           "degrees", "degrees", "Longitude, east positive."),
    ReadMapping(
        Capability("aircraft.engine1.throttle", Kind.VALUE, ValueType.NUMBER,
                   "percent", "Engine 1 throttle position."),
        ENGINE1_THROTTLE,
    ),
    ReadMapping(
        Capability("radio.com1.active_frequency", Kind.VALUE, ValueType.INTEGER,
                   "Hz", "COM1 active frequency."),
        COM1_ACTIVE,
    ),
    ReadMapping(
        Capability("radio.com1.standby_frequency", Kind.VALUE, ValueType.INTEGER,
                   "Hz", "COM1 standby frequency."),
        COM1_STANDBY,
    ),
    ReadMapping(
        Capability("sim.pause_state", Kind.EVENT, ValueType.INTEGER, None, "Simulator pause flags."),
        Signal("Pause_EX1", None, ValueType.INTEGER, event=True),
    ),
    _system("sim.running", "Sim", ValueType.BOOLEAN, "Simulator currently running."),
    _system("sim.dialog_mode", "DialogMode", ValueType.BOOLEAN, "Simulator dialog mode."),
    _system("sim.loaded_aircraft_file", "AircraftLoaded", ValueType.TEXT, "Last loaded aircraft file."),
    _system("sim.loaded_flight_file", "FlightLoaded", ValueType.TEXT, "Last loaded flight file."),
    _system("sim.active_flight_plan_file", "FlightPlan", ValueType.TEXT, "Active flight plan file, or empty."),
    _system("sim.running_changed", "Sim", ValueType.BOOLEAN, "Initial and changed running state.", event=True),
    _system("sim.started", "SimStart", ValueType.INTEGER, "Simulation started or resumed.", event=True),
    _system("sim.stopped", "SimStop", ValueType.INTEGER, "Simulation stopped or left flight.", event=True),
    _system("sim.aircraft_loaded", "AircraftLoaded", ValueType.TEXT, "Loaded aircraft file.", event=True),
    _system("sim.flight_loaded", "FlightLoaded", ValueType.TEXT, "Loaded flight file.", event=True),
    _system("sim.flight_saved", "FlightSaved", ValueType.TEXT, "Saved flight file.", event=True),
    _system("sim.flight_plan_activated", "FlightPlanActivated", ValueType.TEXT,
            "Activated flight plan file.", event=True),
    _system("sim.flight_plan_deactivated", "FlightPlanDeactivated", ValueType.INTEGER,
            "Active flight plan was deactivated.", event=True),
    _system("sim.aircraft_crashed", "Crashed", ValueType.INTEGER, "User aircraft crashed.", event=True),
    _system("sim.crash_reset", "CrashReset", ValueType.INTEGER, "Crash scene completed.", event=True),
    _system("sim.position_changed", "PositionChanged", ValueType.INTEGER,
            "User position was changed through a dialog.", event=True),
    _system("sim.paused", "Paused", ValueType.INTEGER, "Simulation paused.", event=True),
    _system("sim.unpaused", "Unpaused", ValueType.INTEGER, "Simulation unpaused.", event=True),
    _system("sim.pause_changed", "Pause", ValueType.BOOLEAN, "Initial and changed basic pause state.", event=True),
    _system("sim.sound_changed", "Sound", ValueType.INTEGER, "Master sound flags.", event=True),
    _system("sim.view_changed", "View", ValueType.INTEGER, "Aircraft view flags.", event=True),
    _system("sim.tick_1sec", "1sec", ValueType.INTEGER, "One-second SDK notification.", event=True),
    _system("sim.tick_4sec", "4sec", ValueType.INTEGER, "Four-second SDK notification.", event=True),
    _system("sim.tick_6hz", "6Hz", ValueType.INTEGER, "Six-per-second SDK notification.", event=True),
    _system("sim.frame", "Frame", ValueType.FRAME, "Frame rate and simulation speed per visual frame.", event=True),
    _system("sim.pause_frame", "PauseFrame", ValueType.FRAME, "Frame metrics while paused.", event=True),
    _system("sim.object_added", "ObjectAdded", ValueType.OBJECT, "A simulator object was added.", event=True),
    _system("sim.object_removed", "ObjectRemoved", ValueType.OBJECT, "A simulator object was removed.", event=True),
    _system("sim.weather_mode_changed", "WeatherModeChanged", ValueType.INTEGER,
            "Simulator weather mode changed.", event=True),
    _system("sim.mission_action", "CustomMissionActionExecuted", ValueType.MISSION_ACTION,
            "A simulator mission action executed.", event=True),
)
READS = {entry.capability.id: entry for entry in _READS}
SYSTEM_EVENTS = {entry.signal.name: entry.signal for entry in _READS if entry.signal.event}
SYSTEM_STATES = {entry.signal.name: entry.signal for entry in _READS if entry.signal.system_state}


@dataclass(frozen=True, slots=True)
class CommandMapping:
    capability: Capability
    operation: str
    transport: CommandTransport
    signal: Signal | None = None

    def arguments(self, value: Scalar | tuple[Scalar, ...] | None) -> tuple[Scalar, ...]:
        parameters = self.capability.parameters
        if not parameters:
            if value is not None:
                raise CoreRequestError("This command takes no argument.")
            return ()
        values = value if isinstance(value, tuple) else (value,)
        if len(values) != len(parameters):
            raise CoreRequestError("Supply the documented number of command arguments.")
        result = []
        for parameter, argument in zip(parameters, values):
            try:
                if parameter.value_type is ValueType.BOOLEAN and type(argument) is not bool:
                    raise ValueError("Expected a Boolean.")
                argument = decode_value(parameter.value_type, argument)
            except ValueError as error:
                raise CoreRequestError(f"Invalid type for {parameter.name}.") from error
            if ((parameter.minimum is not None and argument < parameter.minimum)
                    or (parameter.maximum is not None and argument > parameter.maximum)):
                raise CoreRequestError(f"{parameter.name} is outside its supported range.")
            result.append(argument)
        return tuple(result)


class CommandTransport(str, Enum):
    EVENT = "event"
    EXTENDED_EVENT = "extended_event"
    SIMVAR = "simvar"


def _command(id, description, group, operation, transport, parameters=(), signal=None):
    single = parameters[0] if len(parameters) == 1 else None
    capability = Capability(
        id, Kind.COMMAND, single.value_type if single else None, single.unit if single else None,
        description, group, parameters,
    )
    return CommandMapping(capability, operation, transport, signal)


_COMMANDS = (
    _command(
        "radio.com1.set_standby", "Set the COM1 standby frequency.", "radios",
        "COM_STBY_RADIO_SET_HZ", CommandTransport.EVENT,
        (CommandParameter("frequency", ValueType.INTEGER, "Hz", 1, 0xFFFFFFFF),),
    ),
    _command(
        "radio.com1.swap", "Swap COM1 active and standby frequencies.", "radios",
        "COM_STBY_RADIO_SWAP", CommandTransport.EVENT,
    ),
    _command(
        "aircraft.engine1.set_throttle", "Set engine 1 forward throttle.", "engine_controls",
        ENGINE1_THROTTLE.name, CommandTransport.SIMVAR,
        (CommandParameter("position", ValueType.NUMBER, "percent", 0, 100),),
        ENGINE1_THROTTLE,
    ),
    _command(
        "aircraft.electrical.toggle_bus_connection", "Toggle a connection between aircraft buses.",
        "electrical", "ELECTRICAL_BUS_TO_BUS_CONNECTION_TOGGLE", CommandTransport.EXTENDED_EVENT,
        (CommandParameter("source_bus", ValueType.INTEGER, minimum=0, maximum=0xFFFFFFFF),
         CommandParameter("target_bus", ValueType.INTEGER, minimum=0, maximum=0xFFFFFFFF)),
    ),
)
COMMANDS = {entry.capability.id: entry for entry in _COMMANDS}
OPERATIONS = {entry.operation: entry for entry in _COMMANDS}


def decode_event_value(
    value_type: ValueType, value: object,
) -> Scalar | FrameSample | SimObjectChange | MissionAction | ClientEventData | HardwareSample:
    if value_type is ValueType.HARDWARE_INPUT and isinstance(value, HardwareSample):
        return value
    if value_type is ValueType.CLIENT_EVENT and isinstance(value, ClientEventData):
        return value
    if value_type is ValueType.FRAME and isinstance(value, FrameSample):
        if all(isfinite(item) and item >= 0 for item in (value.frames_per_second, value.simulation_rate)):
            return value
    if value_type is ValueType.OBJECT and isinstance(value, SimObjectChange):
        return value
    if value_type is ValueType.MISSION_ACTION and isinstance(value, MissionAction):
        return value
    return decode_value(value_type, value)


def decode_value(value_type: ValueType, value: object) -> Scalar:
    if value_type is ValueType.BOOLEAN and type(value) in (bool, int):
        return bool(value)
    if value_type is ValueType.INTEGER and type(value) is int:
        return value
    if value_type is ValueType.TEXT and isinstance(value, str):
        return value
    if value_type is ValueType.BYTES and isinstance(value, bytes):
        return value
    if value_type is ValueType.NUMBER and type(value) in (int, float):
        try:
            if isfinite(value):
                return float(value)
        except OverflowError:
            pass
    raise ValueError(f"Invalid {value_type.value} value from the provider.")
