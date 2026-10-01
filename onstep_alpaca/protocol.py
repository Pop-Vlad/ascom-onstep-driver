"""OnStep's LX200-derived serial protocol: command framing, reply shapes, coordinates.

Read out of the OnStep 3.16q firmware source, because the details that bite are all in
the source:

  * A reply's shape is a property of the command, not of the data (``Command.ino``
    ~2100). Nothing in the byte stream says which is coming, so ``ReplyShape`` is
    declared per command and is not negotiable.
  * ``:GR#``/``:GD#`` return low or high precision depending on a mutable toggle
    (``:U#``), so only the explicit ``:GRa#``/``:GDe#`` are ever read.
  * The firmware validates coordinates by STRING LENGTH and the accepted sets are not
    contiguous: RA takes 8 or >=10 characters, declination 9 or >=11. An unpadded field
    is what trips it, so the formatters emit fixed widths.
  * ``dmsToDouble``'s seconds-separator check increments twice, making its apostrophe
    branch unreachable -- only ``:`` works on input, though OnStep *emits* an
    apostrophe.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass


# --------------------------------------------------------------------------------------
# Reply framing


class ReplyShape(enum.Enum):
	"""How to know a reply is complete. A property of the command, not of the data."""

	NONE = "none"
	"""No reply at all. The firmware sends nothing when ``strlen(reply) == 0`` (Command.ino
	~2107), so the reader must not wait for bytes that never come."""

	BOOLEAN = "boolean"
	"""A single ``0``/``1`` with NO ``#`` terminator (``supress_frame=true``, Command.ino
	~2100). Read exactly one byte."""

	DIGIT = "digit"
	"""A single ``0``-``9``, no terminator: the goto-result codes of ``:MS#``/``:MA#``
	(Command.ino ~1215, ~1273). Read exactly one byte."""

	TERMINATED = "terminated"
	"""Payload followed by ``#``. Read until the terminator."""

	OPTIONAL = "optional"
	"""A terminated reply, or a bare ``0`` meaning the mount would not answer."""


DEFAULT_TIMEOUT = 2.0
"""Seconds to wait for a reply to a command that only reads state."""

SLEW_START_TIMEOUT = 10.0
"""``:MS#`` and friends validate the target and spin up the motion profile before they
answer; they are measurably slower than a plain read, especially on an AVR build."""


@dataclass(frozen=True, slots=True)
class Cmd:
	"""One OnStep command, bound to the reply shape its handler actually produces."""

	text: str
	shape: ReplyShape
	timeout: float = DEFAULT_TIMEOUT

	def __str__(self) -> str:  # pragma: no cover - debugging aid
		return self.text


# --------------------------------------------------------------------------------------
# Command errors (:GE# returns an index into this enum -- Globals.h:192)


class CommandError(enum.IntEnum):
	NONE = 0
	ZERO = 1
	CMD_UNKNOWN = 2
	REPLY_UNKNOWN = 3
	PARAM_RANGE = 4
	PARAM_FORM = 5
	ALIGN_FAIL = 6
	ALIGN_NOT_ACTIVE = 7
	NOT_PARKED_OR_AT_HOME = 8
	PARKED = 9
	PARK_FAILED = 10
	NOT_PARKED = 11
	NO_PARK_POSITION_SET = 12
	GOTO_FAIL = 13
	LIBRARY_FULL = 14
	GOTO_ERR_BELOW_HORIZON = 15
	GOTO_ERR_ABOVE_OVERHEAD = 16
	SLEW_ERR_IN_STANDBY = 17
	SLEW_ERR_IN_PARK = 18
	GOTO_ERR_GOTO = 19
	SLEW_ERR_OUTSIDE_LIMITS = 20
	SLEW_ERR_HARDWARE_FAULT = 21
	MOUNT_IN_MOTION = 22
	GOTO_ERR_UNSPECIFIED = 23
	NULL = 24


