"""Starter data dictionary and SimConnect translation for the offline slice."""

from __future__ import annotations

from dataclasses import dataclass
from math import floor, isfinite
from typing import Callable

from .contract import (
    Capability, ClientEventData, CommandParameter, CoreRequestError, FrameSample, Kind, MissionAction,
    PauseState, Scalar, SimObjectChange, StrEnum, ValueType,
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
COM1_SPACING = Signal("COM SPACING MODE:1", "enum", ValueType.INTEGER)
ENGINE1_THROTTLE = Signal("GENERAL ENG THROTTLE LEVER POSITION:1", "percent", ValueType.NUMBER)
PAUSE_STATE = Signal("Pause_EX1", None, ValueType.INTEGER, event=True)

# Documented MSFS 2020 SimVars a Flight Recorder needs. Names and units follow the SDK SimVar
# tables. Native behaviour (datum, sign conventions, freshness and aircraft support) is still
# to be validated live, so each description says what the SDK promises, not what was observed.
_UNVALIDATED = " Native behaviour not yet validated."

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
        Capability("radio.com1.spacing_mode", Kind.VALUE, ValueType.INTEGER, None,
                   "COM1 channel spacing: 0 is 25 kHz, 1 is 8.33 kHz." + _UNVALIDATED),
        COM1_SPACING,
    ),
    ReadMapping(
        Capability("sim.pause_state", Kind.EVENT, ValueType.INTEGER, None,
                   "Simulator pause flags as a PauseState (an int): full pause, Active Pause and sim pause."),
        PAUSE_STATE,
    ),
    # Position, altitude and attitude.
    _value("aircraft.altitude_msl", "PLANE ALTITUDE", ValueType.NUMBER, "feet", "feet",
           "Aircraft altitude above mean sea level." + _UNVALIDATED),
    _value("aircraft.altitude_agl", "PLANE ALT ABOVE GROUND", ValueType.NUMBER, "feet", "feet",
           "Altitude above the ground below the aircraft." + _UNVALIDATED),
    _value("aircraft.altitude_agl_minus_cg", "PLANE ALT ABOVE GROUND MINUS CG", ValueType.NUMBER, "feet", "feet",
           "Altitude above ground minus the static centre-of-gravity height." + _UNVALIDATED),
    _value("aircraft.ground_altitude", "GROUND ALTITUDE", ValueType.NUMBER, "feet", "feet",
           "Ground elevation below the aircraft." + _UNVALIDATED),
    _value("aircraft.indicated_airspeed", "AIRSPEED INDICATED", ValueType.NUMBER, "knots", "knots",
           "Indicated airspeed." + _UNVALIDATED),
    _value("aircraft.true_airspeed", "AIRSPEED TRUE", ValueType.NUMBER, "knots", "knots",
           "True airspeed." + _UNVALIDATED),
    _value("aircraft.vertical_speed", "VERTICAL SPEED", ValueType.NUMBER, "feet per minute", "feet per minute",
           "Vertical speed relative to the earth, climb positive." + _UNVALIDATED),
    _value("aircraft.heading_true", "PLANE HEADING DEGREES TRUE", ValueType.NUMBER, "degrees", "degrees",
           "True heading." + _UNVALIDATED),
    _value("aircraft.heading_magnetic", "PLANE HEADING DEGREES MAGNETIC", ValueType.NUMBER, "degrees", "degrees",
           "Magnetic heading." + _UNVALIDATED),
    _value("aircraft.pitch", "PLANE PITCH DEGREES", ValueType.NUMBER, "degrees", "degrees",
           "Pitch attitude. The SDK sign convention is unvalidated." + _UNVALIDATED),
    _value("aircraft.bank", "PLANE BANK DEGREES", ValueType.NUMBER, "degrees", "degrees",
           "Bank attitude. The SDK sign convention is unvalidated." + _UNVALIDATED),
    _value("aircraft.g_force", "G FORCE", ValueType.NUMBER, "gforce", "gforce",
           "Current load factor." + _UNVALIDATED),
    _value("aircraft.acceleration_body_x", "ACCELERATION BODY X", ValueType.NUMBER,
           "feet per second squared", "feet per second squared", "Body-axis lateral acceleration." + _UNVALIDATED),
    _value("aircraft.acceleration_body_y", "ACCELERATION BODY Y", ValueType.NUMBER,
           "feet per second squared", "feet per second squared", "Body-axis vertical acceleration." + _UNVALIDATED),
    _value("aircraft.acceleration_body_z", "ACCELERATION BODY Z", ValueType.NUMBER,
           "feet per second squared", "feet per second squared",
           "Body-axis longitudinal acceleration." + _UNVALIDATED),
    _value("aircraft.on_any_runway", "ON ANY RUNWAY", ValueType.BOOLEAN, None, "bool",
           "Whether the aircraft is on any runway." + _UNVALIDATED),
    # Landing gear, brakes and flaps. Gear indices count from zero: 0 centre, 1 left, 2 right.
    _value("aircraft.gear.center_on_ground", "GEAR IS ON GROUND:0", ValueType.BOOLEAN, None, "bool",
           "Centre or nose gear touching the ground." + _UNVALIDATED),
    _value("aircraft.gear.left_on_ground", "GEAR IS ON GROUND:1", ValueType.BOOLEAN, None, "bool",
           "Left gear touching the ground." + _UNVALIDATED),
    _value("aircraft.gear.right_on_ground", "GEAR IS ON GROUND:2", ValueType.BOOLEAN, None, "bool",
           "Right gear touching the ground." + _UNVALIDATED),
    _value("aircraft.gear.handle_down", "GEAR HANDLE POSITION", ValueType.BOOLEAN, None, "bool",
           "Gear handle down." + _UNVALIDATED),
    _value("aircraft.gear.extended_percent", "GEAR TOTAL PCT EXTENDED", ValueType.NUMBER, "percent", "percent",
           "Average gear extension." + _UNVALIDATED),
    _value("aircraft.parking_brake", "BRAKE PARKING POSITION", ValueType.BOOLEAN, None, "bool",
           "Parking brake set." + _UNVALIDATED),
    _value("aircraft.flaps.handle_index", "FLAPS HANDLE INDEX", ValueType.INTEGER, None, "number",
           "Flap handle detent index." + _UNVALIDATED),
    _value("aircraft.flaps.handle_percent", "FLAPS HANDLE PERCENT", ValueType.NUMBER, "percent", "percent",
           "Flap handle position." + _UNVALIDATED),
    _value("aircraft.spoilers.handle_percent", "SPOILERS HANDLE POSITION", ValueType.NUMBER, "percent", "percent",
           "Spoiler handle position." + _UNVALIDATED),
    # Touchdown. These keep the last landing's values and can be stale from an earlier landing.
    _value("aircraft.touchdown.normal_velocity", "PLANE TOUCHDOWN NORMAL VELOCITY", ValueType.NUMBER,
           "feet per second", "feet per second",
           "Vertical speed at the last touchdown. May belong to an earlier landing; sign unvalidated."
           + _UNVALIDATED),
    _value("aircraft.touchdown.pitch", "PLANE TOUCHDOWN PITCH DEGREES", ValueType.NUMBER, "degrees", "degrees",
           "Pitch at the last touchdown. May belong to an earlier landing." + _UNVALIDATED),
    _value("aircraft.touchdown.bank", "PLANE TOUCHDOWN BANK DEGREES", ValueType.NUMBER, "degrees", "degrees",
           "Bank at the last touchdown. May belong to an earlier landing." + _UNVALIDATED),
    # Engines and fuel.
    _value("aircraft.engine_count", "NUMBER OF ENGINES", ValueType.INTEGER, None, "number",
           "Number of engines." + _UNVALIDATED),
    *(
        _value(f"aircraft.engine{index}.combustion", f"GENERAL ENG COMBUSTION:{index}", ValueType.BOOLEAN,
               None, "bool", f"Engine {index} combustion (running)." + _UNVALIDATED)
        for index in (1, 2, 3, 4)
    ),
    _value("aircraft.fuel_total_quantity", "FUEL TOTAL QUANTITY", ValueType.NUMBER, "gallons", "gallons",
           "Usable fuel on board by volume." + _UNVALIDATED),
    _value("aircraft.fuel_total_weight", "FUEL TOTAL QUANTITY WEIGHT", ValueType.NUMBER, "pounds", "pounds",
           "Usable fuel on board by weight." + _UNVALIDATED),
    _value("aircraft.total_weight", "TOTAL WEIGHT", ValueType.NUMBER, "pounds", "pounds",
           "Total aircraft weight." + _UNVALIDATED),
    # Identity. MSFS can return localisation keys such as TT:ATCCOM.AC_MODEL... for these.
    _value("aircraft.atc_id", "ATC ID", ValueType.TEXT, None, None,
           "ATC identifier (tail number) as set in the simulator." + _UNVALIDATED),
    _value("aircraft.atc_model", "ATC MODEL", ValueType.TEXT, None, None,
           "ATC model text. Can be a localisation key." + _UNVALIDATED),
    _value("aircraft.atc_type", "ATC TYPE", ValueType.TEXT, None, None,
           "ATC type text. Can be a localisation key." + _UNVALIDATED),
    _value("aircraft.atc_airline", "ATC AIRLINE", ValueType.TEXT, None, None,
           "ATC airline name, or empty." + _UNVALIDATED),
    _value("aircraft.atc_flight_number", "ATC FLIGHT NUMBER", ValueType.TEXT, None, None,
           "ATC flight number, or empty." + _UNVALIDATED),
    # Simulator clocks and rate. Host receipt time is separate from all of these.
    _value("sim.rate", "SIMULATION RATE", ValueType.NUMBER, None, "number",
           "Simulation rate multiplier." + _UNVALIDATED),
    _value("sim.simulation_time", "SIMULATION TIME", ValueType.NUMBER, "seconds", "seconds",
           "Simulated seconds since the flight started; scales with rate and stops in pause." + _UNVALIDATED),
    _value("sim.absolute_time", "ABSOLUTE TIME", ValueType.NUMBER, "seconds", "seconds",
           "Simulator UTC as seconds since 0001-01-01." + _UNVALIDATED),
    _value("sim.zulu_time", "ZULU TIME", ValueType.NUMBER, "seconds", "seconds",
           "Simulator UTC seconds since midnight." + _UNVALIDATED),
    _value("sim.zulu_day_of_month", "ZULU DAY OF MONTH", ValueType.INTEGER, None, "number",
           "Simulator UTC day of month." + _UNVALIDATED),
    _value("sim.zulu_month", "ZULU MONTH OF YEAR", ValueType.INTEGER, None, "number",
           "Simulator UTC month." + _UNVALIDATED),
    _value("sim.zulu_year", "ZULU YEAR", ValueType.INTEGER, None, "number",
           "Simulator UTC year." + _UNVALIDATED),
    _value("sim.local_time", "LOCAL TIME", ValueType.NUMBER, "seconds", "seconds",
           "Simulator local seconds since midnight." + _UNVALIDATED),
    _value("sim.slew_active", "IS SLEW ACTIVE", ValueType.BOOLEAN, None, "bool",
           "Slew mode active. A Recorder should not credit slewed movement." + _UNVALIDATED),
    _system("sim.running", "Sim", ValueType.BOOLEAN, "Simulator currently running."),
    _system("sim.dialog_mode", "DialogMode", ValueType.BOOLEAN,
            "Simulator dialog mode. No simulator event announces a change, so it is refreshed only on "
            "request (request_system_state) or after a provider gap."),
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


