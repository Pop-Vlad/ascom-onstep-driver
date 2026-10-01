"""A fake OnStep controller, faithful at the byte level.
"""

from __future__ import annotations

import datetime as dt
import math
import socket
import threading
import time

from . import protocol
from .transport import STANDARD_COMMAND_PORT, ONESHOT_CLIENT_BUDGET_S, Transport

SIDEREAL_HOURS_PER_SECOND = 1.0027379 / 3600.0
"""Sidereal hours elapsed per wall-clock second."""

DEFAULT_SLEW_DEG_PER_S = 3.0


def _fmt_hms_firmware(hours: float) -> str:
	"""``HH:MM:SS.ss`` -- what ``:GRa#`` emits (PM_HIGHEST, Astro.ino:doubleToHms)."""
	total = round(hours * 3600 * 100) % (24 * 3600 * 100)
	h, rem = divmod(total, 3600 * 100)
	m, cs = divmod(rem, 60 * 100)
	return f"{h:02d}:{m:02d}:{cs / 100:05.2f}"


def _fmt_hms_plain(hours: float) -> str:
	"""``HH:MM:SS`` -- what ``:GS#`` emits."""
	total = round(hours * 3600) % (24 * 3600)
	h, rem = divmod(total, 3600)
	m, s = divmod(rem, 60)
	return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_dms_firmware(degrees: float) -> str:
	"""``sDD*MM'SS.s`` -- what ``:GDe#`` emits: apostrophe separator, one decimal."""
	sign = "-" if degrees < 0 else "+"
	total = round(abs(degrees) * 3600 * 10)
	d, rem = divmod(total, 3600 * 10)
	m, ds = divmod(rem, 60 * 10)
	return f"{sign}{d:02d}*{m:02d}'{ds / 10:04.1f}"


def _fmt_time_zone(hours: float) -> str:
	"""``sHH`` with an optional ``:30``/``:45`` -- exactly ``Astro.ino:timeZoneToHM``."""
	text = f"{int(hours):+03d}"
	fraction = abs(hours - int(hours))
	if abs(fraction - 0.5) < 1e-8:
		text += ":30"
	elif abs(fraction - 0.75) < 1e-8:
		text += ":45"
	return text


def _fmt_dm(degrees: float, width: int = 2) -> str:
	"""``sDD*MM`` -- low precision, what ``:GA#``/``:Gt#`` emit by default."""
	sign = "-" if degrees < 0 else "+"
	total = round(abs(degrees) * 60)
	d, m = divmod(total, 60)
	return f"{sign}{d:0{width}d}*{m:02d}"