COMMAND_ERROR_TEXT = {
	CommandError.NONE: "no error",
	CommandError.CMD_UNKNOWN: "command not recognised by the mount",
	CommandError.REPLY_UNKNOWN: "mount could not form a reply",
	CommandError.PARAM_RANGE: "parameter out of range",
	CommandError.PARAM_FORM: "malformed parameter",
	CommandError.ALIGN_FAIL: "alignment failed",
	CommandError.ALIGN_NOT_ACTIVE: "no alignment in progress",
	CommandError.NOT_PARKED_OR_AT_HOME: "mount is neither parked nor at home",
	CommandError.PARKED: "not allowed while parked",
	CommandError.PARK_FAILED: "park failed",
	CommandError.NOT_PARKED: "mount is not parked",
	CommandError.NO_PARK_POSITION_SET: "no park position has been set",
	CommandError.GOTO_FAIL: "goto failed",
	CommandError.LIBRARY_FULL: "object library is full",
	CommandError.GOTO_ERR_BELOW_HORIZON: "target is below the horizon limit",
	CommandError.GOTO_ERR_ABOVE_OVERHEAD: "target is above the overhead limit",
	CommandError.SLEW_ERR_IN_STANDBY: "mount is in standby",
	CommandError.SLEW_ERR_IN_PARK: "mount is parked",
	CommandError.GOTO_ERR_GOTO: "a goto is already running",
	CommandError.SLEW_ERR_OUTSIDE_LIMITS: "target is outside the mount's limits",
	CommandError.SLEW_ERR_HARDWARE_FAULT: "hardware fault",
	CommandError.MOUNT_IN_MOTION: "mount is in motion",
	CommandError.GOTO_ERR_UNSPECIFIED: "goto refused",
}

GOTO_RESULT_ERRORS = {
	"0": CommandError.NONE,
	"1": CommandError.GOTO_ERR_BELOW_HORIZON,
	"2": CommandError.GOTO_ERR_ABOVE_OVERHEAD,
	"3": CommandError.SLEW_ERR_IN_STANDBY,
	"4": CommandError.SLEW_ERR_IN_PARK,
	"5": CommandError.GOTO_ERR_GOTO,
	"6": CommandError.SLEW_ERR_OUTSIDE_LIMITS,
	"7": CommandError.SLEW_ERR_HARDWARE_FAULT,
	"8": CommandError.MOUNT_IN_MOTION,
	"9": CommandError.GOTO_ERR_UNSPECIFIED,
}
"""``:MS#``/``:MA#`` map ``CE_GOTO_ERR_BELOW_HORIZON..CE_GOTO_ERR_UNSPECIFIED`` onto the
digits ``1``-``9``, with ``0`` meaning accepted (Command.ino ~1269)."""


class ProtocolError(Exception):
	"""The mount's answer did not fit the protocol (bad framing, unparseable value)."""


class CommandRejected(Exception):
	"""The mount understood the command and refused it."""

	def __init__(self, command: str, error: CommandError | None = None):
		self.command = command
		self.error = error
		if error is None or error is CommandError.NONE:
			text = "refused, no reason reported"
		else:
			text = COMMAND_ERROR_TEXT.get(error, f"error {int(error)}")
		super().__init__(f"{command} rejected: {text}")


# --------------------------------------------------------------------------------------
# Coordinate conversion. Parsing is permissive (whatever precision the mount is in);
# formatting is strict (see the module docstring on length-based validation).

_HMS_RE = re.compile(r"^\s*(\d{1,3}):(\d{2})(?::(\d{2}(?:\.\d+)?)|\.(\d))?\s*$")
_DMS_RE = re.compile(
	r"^\s*([+-]?)(\d{1,3})[*:°ß]"  # degrees, then * : degree-sign or char(223)
	r"(\d{2})"  # minutes
	r"(?:['′:](\d{2}(?:\.\d+)?))?\s*$"  # optional ' or : then seconds
)


def parse_hms(text: str) -> float:
	"""Parse ``HH:MM:SS.ss``, ``HH:MM:SS`` or low-precision ``HH:MM.T`` to hours."""
	m = _HMS_RE.match(text)
	if not m:
		raise ProtocolError(f"not an HMS value: {text!r}")
	hours, minutes, seconds, tenths = m.groups()
	value = int(hours) + int(minutes) / 60.0
	if seconds is not None:
		value += float(seconds) / 3600.0
	elif tenths is not None:
		value += int(tenths) / 600.0
	return value


