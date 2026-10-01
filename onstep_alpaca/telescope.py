"""ASCOM Telescope semantics on top of :class:`~onstep_alpaca.link.MountLink`.

Three principles shape this layer:

  * **Capabilities are derived, not declared.** ``:GU#`` reports the mount type, so a
    fork mount does not advertise a pier side and an alt/az one does not advertise a
    meridian. The same binary adapts to any OnStep rig.
  * **Reads come from the cache, writes go through the queue**, so a client may poll as
    hard as it likes. The exceptions that cost a round trip are marked where they are.

Two conventions, each invisible until a real mount is pointed at the sky: ASCOM's
``pierEast`` means an hour angle at or past the meridian, which is what OnStep means too
(``Goto.ino`` picks west exactly when its axis-1 hour angle is negative), so they map
directly. And ``SiteLongitude`` is east-positive in ASCOM, west-positive in OnStep --
that inversion lives in ``protocol`` and the link's capability read, not here.
"""

from __future__ import annotations

import datetime as dt
import enum
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import errors, protocol
from .link import LinkError, Motion, MountLink
from .protocol import CommandError, CommandRejected, MountType, ParkState

log = logging.getLogger(__name__)

DRIVER_NAME = "OnStep Alpaca Telescope"
DRIVER_VERSION = "0.1.0"
INTERFACE_VERSION = 3
"""ITelescopeV3, which is what every current client expects."""

SLEW_POLL_INTERVAL = 0.2
DEFAULT_SLEW_TIMEOUT = 300.0
"""A synchronous slew must not return early, nor hang forever. A full sky traverse on slow
gearing is a few minutes; past this, saying so beats pretending it arrived."""

SIDEREAL_DEG_PER_SEC = protocol.SIDEREAL_ARCSEC_PER_SEC / 3600.0

FALLBACK_MAX_AXIS_RATE = 4.0
"""Degrees per second to advertise when the mount will not say what it can do."""


class AlignmentMode(enum.IntEnum):
	ALT_AZ = 0
	POLAR = 1
	GERMAN_POLAR = 2


class EquatorialSystem(enum.IntEnum):
	OTHER = 0
	TOPOCENTRIC = 1
	J2000 = 2
	J2050 = 3
	B1950 = 4


class DriveRate(enum.IntEnum):
	SIDEREAL = 0
	LUNAR = 1
	SOLAR = 2
	KING = 3


class GuideDirection(enum.IntEnum):
	NORTH = 0
	SOUTH = 1
	EAST = 2
	WEST = 3


class AscomPierSide(enum.IntEnum):
	UNKNOWN = -1
	EAST = 0
	WEST = 1


class Axis(enum.IntEnum):
	PRIMARY = 0
	SECONDARY = 1
	TERTIARY = 2


@dataclass(frozen=True, slots=True)
class AxisRate:
	"""One advertised rate band for ``MoveAxis``, in degrees per second."""

	minimum: float
	maximum: float


_TRACKING_RATE_COMMANDS = {
	DriveRate.SIDEREAL: protocol.TRACK_RATE_SIDEREAL,
	DriveRate.LUNAR: protocol.TRACK_RATE_LUNAR,
	DriveRate.SOLAR: protocol.TRACK_RATE_SOLAR,
	DriveRate.KING: protocol.TRACK_RATE_KING,
}

_TRACKING_RATE_HZ = {
	# getTrackingRate60Hz() values, from the comparisons in the :Gu# handler.
	DriveRate.SIDEREAL: 60.164,
	DriveRate.SOLAR: 60.000,
	DriveRate.LUNAR: 57.900,
	DriveRate.KING: 60.136,
}

_ALIGNMENT_MODES = {
	MountType.GEM: AlignmentMode.GERMAN_POLAR,
	MountType.FORK: AlignmentMode.POLAR,
	MountType.ALTAZ: AlignmentMode.ALT_AZ,
}

