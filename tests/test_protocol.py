"""Protocol-layer tests.

The length assertions are the important ones. OnStep validates coordinates by string
length and its accepted lengths are not contiguous, so a formatter that drifts by one
character produces a mount that silently refuses every goto.
"""

from __future__ import annotations

import pytest

from onstep_alpaca import protocol as p


# --------------------------------------------------------------------------------------
# Coordinate formatting: the length rules the firmware enforces


@pytest.mark.parametrize("hours", [0.0, 1.5, 12.0, 18.456789, 23.9, 6.000001])
def test_format_hms_only_emits_lengths_the_firmware_accepts(hours):
	# hmsToDouble: PM_HIGH rejects unless len == 8 or len >= 10.
	assert len(p.format_hms(hours, high_precision=True)) == 11
	assert len(p.format_hms(hours, high_precision=False)) == 8


@pytest.mark.parametrize("degrees", [0.0, -0.5, 45.25, -89.9, 89.99999, 12.345678])
def test_format_dms_only_emits_lengths_the_firmware_accepts(degrees):
	# dmsToDouble: PM_HIGH rejects unless len == 9 or len >= 11.
	assert len(p.format_dms(degrees, high_precision=True)) == 12
	assert len(p.format_dms(degrees, high_precision=False)) == 9


def test_format_dms_uses_a_colon_before_seconds_not_an_apostrophe():
	# The firmware's apostrophe branch is unreachable because of a double ++, so only
	# a colon parses reliably on input.
	assert p.format_dms(45.5, high_precision=True).count(":") == 1
	assert "'" not in p.format_dms(45.5, high_precision=True)
	assert "'" not in p.format_dms(45.5, high_precision=False)


def test_format_hms_carries_instead_of_emitting_hour_24():
	# hmsToDouble rejects h1 > 23, so a rounding carry must wrap rather than overflow.
	assert p.format_hms(23.99999999) == "00:00:00.00"
	assert p.format_hms(23.99999999, high_precision=False) == "00:00:00"
	assert p.format_hms(24.0) == "00:00:00.00"


def test_format_dms_clamps_at_the_poles():
	assert p.format_dms(95.0) == "+90*00:00.00"
	assert p.format_dms(-95.0) == "-90*00:00.00"


def test_format_dms_keeps_negative_sign_just_below_zero():
	# dmsToDouble requires an explicit sign, and a value rounding to -00*00:00 must
	# still carry one or the mount reads it as northern.
	assert p.format_dms(-0.001).startswith("-")
	assert p.format_dms(0.0).startswith("+")


def test_format_dm_is_six_characters_after_the_sign():
	# :St#/:Sg# parse with PM_LOW, which demands exactly 6 characters past the sign.
	assert len(p.format_dm(45.5)) == 6
	assert len(p.format_dm(-122.25, width=3)) == 7
	assert p.format_dm(45.5) == "+45*30"
	assert p.format_dm(-33.75) == "-33*45"


# --------------------------------------------------------------------------------------
# Coordinate parsing: tolerant of every precision the mount might be in


@pytest.mark.parametrize(
	"text,expected",
	[
		("12:34:56.78", 12 + 34 / 60 + 56.78 / 3600),
		("12:34:56", 12 + 34 / 60 + 56 / 3600),
		("12:34.5", 12 + 34 / 60 + 5 / 600),  # low precision HH:MM.T
		("00:00:00.00", 0.0),
		("23:59:59.99", 23 + 59 / 60 + 59.99 / 3600),
	],
)
def test_parse_hms_accepts_every_precision(text, expected):
	assert p.parse_hms(text) == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize(
	"text,expected",
	[
		("+45*30'15.5", 45 + 30 / 60 + 15.5 / 3600),  # what :GDe# actually emits
		("+45*30:15", 45 + 30 / 60 + 15 / 3600),
		("-45*30'15.5", -(45 + 30 / 60 + 15.5 / 3600)),
		("+45*30", 45.5),
		("-00*30", -0.5),
		("+90*00'00.0", 90.0),
		("45*30", 45.5),  # unsigned, as :Gg# low-precision output can be
	],
)
def test_parse_dms_accepts_firmware_output_forms(text, expected):
	assert p.parse_dms(text) == pytest.approx(expected, abs=1e-9)


def test_parse_dms_accepts_the_degree_sign_separator():
	# dmsToDouble allows ':', '*' and char(223).
	assert p.parse_dms("+45°30") == pytest.approx(45.5)
	assert p.parse_dms("+45ß30") == pytest.approx(45.5)


@pytest.mark.parametrize("junk", ["", "nonsense", "12:34:56:78", "#", "+45"])
def test_parsers_reject_junk(junk):
	with pytest.raises(p.ProtocolError):
		p.parse_hms(junk)
	with pytest.raises(p.ProtocolError):
		p.parse_dms(junk)


