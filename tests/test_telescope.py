"""ASCOM semantics tests.

The point of these is conformance rather than plumbing: that capabilities are derived
from the mount instead of asserted, that an unsupported feature is reported as
unsupported rather than faked, and that the conventions ASCOM and OnStep disagree about
(longitude sign, pier side, what counts as slewing) come out the right way round.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest

from onstep_alpaca import errors, protocol as p
from onstep_alpaca.link import MountLink
from onstep_alpaca.simulator import OnStepSimulator, SimulatedTransport
from onstep_alpaca.telescope import (
	AlignmentMode,
	AscomPierSide,
	Axis,
	DriveRate,
	EquatorialSystem,
	GuideDirection,
	Telescope,
)


def make(sim: OnStepSimulator | None = None, **kwargs) -> tuple[Telescope, OnStepSimulator]:
	sim = sim or OnStepSimulator(
		mount_type="E", latitude=44.43, longitude=26.10, slew_rate_deg_s=2000.0
	)
	scope = Telescope(
		lambda: MountLink(SimulatedTransport(sim), poll_interval=0.05), **kwargs
	)
	return scope, sim


@pytest.fixture
def scope():
	scope, sim = make()
	scope.connected = True
	scope.sim = sim
	yield scope
	scope.connected = False


def wait_until(predicate, timeout=10.0):
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		if predicate():
			return
		time.sleep(0.01)
	raise AssertionError("condition never held")


# --------------------------------------------------------------------------------------
# Connection


def test_connect_and_disconnect():
	scope, _ = make()
	assert scope.connected is False
	scope.connected = True
	assert scope.connected is True
	assert "On-Step" in scope.description
	scope.connected = False
	assert scope.connected is False


def test_connecting_to_something_that_is_not_a_mount_is_a_clean_error():
	scope, _ = make(OnStepSimulator(product_name="u-blox GNSS"))
	with pytest.raises(errors.NotConnectedError):
		scope.connected = True
	assert scope.connected is False


def test_every_property_raises_not_connected_before_connecting():
	scope, _ = make()
	for name in (
			"right_ascension", "declination", "altitude", "azimuth", "sidereal_time",
			"at_home", "at_park", "slewing", "tracking", "alignment_mode",
			"equatorial_system", "site_latitude", "site_longitude", "is_pulse_guiding",
			"guide_rate_right_ascension", "tracking_rate", "does_refraction",
			"right_ascension_rate", "declination_rate",
	):
		with pytest.raises(errors.NotConnectedError):
			getattr(scope, name)


def test_identity_properties_do_not_need_a_connection():
	scope, _ = make()
	assert scope.interface_version == 3
	assert scope.name
	assert scope.driver_version
	assert "OnStep" in scope.driver_info


# --------------------------------------------------------------------------------------
# Capabilities derived from the mount


def test_a_gem_reports_german_polar_alignment(scope):
	assert scope.alignment_mode is AlignmentMode.GERMAN_POLAR
	assert scope.is_gem is True


def test_a_fork_mount_reports_polar_alignment_and_no_pier_side():
	scope, _ = make(OnStepSimulator(mount_type="K"))
	scope.connected = True
	try:
		assert scope.alignment_mode is AlignmentMode.POLAR
		assert scope.is_gem is False
		# A fork has no pier side, so claiming one would be a lie.
		with pytest.raises(errors.NotImplementedError_):
			scope.side_of_pier
		with pytest.raises(errors.NotImplementedError_):
			scope.destination_side_of_pier(6.0, 30.0)
	finally:
		scope.connected = False


def test_an_altaz_mount_reports_altaz_alignment():
	scope, _ = make(OnStepSimulator(mount_type="A"))
	scope.connected = True
	try:
		assert scope.alignment_mode is AlignmentMode.ALT_AZ
		# Refraction tracking is compiled out of alt/az builds.
		with pytest.raises(errors.NotImplementedError_):
			scope.does_refraction = True
	finally:
		scope.connected = False


def test_equatorial_system_follows_the_mounts_configured_frame():
	"""Hand a J2000 solve to a mount expecting apparent coordinates and the error is tens of
	arcminutes with nothing to show why."""
	topo, _ = make(OnStepSimulator(coordinate_mode=1))
	topo.connected = True
	assert topo.equatorial_system is EquatorialSystem.TOPOCENTRIC
	topo.connected = False

	j2000, _ = make(OnStepSimulator(coordinate_mode=2))
	j2000.connected = True
	assert j2000.equatorial_system is EquatorialSystem.J2000
	j2000.connected = False

	observed, _ = make(OnStepSimulator(coordinate_mode=0))
	observed.connected = True
	# ASCOM has one name for both apparent-place frames.
	assert observed.equatorial_system is EquatorialSystem.TOPOCENTRIC
	observed.connected = False


def test_unsupported_rate_offsets_are_reported_as_unsupported(scope):
	"""OnStep has no arbitrary dual-axis rate offset. Reporting the capability anyway would
	have clients silently depend on it."""
	assert scope.can_set_right_ascension_rate is False
	assert scope.can_set_declination_rate is False
	# ASCOM still requires the getters to work and read zero.
	assert scope.right_ascension_rate == 0.0
	assert scope.declination_rate == 0.0
	with pytest.raises(errors.NotImplementedError_):
		scope.right_ascension_rate = 0.5
	with pytest.raises(errors.NotImplementedError_):
		scope.declination_rate = 0.5


def test_alt_az_sync_is_not_offered(scope):
	assert scope.can_sync_altaz is False
	assert scope.can_set_pier_side is False


def test_axis_rates_advertise_a_continuous_band_for_both_real_axes(scope):
	for axis in (Axis.PRIMARY, Axis.SECONDARY):
		assert scope.can_move_axis(axis) is True
		rates = scope.axis_rates(axis)
		assert len(rates) == 1
		assert rates[0].minimum == 0.0
		assert rates[0].maximum > 0.0

	assert scope.can_move_axis(Axis.TERTIARY) is False
	assert scope.axis_rates(Axis.TERTIARY) == []


def test_tracking_rates_lists_all_four_onstep_supports(scope):
	assert set(scope.tracking_rates) == {
		DriveRate.SIDEREAL, DriveRate.LUNAR, DriveRate.SOLAR, DriveRate.KING
	}


def test_a_harmonic_mount_configured_never_to_flip_is_detected():
	""":GX94# appends ' N' when meridianFlip is MeridianFlipNever, which is how a harmonic
	drive that tracks through the meridian is normally set up."""

	class NeverFlips(OnStepSimulator):
		def _get_extended(self, index):
			if index == "94":
				return self._term("0 N")
			return super()._get_extended(index)

	scope, _ = make(NeverFlips(mount_type="E"))
	scope.connected = True
	try:
		assert scope.never_flips is True
		# With no flip, the destination side is simply the current one.
		assert scope.destination_side_of_pier(1.0, 30.0) == scope.side_of_pier
		assert scope.destination_side_of_pier(20.0, 30.0) == scope.side_of_pier
	finally:
		scope.connected = False


def test_a_flipping_gem_predicts_the_side_from_the_hour_angle(scope):
	"""Goto.ino picks PierSideWest exactly when its axis-1 hour angle is negative, and
	ASCOM's pierEast likewise means an hour angle at or past the meridian."""
	assert scope.never_flips is False
	lst = scope.sidereal_time
	east_of_meridian = (lst + 2.0) % 24.0  # negative hour angle
	west_of_meridian = (lst - 2.0) % 24.0  # positive hour angle
	assert scope.destination_side_of_pier(east_of_meridian, 30.0) is AscomPierSide.WEST
	assert scope.destination_side_of_pier(west_of_meridian, 30.0) is AscomPierSide.EAST