def parse_dms(text: str) -> float:
	"""Parse ``sDD*MM'SS.s``, ``sDD*MM:SS`` or ``sDD*MM`` to degrees."""
	m = _DMS_RE.match(text)
	if not m:
		raise ProtocolError(f"not a DMS value: {text!r}")
	sign, degrees, minutes, seconds = m.groups()
	value = int(degrees) + int(minutes) / 60.0
	if seconds is not None:
		value += float(seconds) / 3600.0
	return -value if sign == "-" else value


def format_hms(hours: float, high_precision: bool = True) -> str:
	"""Hours -> ``HH:MM:SS.ss`` (11 chars) or ``HH:MM:SS`` (8 chars)."""
	if high_precision:
		total = round(hours * 3600 * 100) % (24 * 3600 * 100)
		h, rem = divmod(total, 3600 * 100)
		m, cs = divmod(rem, 60 * 100)
		return f"{h:02d}:{m:02d}:{cs / 100:05.2f}"
	total = round(hours * 3600) % (24 * 3600)
	h, rem = divmod(total, 3600)
	m, s = divmod(rem, 60)
	return f"{h:02d}:{m:02d}:{s:02d}"


def format_dms(degrees: float, high_precision: bool = True) -> str:
	"""Degrees -> ``sDD*MM:SS.ss`` (12 chars) or ``sDD*MM:SS`` (9 chars)."""
	degrees = max(-90.0, min(90.0, degrees))
	sign = "-" if degrees < 0 else "+"
	magnitude = abs(degrees)
	if high_precision:
		total = min(round(magnitude * 3600 * 100), 90 * 3600 * 100)
		d, rem = divmod(total, 3600 * 100)
		m, cs = divmod(rem, 60 * 100)
		return f"{sign}{d:02d}*{m:02d}:{cs / 100:05.2f}"
	total = min(round(magnitude * 3600), 90 * 3600)
	d, rem = divmod(total, 3600)
	m, s = divmod(rem, 60)
	return f"{sign}{d:02d}*{m:02d}:{s:02d}"


_TIME_ZONE_RE = re.compile(r"^\s*([+-]?)(\d{1,2})(?::(\d{2}))?\s*$")


def parse_time_zone(text: str) -> float:
	"""Parse ``:GG#`` (``sHH`` or ``sHH:MM``) into a conventional offset."""
	m = _TIME_ZONE_RE.match(text)
	if not m:
		raise ProtocolError(f"not a time zone: {text!r}")
	sign, hours, minutes = m.groups()
	value = int(hours) + (int(minutes) / 60.0 if minutes else 0.0)
	if sign == "-":
		value = -value
	return -value


def parse_last_error(text: str) -> CommandError:
	"""Parse ``:GE#``, which answers two zero-padded digits (``sprintf("%02d")``)."""
	try:
		return CommandError(int(text))
	except ValueError as exc:
		raise ProtocolError(f"not an error code: {text!r}") from exc


def format_dm(degrees: float, width: int = 2) -> str:
	"""Degrees -> ``sDD*MM`` / ``sDDD*MM``, the only form ``:St#``/``:Sg#`` accept."""
	sign = "-" if degrees < 0 else "+"
	total = round(abs(degrees) * 60)
	d, m = divmod(total, 60)
	return f"{sign}{d:0{width}d}*{m:02d}"


# --------------------------------------------------------------------------------------
# Status (:GU#)


class MountType(enum.Enum):
	GEM = "E"
	FORK = "K"
	ALTAZ = "A"
	UNKNOWN = "?"


class PierSide(enum.Enum):
	NONE = "o"
	EAST = "T"
	WEST = "W"


class ParkState(enum.Enum):
	NOT_PARKED = "p"
	PARKING = "I"
	PARKED = "P"
	PARK_FAILED = "F"


class RateCompensation(enum.Enum):
	NONE = "none"
	REFRACTION_RA = "refr_ra"
	REFRACTION_BOTH = "refr_both"
	ONTRACK_RA = "ontrack_ra"
	ONTRACK_BOTH = "ontrack_both"


_PARK_CHARS = {state.value: state for state in ParkState}
_PIER_CHARS = {side.value: side for side in PierSide}
_MOUNT_CHARS = {kind.value: kind for kind in MountType if kind is not MountType.UNKNOWN}
PEC_CHARS = "/,~;^"
"""Ignore / ready-to-play / playing / ready-to-record / recording."""


