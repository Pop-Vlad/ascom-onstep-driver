"""Tests for the simulated mount itself.

The simulator is the only thing standing in for hardware, so its own fidelity has to be
pinned down -- a driver tested against a lenient fake is a driver that fails on the
mount. These tests assert the awkward behaviours specifically: unterminated replies,
the output/input format asymmetry, silent refusals, and motion over wall-clock time.
"""

from __future__ import annotations

import time

import pytest

from onstep_alpaca import protocol as p
from onstep_alpaca.simulator import OnStepSimulator, SimulatedTransport


@pytest.fixture
def sim():
	return OnStepSimulator(slew_rate_deg_s=2000.0)


def settle(sim: OnStepSimulator, timeout: float = 5.0) -> None:
	"""Let motion finish. State advances when it is read, so this polls."""
	deadline = time.monotonic() + timeout
	while sim.slewing:
		assert time.monotonic() < deadline, "motion never finished"
		time.sleep(0.01)
		sim.handle(":GU#")


# --------------------------------------------------------------------------------------
# Reply shapes at the byte level


def test_boolean_commands_answer_one_byte_with_no_terminator(sim):
	assert sim.handle(":Te#") == b"1"
	assert sim.handle(":Sr12:00:00.00#") == b"1"


def test_commands_that_answer_nothing_really_answer_nothing(sim):
	for command in (":Q#", ":TQ#", ":TL#", ":R5#", ":hC#", ":hF#", ":RA1.0000#"):
		assert sim.handle(command) == b"", command


def test_terminated_commands_carry_the_terminator(sim):
	assert sim.handle(":GVP#") == b"On-Step#"
	assert sim.handle(":GU#").endswith(b"#")


def test_goto_answers_a_bare_digit(sim):
	sim.handle(":Sr06:00:00.00#")
	sim.handle(":Sd+30*00:00.00#")
	assert sim.handle(":MS#") == b"0"


# --------------------------------------------------------------------------------------
# The format asymmetry


def test_declination_is_emitted_with_an_apostrophe_and_one_decimal(sim):
	""":GDe# emits sDD*MM'SS.s -- a form the firmware's own parser would reject. The driver
	has to read this and write something different."""
	sim.dec_degrees = 45.5
	reply = sim.handle(":GDe#").decode().rstrip("#")
	assert reply == "+45*30'00.0"
	assert p.parse_dms(reply) == pytest.approx(45.5)
	# And what we would send back is NOT the same string.
	assert p.format_dms(45.5) != reply


def test_right_ascension_is_emitted_with_two_decimals(sim):
	sim.ra_hours = 12.5
	reply = sim.handle(":GRa#").decode().rstrip("#")
	assert reply == "12:30:00.00"
	assert p.parse_hms(reply) == pytest.approx(12.5)


def test_altitude_is_emitted_in_low_precision_like_a_stock_controller(sim):
	reply = sim.handle(":GA#").decode().rstrip("#")
	assert len(reply) == 6, f"expected sDD*MM, got {reply!r}"
	p.parse_dms(reply)  # must still parse


def test_target_setters_enforce_the_firmware_length_rules(sim):
	"""hmsToDouble accepts 8 or >=10 and refuses 9; dmsToDouble accepts 9 or >=11 and
	refuses 10. A real formatter lands there by leaving a field unpadded."""
	assert sim.handle(":Sr12:30:00#") == b"1"  # 8, ok
	assert sim.handle(":Sr12:30:00.0#") == b"1"  # 10, ok
	assert sim.handle(":Sr12:30:00.00#") == b"1"  # 11, ok
	assert sim.handle(":Sr12:30:0.0#") == b"0"  # 9, refused (unpadded seconds)

	assert sim.handle(":Sd+45*30:00#") == b"1"  # 9, ok
	assert sim.handle(":Sd+45*30:00.0#") == b"1"  # 11, ok
	assert sim.handle(":Sd+45*30:00.00#") == b"1"  # 12, ok
	assert sim.handle(":Sd+45*30:0.0#") == b"0"  # 10, refused (unpadded seconds)