# --------------------------------------------------------------------------------------
# Position and site


def test_position_is_read_from_the_mount(scope):
	assert 0.0 <= scope.right_ascension < 24.0
	assert -90.0 <= scope.declination <= 90.0
	assert -90.0 <= scope.altitude <= 90.0
	assert 0.0 <= scope.azimuth < 360.0
	assert 0.0 <= scope.sidereal_time < 24.0


def test_longitude_is_east_positive_as_ascom_defines_it(scope):
	"""OnStep stores west-positive; the inversion must not leak out here."""
	assert scope.site_longitude == pytest.approx(26.10, abs=1 / 60)
	assert scope.site_latitude == pytest.approx(44.43, abs=1 / 60)


def test_setting_the_site_round_trips_through_the_sign_inversion(scope):
	scope.site_longitude = -122.5  # west
	scope.site_latitude = 37.25
	assert scope.site_longitude == pytest.approx(-122.5, abs=1 / 60)
	assert scope.site_latitude == pytest.approx(37.25, abs=1 / 60)
	# And the mount itself agrees, in its own convention.
	assert scope.sim.longitude == pytest.approx(-122.5, abs=1 / 60)


def test_site_coordinates_are_range_checked(scope):
	for bad in (-91.0, 91.0):
		with pytest.raises(errors.InvalidValueError):
			scope.site_latitude = bad
	for bad in (-181.0, 181.0):
		with pytest.raises(errors.InvalidValueError):
			scope.site_longitude = bad