@dataclass(frozen=True, slots=True)
class Status:
	"""Decoded ``:GU#``. One round trip answers nearly every ASCOM state property."""

	tracking: bool
	slewing: bool
	park: ParkState
	at_home: bool
	waiting_at_home: bool
	pause_at_home: bool
	guiding: bool
	pps_synced: bool
	pec_recorded: bool
	sync_to_encoders_only: bool
	sound_enabled: bool
	auto_meridian_flip: bool
	rate_compensation: RateCompensation
	mount_type: MountType
	pier_side: PierSide
	pulse_guide_rate_index: int
	guide_rate_index: int
	general_error: int
	raw: str

	@property
	def parked(self) -> bool:
		return self.park is ParkState.PARKED


def parse_status(reply: str) -> Status:
	"""Decode the ``:GU#`` flag string (Command.ino:650)."""
	text = reply.strip()
	if len(text) < 5:
		raise ProtocolError(f"status reply too short: {reply!r}")

	try:
		general_error = int(text[-1])
		guide_rate_index = int(text[-2])
		pulse_guide_rate_index = int(text[-3])
	except ValueError as exc:
		raise ProtocolError(f"status reply has no numeric tail: {reply!r}") from exc

	pier_side = _PIER_CHARS.get(text[-4])
	if pier_side is None:
		raise ProtocolError(f"status reply has no pier side: {reply!r}")

	mount_type = _MOUNT_CHARS.get(text[-5], MountType.UNKNOWN)
	prefix = text[:-5]

	park = next((state for ch, state in _PARK_CHARS.items() if ch in prefix), None)
	if park is None:
		raise ProtocolError(f"status reply has no park state: {reply!r}")

	if "r" in prefix:
		compensation = (
			RateCompensation.REFRACTION_RA
			if "s" in prefix
			else RateCompensation.REFRACTION_BOTH
		)
	elif "t" in prefix:
		compensation = (
			RateCompensation.ONTRACK_RA
			if "s" in prefix
			else RateCompensation.ONTRACK_BOTH
		)
	else:
		compensation = RateCompensation.NONE

	return Status(
		# Both of these are reported as negatives by the firmware: 'n' means NOT
		# tracking and 'N' means NO goto in progress.
		tracking="n" not in prefix,
		slewing="N" not in prefix,
		park=park,
		at_home="H" in prefix,
		waiting_at_home="w" in prefix,
		pause_at_home="u" in prefix,
		guiding="G" in prefix,
		pps_synced="S" in prefix,
		pec_recorded="R" in prefix,
		sync_to_encoders_only="e" in prefix,
		sound_enabled="z" in prefix,
		auto_meridian_flip="a" in prefix,
		rate_compensation=compensation,
		mount_type=mount_type,
		pier_side=pier_side,
		pulse_guide_rate_index=pulse_guide_rate_index,
		guide_rate_index=guide_rate_index,
		general_error=general_error,
		raw=text,
	)


GENERAL_ERROR_TEXT = {
	0: "no error",
	1: "motor or driver fault",
	2: "both axes should not be in standby",
	3: "unspecified error",
	4: "altitude below the minimum limit",
	5: "altitude above the maximum limit",
	6: "mount is in standby",
	7: "mount is parked",
	8: "goto in progress",
	9: "outside the mount's limits",
	10: "hardware fault",
}
"""Decodes the trailing digit of ``:GU#``."""


# --------------------------------------------------------------------------------------
# Coordinate frame (:GXEE#)


class CoordinateMode(enum.IntEnum):
	"""What frame OnStep's RA/Dec are expressed in (Command.ino ~931)."""
	OBSERVED_PLACE = 0
	TOPOCENTRIC = 1
	ASTROMETRIC_J2000 = 2


# --------------------------------------------------------------------------------------
# The command set this driver uses, as constants and small builders so the reply shape
# travels with the command. Each shape was confirmed against its handler in Command.ino.

# -- identity and capability, read once at connect
GET_PRODUCT_NAME = Cmd(":GVP#", ReplyShape.TERMINATED)
GET_FIRMWARE_NUMBER = Cmd(":GVN#", ReplyShape.TERMINATED)
GET_FIRMWARE_DATE = Cmd(":GVD#", ReplyShape.TERMINATED)
GET_FIRMWARE_TIME = Cmd(":GVT#", ReplyShape.TERMINATED)
GET_FASTEST_BAUD = Cmd(":GB#", ReplyShape.TERMINATED)