def test_formatters_never_produce_a_refused_length():
	"""The property that keeps the above from ever mattering in production."""
	for hours in (0.0, 1.0, 6.25, 12.5, 18.9, 23.99, 9.000001):
		assert len(p.format_hms(hours)) != 9
		assert len(p.format_hms(hours, False)) != 9
	for degrees in (-89.5, -0.5, 0.0, 30.25, 89.9, 5.000001):
		assert len(p.format_dms(degrees)) != 10
		assert len(p.format_dms(degrees, False)) != 10


def test_everything_the_driver_formats_is_accepted_by_the_simulated_firmware():
	"""The real contract: every value our formatters produce must be accepted."""
	sim = OnStepSimulator()
	for hours in (0.0, 1.0, 6.25, 12.5, 18.9, 23.99):
		assert sim.handle(f":Sr{p.format_hms(hours)}#") == b"1", hours
		assert sim.handle(f":Sr{p.format_hms(hours, False)}#") == b"1", hours
	for degrees in (-89.5, -45.0, -0.5, 0.0, 30.25, 89.9):
		assert sim.handle(f":Sd{p.format_dms(degrees)}#") == b"1", degrees
		assert sim.handle(f":Sd{p.format_dms(degrees, False)}#") == b"1", degrees
	for lat in (-33.75, 0.0, 45.5, 60.0):
		assert sim.handle(p.set_latitude(lat).text) == b"1", lat
	for lon in (-122.5, 0.0, 25.0, 179.9):
		assert sim.handle(p.set_longitude(lon).text) == b"1", lon


# --------------------------------------------------------------------------------------
# Status


def test_status_round_trips_through_the_real_parser(sim):
	status = p.parse_status(sim.handle(":GU#").decode().rstrip("#"))
	assert status.mount_type is p.MountType.GEM
	assert status.tracking is False
	assert status.slewing is False
	assert status.at_home is True


def test_status_reflects_tracking(sim):
	sim.handle(":Te#")
	status = p.parse_status(sim.handle(":GU#").decode().rstrip("#"))
	assert status.tracking is True


def test_altaz_status_parses(sim):
	altaz = OnStepSimulator(mount_type="A")
	status = p.parse_status(altaz.handle(":GU#").decode().rstrip("#"))
	assert status.mount_type is p.MountType.ALTAZ
	assert status.pier_side is p.PierSide.NONE


# --------------------------------------------------------------------------------------
# Motion


def test_goto_converges_and_resumes_tracking(sim):
	sim.handle(":Sr06:00:00.00#")
	sim.handle(":Sd+30*00:00.00#")
	assert sim.handle(":MS#") == b"0"
	assert sim.slewing is True

	settle(sim)
	assert sim.ra_hours == pytest.approx(6.0, abs=1e-6)
	assert sim.dec_degrees == pytest.approx(30.0, abs=1e-6)
	assert sim.tracking is True
	assert sim.at_home is False


def test_tracking_holds_position_while_idle_drifts(sim):
	sim.handle(":Te#")
	sim.ra_hours = 10.0
	sim.handle(":GU#")
	time.sleep(0.15)
	sim.handle(":GU#")
	assert sim.ra_hours == pytest.approx(10.0, abs=1e-9), "tracking should hold RA"

	sim.handle(":Td#")
	sim.handle(":GU#")
	time.sleep(0.15)
	sim.handle(":GU#")
	assert sim.ra_hours > 10.0, "an untracked mount's RA should climb with the sky"


def test_abort_stops_a_goto(sim):
	slow = OnStepSimulator(slew_rate_deg_s=1.0)
	slow.handle(":Sr00:00:00.00#")
	slow.handle(":Sd-30*00:00.00#")
	slow.handle(":MS#")
	assert slow.slewing is True
	slow.handle(":Q#")
	assert slow.slewing is False


def test_goto_is_refused_while_parked(sim):
	sim.handle(":hQ#")  # set park at the current position
	assert sim.handle(":hP#") == b"1"
	settle(sim)
	assert sim.park_state is p.ParkState.PARKED
	assert sim.handle(":MS#") == b"4"  # SLEW_ERR_IN_PARK