def test_site_elevation_is_held_by_the_driver(scope):
	"""OnStep has nowhere to store an elevation, so the driver keeps it."""
	scope.site_elevation = 120.0
	assert scope.site_elevation == 120.0
	for bad in (-301.0, 10001.0):
		with pytest.raises(errors.InvalidValueError):
			scope.site_elevation = bad


def test_utc_date_round_trips_through_the_mounts_clock(scope):
	when = dt.datetime(2026, 3, 14, 21, 30, 15, tzinfo=dt.timezone.utc)
	scope.utc_date = when
	read_back = scope.utc_date
	assert abs((read_back - when).total_seconds()) < 90


# --------------------------------------------------------------------------------------
# Tracking


def test_tracking_can_be_turned_on_and_off(scope):
	scope.tracking = True
	wait_until(lambda: scope.tracking is True)
	scope.tracking = False
	wait_until(lambda: scope.tracking is False)


def test_tracking_cannot_be_enabled_while_parked(scope):
	scope.set_park()
	scope.park()
	with pytest.raises((errors.ParkedError, errors.InvalidOperationError)):
		scope.tracking = True


def test_tracking_rate_can_be_selected(scope):
	for rate in (DriveRate.LUNAR, DriveRate.SOLAR, DriveRate.KING, DriveRate.SIDEREAL):
		scope.tracking_rate = rate
		assert scope.tracking_rate is rate


def test_tracking_rate_rejects_a_value_that_is_not_a_drive_rate(scope):
	with pytest.raises(errors.InvalidValueError):
		scope.tracking_rate = 9


def test_tracking_rate_survives_tracking_being_switched_off(scope):
	""":GT# answers 0.0 whenever the mount is not actively tracking sidereal, so a driver
	that read it directly would report a meaningless rate the moment tracking stopped."""
	scope.tracking_rate = DriveRate.LUNAR
	scope.tracking = False
	assert scope.tracking_rate is DriveRate.LUNAR


def test_does_refraction_reflects_the_mounts_compensation_setting(scope):
	scope.does_refraction = True
	wait_until(lambda: scope.does_refraction is True)
	scope.does_refraction = False
	wait_until(lambda: scope.does_refraction is False)


# --------------------------------------------------------------------------------------
# Slewing


def test_async_slew_returns_immediately_and_reports_slewing():
	sim = OnStepSimulator(slew_rate_deg_s=3.0)
	scope, _ = make(sim)
	scope.connected = True
	try:
		scope.tracking = True
		scope.slew_to_coordinates_async(12.0, 42.0)
		assert scope.slewing is True
		wait_until(lambda: scope.slewing is False, timeout=30.0)
		assert scope.right_ascension == pytest.approx(12.0, abs=0.01)
		assert scope.declination == pytest.approx(42.0, abs=0.05)
	finally:
		scope.connected = False


def test_synchronous_slew_does_not_return_until_the_mount_has_arrived(scope):
	scope.tracking = True
	scope.slew_to_coordinates(6.0, 20.0)
	assert scope.slewing is False
	assert scope.right_ascension == pytest.approx(6.0, abs=0.01)
	assert scope.declination == pytest.approx(20.0, abs=0.05)