def test_round_trip_through_the_formats_the_firmware_uses_on_each_side():
	"""Format for sending, parse what comes back -- the asymmetry has to survive."""
	for hours in (0.0, 3.14159, 12.5, 23.75):
		assert p.parse_hms(p.format_hms(hours)) == pytest.approx(hours, abs=1e-5)
	for degrees in (-89.5, -0.25, 0.0, 45.123, 89.9):
		assert p.parse_dms(p.format_dms(degrees)) == pytest.approx(degrees, abs=1e-5)


# --------------------------------------------------------------------------------------
# :GU# status decoding


def test_parse_status_typical_gem_tracking():
	# not slewing (N), not parked (p), PEC ignored (/), GEM (E), pier east (T),
	# pulse guide rate 2, guide rate 2, no error.
	status = p.parse_status("Np/ET220")
	assert status.tracking is True
	assert status.slewing is False
	assert status.park is p.ParkState.NOT_PARKED
	assert status.mount_type is p.MountType.GEM
	assert status.pier_side is p.PierSide.EAST
	assert status.pulse_guide_rate_index == 2
	assert status.guide_rate_index == 2
	assert status.general_error == 0
	assert status.parked is False


def test_parse_status_reads_tracking_and_slewing_as_negatives():
	"""'n' means NOT tracking and 'N' means NO goto -- inverting these is the single easiest
	way to ship a driver that reports a permanent slew."""
	idle = p.parse_status("nNp/ET220")
	assert idle.tracking is False
	assert idle.slewing is False

	slewing = p.parse_status("np/ET220")  # no 'N' -> a goto IS running
	assert slewing.slewing is True
	assert slewing.tracking is False


def test_parse_status_parked_and_at_home():
	parked = p.parse_status("nNP/ET220")
	assert parked.park is p.ParkState.PARKED
	assert parked.parked is True

	homed = p.parse_status("nNpH/ET220")
	assert homed.at_home is True
	assert homed.park is p.ParkState.NOT_PARKED

	parking = p.parse_status("nNI/ET220")
	assert parking.park is p.ParkState.PARKING
	assert parking.parked is False


def test_parse_status_guiding_flag():
	assert p.parse_status("NpG/ET220").guiding is True
	assert p.parse_status("Np/ET220").guiding is False


def test_parse_status_altaz_omits_the_pec_field():
	"""ALTAZM builds compile out both the PEC character and the refraction flags, so the
	tail-anchored read is what keeps this parseable."""
	status = p.parse_status("NpAo220")
	assert status.mount_type is p.MountType.ALTAZ
	assert status.pier_side is p.PierSide.NONE
	assert status.park is p.ParkState.NOT_PARKED


def test_parse_status_fork_mount():
	status = p.parse_status("Np/Ko220")
	assert status.mount_type is p.MountType.FORK
	assert status.pier_side is p.PierSide.NONE


def test_parse_status_rate_compensation():
	assert p.parse_status("Np/ET220").rate_compensation is p.RateCompensation.NONE
	assert (
			p.parse_status("Npr/ET220").rate_compensation
			is p.RateCompensation.REFRACTION_BOTH
	)
	assert (
			p.parse_status("Nprs/ET220").rate_compensation
			is p.RateCompensation.REFRACTION_RA
	)
	assert (
			p.parse_status("Npt/ET220").rate_compensation is p.RateCompensation.ONTRACK_BOTH
	)
	assert (
			p.parse_status("Npts/ET220").rate_compensation is p.RateCompensation.ONTRACK_RA
	)


def test_parse_status_general_error_is_the_last_digit():
	status = p.parse_status("nNp/ET227")
	assert status.general_error == 7
	assert p.GENERAL_ERROR_TEXT[7] == "mount is parked"


def test_parse_status_unknown_mount_type_degrades_rather_than_raising():
	"""An OnStepX build reporting a type we have no enum for must still give us tracking,
	park and pier side -- the fields the driver cannot work without."""
	status = p.parse_status("Np/XT220")
	assert status.mount_type is p.MountType.UNKNOWN
	assert status.pier_side is p.PierSide.EAST
	assert status.tracking is True


@pytest.mark.parametrize("junk", ["", "abc", "Np/ETxyz", "Np/EX220"])
def test_parse_status_rejects_unusable_replies(junk):
	with pytest.raises(p.ProtocolError):
		p.parse_status(junk)


def test_status_round_trips_with_whitespace():
	assert p.parse_status("  Np/ET220  ").mount_type is p.MountType.GEM


# --------------------------------------------------------------------------------------
# Command construction