GET_COORDINATE_MODE = Cmd(":GXEE#", ReplyShape.OPTIONAL)
GET_MERIDIAN_LIMIT_EAST = Cmd(":GXE9#", ReplyShape.OPTIONAL)
GET_MERIDIAN_LIMIT_WEST = Cmd(":GXEA#", ReplyShape.OPTIONAL)
GET_AXIS1_STEPS_PER_DEG = Cmd(":GXE4#", ReplyShape.OPTIONAL)
GET_AXIS2_STEPS_PER_DEG = Cmd(":GXE5#", ReplyShape.OPTIONAL)
GET_DEC_LIMIT_MIN = Cmd(":GXEC#", ReplyShape.OPTIONAL)
GET_DEC_LIMIT_MAX = Cmd(":GXED#", ReplyShape.OPTIONAL)
GET_MAX_RATE = Cmd(":GXE1#", ReplyShape.OPTIONAL)

# -- polled state
GET_STATUS = Cmd(":GU#", ReplyShape.TERMINATED)
GET_RA = Cmd(":GRa#", ReplyShape.TERMINATED)
GET_DEC = Cmd(":GDe#", ReplyShape.TERMINATED)
GET_ALTITUDE = Cmd(":GA#", ReplyShape.TERMINATED)
GET_AZIMUTH = Cmd(":GZ#", ReplyShape.TERMINATED)
GET_SIDEREAL_TIME = Cmd(":GS#", ReplyShape.TERMINATED)
GET_TRACKING_RATE = Cmd(":GT#", ReplyShape.TERMINATED)
GET_LATITUDE = Cmd(":Gt#", ReplyShape.TERMINATED)
GET_LONGITUDE = Cmd(":Gg#", ReplyShape.TERMINATED)
GET_UTC_OFFSET = Cmd(":GG#", ReplyShape.TERMINATED)
GET_LOCAL_TIME = Cmd(":GL#", ReplyShape.TERMINATED)
GET_LOCAL_DATE = Cmd(":GC#", ReplyShape.TERMINATED)
GET_PIER_SIDE = Cmd(":Gm#", ReplyShape.TERMINATED)
GET_LAST_ERROR = Cmd(":GE#", ReplyShape.TERMINATED)
GET_TARGET_RA = Cmd(":Gra#", ReplyShape.TERMINATED)
GET_TARGET_DEC = Cmd(":Gde#", ReplyShape.TERMINATED)
GET_HORIZON_LIMIT = Cmd(":Gh#", ReplyShape.TERMINATED)
GET_OVERHEAD_LIMIT = Cmd(":Go#", ReplyShape.TERMINATED)

# -- tracking
TRACKING_ON = Cmd(":Te#", ReplyShape.BOOLEAN)
TRACKING_OFF = Cmd(":Td#", ReplyShape.BOOLEAN)
"""``:Te#``/``:Td#`` leave ``booleanReply`` set, so unlike the other ``:T*`` commands they
answer 0/1 -- and answer 0 when slewing, homing or parked (Command.ino ~1945)."""

TRACK_RATE_SIDEREAL = Cmd(":TQ#", ReplyShape.NONE)
TRACK_RATE_SOLAR = Cmd(":TS#", ReplyShape.NONE)
TRACK_RATE_LUNAR = Cmd(":TL#", ReplyShape.NONE)
TRACK_RATE_KING = Cmd(":TK#", ReplyShape.NONE)
TRACK_DUAL_AXIS = Cmd(":T2#", ReplyShape.NONE)
TRACK_SINGLE_AXIS = Cmd(":T1#", ReplyShape.NONE)


# -- slewing
def set_target_ra(hours: float, high_precision: bool = True) -> Cmd:
	return Cmd(f":Sr{format_hms(hours, high_precision)}#", ReplyShape.BOOLEAN)


def set_target_dec(degrees: float, high_precision: bool = True) -> Cmd:
	return Cmd(f":Sd{format_dms(degrees, high_precision)}#", ReplyShape.BOOLEAN)


def set_target_altitude(degrees: float, high_precision: bool = True) -> Cmd:
	return Cmd(f":Sa{format_dms(degrees, high_precision)}#", ReplyShape.BOOLEAN)