def test_slew_coordinates_are_range_checked(scope):
	for bad_ra in (-0.1, 24.0, 25.0):
		with pytest.raises(errors.InvalidValueError):
			scope.slew_to_coordinates_async(bad_ra, 0.0)
	for bad_dec in (-91.0, 91.0):
		with pytest.raises(errors.InvalidValueError):
			scope.slew_to_coordinates_async(0.0, bad_dec)


def test_slewing_while_parked_raises_the_parked_error_clients_look_for(scope):
	scope.set_park()
	scope.park()
	with pytest.raises(errors.ParkedError):
		scope.slew_to_coordinates_async(6.0, 30.0)


def test_target_coordinates_must_be_set_before_they_can_be_read(scope):
	with pytest.raises(errors.ValueNotSetError):
		scope.target_right_ascension
	with pytest.raises(errors.ValueNotSetError):
		scope.target_declination

	scope.target_right_ascension = 7.5
	scope.target_declination = -12.25
	assert scope.target_right_ascension == pytest.approx(7.5)
	assert scope.target_declination == pytest.approx(-12.25)


def test_slew_to_target_uses_the_stored_target(scope):
	scope.tracking = True
	scope.target_right_ascension = 9.0
	scope.target_declination = 15.0
	scope.slew_to_target()
	assert scope.right_ascension == pytest.approx(9.0, abs=0.01)


def test_slew_to_altaz(scope):
	scope.tracking = True
	scope.slew_to_altaz(120.0, 45.0)
	assert scope.slewing is False


def test_altaz_slew_coordinates_are_range_checked(scope):
	with pytest.raises(errors.InvalidValueError):
		scope.slew_to_altaz(360.0, 45.0)
	with pytest.raises(errors.InvalidValueError):
		scope.slew_to_altaz(10.0, 91.0)


def test_abort_slew_stops_a_goto():
	sim = OnStepSimulator(slew_rate_deg_s=1.0)
	scope, _ = make(sim)
	scope.connected = True
	try:
		scope.tracking = True
		scope.slew_to_coordinates_async(0.0, -30.0)
		assert scope.slewing is True
		scope.abort_slew()
		wait_until(lambda: scope.slewing is False)
	finally:
		scope.connected = False


# --------------------------------------------------------------------------------------
# Sync


def test_sync_moves_the_mounts_idea_of_where_it_is(scope):
	scope.tracking = True
	scope.sync_to_coordinates(3.5, 55.0)
	wait_until(lambda: abs(scope.right_ascension - 3.5) < 0.01)
	assert scope.declination == pytest.approx(55.0, abs=0.05)


def test_sync_reports_a_refusal_rather_than_failing_silently(scope):
	""":CS# cannot report failure at all, which is why the driver uses :CM#. A plate-solve
	workflow must not build on a sync that never happened."""
	scope.set_park()
	scope.park()
	with pytest.raises((errors.ParkedError, errors.InvalidOperationError)):
		scope.sync_to_coordinates(3.5, 55.0)


def test_sync_to_target(scope):
	scope.tracking = True
	scope.target_right_ascension = 4.0
	scope.target_declination = 33.0
	scope.sync_to_target()
	wait_until(lambda: abs(scope.right_ascension - 4.0) < 0.01)


# --------------------------------------------------------------------------------------
# Park and home


def test_park_then_unpark(scope):
	scope.set_park()
	scope.park()
	assert scope.at_park is True
	scope.unpark()
	wait_until(lambda: scope.at_park is False)


def test_park_without_a_park_position_is_refused():
	"""A controller whose park position was never set refuses, and the driver reports the
	mount's own reason rather than a bare failure."""
	scope, _ = make(OnStepSimulator(slew_rate_deg_s=2000.0, park_position=None))
	scope.connected = True
	try:
		with pytest.raises((errors.InvalidOperationError, errors.DriverError)):
			scope.park()
	finally:
		scope.connected = False


def test_find_home(scope):
	scope.find_home()
	wait_until(lambda: scope.at_home is True)