class OnStepSimulator:
	"""OnStep's command handler and mount state, without the motors."""

	def __init__(self, mount_type: str = "E", latitude: float = 45.0, longitude: float = 25.0,
	             slew_rate_deg_s: float = DEFAULT_SLEW_DEG_PER_S, coordinate_mode: int = 1,
	             firmware_version: str = "3.16q", product_name: str = "On-Step",
	             park_position: tuple[float, float] | None = (0.0, 60.0), utc: "dt.datetime | None" = None):
		self.mount_type = mount_type
		self.latitude = latitude
		self.longitude = longitude  # east-positive, as ASCOM means it
		self.slew_rate_deg_s = slew_rate_deg_s
		self.coordinate_mode = coordinate_mode
		self.firmware_version = firmware_version
		self.product_name = product_name

		self.ra_hours = 12.0
		self.dec_degrees = 45.0
		self.target_ra_hours = 12.0
		self.target_dec_degrees = 45.0
		self.target_altitude = 45.0
		self.target_azimuth = 180.0
		#: Set to True by a build configured never to flip, so :GX94# gains its " N".
		self.never_flips = False

		self.tracking = False
		self.slewing = False
		self.park_state = protocol.ParkState.NOT_PARKED
		self.at_home = True
		# Only a GEM has a pier side at all; fork and alt/az builds report 'o' (none),
		# and the driver keys CanSetPierSide / meridian-flip behaviour off exactly this.
		self.pier_side = (protocol.PierSide.EAST if mount_type == "E" else protocol.PierSide.NONE)
		# A real OnStep persists its park position in EEPROM. Starting with None made
		# CanPark true while every Park failed, which no configured mount ever is.
		self.park_position: tuple[float, float] | None = park_position

		self.slew_rate_index = 5
		self.guide_rate_index = 2
		self.pulse_guide_rate_index = 2
		self.general_error = 0
		self.last_command_error = protocol.CommandError.NONE
		self._command_error = protocol.CommandError.NONE

		self.time_zone = 0.0
		#: A real clock, so :GL#/:GC#, UTCDate and the computed sidereal time mean something.
		#: Defaults to now: ConformU checks sidereal time, which a frozen clock would fail.
		self.utc = utc or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
		self.sound_enabled = False
		self.auto_meridian_flip = False
		self.dual_axis = True
		self.rate_compensation = protocol.RateCompensation.NONE

		self._guide_axis1_until = 0.0
		self._guide_axis2_until = 0.0
		self._moving: set[str] = set()
		self._homing = False
		self._last_tick = time.monotonic()

		#: Every command received, for tests that assert on traffic volume -- the whole
		#: point of the caching layer is that this list stays short.
		self.log: list[str] = []

	# -- time and motion ---------------------------------------------------------

	def _tick(self) -> None:
		now = time.monotonic()
		elapsed = now - self._last_tick
		self._last_tick = now
		if elapsed <= 0:
			return

		self.utc += dt.timedelta(seconds=elapsed)

		if self.slewing:
			self._advance_slew(elapsed)
		elif self._moving:
			self._advance_manual_move(elapsed)
		elif not self.tracking and self.park_state is not protocol.ParkState.PARKED:
			# A stationary mount that is not tracking holds hour angle, so its RA climbs with
			# the sky -- which makes "tracking holds position" testable rather than assumed.
			self.ra_hours = (self.ra_hours + elapsed * SIDEREAL_HOURS_PER_SECOND) % 24.0

	def _advance_slew(self, elapsed: float) -> None:
		step = self.slew_rate_deg_s * elapsed
		dec_error = self.target_dec_degrees - self.dec_degrees
		ra_error_deg = _wrap180((self.target_ra_hours - self.ra_hours) * 15.0)

		if abs(dec_error) <= step:
			self.dec_degrees = self.target_dec_degrees
		else:
			self.dec_degrees += step * (1 if dec_error > 0 else -1)

		if abs(ra_error_deg) <= step:
			self.ra_hours = self.target_ra_hours
		else:
			self.ra_hours = (self.ra_hours + (step if ra_error_deg > 0 else -step) / 15.0) % 24.0

		if self.ra_hours == self.target_ra_hours and (self.dec_degrees == self.target_dec_degrees):
			self.slewing = False
			self.at_home = self._homing
			self._homing = False
			if self.park_state is protocol.ParkState.PARKING:
				self.park_state = protocol.ParkState.PARKED
				self.tracking = False
			else:
				self.tracking = True
			self._update_pier_side()

	def _advance_manual_move(self, elapsed: float) -> None:
		step = self.slew_rate_deg_s * elapsed
		# East increases RA: axis 1 is HOUR ANGLE, 'e' is a negative rate (Guide.ino:135), and
		# HA = LST - RA. Easy to invert; ASCOM is explicit that guideEast means +RA.
		if "e" in self._moving:
			self.ra_hours = (self.ra_hours + step / 15.0) % 24.0
		if "w" in self._moving:
			self.ra_hours = (self.ra_hours - step / 15.0) % 24.0
		if "n" in self._moving:
			self.dec_degrees = min(90.0, self.dec_degrees + step)
		if "s" in self._moving:
			self.dec_degrees = max(-90.0, self.dec_degrees - step)

	def _update_pier_side(self) -> None:
		if self.mount_type != "E":
			self.pier_side = protocol.PierSide.NONE
			return
		hour_angle = _wrap12(self.lst_hours - self.ra_hours)
		self.pier_side = (protocol.PierSide.WEST if hour_angle < 0 else protocol.PierSide.EAST)

	@property
	def altitude(self) -> float:
		ha = math.radians(_wrap12(self.lst_hours - self.ra_hours) * 15.0)
		dec = math.radians(self.dec_degrees)
		lat = math.radians(self.latitude)
		return math.degrees(
			math.asin(
				max(
					-1.0,
					min(
						1.0,
						math.sin(dec) * math.sin(lat) + math.cos(dec) * math.cos(lat) * math.cos(ha),
					),
				)
			)
		)

	@property
	def azimuth(self) -> float:
		ha = math.radians(_wrap12(self.lst_hours - self.ra_hours) * 15.0)
		dec = math.radians(self.dec_degrees)
		lat = math.radians(self.latitude)
		y = -math.cos(dec) * math.sin(ha)
		x = math.sin(dec) * math.cos(lat) - math.cos(dec) * math.sin(lat) * math.cos(ha)
		return math.degrees(math.atan2(y, x)) % 360.0

	@property
	def lst_hours(self) -> float:
		"""Local apparent sidereal time, derived from the clock and the longitude."""
		days = (self.utc - dt.datetime(2000, 1, 1, 12)).total_seconds() / 86400.0
		gmst = 18.697374558 + 24.06570982441908 * days
		return (gmst + self.longitude / 15.0) % 24.0

	@property
	def local_time(self) -> dt.datetime:
		"""The mount's clock in local time, which is what :GL#/:GC# report."""
		return self.utc + dt.timedelta(hours=self.time_zone)

	@property
	def pier_side_code(self) -> str:
		"""What ``:GX94#`` answers: the pier-side ordinal, plus ``" N"`` for
		``MeridianFlipNever`` -- how a client learns the mount does not flip."""
		ordinal = {
			protocol.PierSide.NONE: 0,
			protocol.PierSide.EAST: 1,
			protocol.PierSide.WEST: 2,
		}[self.pier_side]
		return f"{ordinal} N" if self.never_flips else str(ordinal)

	def _start_goto_altaz(self) -> bytes:
		"""``:MA#`` -- goto the alt/az target, converting it to the equatorial one."""
		alt = math.radians(self.target_altitude)
		az = math.radians(self.target_azimuth)
		lat = math.radians(self.latitude)
		dec = math.asin(
			max(-1.0, min(1.0, math.sin(alt) * math.sin(lat) + math.cos(alt) * math.cos(lat) * math.cos(az))))
		y = -math.cos(alt) * math.sin(az)
		x = math.sin(alt) * math.cos(lat) - math.cos(alt) * math.sin(lat) * math.cos(az)
		hour_angle = math.degrees(math.atan2(y, x)) / 15.0
		self.target_dec_degrees = math.degrees(dec)
		self.target_ra_hours = (self.lst_hours - hour_angle) % 24.0
		return self._start_goto()

	@property
	def guiding(self) -> bool:
		now = time.monotonic()
		return now < self._guide_axis1_until or now < self._guide_axis2_until

	# -- status ------------------------------------------------------------------

	def status_string(self) -> str:
		"""Build ``:GU#`` in the firmware's own field order (Command.ino:650)."""
		out = []
		if not self.tracking:
			out.append("n")
		if not self.slewing:
			out.append("N")
		out.append(self.park_state.value)
		if self.at_home:
			out.append("H")
		if self.guiding:
			out.append("G")
		if self.mount_type != "A":
			compensation = self.rate_compensation
			if compensation is protocol.RateCompensation.REFRACTION_RA:
				out.extend(("r", "s"))
			elif compensation is protocol.RateCompensation.REFRACTION_BOTH:
				out.append("r")
			elif compensation is protocol.RateCompensation.ONTRACK_RA:
				out.extend(("t", "s"))
			elif compensation is protocol.RateCompensation.ONTRACK_BOTH:
				out.append("t")
		if self.sound_enabled:
			out.append("z")
		if self.mount_type == "E" and self.auto_meridian_flip:
			out.append("a")
		if self.mount_type != "A":
			out.append("/")  # PEC ignored
		out.append(self.mount_type)
		out.append(self.pier_side.value)
		out.append(str(self.pulse_guide_rate_index))
		out.append(str(self.guide_rate_index))
		out.append(str(self.general_error))
		return "".join(out)

	# -- dispatch ----------------------------------------------------------------

	def handle(self, command: str) -> bytes:
		"""Process one ``:XX...#`` command and return the firmware's exact reply bytes."""
		self.log.append(command)
		self._tick()

		if not command.startswith(":") or not command.endswith("#"):
			return b""
		body = command[1:-1]
		if not body:
			return b""

		# Per-channel `lastError`, written at the end of each command except when the handler
		# set CE_NULL -- which :GE# does for itself, so asking does not destroy the answer.
		self._command_error = protocol.CommandError.NONE
		reply = self._dispatch(body)
		if body != "GE":
			self.last_command_error = self._command_error
		return reply

	def _dispatch(self, body: str) -> bytes:
		head, rest = body[0], body[1:]

		if head == "G":
			return self._get(rest)
		if head == "S":
			return self._set(rest)
		if head == "M":
			return self._move(rest)
		if head == "Q":
			return self._halt(rest)
		if head == "T":
			return self._track(rest)
		if head == "R":
			return self._rate(rest)
		if head == "h":
			return self._home_park(rest)
		if head == "C":
			return self._sync(rest)
		if head == "U":
			return b""  # precision toggle; this simulator always reports high
		return self._fail(protocol.CommandError.CMD_UNKNOWN)

	# -- helpers -----------------------------------------------------------------

	@staticmethod
	def _term(payload: str) -> bytes:
		return payload.encode("ascii") + b"#"

	@staticmethod
	def _ok() -> bytes:
		return b"1"

	def _fail(self, error: protocol.CommandError) -> bytes:
		self._command_error = error
		return b"0"

	# -- :G* getters -------------------------------------------------------------

	def _get(self, rest: str) -> bytes:
		if rest == "VP":
			return self._term(self.product_name)
		if rest == "VN":
			return self._term(self.firmware_version)
		if rest == "VD":
			return self._term("Jan 01 2024")
		if rest == "VT":
			return self._term("00:00:00")
		if rest == "B":
			return self._term("9")
		if rest == "U":
			return self._term(self.status_string())
		if rest == "Ra":
			return self._term(_fmt_hms_firmware(self.ra_hours))
		if rest == "De":
			return self._term(_fmt_dms_firmware(self.dec_degrees))
		if rest == "ra":
			return self._term(_fmt_hms_firmware(self.target_ra_hours))
		if rest == "de":
			return self._term(_fmt_dms_firmware(self.target_dec_degrees))
		if rest == "A":
			return self._term(_fmt_dm(self.altitude))  # low precision, as stock
		if rest == "Z":
			return self._term(_fmt_dm(self.azimuth, width=3)[1:])
		if rest == "S":
			return self._term(_fmt_hms_plain(self.lst_hours))
		if rest == "T":
			return self._term("60.00000" if self.tracking else "0.00000")
		if rest == "t":
			return self._term(_fmt_dm(self.latitude))
		if rest == "g":
			# OnStep's longitude is west-positive, the opposite of ASCOM's.
			return self._term(_fmt_dm(-self.longitude, width=3))
		if rest == "G":
			return self._term(_fmt_time_zone(-self.time_zone))
		if rest == "L":
			return self._term(self.local_time.strftime("%H:%M:%S"))
		if rest == "C":
			# MM/DD/YY, the only date form OnStep speaks.
			return self._term(self.local_time.strftime("%m/%d/%y"))
		if rest == "m":
			return self._term(
				{
					protocol.PierSide.EAST: "E",
					protocol.PierSide.WEST: "W",
					protocol.PierSide.NONE: "N",
				}[self.pier_side]
			)
		if rest == "E":
			return self._term(f"{int(self.last_command_error):02d}")
		if rest == "h":
			return self._term("+00*")
		if rest == "o":
			return self._term("+80*")
		if rest.startswith("X"):
			return self._get_extended(rest[1:])
		return self._fail(protocol.CommandError.CMD_UNKNOWN)

	def _get_extended(self, index: str) -> bytes:
		values = {
			# :GX90# -- pulse-guide rate as a multiple of sidereal.
			"90": f"{(protocol.guide_rate_index_to_sidereal(self.pulse_guide_rate_index) or 1.0):.2f}",
			"92": "3.000",  # MaxRate (current), microseconds per step
			"94": str(self.pier_side_code),
			"95": "0",
			"96": "B",
			"97": f"{self.slew_rate_deg_s:.1f}",
			"EE": str(self.coordinate_mode),
			"E1": "3.000",
			"E4": "12800",
			"E5": "12800",
			"E9": "60",  # minutes past meridian east
			"EA": "60",  # minutes past meridian west
			"EC": "-91",
			"ED": "91",
		}
		if index in values:
			return self._term(values[index])
		return self._fail(protocol.CommandError.CMD_UNKNOWN)

	# -- :S* setters -------------------------------------------------------------

	def _set(self, rest: str) -> bytes:
		key, value = rest[0], rest[1:]
		try:
			if key == "r":
				hours = protocol.parse_hms(value)
				if not self._accepts_hms_length(value):
					return self._fail(protocol.CommandError.PARAM_RANGE)
				self.target_ra_hours = hours
				return self._ok()
			if key == "d":
				if not self._accepts_dms_length(value):
					return self._fail(protocol.CommandError.PARAM_FORM)
				self.target_dec_degrees = protocol.parse_dms(value)
				return self._ok()
			if key == "t":
				self.latitude = protocol.parse_dms(value)
				return self._ok()
			if key == "g":
				self.longitude = -protocol.parse_dms(value)
				return self._ok()
			if key == "G":
				return self._set_time_zone(value)
			if key == "a":
				if not self._accepts_dms_length(value):
					return self._fail(protocol.CommandError.PARAM_FORM)
				self.target_altitude = protocol.parse_dms(value)
				return self._ok()
			if key == "z":
				# :Sz is unsigned DDD*MM, parsed with sign_present=false.
				if len(value) not in (6, 9) and len(value) < 11:
					return self._fail(protocol.CommandError.PARAM_FORM)
				self.target_azimuth = protocol.parse_dms(value) % 360.0
				return self._ok()
			if key == "L":
				hours = protocol.parse_hms(value)
				local = self.local_time
				midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
				self.utc = (midnight + dt.timedelta(hours=hours)) - dt.timedelta(hours=self.time_zone)
				return self._ok()
			if key == "C":
				month, day, year = (int(part) for part in value.split("/"))
				local = self.local_time
				self.utc = local.replace(
					year=2000 + year if year < 70 else 1900 + year,
					month=month,
					day=day,
				) - dt.timedelta(hours=self.time_zone)
				return self._ok()
			if key == "S":
				return self._ok()
		except (protocol.ProtocolError, ValueError):
			return self._fail(protocol.CommandError.PARAM_FORM)
		return self._fail(protocol.CommandError.CMD_UNKNOWN)

	def _set_time_zone(self, value: str) -> bytes:
		"""``:SG#`` as the firmware parses it: ``sHH`` or ``sHH:MM`` with MM in {00, 30, 45},
		hours read by a strict integer parser, parameter under 7 chars."""
		if len(value) >= 7:
			return self._fail(protocol.CommandError.PARAM_FORM)
		fraction = 0.0
		hours_text = value
		if ":" in value:
			hours_text, _, minutes_text = value.partition(":")
			if minutes_text not in ("00", "30", "45"):
				return self._fail(protocol.CommandError.PARAM_FORM)
			fraction = {"00": 0.0, "30": 0.5, "45": 0.75}[minutes_text]
		try:
			whole = int(hours_text)  # atoi2: no decimal point, no trailing junk
		except ValueError:
			return self._fail(protocol.CommandError.PARAM_FORM)
		if not -24 <= whole <= 24:
			return self._fail(protocol.CommandError.PARAM_RANGE)
		zone = whole - fraction if whole < 0 else whole + fraction
		self.time_zone = -zone
		return self._ok()

	@staticmethod
	def _accepts_hms_length(value: str) -> bool:
		"""``hmsToDouble`` with PM_HIGH then PM_LOW: 8, >=10, or exactly 7."""
		length = len(value)
		return length == 8 or length >= 10 or length == 7

	@staticmethod
	def _accepts_dms_length(value: str) -> bool:
		"""``dmsToDouble`` with PM_HIGH then PM_LOW: 9, >=11, or exactly 6."""
		length = len(value)
		return length == 9 or length >= 11 or length == 6

	# -- :M* motion --------------------------------------------------------------

	def _move(self, rest: str) -> bytes:
		if rest == "S":
			return self._start_goto()
		if rest == "A":
			return self._start_goto_altaz()
		if rest and rest[0] in ("G", "g"):
			return self._pulse_guide(rest)
		if rest in ("e", "w", "n", "s"):
			if self.park_state is protocol.ParkState.PARKED:
				return b""
			self._moving.add(rest)
			return b""
		return self._fail(protocol.CommandError.CMD_UNKNOWN)

	def _start_goto(self) -> bytes:
		"""``:MS#``/``:MA#`` answer a bare digit with no terminator."""
		if self.park_state is protocol.ParkState.PARKED:
			self._command_error = protocol.CommandError.SLEW_ERR_IN_PARK
			return b"4"
		if self.slewing:
			self._command_error = protocol.CommandError.GOTO_ERR_GOTO
			return b"5"
		if self.target_dec_degrees > 90.0 or self.target_dec_degrees < -90.0:
			self._command_error = protocol.CommandError.SLEW_ERR_OUTSIDE_LIMITS
			return b"6"
		self.slewing = True
		self.at_home = False
		return b"0"

	def _pulse_guide(self, rest: str) -> bytes:
		uppercase = rest[0] == "G"
		direction = rest[1] if len(rest) > 1 else ""
		try:
			duration_ms = int(rest[2:])
		except ValueError:
			return self._fail(protocol.CommandError.PARAM_FORM) if uppercase else b""

		if direction not in ("n", "s", "e", "w"):
			return self._fail(protocol.CommandError.PARAM_FORM) if uppercase else b""
		if not 0 <= duration_ms <= protocol.MAX_PULSE_GUIDE_MS:
			return self._fail(protocol.CommandError.PARAM_RANGE) if uppercase else b""
		if self.park_state is protocol.ParkState.PARKED or self.slewing:
			return (
				self._fail(protocol.CommandError.MOUNT_IN_MOTION) if uppercase else b""
			)

		until = time.monotonic() + duration_ms / 1000.0
		rate = protocol.guide_rate_index_to_sidereal(self.pulse_guide_rate_index)
		if rate is None:
			rate = 1.0  # indices 8/9 are MaxRate-derived; irrelevant for a pulse
		shift_deg = rate * (15.0 / 3600.0) * (duration_ms / 1000.0)
		if direction in ("e", "w"):
			self._guide_axis1_until = until
			self.ra_hours = (
									self.ra_hours + (shift_deg if direction == "e" else -shift_deg) / 15.0
							) % 24.0
		else:
			self._guide_axis2_until = until
			self.dec_degrees = max(
				-90.0,
				min(
					90.0,
					self.dec_degrees + (shift_deg if direction == "n" else -shift_deg),
				),
			)
		return self._ok() if uppercase else b""

	def _halt(self, rest: str) -> bytes:
		if rest == "":
			self.slewing = False
			self._moving.clear()
			if self.park_state is protocol.ParkState.PARKING:
				self.park_state = protocol.ParkState.NOT_PARKED
			return b""
		if rest in ("e", "w", "n", "s"):
			self._moving.discard(rest)
			return b""
		return b""

	# -- :T* tracking ------------------------------------------------------------

	def _track(self, rest: str) -> bytes:
		if rest == "e":
			# The firmware refuses silently-ish: a 0 reply, no error state.
			if self.slewing or self.park_state is protocol.ParkState.PARKED:
				return b"0"
			self.tracking = True
			return b"1"
		if rest == "d":
			if self.slewing:
				return b"0"
			self.tracking = False
			return b"1"
		if rest in ("Q", "S", "L", "K", "R", "1", "2", "o", "r", "n", "+", "-"):
			if rest == "1":
				self.dual_axis = False
			elif rest == "2":
				self.dual_axis = True
			elif rest == "r":
				self.rate_compensation = (
					protocol.RateCompensation.REFRACTION_BOTH
					if self.dual_axis
					else protocol.RateCompensation.REFRACTION_RA
				)
			elif rest == "n":
				self.rate_compensation = protocol.RateCompensation.NONE
			elif rest == "o":
				self.rate_compensation = (
					protocol.RateCompensation.ONTRACK_BOTH
					if self.dual_axis
					else protocol.RateCompensation.ONTRACK_RA
				)
			elif rest in ("Q", "S", "L", "K"):
				# Selecting a fixed rate clears compensation, as setTrackingRate does.
				self.rate_compensation = protocol.RateCompensation.NONE
			return b""  # these answer with nothing at all
		return self._fail(protocol.CommandError.CMD_UNKNOWN)

	# -- :R* rates ---------------------------------------------------------------

	def _rate(self, rest: str) -> bytes:
		if rest.isdigit() and len(rest) == 1:
			self._set_guide_rate(int(rest))
			return b""
		if rest in ("G", "C", "M", "F", "S"):
			self._set_guide_rate({"G": 2, "C": 5, "M": 6, "F": 7, "S": 9}[rest])
			return b""
		if rest.startswith("A") or rest.startswith("E"):
			return b""  # guide rate set, no reply
		return self._fail(protocol.CommandError.CMD_UNKNOWN)

	# -- :h* home and park -------------------------------------------------------

	def _set_guide_rate(self, index: int) -> None:
		"""``setGuideRate`` (Guide.ino:226): sets the slew/guide rate and, only at or
		below ``GuideRate1x``, the pulse-guide rate too."""
		self.slew_rate_index = index
		if index <= protocol.GUIDE_RATE_1X_INDEX:
			self.pulse_guide_rate_index = index

	def _home_park(self, rest: str) -> bytes:
		if rest == "P":
			if self.park_state is protocol.ParkState.PARKED:
				return self._fail(protocol.CommandError.PARKED)
			if self.park_position is None:
				return self._fail(protocol.CommandError.NO_PARK_POSITION_SET)
			self.target_ra_hours, self.target_dec_degrees = self.park_position
			self.park_state = protocol.ParkState.PARKING
			self.slewing = True
			return self._ok()
		if rest == "R":
			if self.park_state is not protocol.ParkState.PARKED:
				return self._fail(protocol.CommandError.NOT_PARKED)
			self.park_state = protocol.ParkState.NOT_PARKED
			self.tracking = True
			return self._ok()
		if rest == "Q":
			self.park_position = (self.ra_hours, self.dec_degrees)
			return self._ok()
		if rest == "C":
			self.target_ra_hours = self.lst_hours % 24.0
			self.target_dec_degrees = 90.0 if self.latitude >= 0 else -90.0
			self.slewing = True
			self._homing = True
			return b""  # no reply
		if rest == "F":
			self.at_home = True
			self.slewing = False
			self.tracking = False
			self.ra_hours = self.lst_hours % 24.0
			self.dec_degrees = 90.0 if self.latitude >= 0 else -90.0
			return b""  # no reply
		return self._fail(protocol.CommandError.CMD_UNKNOWN)

	# -- :C* sync ----------------------------------------------------------------

	def _sync(self, rest: str) -> bytes:
		if rest not in ("S", "M"):
			return self._fail(protocol.CommandError.CMD_UNKNOWN)
		if self.park_state is protocol.ParkState.PARKED or self.slewing:
			# :CM# reports failure as "En#"; :CS# says nothing at all.
			return self._term("E4") if rest == "M" else b""
		self.ra_hours = self.target_ra_hours
		self.dec_degrees = self.target_dec_degrees
		self.at_home = False
		self._update_pier_side()
		return self._term("N/A") if rest == "M" else b""