def set_target_azimuth(degrees: float) -> Cmd:
	"""``:Sz[DDD*MM]#`` -- unsigned, three-digit degrees, whole arc-minutes."""
	return Cmd(f":Sz{format_dm(degrees % 360.0, width=3)[1:]}#", ReplyShape.BOOLEAN)


GOTO_TARGET = Cmd(":MS#", ReplyShape.DIGIT, timeout=SLEW_START_TIMEOUT)
GOTO_TARGET_ALTAZ = Cmd(":MA#", ReplyShape.DIGIT, timeout=SLEW_START_TIMEOUT)

SYNC_TO_TARGET = Cmd(":CM#", ReplyShape.TERMINATED)
"""``:CS#`` returns nothing at all, so a refused sync is indistinguishable from a good one.
``:CM#`` does the same work and answers ``N/A#`` or ``En#`` (Command.ino ~380)."""

ABORT_SLEW = Cmd(":Q#", ReplyShape.NONE)
HALT_EAST = Cmd(":Qe#", ReplyShape.NONE)
HALT_WEST = Cmd(":Qw#", ReplyShape.NONE)
HALT_NORTH = Cmd(":Qn#", ReplyShape.NONE)
HALT_SOUTH = Cmd(":Qs#", ReplyShape.NONE)

MOVE_EAST = Cmd(":Me#", ReplyShape.NONE)
MOVE_WEST = Cmd(":Mw#", ReplyShape.NONE)
MOVE_NORTH = Cmd(":Mn#", ReplyShape.NONE)
MOVE_SOUTH = Cmd(":Ms#", ReplyShape.NONE)

SLEW_RATE_MULTIPLIERS = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0)
"""Nominal sidereal multiples for ``:R0#``..``:R9#``. OnStep derives the real ceiling from
its own ``MaxRate``, so ``AxisRates`` clamps these against ``:GXE1#``."""


def set_slew_rate(index: int) -> Cmd:
	"""``:Rn#`` where n is 0 (slowest) to 9 (fastest)."""
	if not 0 <= index <= 9:
		raise ValueError(f"slew rate index out of range: {index}")
	return Cmd(f":R{index}#", ReplyShape.NONE)


# -- park / home
PARK = Cmd(":hP#", ReplyShape.BOOLEAN)
UNPARK = Cmd(":hR#", ReplyShape.BOOLEAN)
SET_PARK = Cmd(":hQ#", ReplyShape.BOOLEAN)
GOTO_HOME = Cmd(":hC#", ReplyShape.NONE)
RESET_AT_HOME = Cmd(":hF#", ReplyShape.NONE)

# -- guiding
MAX_PULSE_GUIDE_MS = 16399
"""Command.ino ~1225 rejects anything above this outright."""

_GUIDE_DIRECTION_CHARS = {0: "n", 1: "s", 2: "e", 3: "w"}
"""ASCOM GuideDirections: North=0, South=1, East=2, West=3."""


def pulse_guide(direction: int, duration_ms: int) -> Cmd:
	"""``:MGd[n]#`` -- the uppercase form, which answers 0/1."""
	try:
		char = _GUIDE_DIRECTION_CHARS[direction]
	except KeyError:
		raise ValueError(f"not an ASCOM guide direction: {direction}") from None
	if not 0 <= duration_ms <= MAX_PULSE_GUIDE_MS:
		raise ValueError(f"pulse duration {duration_ms} ms outside 0..{MAX_PULSE_GUIDE_MS}")
	return Cmd(f":MG{char}{duration_ms:04d}#", ReplyShape.BOOLEAN)


GUIDE_RATES_ARCSEC_PER_SEC = (3.75, 7.5, 15.0, 30.0, 60.0, 120.0, 300.0, 720.0)
"""``Globals.h:262``: ``guideRates[10]`` in arc-seconds per second, indices 0-7."""

SIDEREAL_ARCSEC_PER_SEC = 15.041067
"""One sidereal rate. The firmware's table uses a round 15.0 for 1x; this is the real figure,
used when converting to the deg/sec ASCOM asks for."""