def test_find_home_is_refused_while_parked(scope):
	scope.set_park()
	scope.park()
	with pytest.raises(errors.ParkedError):
		scope.find_home()


def test_slew_settle_time_is_range_checked(scope):
	scope.slew_settle_time = 2.0
	assert scope.slew_settle_time == 2.0
	for bad in (-1.0, 101.0):
		with pytest.raises(errors.InvalidValueError):
			scope.slew_settle_time = bad


# --------------------------------------------------------------------------------------
# Guiding


def test_pulse_guide_runs_and_reports_itself(scope):
	scope.tracking = True
	scope.pulse_guide(GuideDirection.NORTH, 300)
	assert scope.is_pulse_guiding is True
	wait_until(lambda: scope.is_pulse_guiding is False, timeout=3.0)


def test_guiding_is_not_slewing(scope):
	"""ASCOM is explicit about this, and PHD2 depends on it: a driver reporting Slewing
	during a pulse leaves PHD2 waiting for the mount to settle forever."""
	scope.tracking = True
	scope.pulse_guide(GuideDirection.EAST, 500)
	assert scope.is_pulse_guiding is True
	assert scope.slewing is False


def test_pulse_guide_rejects_a_duration_the_firmware_cannot_take(scope):
	with pytest.raises(errors.InvalidValueError) as excinfo:
		scope.pulse_guide(GuideDirection.NORTH, p.MAX_PULSE_GUIDE_MS + 1)
	assert "16399" in str(excinfo.value)
	with pytest.raises(errors.InvalidValueError):
		scope.pulse_guide(GuideDirection.NORTH, -1)


def test_pulse_guide_rejects_a_bad_direction(scope):
	with pytest.raises(errors.InvalidValueError):
		scope.pulse_guide(7, 100)


def test_guide_rate_is_reported_in_degrees_per_second(scope):
	rate = scope.guide_rate_right_ascension
	# Index 2 is 1x sidereal, which is 15.04 arcsec/s.
	assert rate == pytest.approx(15.041067 / 3600.0, rel=1e-3)
	assert scope.guide_rate_declination == rate


def test_setting_the_guide_rate_snaps_to_what_the_firmware_will_use(scope):
	"""setGuideRate only updates the pulse-guide rate at or below 1x, so 0.25x, 0.5x and 1x
	are the whole menu; accepting an arbitrary value would be a lie."""
	sidereal = 15.041067 / 3600.0
	scope.guide_rate_right_ascension = 0.5 * sidereal
	wait_until(
		lambda: scope.guide_rate_right_ascension == pytest.approx(0.5 * sidereal, rel=1e-3)
	)
	scope.guide_rate_declination = 1.0 * sidereal
	wait_until(
		lambda: scope.guide_rate_right_ascension == pytest.approx(sidereal, rel=1e-3)
	)


def test_guide_rate_rejects_a_non_positive_value(scope):
	with pytest.raises(errors.InvalidValueError):
		scope.guide_rate_right_ascension = 0.0


# --------------------------------------------------------------------------------------
# MoveAxis


def test_move_axis_drives_then_stops(scope):
	scope.tracking = True
	scope.move_axis(Axis.PRIMARY, 0.5)
	assert scope.slewing is True
	scope.move_axis(Axis.PRIMARY, 0.0)
	wait_until(lambda: scope.slewing is False)


def test_move_axis_issues_direction_before_rate(scope):
	"""customGuideRateAxis1 refuses unless a guide direction is already set, so the reverse
	order silently leaves the mount at whatever :Rn# rate was last selected."""
	scope.tracking = True
	scope.sim.log.clear()
	scope.move_axis(Axis.PRIMARY, 0.25)
	sent = [c for c in scope.sim.log if c.startswith((":Mw", ":Me", ":RA"))]
	assert sent[:2] == [":Mw#", ":RA0.250000#"], sent