COM_BAND_HZ = (118_000_000, 137_000_000)
# COM frequency reads whose provider value may be a FLOAT64 number of Hz. Core rounds them to
# the nearest whole Hz (never truncates), so 128.010 MHz read as 128009999.9999 is 128010000.
FREQUENCY_HZ_SIGNALS = frozenset({COM1_ACTIVE, COM1_STANDBY})
COM_SPACING_25_KHZ = 0
COM_SPACING_8_33_KHZ = 1


def decode_frequency_hz(value: object) -> int:
    """A provider's COM frequency in Hz (an int, or a finite FLOAT64) as the nearest whole Hz."""
    if type(value) is int:
        return value
    if type(value) is float and isfinite(value):
        return int(floor(value + 0.5))
    raise ValueError("Invalid frequency value from the provider.")


def com_spacing(value: object) -> int | None:
    """COM SPACING MODE as 0 (25 kHz) or 1 (8.33 kHz), or None when it is not known."""
    if type(value) in (int, float) and value in (COM_SPACING_25_KHZ, COM_SPACING_8_33_KHZ):
        return int(value)
    return None


def com_channel_problem(hz: object, spacing: int | None = None) -> str | None:
    """Why ``hz`` cannot be tuned, or None when it is a valid COM channel.

    Core, the simulated provider and the live adapter use this one rule. ``spacing`` is
    COM SPACING MODE (0 for 25 kHz, 1 for 8.33 kHz) when known, otherwise None. 8.33 kHz
    channel names sit on a 5 kHz grid but never end in .x20, .x45, .x70 or .x95. A radio set
    to 25 kHz spacing accepts only 25 kHz channels.
    """
    if type(hz) is not int or not COM_BAND_HZ[0] <= hz < COM_BAND_HZ[1] or hz % 5_000:
        return "Use a COM frequency from 118.000 to 136.990 MHz on a 25 kHz or 8.33 kHz channel, in Hz."
    if hz % 25_000 == 20_000:
        return f"{hz / 1e6:.3f} MHz is not a 25 kHz or 8.33 kHz COM channel."
    if com_spacing(spacing) == COM_SPACING_25_KHZ and hz % 25_000:
        return (f"COM1 is set to 25 kHz channel spacing, so the 8.33 kHz channel {hz / 1e6:.3f} MHz "
                "cannot be tuned. Use a 25 kHz channel or switch the radio to 8.33 kHz spacing.")
    return None