def test_park_requires_a_park_position():
	"""A controller that has never had its park position set refuses to park."""
	unconfigured = OnStepSimulator(park_position=None)
	assert unconfigured.handle(":hP#") == b"0"
	assert unconfigured.park_state is p.ParkState.NOT_PARKED


def test_park_then_unpark(sim):
	sim.handle(":hQ#")
	sim.handle(":hP#")
	settle(sim)
	assert p.parse_status(sim.handle(":GU#").decode().rstrip("#")).parked is True
	assert sim.handle(":hR#") == b"1"
	assert sim.park_state is p.ParkState.NOT_PARKED
	assert sim.tracking is True


def test_unpark_when_not_parked_is_refused(sim):
	assert sim.handle(":hR#") == b"0"


def test_tracking_cannot_be_enabled_while_parked(sim):
	sim.handle(":hQ#")
	sim.handle(":hP#")
	settle(sim)
	assert sim.handle(":Te#") == b"0", "the firmware refuses with 0, not an error"


def test_find_home_resets_position(sim):
	sim.dec_degrees = 10.0
	assert sim.handle(":hF#") == b""  # no reply at all
	assert sim.at_home is True
	assert sim.dec_degrees == pytest.approx(90.0)


# --------------------------------------------------------------------------------------
# Sync


def test_sync_with_cm_reports_success(sim):
	sim.handle(":Sr08:00:00.00#")
	sim.handle(":Sd+20*00:00.00#")
	assert sim.handle(":CM#") == b"N/A#"
	assert sim.ra_hours == pytest.approx(8.0)
	assert sim.dec_degrees == pytest.approx(20.0)


def test_sync_with_cm_reports_failure_while_parked(sim):
	sim.handle(":hQ#")
	sim.handle(":hP#")
	settle(sim)
	assert sim.handle(":CM#") == b"E4#"


def test_sync_with_cs_cannot_report_anything(sim):
	""":CS# answers nothing whether it worked or not, which is why the driver uses :CM#
	instead."""
	sim.handle(":hQ#")
	sim.handle(":hP#")
	settle(sim)
	assert sim.handle(":CS#") == b""


# --------------------------------------------------------------------------------------
# Guiding


def test_pulse_guide_moves_the_axis_and_raises_the_guiding_flag(sim):
	sim.handle(":Te#")
	before = sim.dec_degrees
	assert sim.handle(":MGn0500#") == b"1"
	assert sim.guiding is True
	assert sim.dec_degrees > before
	assert p.parse_status(sim.handle(":GU#").decode().rstrip("#")).guiding is True


def test_pulse_guide_flag_clears_after_the_duration(sim):
	sim.handle(":Te#")
	sim.handle(":MGe0050#")
	assert sim.guiding is True
	time.sleep(0.12)
	assert sim.guiding is False


def test_pulse_guide_east_increases_right_ascension(sim):
	"""ASCOM defines guideEast as +RA, and OnStep agrees once the frames line up: axis 1 is
	HOUR ANGLE, 'e' is negative there, and HA = LST - RA."""
	sim.handle(":Te#")
	sim.ra_hours = 12.0
	sim.handle(":MGe0500#")
	east = sim.ra_hours
	sim.ra_hours = 12.0
	sim.handle(":MGw0500#")
	west = sim.ra_hours
	assert east > 12.0 > west, f"east={east} west={west}"


def test_pulse_guide_over_the_limit_is_refused(sim):
	assert sim.handle(f":MGn{p.MAX_PULSE_GUIDE_MS + 1}#") == b"0"


def test_lowercase_pulse_guide_cannot_report_failure(sim):
	""":Mgd# is the same motion with no reply -- a refusal would be invisible, which is why
	the driver uses the uppercase form."""
	assert sim.handle(f":Mgn{p.MAX_PULSE_GUIDE_MS + 1}#") == b""


def test_pulse_guide_is_refused_while_slewing(sim):
	slow = OnStepSimulator(slew_rate_deg_s=1.0)
	slow.handle(":Sr00:00:00.00#")
	slow.handle(":Sd-30*00:00.00#")
	slow.handle(":MS#")
	assert slow.handle(":MGn0500#") == b"0"


# --------------------------------------------------------------------------------------
# Site and capability reads