def test_move_axis_sign_picks_the_direction_not_a_negative_rate(scope):
	""":RA# clamps anything below 0.001 arcsec/s *up*, so a negative rate would become a tiny
	positive one -- the sign has to become a direction command."""
	scope.tracking = True
	scope.sim.log.clear()
	scope.move_axis(Axis.PRIMARY, -0.25)
	sent = [c for c in scope.sim.log if c.startswith((":Mw", ":Me", ":RA"))]
	assert sent[0] == ":Me#"
	assert all("-" not in command for command in sent), sent


def test_move_axis_on_the_secondary_axis(scope):
	scope.tracking = True
	scope.sim.log.clear()
	scope.move_axis(Axis.SECONDARY, 0.25)
	sent = [c for c in scope.sim.log if c.startswith((":Mn", ":Ms", ":RE"))]
	assert sent[:2] == [":Mn#", ":RE0.250000#"], sent
	scope.move_axis(Axis.SECONDARY, 0.0)


def test_move_axis_counts_as_slewing(scope):
	"""ASCOM requires MoveAxis motion to show up as Slewing."""
	scope.tracking = True
	scope.move_axis(Axis.SECONDARY, 0.3)
	assert scope.slewing is True
	scope.move_axis(Axis.SECONDARY, 0.0)
	wait_until(lambda: scope.slewing is False)


def test_move_axis_rejects_a_rate_above_the_mounts_maximum(scope):
	ceiling = scope.axis_rates(Axis.PRIMARY)[0].maximum
	with pytest.raises(errors.InvalidValueError):
		scope.move_axis(Axis.PRIMARY, ceiling * 2)


def test_move_axis_rejects_the_nonexistent_third_axis(scope):
	with pytest.raises(errors.InvalidValueError):
		scope.move_axis(Axis.TERTIARY, 0.1)


def test_move_axis_is_refused_while_parked(scope):
	scope.set_park()
	scope.park()
	with pytest.raises(errors.ParkedError):
		scope.move_axis(Axis.PRIMARY, 0.1)


def test_abort_slew_clears_move_axis_motion(scope):
	scope.tracking = True
	scope.move_axis(Axis.PRIMARY, 0.4)
	assert scope.slewing is True
	scope.abort_slew()
	wait_until(lambda: scope.slewing is False)


# --------------------------------------------------------------------------------------
# Actions: the OnStep-specific escape hatch


def test_action_can_read_the_raw_status(scope):
	raw = scope.action("onstep:status", "")
	assert p.parse_status(raw).mount_type is p.MountType.GEM


def test_action_can_send_an_arbitrary_command(scope):
	assert scope.action("onstep:command", ":GVP#") == "On-Step"


def test_action_rejects_a_malformed_command(scope):
	with pytest.raises(errors.InvalidValueError):
		scope.action("onstep:command", "GVP")


def test_unknown_action_is_reported_as_such(scope):
	with pytest.raises(errors.ActionNotImplementedError):
		scope.action("onstep:teleport", "")
	assert "onstep:command" in scope.supported_actions


# --------------------------------------------------------------------------------------
# Park and unpark are idempotent, as ASCOM requires


def test_parking_an_already_parked_mount_does_nothing(scope):
	"""OnStep refuses the second :hP# with CE_PARKED, but ASCOM treats Park as idempotent --
	passing that refusal on would make asking twice look like a fault."""
	scope.set_park()
	scope.park()
	assert scope.at_park is True
	scope.park()  # must not raise
	assert scope.at_park is True


def test_unparking_an_unparked_mount_succeeds_silently(scope):
	""":hR# answers 0 with CE_NOT_PARKED, but clients call Unpark unconditionally at the
	start of a session, so erroring there makes a normal startup look broken."""
	assert scope.at_park is False
	scope.unpark()  # must not raise
	assert scope.at_park is False


def test_unpark_still_works_when_actually_parked(scope):
	scope.set_park()
	scope.park()
	scope.unpark()
	wait_until(lambda: scope.at_park is False)


# --------------------------------------------------------------------------------------
# Regressions found by ConformU


def test_slew_settle_time_is_an_integer(scope):
	"""ASCOM declares SlewSettleTime as an Int16, and a strict client refuses a JSON `0.0`
	outright, so it must be an int rather than an integral float."""
	assert isinstance(scope.slew_settle_time, int)
	assert not isinstance(scope.slew_settle_time, bool)
	scope.slew_settle_time = 3.0
	assert scope.slew_settle_time == 3
	assert isinstance(scope.slew_settle_time, int)