def valid_com_channel(hz: int, spacing: int | None = None) -> bool:
    """Check a COM frequency in Hz against the VHF band and the channel grid (see com_channel_problem)."""
    return com_channel_problem(hz, spacing) is None


def check_com_frequency(arguments: tuple[Scalar, ...]) -> None:
    """Spacing-independent check. Core also checks the radio's known spacing before sending."""
    problem = com_channel_problem(arguments[0])
    if problem is not None:
        raise CoreRequestError(problem)


@dataclass(frozen=True, slots=True)
class CommandMapping:
    capability: Capability
    operation: str
    transport: CommandTransport
    signal: Signal | None = None
    validate: Callable[[tuple[Scalar, ...]], None] | None = None
    # A newer write from the same module replaces a queued, unsent one (absolute setters only).
    coalesce: bool = False

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
        result = tuple(result)
        if self.validate is not None:
            self.validate(result)
        return result


class CommandTransport(StrEnum):
    EVENT = "event"
    EXTENDED_EVENT = "extended_event"
    SIMVAR = "simvar"


def _command(id, description, group, operation, transport, parameters=(), signal=None, *,
             validate=None, coalesce=False):
    single = parameters[0] if len(parameters) == 1 else None
    capability = Capability(
        id, Kind.COMMAND, single.value_type if single else None, single.unit if single else None,
        description, group, parameters,
    )
    return CommandMapping(capability, operation, transport, signal, validate, coalesce)