def _wrap180(degrees: float) -> float:
	return (degrees + 180.0) % 360.0 - 180.0


def _wrap12(hours: float) -> float:
	return (hours + 12.0) % 24.0 - 12.0


# --------------------------------------------------------------------------------------
# Transports onto a simulator


class SimulatedTransport(Transport):
	"""Wires a :class:`Transport` straight onto an :class:`OnStepSimulator`."""

	def __init__(self, simulator: OnStepSimulator | None = None, byte_delay: float = 0.0):
		self.simulator = simulator or OnStepSimulator()
		self.byte_delay = byte_delay
		self.name = "simulated"
		self._open = False
		self._pending = bytearray()

	def open(self) -> None:
		self._open = True

	def close(self) -> None:
		self._open = False

	@property
	def is_open(self) -> bool:
		return self._open

	def _send(self, payload: bytes) -> None:
		if self.byte_delay:
			time.sleep(self.byte_delay * len(payload))
		self._pending.extend(self.simulator.handle(payload.decode("ascii")))

	def _recv(self, max_bytes: int, timeout: float) -> bytes:
		if not self._pending:
			return b""
		take = min(max_bytes, len(self._pending))
		if self.byte_delay:
			time.sleep(self.byte_delay * take)
		data = bytes(self._pending[:take])
		del self._pending[:take]
		return data

	def _drain(self) -> None:
		self._pending.clear()