GUIDE_RATE_1X_INDEX = 2
"""``Globals.h:252``. ``setGuideRate`` only updates the *pulse*-guide rate for indices at or
below this, so these are the only rates PHD2 can be given."""


def guide_rate_index_to_sidereal(index: int) -> float | None:
	"""Index from ``:GU#`` -> multiple of the sidereal rate, or ``None`` if the index is one
	of the two the firmware derives from ``MaxRate``."""
	if 0 <= index < len(GUIDE_RATES_ARCSEC_PER_SEC):
		return GUIDE_RATES_ARCSEC_PER_SEC[index] / 15.0
	return None


GET_PULSE_GUIDE_RATE = Cmd(":GX90#", ReplyShape.OPTIONAL)
"""Pulse-guide rate as a multiple of sidereal, straight from the mount."""

GET_MAX_RATE_CURRENT = Cmd(":GX92#", ReplyShape.OPTIONAL)
"""Current ``MaxRate`` in microseconds per step -- the fastest the axes can be driven."""

GET_PIER_SIDE_EXTENDED = Cmd(":GX94#", ReplyShape.OPTIONAL)
"""Pier side, with ``" N"`` appended when ``meridianFlip == MeridianFlipNever``."""

GET_AUTO_MERIDIAN_FLIP = Cmd(":GX95#", ReplyShape.OPTIONAL)
GET_PREFERRED_PIER_SIDE = Cmd(":GX96#", ReplyShape.OPTIONAL)  # "E", "W" or "B"
GET_SLEW_SPEED = Cmd(":GX97#", ReplyShape.OPTIONAL)
"""Goto slew speed in degrees per second, which is the natural ceiling for ``AxisRates``."""


def move_axis1_at_rate(degrees_per_second: float) -> Cmd:
	"""``:RA[n.n]#`` -- start axis 1 moving at an arbitrary rate, in degrees/second."""
	return Cmd(f":RA{degrees_per_second:.6f}#", ReplyShape.NONE)


def move_axis2_at_rate(degrees_per_second: float) -> Cmd:
	"""``:RE[n.n]#`` -- axis 2's counterpart to :func:`move_axis1_at_rate`. Stop with
	``:Qn#``/``:Qs#``."""
	return Cmd(f":RE{degrees_per_second:.6f}#", ReplyShape.NONE)


# -- site
def set_latitude(degrees: float) -> Cmd:
	return Cmd(f":St{format_dm(degrees)}#", ReplyShape.BOOLEAN)


def set_longitude(degrees: float) -> Cmd:
	"""ASCOM longitude is east-positive; OnStep's is west-positive (":Gg# Get Current Site
	Longitude, east is negative")."""
	return Cmd(f":Sg{format_dm(-degrees, width=3)}#", ReplyShape.BOOLEAN)


UTC_OFFSET_MINUTES_ALLOWED = (0, 30, 45)
"""The only sub-hour parts ``:SG#`` accepts (Command.ino ~1600). Every real time zone is a
whole hour or one of these."""


def set_time_zone(zone_hours: float) -> Cmd:
	"""``:SG[sHH]#`` / ``:SG[sHH:MM]#``, from a conventional zone offset."""
	hours = -zone_hours
	sign = "-" if hours < 0 else "+"
	total_minutes = round(abs(hours) * 60)
	whole, minutes = divmod(total_minutes, 60)
	if minutes not in UTC_OFFSET_MINUTES_ALLOWED:
		minutes = min((*UTC_OFFSET_MINUTES_ALLOWED, 60), key=lambda m: (abs(m - minutes), m))
		if minutes == 60:
			whole, minutes = whole + 1, 0
	if minutes == 0:
		return Cmd(f":SG{sign}{whole:02d}#", ReplyShape.BOOLEAN)
	return Cmd(f":SG{sign}{whole:02d}:{minutes:02d}#", ReplyShape.BOOLEAN)


def set_local_time(hours: int, minutes: int, seconds: int) -> Cmd:
	return Cmd(f":SL{hours:02d}:{minutes:02d}:{seconds:02d}#", ReplyShape.BOOLEAN)


def set_local_date(month: int, day: int, year: int) -> Cmd:
	"""``:SC[MM/DD/YY]#`` -- two-digit year."""
	return Cmd(f":SC{month:02d}/{day:02d}/{year % 100:02d}#", ReplyShape.BOOLEAN)