def test_longitude_is_reported_west_positive(sim):
	"""OnStep's :Gg# is the opposite sign from ASCOM's SiteLongitude."""
	sim.longitude = 25.0  # 25 deg EAST
	reply = sim.handle(":Gg#").decode().rstrip("#")
	assert p.parse_dms(reply) == pytest.approx(-25.0)


def test_longitude_survives_a_set_then_get_round_trip(sim):
	sim.handle(p.set_longitude(-122.5).text)
	assert sim.longitude == pytest.approx(-122.5, abs=1 / 60)
	reply = sim.handle(":Gg#").decode().rstrip("#")
	assert p.parse_dms(reply) == pytest.approx(122.5, abs=1 / 60)


def test_coordinate_mode_is_reported(sim):
	assert sim.handle(":GXEE#") == b"1#"  # topocentric
	j2000 = OnStepSimulator(coordinate_mode=2)
	assert j2000.handle(":GXEE#") == b"2#"


def test_unknown_commands_are_refused_not_ignored(sim):
	assert sim.handle(":ZZ#") == b"0"


def test_the_command_log_records_traffic(sim):
	"""The caching layer's whole purpose is keeping this short, so tests need to see it."""
	sim.handle(":GU#")
	sim.handle(":GRa#")
	assert sim.log == [":GU#", ":GRa#"]


# --------------------------------------------------------------------------------------
# Through a transport


def test_simulator_works_through_the_transport_framing(sim):
	with SimulatedTransport(sim) as link:
		assert link.exchange(p.GET_PRODUCT_NAME) == "On-Step"
		assert link.exchange(p.set_target_ra(6.0)) == "1"
		assert link.exchange(p.set_target_dec(30.0)) == "1"
		assert link.exchange(p.GOTO_TARGET) == "0"
		assert link.exchange(p.ABORT_SLEW) is None
		status = p.parse_status(link.exchange(p.GET_STATUS))
		assert status.slewing is False


def test_byte_delay_models_serial_cost(sim):
	"""At 9600 baud a byte costs about a millisecond, which is the whole reason the driver
	caches instead of reading per property."""
	link = SimulatedTransport(sim, byte_delay=1 / 960)
	with link:
		started = time.monotonic()
		link.exchange(p.GET_STATUS)
		assert time.monotonic() - started > 0.005


# --------------------------------------------------------------------------------------
# Time zone: the one command whose parameter is parsed as a strict integer


def test_time_zone_accepts_whole_hours(sim):
	assert sim.handle(":SG+00#") == b"1"
	assert sim.handle(":SG-05#") == b"1"
	assert sim.handle(":SG+12#") == b"1"


def test_time_zone_accepts_only_the_three_legal_fractions(sim):
	assert sim.handle(":SG-05:30#") == b"1"
	assert sim.handle(":SG-05:45#") == b"1"
	assert sim.handle(":SG-05:00#") == b"1"
	assert sim.handle(":SG-05:15#") == b"0"


def test_time_zone_refuses_a_decimal_form(sim):
	"""The hours field goes through an integer parser, so ':SG-03.0#' -- the obvious thing to
	format from a float -- is rejected."""
	assert sim.handle(":SG-03.0#") == b"0"


def test_time_zone_refuses_out_of_range(sim):
	assert sim.handle(":SG+25#") == b"0"


@pytest.mark.parametrize(
	"zone_hours", [0.0, 1.0, -5.0, 5.5, 5.75, -3.5, 12.0, -11.0, 13.0, 2.0]
)
def test_every_zone_the_driver_formats_is_accepted(sim, zone_hours):
	command = p.set_time_zone(zone_hours)
	assert len(command.text) - len(":SG#") < 7, "parameter must stay under 7 chars"
	assert sim.handle(command.text) == b"1", command.text


def test_time_zone_is_inverted_on_the_way_to_the_mount(sim):
	"""set_time_zone takes local-minus-UTC while :SG# wants hours added to local time to
	reach UTC. Backwards puts sidereal time out by twice the zone."""
	assert p.set_time_zone(2.0).text == ":SG-02#"
	assert p.set_time_zone(-5.0).text == ":SG+05#"
	assert p.set_time_zone(5.5).text == ":SG-05:30#"