class SimulatorServer:
	"""Serves an :class:`OnStepSimulator` over TCP, like the ESP WiFi addon."""

	def __init__(
			self,
			simulator: OnStepSimulator | None = None,
			port: int = 0,
			oneshot_drop: bool | None = None,
			client_budget_s: float = ONESHOT_CLIENT_BUDGET_S,
	):
		self.simulator = simulator or OnStepSimulator()
		self._requested_port = port
		self.oneshot_drop = (
			oneshot_drop if oneshot_drop is not None else port == STANDARD_COMMAND_PORT
		)
		self.client_budget_s = client_budget_s
		self._server: socket.socket | None = None
		self._thread: threading.Thread | None = None
		self._stop = threading.Event()
		self.port = port

	def start(self) -> "SimulatorServer":
		server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		server.bind(("127.0.0.1", self._requested_port))
		server.listen(1)
		server.settimeout(0.2)
		self.port = server.getsockname()[1]
		self._server = server
		self._thread = threading.Thread(target=self._serve, daemon=True)
		self._thread.start()
		return self

	def stop(self) -> None:
		self._stop.set()
		if self._thread is not None:
			self._thread.join(timeout=2.0)
		if self._server is not None:
			self._server.close()
			self._server = None

	def __enter__(self):
		return self.start()

	def __exit__(self, *exc_info):
		self.stop()
		return False

	def _serve(self) -> None:
		while not self._stop.is_set():
			try:
				client, _ = self._server.accept()
			except (TimeoutError, socket.timeout):
				continue
			except OSError:
				return
			with client:
				self._handle_client(client)

	def _handle_client(self, client: socket.socket) -> None:
		client.settimeout(0.1)
		deadline = time.monotonic() + self.client_budget_s
		buf = bytearray()
		while not self._stop.is_set():
			if self.oneshot_drop and time.monotonic() > deadline:
				return  # the firmware's 2 s guillotine
			try:
				chunk = client.recv(64)
			except (TimeoutError, socket.timeout):
				continue
			except OSError:
				return
			if not chunk:
				return
			buf.extend(chunk)
			while b"#" in buf:
				index = buf.index(b"#")
				command = bytes(buf[: index + 1]).decode("ascii", "replace")
				del buf[: index + 1]
				reply = self.simulator.handle(command)
				if reply:
					try:
						client.sendall(reply)
					except OSError:
						return