_PIER_SIDES = {
	protocol.PierSide.EAST: AscomPierSide.EAST,
	protocol.PierSide.WEST: AscomPierSide.WEST,
	protocol.PierSide.NONE: AscomPierSide.UNKNOWN,
}

_PARK_REJECTIONS = {
	CommandError.PARKED,
	CommandError.SLEW_ERR_IN_PARK,
}


def _rejection_to_ascom(exc: CommandRejected) -> errors.AscomError:
	"""Turn the mount's own refusal into the ASCOM exception a client expects."""
	if exc.error in _PARK_REJECTIONS:
		return errors.ParkedError(str(exc))
	if exc.error in (CommandError.PARAM_RANGE, CommandError.PARAM_FORM):
		return errors.InvalidValueError(str(exc))
	if exc.error is CommandError.CMD_UNKNOWN:
		# OnStep answers CE_CMD_UNKNOWN for a recognised command whose preconditions failed
		# (:Te# while homing), so this is a state problem far more often than a bad command.
		return errors.InvalidOperationError(str(exc))
	return errors.InvalidOperationError(str(exc))


class Telescope:
	"""One ASCOM Telescope device backed by one OnStep mount."""

	def __init__(self, link_factory: Callable[[], MountLink], unique_id: str = "onstep-alpaca-telescope-0",
	             name: str = "OnStep", site_elevation: float = 0.0, slew_timeout: float = DEFAULT_SLEW_TIMEOUT, ):
		self._link_factory = link_factory
		self._link: MountLink | None = None
		self._lock = threading.RLock()

		self.unique_id = unique_id
		self._name = name
		self._slew_timeout = slew_timeout

		# ASCOM state the mount does not store for us.
		self._site_elevation = site_elevation
		self._slew_settle_time = 0
		self._target_ra: float | None = None
		self._target_dec: float | None = None
		self._tracking_rate = DriveRate.SIDEREAL
		self._moving_axes: set[int] = set()

		# Cached capability answers, filled at connect.
		self._max_axis_rate = FALLBACK_MAX_AXIS_RATE
		self._never_flips = False

	# -- connection --------------------------------------------------------------

	@property
	def connected(self) -> bool:
		link = self._link
		return link is not None and link.connected

	@connected.setter
	def connected(self, value: bool) -> None:
		with self._lock:
			if value:
				self._connect()
			else:
				self._disconnect()

	def _connect(self) -> None:
		if self.connected:
			return
		link = self._link_factory()
		try:
			link.connect()
		except (LinkError, OSError) as exc:
			raise errors.NotConnectedError(f"could not connect: {exc}") from exc
		self._link = link
		self._target_ra = None
		self._target_dec = None
		self._moving_axes.clear()
		self._read_extended_capabilities()
		log.info("telescope connected: %s", link.info.description)

	def _disconnect(self) -> None:
		link, self._link = self._link, None
		if link is not None:
			try:
				link.disconnect()
			except Exception:  # pragma: no cover - disconnect must not raise
				log.debug("error during disconnect", exc_info=True)

	def _read_extended_capabilities(self) -> None:
		"""One-off reads that shape what this driver advertises."""
		link = self._require_link()

		# Does this mount flip at all? :GX94# appends " N" for MeridianFlipNever, the normal
		# setup for a harmonic drive, and decides whether a flip is worth planning for.
		self._never_flips = False
		if link.info.mount_type is not MountType.GEM:
			self._never_flips = True
		else:
			try:
				reply = link.execute(protocol.GET_PIER_SIDE_EXTENDED)
				if reply and reply.strip().upper().endswith("N"):
					self._never_flips = True
			except (LinkError, CommandRejected, protocol.ProtocolError) as exc:
				log.debug("could not read extended pier side: %s", exc)

		self._max_axis_rate = self._compute_max_axis_rate(link)

		try:
			reply = link.execute(protocol.GET_TRACKING_RATE)
			if reply:
				measured = float(reply)
				if measured > 0:
					self._tracking_rate = min(
						_TRACKING_RATE_HZ,
						key=lambda rate: abs(_TRACKING_RATE_HZ[rate] - measured),
					)
		except (LinkError, CommandRejected, ValueError, protocol.ProtocolError):
			pass  # keep the sidereal default

	def _compute_max_axis_rate(self, link: MountLink) -> float:
		"""Fastest ``MoveAxis`` rate, in degrees per second."""
		steps_per_degree = link.info.steps_per_degree_axis1
		try:
			reply = link.execute(protocol.GET_MAX_RATE_CURRENT)
			microseconds = float(reply) if reply else 0.0
		except (LinkError, CommandRejected, ValueError, protocol.ProtocolError):
			microseconds = 0.0

		if microseconds > 0 and steps_per_degree:
			steps_per_second = 1_000_000.0 / microseconds
			rate = steps_per_second / steps_per_degree
			if 0.01 < rate < 100.0:
				return rate

		# Fall back to the mount's reported goto speed, then to a safe constant.
		try:
			reply = link.execute(protocol.GET_SLEW_SPEED)
			speed = float(reply) if reply else 0.0
			if 0.01 < speed < 100.0:
				return speed
		except (LinkError, CommandRejected, ValueError, protocol.ProtocolError):
			pass
		return FALLBACK_MAX_AXIS_RATE

	def _require_link(self) -> MountLink:
		link = self._link
		if link is None or not link.connected:
			raise errors.NotConnectedError()
		return link

	def _state(self):
		try:
			return self._require_link().state
		except LinkError as exc:
			raise errors.NotConnectedError(str(exc)) from exc

	def _run(self, *cmds, motion: Motion | None = None, settle: bool = False):
		"""Send commands, translating the mount's refusals into ASCOM errors."""
		link = self._require_link()
		try:
			if len(cmds) == 1:
				result = link.execute(cmds[0], motion=motion)
			else:
				result = link.execute_all(*cmds, motion=motion)
			if settle:
				link.refresh()
			return result
		except CommandRejected as exc:
			raise _rejection_to_ascom(exc) from exc
		except LinkError as exc:
			raise errors.DriverError(str(exc)) from exc
		except protocol.ProtocolError as exc:
			raise errors.DriverError(f"unexpected reply from the mount: {exc}") from exc

	# -- identity ----------------------------------------------------------------

	@property
	def name(self) -> str:
		return self._name

	@property
	def description(self) -> str:
		if self.connected:
			info = self._require_link().info
			return f"{DRIVER_NAME} - {info.description} on {info.transport_name}"
		return DRIVER_NAME

	@property
	def driver_info(self) -> str:
		return (
			f"{DRIVER_NAME} {DRIVER_VERSION}. Speaks OnStep's LX200-derived command set over USB serial or the WiFi command channel.")

	@property
	def driver_version(self) -> str:
		return DRIVER_VERSION

	@property
	def interface_version(self) -> int:
		return INTERFACE_VERSION

	@property
	def supported_actions(self) -> list[str]:
		return ["onstep:command", "onstep:status"]

	def action(self, name: str, parameters: str) -> str:
		"""Escape hatch for OnStep-specific things ASCOM has no vocabulary for."""
		action = name.strip().lower()
		if action == "onstep:status":
			return self._state().status.raw
		if action == "onstep:command":
			text = parameters.strip()
			if not text.startswith(":") or not text.endswith("#"):
				raise errors.InvalidValueError("an OnStep command must look like ':GVP#'")
			# An arbitrary command's reply shape is unknowable, so this assumes a terminated
			# one; boolean and no-reply commands time out, which is the honest outcome.
			reply = self._run(protocol.Cmd(text, protocol.ReplyShape.TERMINATED))
			return reply or ""
		raise errors.ActionNotImplementedError(f"unknown action {name!r}")

	# -- capabilities ------------------------------------------------------------

	@property
	def alignment_mode(self) -> AlignmentMode:
		mount_type = self._require_link().info.mount_type
		return _ALIGNMENT_MODES.get(mount_type, AlignmentMode.GERMAN_POLAR)

	@property
	def equatorial_system(self) -> EquatorialSystem:
		"""Which frame the mount's RA/Dec are in, from ``:GXEE#``."""
		mode = self._require_link().info.coordinate_mode
		if mode is protocol.CoordinateMode.ASTROMETRIC_J2000:
			return EquatorialSystem.J2000
		# OBSERVED_PLACE and TOPOCENTRIC are both apparent-place frames; ASCOM has one
		# name for that.
		return EquatorialSystem.TOPOCENTRIC

	@property
	def is_gem(self) -> bool:
		return self._require_link().info.is_gem

	can_find_home = True
	can_park = True
	can_set_park = True
	can_unpark = True
	can_pulse_guide = True
	can_set_tracking = True
	can_slew = True
	can_slew_async = True
	can_slew_altaz = True
	can_slew_altaz_async = True
	can_sync = True
	can_set_guide_rates = True

	#: OnStep has no arbitrary dual-axis rate offset, so these stay off rather than
	#: being faked with a guide pulse train.
	can_set_declination_rate = False
	can_set_right_ascension_rate = False

	#: Commanding a flip is not the same as OnStep's automatic one, and there is no
	#: command that means "go to the other side of the pier now".
	can_set_pier_side = False

	#: OnStep has no alt/az sync: :CM# syncs equatorial coordinates only.
	can_sync_altaz = False

	@property
	def can_set_tracking_rates(self) -> bool:
		return True

	def can_move_axis(self, axis: int) -> bool:
		"""Both real axes can be driven continuously; there is no third."""
		return axis in (Axis.PRIMARY, Axis.SECONDARY)

	def axis_rates(self, axis: int) -> list[AxisRate]:
		"""Rates ``MoveAxis`` will accept, in degrees per second."""
		if not self.can_move_axis(axis):
			return []
		return [AxisRate(minimum=0.0, maximum=self._max_axis_rate)]

	@property
	def tracking_rates(self) -> list[DriveRate]:
		return [DriveRate.SIDEREAL, DriveRate.LUNAR, DriveRate.SOLAR, DriveRate.KING]

	# -- position ----------------------------------------------------------------

	@property
	def right_ascension(self) -> float:
		return self._state().ra_hours

	@property
	def declination(self) -> float:
		return self._state().dec_degrees

	@property
	def altitude(self) -> float:
		return self._state().altitude

	@property
	def azimuth(self) -> float:
		return self._state().azimuth

	@property
	def sidereal_time(self) -> float:
		return self._state().sidereal_time

	@property
	def at_home(self) -> bool:
		return self._state().status.at_home

	@property
	def at_park(self) -> bool:
		return self._state().status.parked

	@property
	def slewing(self) -> bool:
		"""True during a goto, park or home -- and never during a guide pulse."""
		try:
			link = self._require_link()
		except errors.NotConnectedError:
			raise
		if self._moving_axes:
			# ASCOM requires MoveAxis motion to show up as Slewing.
			return True
		return link.slewing

	@property
	def side_of_pier(self) -> AscomPierSide:
		link = self._require_link()
		if not link.info.is_gem:
			raise errors.NotImplementedError_("side of pier is only meaningful on a German equatorial mount")
		return _PIER_SIDES.get(link.state.status.pier_side, AscomPierSide.UNKNOWN)

	def destination_side_of_pier(self, ra_hours: float, dec_degrees: float) -> AscomPierSide:
		"""Which side the mount would end up on after slewing to a target."""
		link = self._require_link()
		if not link.info.is_gem:
			raise errors.NotImplementedError_("side of pier is only meaningful on a German equatorial mount")
		self._validate_ra(ra_hours)
		self._validate_dec(dec_degrees)
		if self._never_flips:
			return _PIER_SIDES.get(link.state.status.pier_side, AscomPierSide.UNKNOWN)
		hour_angle = _wrap_hours(link.state.sidereal_time - ra_hours)
		return AscomPierSide.WEST if hour_angle < 0 else AscomPierSide.EAST

	@property
	def never_flips(self) -> bool:
		"""True when the mount is set to track through the meridian rather than flip."""
		self._require_link()
		return self._never_flips

	# -- site --------------------------------------------------------------------

	@property
	def site_latitude(self) -> float:
		return self._require_link().info.site_latitude

	@site_latitude.setter
	def site_latitude(self, value: float) -> None:
		if not -90.0 <= value <= 90.0:
			raise errors.InvalidValueError(f"latitude {value} is outside -90..90")
		self._run(protocol.set_latitude(value))
		self._require_link().note_site(latitude=value)

	@property
	def site_longitude(self) -> float:
		"""East-positive, as ASCOM defines it. OnStep stores the opposite sign."""
		return self._require_link().info.site_longitude

	@site_longitude.setter
	def site_longitude(self, value: float) -> None:
		if not -180.0 <= value <= 180.0:
			raise errors.InvalidValueError(f"longitude {value} is outside -180..180")
		self._run(protocol.set_longitude(value))
		self._require_link().note_site(longitude=value)

	@property
	def site_elevation(self) -> float:
		"""Held by the driver: OnStep has nowhere to store an elevation."""
		return self._site_elevation

	@site_elevation.setter
	def site_elevation(self, value: float) -> None:
		if not -300.0 <= value <= 10000.0:
			raise errors.InvalidValueError(f"elevation {value} is outside the ASCOM range -300..10000 m")
		self._site_elevation = value

	@property
	def utc_date(self) -> dt.datetime:
		"""The mount's own clock, in UTC."""
		link = self._require_link()
		try:
			date_text, time_text, zone_text = link.execute_all(
				protocol.GET_LOCAL_DATE,
				protocol.GET_LOCAL_TIME,
				protocol.GET_UTC_OFFSET,
			)
		except (LinkError, CommandRejected) as exc:
			raise errors.DriverError(f"could not read the mount's clock: {exc}") from exc

		try:
			month, day, year = (int(part) for part in date_text.split("/"))
			hours = protocol.parse_hms(time_text)
			zone = protocol.parse_time_zone(zone_text)
		except (ValueError, protocol.ProtocolError) as exc:
			raise errors.DriverError(
				f"could not parse the mount's clock ({date_text!r} {time_text!r} " f"{zone_text!r}): {exc}") from exc

		local = dt.datetime(2000 + year if year < 70 else 1900 + year, month, day)
		local += dt.timedelta(hours=hours)
		return (local - dt.timedelta(hours=zone)).replace(tzinfo=dt.timezone.utc)

	@utc_date.setter
	def utc_date(self, value: dt.datetime) -> None:
		if value.tzinfo is None:
			value = value.replace(tzinfo=dt.timezone.utc)
		utc = value.astimezone(dt.timezone.utc)
		# Give the mount UTC with a zero zone rather than guessing a local zone: the
		# mount only needs a consistent pair, and sidereal time comes out the same.
		self._run(
			protocol.set_time_zone(0.0),
			protocol.set_local_date(utc.month, utc.day, utc.year),
			protocol.set_local_time(utc.hour, utc.minute, utc.second),
		)

	# -- tracking ----------------------------------------------------------------

	@property
	def tracking(self) -> bool:
		return self._state().status.tracking

	@tracking.setter
	def tracking(self, value: bool) -> None:
		self._run(
			protocol.TRACKING_ON if value else protocol.TRACKING_OFF, settle=True
		)

	@property
	def tracking_rate(self) -> DriveRate:
		"""The commanded rate."""
		self._require_link()
		return self._tracking_rate

	@tracking_rate.setter
	def tracking_rate(self, value: int) -> None:
		try:
			rate = DriveRate(value)
		except ValueError:
			raise errors.InvalidValueError(f"{value} is not an ASCOM DriveRate") from None
		self._run(_TRACKING_RATE_COMMANDS[rate])
		self._tracking_rate = rate

	@property
	def does_refraction(self) -> bool:
		compensation = self._state().status.rate_compensation
		return compensation in (
			protocol.RateCompensation.REFRACTION_RA,
			protocol.RateCompensation.REFRACTION_BOTH,
		)

	@does_refraction.setter
	def does_refraction(self, value: bool) -> None:
		if self._require_link().info.mount_type is MountType.ALTAZ:
			raise errors.NotImplementedError_("refraction tracking is compiled out of alt/az builds")
		self._run(protocol.Cmd(":Tr#" if value else ":Tn#", protocol.ReplyShape.NONE), settle=True, )

	@property
	def right_ascension_rate(self) -> float:
		self._require_link()
		return 0.0

	@right_ascension_rate.setter
	def right_ascension_rate(self, value: float) -> None:
		raise errors.NotImplementedError_("OnStep has no arbitrary right ascension rate offset")

	@property
	def declination_rate(self) -> float:
		self._require_link()
		return 0.0

	@declination_rate.setter
	def declination_rate(self, value: float) -> None:
		raise errors.NotImplementedError_("OnStep has no arbitrary declination rate offset")

	# -- guiding -----------------------------------------------------------------

	@property
	def is_pulse_guiding(self) -> bool:
		return self._require_link().is_pulse_guiding

	@property
	def guide_rate_right_ascension(self) -> float:
		"""Guide rate in degrees per second."""
		status = self._state().status
		multiple = protocol.guide_rate_index_to_sidereal(status.pulse_guide_rate_index)
		if multiple is None:
			# Indices 8 and 9 are derived from MaxRate; report the axis ceiling.
			return self._max_axis_rate
		return multiple * SIDEREAL_DEG_PER_SEC

	@guide_rate_right_ascension.setter
	def guide_rate_right_ascension(self, value: float) -> None:
		self._set_guide_rate(value)

	@property
	def guide_rate_declination(self) -> float:
		"""OnStep keeps one guide rate for both axes, so this mirrors the RA rate."""
		return self.guide_rate_right_ascension

	@guide_rate_declination.setter
	def guide_rate_declination(self, value: float) -> None:
		self._set_guide_rate(value)

	def _set_guide_rate(self, degrees_per_second: float) -> None:
		"""Snap to the nearest rate the firmware will actually use for pulses."""
		if degrees_per_second <= 0:
			raise errors.InvalidValueError("guide rate must be positive")
		requested = degrees_per_second / SIDEREAL_DEG_PER_SEC
		usable = range(protocol.GUIDE_RATE_1X_INDEX + 1)
		index = min(usable, key=lambda i: abs(protocol.GUIDE_RATES_ARCSEC_PER_SEC[i] / 15.0 - requested))
		self._run(protocol.set_slew_rate(index), settle=True)

	def pulse_guide(self, direction: int, duration_ms: int) -> None:
		try:
			GuideDirection(direction)
		except ValueError:
			raise errors.InvalidValueError(
				f"{direction} is not an ASCOM GuideDirection"
			) from None
		if duration_ms < 0:
			raise errors.InvalidValueError("guide duration cannot be negative")
		if duration_ms > protocol.MAX_PULSE_GUIDE_MS:
			raise errors.InvalidValueError(f"OnStep accepts pulses up to {protocol.MAX_PULSE_GUIDE_MS} ms; "
			                               f"{duration_ms} ms was requested")
		link = self._require_link()
		try:
			link.pulse_guide(direction, duration_ms)
		except CommandRejected as exc:
			raise _rejection_to_ascom(exc) from exc
		except LinkError as exc:
			raise errors.DriverError(str(exc)) from exc

	# -- motion ------------------------------------------------------------------

	@property
	def target_right_ascension(self) -> float:
		if self._target_ra is None:
			raise errors.ValueNotSetError("no target right ascension has been set")
		return self._target_ra

	@target_right_ascension.setter
	def target_right_ascension(self, value: float) -> None:
		self._validate_ra(value)
		self._run(protocol.set_target_ra(value))
		self._target_ra = value

	@property
	def target_declination(self) -> float:
		if self._target_dec is None:
			raise errors.ValueNotSetError("no target declination has been set")
		return self._target_dec

	@target_declination.setter
	def target_declination(self, value: float) -> None:
		self._validate_dec(value)
		self._run(protocol.set_target_dec(value))
		self._target_dec = value

	def slew_to_coordinates_async(self, ra_hours: float, dec_degrees: float) -> None:
		self._validate_ra(ra_hours)
		self._validate_dec(dec_degrees)
		self._refuse_while_parked()
		self._run(protocol.set_target_ra(ra_hours), protocol.set_target_dec(dec_degrees), protocol.GOTO_TARGET,
		          motion=Motion.START, )
		self._target_ra, self._target_dec = ra_hours, dec_degrees

	def slew_to_coordinates(self, ra_hours: float, dec_degrees: float) -> None:
		self.slew_to_coordinates_async(ra_hours, dec_degrees)
		self._wait_for_slew()

	def slew_to_target_async(self) -> None:
		ra, dec = self.target_right_ascension, self.target_declination
		self.slew_to_coordinates_async(ra, dec)

	def slew_to_target(self) -> None:
		self.slew_to_target_async()
		self._wait_for_slew()

	def slew_to_altaz_async(self, azimuth: float, altitude: float) -> None:
		self._validate_altaz(azimuth, altitude)
		self._refuse_while_parked()
		self._run(protocol.set_target_altitude(altitude), protocol.set_target_azimuth(azimuth),
		          protocol.GOTO_TARGET_ALTAZ, motion=Motion.START, )

	def slew_to_altaz(self, azimuth: float, altitude: float) -> None:
		self.slew_to_altaz_async(azimuth, altitude)
		self._wait_for_slew()

	def sync_to_coordinates(self, ra_hours: float, dec_degrees: float) -> None:
		"""Re-anchor the pointing model, via ``:CM#``."""
		self._validate_ra(ra_hours)
		self._validate_dec(dec_degrees)
		self._refuse_while_parked()
		# settle=True: a sync changes where the mount thinks it is and clients read it back
		# at once (ConformU within 10 ms), when the snapshot still holds the old position.
		replies = self._run(protocol.set_target_ra(ra_hours), protocol.set_target_dec(dec_degrees),
		                    protocol.SYNC_TO_TARGET, settle=True, )
		answer = (replies[-1] or "").strip()
		if answer.upper().startswith("E"):
			code = answer[1:2]
			raise errors.InvalidOperationError(f"the mount refused the sync: "
			                                   f"{protocol.COMMAND_ERROR_TEXT.get(protocol.GOTO_RESULT_ERRORS.get(code), answer)}")
		self._target_ra, self._target_dec = ra_hours, dec_degrees

	def sync_to_target(self) -> None:
		self.sync_to_coordinates(self.target_right_ascension, self.target_declination)

	def abort_slew(self) -> None:
		"""Stop a goto, without stopping tracking."""
		self._refuse_while_parked()
		self._moving_axes.clear()
		self._run(protocol.ABORT_SLEW, motion=Motion.STOP)

	def move_axis(self, axis: int, rate_deg_per_sec: float) -> None:
		"""Drive one axis continuously, or stop it when the rate is zero."""
		if not self.can_move_axis(axis):
			raise errors.InvalidValueError(f"axis {axis} cannot be moved")
		if abs(rate_deg_per_sec) > self._max_axis_rate + 1e-9:
			raise errors.InvalidValueError(f"rate {rate_deg_per_sec} deg/s exceeds this mount's maximum of "
			                               f"{self._max_axis_rate:.3f} deg/s")

		# Checked before the rate is even looked at: ASCOM requires MoveAxis to raise
		# while parked whatever the rate, and a rate of zero is still a MoveAxis call.
		self._refuse_while_parked()

		primary = axis == Axis.PRIMARY
		if rate_deg_per_sec == 0:
			halts = (
				(protocol.HALT_EAST, protocol.HALT_WEST)
				if primary
				else (protocol.HALT_NORTH, protocol.HALT_SOUTH)
			)
			self._run(*halts, motion=Motion.STOP)
			self._moving_axes.discard(int(axis))
			return

		if primary:
			direction = protocol.MOVE_WEST if rate_deg_per_sec > 0 else protocol.MOVE_EAST
			rate_cmd = protocol.move_axis1_at_rate(abs(rate_deg_per_sec))
		else:
			direction = (
				protocol.MOVE_NORTH if rate_deg_per_sec > 0 else protocol.MOVE_SOUTH
			)
			rate_cmd = protocol.move_axis2_at_rate(abs(rate_deg_per_sec))
		self._run(direction, rate_cmd, motion=Motion.START)
		self._moving_axes.add(int(axis))

	# -- park and home -----------------------------------------------------------

	def park(self) -> None:
		"""Park the mount. A no-op if already parked, as ASCOM specifies."""
		if self.at_park:
			return
		self._run(protocol.PARK, motion=Motion.START)
		self._wait_for(lambda: self._state().status.park is not ParkState.PARKING)
		if self._state().status.park is ParkState.PARK_FAILED:
			raise errors.DriverError("the mount reported that parking failed")

	def unpark(self) -> None:
		"""Take the mount out of the parked state. Succeeds silently if it is not parked."""
		if not self.at_park:
			return
		self._run(protocol.UNPARK, settle=True)

	def set_park(self) -> None:
		self._run(protocol.SET_PARK)

	def find_home(self) -> None:
		self._refuse_while_parked()
		self._run(protocol.GOTO_HOME, motion=Motion.START)
		self._wait_for_slew()

	@property
	def slew_settle_time(self) -> int:
		"""Seconds to pause after a slew, as a whole number."""
		return self._slew_settle_time

	@slew_settle_time.setter
	def slew_settle_time(self, value: float) -> None:
		if value < 0 or value > 100:
			raise errors.InvalidValueError("settle time must be between 0 and 100 s")
		self._slew_settle_time = int(value)

	# -- helpers -----------------------------------------------------------------

	def _refuse_while_parked(self) -> None:
		"""Say no locally rather than letting the mount refuse."""
		if self._state().status.parked:
			raise errors.ParkedError("the mount is parked; unpark it first")

	def _wait_for_slew(self) -> None:
		self._wait_for(lambda: not self.slewing)
		if self._slew_settle_time:
			time.sleep(self._slew_settle_time)

	def _wait_for(self, done: Callable[[], bool]) -> None:
		deadline = time.monotonic() + self._slew_timeout
		while not done():
			if time.monotonic() > deadline:
				raise errors.DriverError(
					f"the mount was still moving after {self._slew_timeout:.0f}s; ""treating that as a fault rather than reporting success")
			time.sleep(SLEW_POLL_INTERVAL)

	@staticmethod
	def _validate_ra(value: float) -> None:
		if not 0.0 <= value < 24.0 or math.isnan(value):
			raise errors.InvalidValueError(f"right ascension {value} is outside 0..24 hours")

	@staticmethod
	def _validate_dec(value: float) -> None:
		if not -90.0 <= value <= 90.0 or math.isnan(value):
			raise errors.InvalidValueError(f"declination {value} is outside -90..90 degrees")

	@staticmethod
	def _validate_altaz(azimuth: float, altitude: float) -> None:
		if not 0.0 <= azimuth < 360.0 or math.isnan(azimuth):
			raise errors.InvalidValueError(f"azimuth {azimuth} is outside 0..360")
		if not -90.0 <= altitude <= 90.0 or math.isnan(altitude):
			raise errors.InvalidValueError(f"altitude {altitude} is outside -90..90")


def _wrap_hours(hours: float) -> float:
	"""Fold an hour angle into -12..12."""
	return (hours + 12.0) % 24.0 - 12.0