def test_a_sync_can_be_read_back_immediately(scope):
	"""The position must reflect a sync the moment it returns."""
	scope.tracking = True
	scope.slew_to_coordinates(9.0, 22.0)
	scope.sync_to_coordinates(8.0, 21.0)
	# No waiting, no polling: straight back out of the driver.
	assert scope.right_ascension == pytest.approx(8.0, abs=0.01)
	assert scope.declination == pytest.approx(21.0, abs=0.05)


def test_a_sync_to_target_can_be_read_back_immediately(scope):
	scope.tracking = True
	scope.target_right_ascension = 4.5
	scope.target_declination = -8.0
	scope.sync_to_target()
	assert scope.right_ascension == pytest.approx(4.5, abs=0.01)
	assert scope.declination == pytest.approx(-8.0, abs=0.05)


def test_pulse_guide_east_increases_right_ascension(scope):
	"""guideEast is +RA: axis 1 is hour angle, 'e' is negative there, HA = LST - RA.
	Inverting it looks like runaway guiding rather than a sign error."""
	scope.tracking = True
	scope.slew_to_coordinates(12.0, 30.0)
	before = scope.right_ascension
	scope.pulse_guide(GuideDirection.EAST, 2000)
	wait_until(lambda: not scope.is_pulse_guiding, timeout=5.0)
	assert scope.right_ascension > before, "guideEast must increase RA"


def test_pulse_guide_north_increases_declination(scope):
	scope.tracking = True
	scope.slew_to_coordinates(12.0, 30.0)
	before = scope.declination
	scope.pulse_guide(GuideDirection.NORTH, 2000)
	wait_until(lambda: not scope.is_pulse_guiding, timeout=5.0)
	assert scope.declination > before, "guideNorth must increase declination"


def test_the_mounts_sidereal_time_is_plausible_for_its_site(scope):
	"""ConformU compares the mount's sidereal time against its own, so the simulator computes
	a real LST rather than counting from an arbitrary start."""
	import datetime as dt

	utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
	days = (utc - dt.datetime(2000, 1, 1, 12)).total_seconds() / 86400.0
	expected = (18.697374558 + 24.06570982441908 * days + 26.10 / 15.0) % 24.0
	difference = abs(scope.sidereal_time - expected)
	difference = min(difference, 24.0 - difference)
	assert difference < 0.5, f"LST {scope.sidereal_time:.3f} vs expected {expected:.3f}"


def test_every_movement_member_refuses_while_parked(scope):
	"""ASCOM requires a parked mount to raise ParkedException from every movement member.
	AbortSlew and a MoveAxis rate of zero are the non-obvious two."""
	scope.set_park()
	scope.park()
	assert scope.at_park is True

	with pytest.raises(errors.ParkedError):
		scope.abort_slew()
	with pytest.raises(errors.ParkedError):
		scope.move_axis(Axis.PRIMARY, 0.25)
	with pytest.raises(errors.ParkedError):
		scope.move_axis(Axis.SECONDARY, 0.25)
	# A rate of zero is still a MoveAxis call.
	with pytest.raises(errors.ParkedError):
		scope.move_axis(Axis.PRIMARY, 0.0)
	with pytest.raises(errors.ParkedError):
		scope.slew_to_coordinates_async(6.0, 30.0)
	with pytest.raises(errors.ParkedError):
		scope.slew_to_altaz_async(120.0, 45.0)
	with pytest.raises(errors.ParkedError):
		scope.find_home()
	with pytest.raises((errors.ParkedError, errors.InvalidOperationError)):
		scope.sync_to_coordinates(6.0, 30.0)
	with pytest.raises((errors.ParkedError, errors.InvalidOperationError)):
		scope.pulse_guide(GuideDirection.NORTH, 100)


def test_abort_slew_still_works_when_not_parked(scope):
	scope.tracking = True
	scope.abort_slew()  # must not raise
	scope.move_axis(Axis.PRIMARY, 0.0)  # nor this