def test_target_commands_are_boolean_shaped():
	cmd = p.set_target_ra(12.5)
	assert cmd.text == ":Sr12:30:00.00#"
	assert cmd.shape is p.ReplyShape.BOOLEAN

	cmd = p.set_target_dec(-45.5)
	assert cmd.text == ":Sd-45*30:00.00#"
	assert cmd.shape is p.ReplyShape.BOOLEAN


def test_goto_is_digit_shaped_not_boolean():
	""":MS# answers 0-9, unterminated. Reading it as a boolean rejects valid replies."""
	assert p.GOTO_TARGET.shape is p.ReplyShape.DIGIT
	assert p.GOTO_TARGET_ALTAZ.shape is p.ReplyShape.DIGIT


def test_commands_that_answer_with_nothing_are_declared_as_such():
	for cmd in (
			p.ABORT_SLEW,
			p.TRACK_RATE_SIDEREAL,
			p.TRACK_RATE_LUNAR,
			p.MOVE_EAST,
			p.GOTO_HOME,
			p.RESET_AT_HOME,
			p.set_slew_rate(5),
			p.move_axis1_at_rate(1.0),
	):
		assert cmd.shape is p.ReplyShape.NONE, cmd.text


def test_tracking_toggle_is_boolean_unlike_the_other_t_commands():
	assert p.TRACKING_ON.shape is p.ReplyShape.BOOLEAN
	assert p.TRACKING_OFF.shape is p.ReplyShape.BOOLEAN
	assert p.TRACK_RATE_SIDEREAL.shape is p.ReplyShape.NONE


def test_sync_uses_cm_not_cs():
	""":CS# cannot report failure at all, so the driver must use :CM#."""
	assert p.SYNC_TO_TARGET.text == ":CM#"
	assert p.SYNC_TO_TARGET.shape is p.ReplyShape.TERMINATED


def test_coordinate_reads_use_the_explicit_high_precision_forms():
	assert p.GET_RA.text == ":GRa#"
	assert p.GET_DEC.text == ":GDe#"


def test_pulse_guide_command_format():
	assert p.pulse_guide(0, 500).text == ":MGn0500#"  # North
	assert p.pulse_guide(1, 500).text == ":MGs0500#"  # South
	assert p.pulse_guide(2, 1200).text == ":MGe1200#"  # East
	assert p.pulse_guide(3, 16399).text == ":MGw16399#"  # West
	assert p.pulse_guide(0, 500).shape is p.ReplyShape.BOOLEAN


def test_pulse_guide_rejects_out_of_range_durations():
	# The firmware hard-rejects above 16399 ms, so clamping has to happen above us.
	with pytest.raises(ValueError):
		p.pulse_guide(0, p.MAX_PULSE_GUIDE_MS + 1)
	with pytest.raises(ValueError):
		p.pulse_guide(0, -1)
	with pytest.raises(ValueError):
		p.pulse_guide(9, 100)  # not an ASCOM guide direction


def test_slew_rate_index_is_bounded():
	assert p.set_slew_rate(0).text == ":R0#"
	assert p.set_slew_rate(9).text == ":R9#"
	for bad in (-1, 10):
		with pytest.raises(ValueError):
			p.set_slew_rate(bad)


def test_longitude_is_inverted_because_onstep_counts_west_positive():
	# ASCOM SiteLongitude is east-positive; :Sg# is not.
	assert p.set_longitude(25.0).text == ":Sg-025*00#"
	assert p.set_longitude(-122.5).text == ":Sg+122*30#"


def test_latitude_is_not_inverted():
	assert p.set_latitude(45.5).text == ":St+45*30#"
	assert p.set_latitude(-33.75).text == ":St-33*45#"


def test_date_command_uses_a_two_digit_year():
	assert p.set_local_date(10, 1, 2026).text == ":SC10/01/26#"


def test_command_error_text_covers_every_error_the_mount_can_report():
	for error in p.CommandError:
		if error in (p.CommandError.ZERO, p.CommandError.NULL):
			continue
		assert error in p.COMMAND_ERROR_TEXT, error


def test_goto_result_digits_map_onto_real_errors():
	assert p.GOTO_RESULT_ERRORS["0"] is p.CommandError.NONE
	assert p.GOTO_RESULT_ERRORS["1"] is p.CommandError.GOTO_ERR_BELOW_HORIZON
	assert p.GOTO_RESULT_ERRORS["4"] is p.CommandError.SLEW_ERR_IN_PARK
	assert len(p.GOTO_RESULT_ERRORS) == 10


def test_command_rejected_carries_readable_text():
	exc = p.CommandRejected(":MS#", p.CommandError.GOTO_ERR_BELOW_HORIZON)
	assert "below the horizon" in str(exc)
	assert exc.error is p.CommandError.GOTO_ERR_BELOW_HORIZON