def test_time_zone_survives_a_set_then_get_round_trip(sim):
	sim.handle(p.set_time_zone(3.0).text)
	assert sim.time_zone == pytest.approx(3.0)
	# :GG# reports the firmware's own convention (sHH), and parsing flips it back.
	assert sim.handle(":GG#") == b"-03#"
	assert p.parse_time_zone("-03") == pytest.approx(3.0)


@pytest.mark.parametrize("zone_hours", [0.0, 2.0, -5.0, 5.5, -3.5, 5.75, 13.0])
def test_time_zone_round_trips_through_set_and_get(sim, zone_hours):
	assert sim.handle(p.set_time_zone(zone_hours).text) == b"1"
	reply = sim.handle(":GG#").decode().rstrip("#")
	assert p.parse_time_zone(reply) == pytest.approx(zone_hours)


def test_get_time_zone_is_not_a_decimal_form(sim):
	"""timeZoneToHM prints %+03d and appends only ':30' or ':45'."""
	sim.handle(p.set_time_zone(5.5).text)
	assert sim.handle(":GG#") == b"-05:30#"
	sim.handle(p.set_time_zone(5.75).text)
	assert sim.handle(":GG#") == b"-05:45#"


def test_parse_time_zone_puts_the_sign_on_the_whole_value(sim):
	"""'-02:30' is two and a half hours, not one and a half -- the sign is on the hours and
	the minutes are a magnitude."""
	assert p.parse_time_zone("-02:30") == pytest.approx(2.5)
	assert p.parse_time_zone("+02:30") == pytest.approx(-2.5)


@pytest.mark.parametrize("junk", ["", "abc", "+02:XY", "--3"])
def test_parse_time_zone_rejects_junk(junk):
	with pytest.raises(p.ProtocolError):
		p.parse_time_zone(junk)


def test_parse_last_error_reads_the_two_digit_code():
	assert p.parse_last_error("00") is p.CommandError.NONE
	assert p.parse_last_error("04") is p.CommandError.PARAM_RANGE
	assert p.parse_last_error("18") is p.CommandError.SLEW_ERR_IN_PARK
	with pytest.raises(p.ProtocolError):
		p.parse_last_error("zz")


def test_time_zone_snaps_an_impossible_fraction_rather_than_failing(sim):
	command = p.set_time_zone(5.9)
	assert sim.handle(command.text) == b"1"
	assert command.text in (":SG-05:45#", ":SG-06#")


# --------------------------------------------------------------------------------------
# :GE# -- how a bare "0" reply gets turned into a real message


def test_last_error_is_two_zero_padded_digits():
	"""sprintf("%02d"), so a single-digit code still arrives as two characters."""
	sim = OnStepSimulator(park_position=None)
	sim.handle(":hP#")  # refused: no park position set
	assert sim.handle(":GE#") == b"12#"  # NO_PARK_POSITION_SET
	assert p.parse_last_error("12") is p.CommandError.NO_PARK_POSITION_SET


def test_last_error_is_zero_when_nothing_failed(sim):
	sim.handle(":GVP#")
	assert sim.handle(":GE#") == b"00#"


def test_asking_for_the_last_error_does_not_destroy_it(sim):
	""":GE# sets CE_NULL for itself, so the stored error survives being read -- the driver
	can query it after a failure without racing itself."""
	sim.handle(":hR#")  # refused: not parked
	assert sim.handle(":GE#") == b"11#"
	assert sim.handle(":GE#") == b"11#", "reading the error cleared it"


def test_a_successful_command_clears_the_previous_error(sim):
	sim.handle(":hR#")
	assert sim.handle(":GE#") == b"11#"
	sim.handle(":GVP#")
	assert sim.handle(":GE#") == b"00#"


def test_goto_refusal_is_reflected_in_the_last_error(sim):
	sim.handle(":hQ#")
	sim.handle(":hP#")
	settle(sim)
	assert sim.handle(":MS#") == b"4"
	assert p.parse_last_error(
		sim.handle(":GE#").decode().rstrip("#")
	) is p.CommandError.SLEW_ERR_IN_PARK