_COMMANDS = (
    _command(
        "radio.com1.set_standby", "Set the COM1 standby frequency.", "radios",
        "COM_STBY_RADIO_SET_HZ", CommandTransport.EVENT,
        (CommandParameter("frequency", ValueType.INTEGER, "Hz", COM_BAND_HZ[0], COM_BAND_HZ[1] - 1),),
        validate=check_com_frequency, coalesce=True,
    ),
    _command(
        "radio.com1.swap", "Swap COM1 active and standby frequencies.", "radios",
        "COM_STBY_RADIO_SWAP", CommandTransport.EVENT,
    ),
    _command(
        "aircraft.engine1.set_throttle", "Set engine 1 forward throttle.", "engine_controls",
        ENGINE1_THROTTLE.name, CommandTransport.SIMVAR,
        (CommandParameter("position", ValueType.NUMBER, "percent", 0, 100),),
        ENGINE1_THROTTLE, coalesce=True,
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
    value_type: ValueType, value: object, signal: Signal | None = None,
) -> Scalar | FrameSample | SimObjectChange | MissionAction | ClientEventData | HardwareSample | PauseState:
    if signal is not None and signal == PAUSE_STATE:
        if type(value) is int and 0 <= value <= 0xFFFFFFFF:
            return PauseState(value)
        raise ValueError("Invalid pause flags from the provider.")
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
