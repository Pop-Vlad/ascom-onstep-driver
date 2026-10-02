"""Transport-layer tests: reply framing, recovery, and the two WiFi channels.

The framing tests matter because nothing in the byte stream says which reply shape is
coming. A reader that guesses wrong does not fail fast -- it blocks for a full timeout
and then reads the *next* command's reply, so every exchange after it is off by one.
"""

from __future__ import annotations

import time

import pytest

from onstep_alpaca import protocol as p
from onstep_alpaca import transport as t
from onstep_alpaca.simulator import (
	OnStepSimulator,
	SimulatedTransport,
	SimulatorServer,
)


@pytest.fixture
def sim():
	# A real mount slews at a few degrees a second; these tests are about framing, not
	# mechanics, so wind it up and let motion finish within a poll or two.
	return OnStepSimulator(slew_rate_deg_s=2000.0)


@pytest.fixture
def link(sim):
	with SimulatedTransport(sim) as link:
		yield link


# --------------------------------------------------------------------------------------
# The three reply shapes


def test_terminated_reply_strips_the_terminator(link):
	assert link.exchange(p.GET_PRODUCT_NAME) == "On-Step"


def test_boolean_reply_is_one_unterminated_character(link, sim):
	sim.tracking = False
	assert link.exchange(p.TRACKING_ON) == "1"
	assert sim.tracking is True


def test_digit_reply_is_one_unterminated_character(link, sim):
	sim.park_position = (6.0, 30.0)
	assert link.exchange(p.PARK) == "1"
	# Parked, so a goto must come back refused with code 4 rather than hanging.
	deadline = time.monotonic() + 5.0
	while sim.park_state is not p.ParkState.PARKED:
		assert time.monotonic() < deadline, "park never completed"
		time.sleep(0.02)
		link.exchange(p.GET_STATUS)
	assert link.exchange(p.GOTO_TARGET) == "4"


def test_none_shaped_command_returns_immediately_with_no_reply(link):
	started = time.monotonic()
	assert link.exchange(p.ABORT_SLEW) is None
	assert link.exchange(p.TRACK_RATE_SIDEREAL) is None
	# If these waited for bytes they would each burn a 2 s timeout.
	assert time.monotonic() - started < 0.5


def test_wrong_shape_times_out_rather_than_returning_wrong_data(link):
	"""Declaring :Q# as TERMINATED is the mistake this design prevents; prove the failure is
	a clean timeout and not silent corruption."""
	mislabelled = p.Cmd(":Q#", p.ReplyShape.TERMINATED, timeout=0.3)
	with pytest.raises(t.ReplyTimeout):
		link.exchange(mislabelled)


def test_boolean_shape_rejects_a_non_boolean_answer(link):
	mislabelled = p.Cmd(":GVP#", p.ReplyShape.BOOLEAN, timeout=0.3)
	with pytest.raises(p.ProtocolError):
		link.exchange(mislabelled)


# --------------------------------------------------------------------------------------
# Recovery


def test_stale_bytes_from_a_timed_out_command_do_not_shift_the_next_reply(link, sim):
	"""A late reply must be discarded, not read as the answer to the next command."""
	# Simulate the aftermath of a timeout: an unread reply sitting in the buffer.
	link._pending.extend(b"+45*30'00.0#")

	name = link.exchange(p.GET_PRODUCT_NAME)
	assert name == "On-Step", "drain failed; the stale declination was read instead"


def test_unterminated_flood_is_reported_rather_than_buffered_forever(link):
	class Flood(SimulatedTransport):
		def _recv(self, max_bytes, timeout):
			return b"x" * max_bytes

	flood = Flood(link.simulator)
	flood.open()
	with pytest.raises(p.ProtocolError, match="128 bytes"):
		flood.exchange(p.GET_PRODUCT_NAME)


def test_exchange_on_a_closed_transport_is_an_error(sim):
	link = SimulatedTransport(sim)
	with pytest.raises(t.TransportError, match="not open"):
		link.exchange(p.GET_STATUS)


# --------------------------------------------------------------------------------------
# probe(): is there actually an OnStep on the other end?


def test_probe_identifies_onstep(link):
	identity = t.probe(link)
	assert identity is not None
	assert identity["product"] == "On-Step"
	assert identity["version"] == "10.23a"


def test_probe_accepts_onstepx_naming():
	sim = OnStepSimulator(product_name="OnStepX", firmware_version="10.24")
	with SimulatedTransport(sim) as link:
		identity = t.probe(link)
	assert identity is not None
	assert identity["version"] == "10.24"


def test_probe_rejects_something_that_is_not_a_mount():
	"""A serial port that merely opens tells you nothing -- a GPS dongle or a stray web
	server will happily accept bytes. Identification is by :GVP# content."""
	sim = OnStepSimulator(product_name="u-blox GNSS")
	with SimulatedTransport(sim) as link:
		assert t.probe(link) is None


def test_probe_rejects_a_silent_port():
	"""An open-but-mute port must cost one short timeout, not the default 2 s -- auto
	detection walks every COM port on the machine."""

	class Silent(SimulatedTransport):
		def _send(self, payload):
			pass  # swallow: nothing ever answers

	with Silent(OnStepSimulator()) as link:
		started = time.monotonic()
		assert t.probe(link, timeout=0.2) is None
		assert time.monotonic() - started < 1.0


# --------------------------------------------------------------------------------------
# The WiFi command channels


def test_persistent_channel_holds_one_connection_across_commands():
	sim = OnStepSimulator()
	with SimulatorServer(sim, oneshot_drop=False) as server:
		link = t.TcpTransport("127.0.0.1", server.port, mode=t.TcpMode.PERSISTENT)
		with link:
			assert link.exchange(p.GET_PRODUCT_NAME) == "On-Step"
			first_socket = link._sock
			assert link.exchange(p.GET_STATUS).endswith("0")
			assert link.exchange(p.GET_RA) is not None
			assert link._sock is first_socket, "persistent mode reconnected needlessly"


def test_oneshot_channel_reconnects_per_command():
	"""Port 9999 drops its client after 2 s whether idle or not, so the transport has to
	treat every command as its own session."""
	sim = OnStepSimulator()
	# Same behaviour as the firmware's 2 s drop, just scaled down so the test is quick.
	with SimulatorServer(sim, oneshot_drop=True, client_budget_s=0.3) as server:
		link = t.TcpTransport("127.0.0.1", server.port, mode=t.TcpMode.ONESHOT)
		with link:
			assert link.exchange(p.GET_PRODUCT_NAME) == "On-Step"
			assert link._sock is None, "one-shot mode left a socket open"
			# Wait out the guillotine, then keep working.
			time.sleep(0.5)
			assert link.exchange(p.GET_PRODUCT_NAME) == "On-Step"
			assert link.exchange(p.GET_STATUS) is not None


def test_oneshot_mode_is_chosen_from_the_port_number():
	assert (
			t.TcpTransport("h", t.STANDARD_COMMAND_PORT).mode is t.TcpMode.ONESHOT
	)
	assert (
			t.TcpTransport("h", t.PERSISTENT_COMMAND_PORT).mode is t.TcpMode.PERSISTENT
	)


def test_unreachable_host_fails_at_open_not_at_first_command():
	"""A wrong address has to surface as a connect error within seconds, or the ASCOM client
	just reports an opaque 'timeout for method Connected'."""
	link = t.TcpTransport("127.0.0.1", 1, connect_timeout=0.5)
	with pytest.raises(t.TransportError, match="cannot reach"):
		link.open()


def test_tcp_transport_recovers_when_the_mount_goes_away():
	sim = OnStepSimulator()
	server = SimulatorServer(sim, oneshot_drop=False).start()
	link = t.TcpTransport("127.0.0.1", server.port, mode=t.TcpMode.PERSISTENT)
	link.open()
	assert link.exchange(p.GET_PRODUCT_NAME) == "On-Step"

	server.stop()
	with pytest.raises(t.TransportError):
		for _ in range(5):
			link.exchange(p.GET_PRODUCT_NAME)
	link.close()


# --------------------------------------------------------------------------------------
# The OPTIONAL shape: a capability read the firmware may refuse


def test_optional_read_returns_the_value_when_the_mount_answers(link):
	reply = link.exchange(p.GET_COORDINATE_MODE)
	assert reply == "1"  # topocentric


def test_optional_read_distinguishes_a_real_zero_from_a_refusal():
	""":GXEE# genuinely answers '0#' for observed-place coordinates, so a bare '0' cannot be
	told apart by its first byte alone."""
	sim = OnStepSimulator(coordinate_mode=0)
	with SimulatedTransport(sim) as link:
		assert link.exchange(p.GET_COORDINATE_MODE) == "0", (
			"a legitimate '0#' was mistaken for a refusal"
		)


def test_optional_read_reports_a_refusal_as_none(link):
	"""A :GX index the build does not implement answers a bare, unterminated 0."""
	unimplemented = p.Cmd(":GXZZ#", p.ReplyShape.OPTIONAL, timeout=2.0)
	assert link.exchange(unimplemented) is None


def test_a_refused_optional_read_costs_far_less_than_a_full_timeout(link):
	"""The bug this shape exists to fix: declared TERMINATED, every unsupported capability
	read burned the whole 2 s timeout, on a real mount, on every connect."""
	unimplemented = p.Cmd(":GXZZ#", p.ReplyShape.OPTIONAL, timeout=2.0)
	started = time.monotonic()
	assert link.exchange(unimplemented) is None
	elapsed = time.monotonic() - started
	assert elapsed < 0.5, f"a refusal took {elapsed:.2f}s"
	assert elapsed >= t.OPTIONAL_FAILURE_GRACE


def test_a_fork_mount_connects_without_paying_for_its_missing_gem_options():
	"""A non-GEM build legitimately refuses the GEM-only reads, and that must not make
	connecting slow."""
	from onstep_alpaca.link import MountLink

	sim = OnStepSimulator(mount_type="K")
	mount = MountLink(SimulatedTransport(sim), poll_interval=0.05)
	started = time.monotonic()
	mount.connect()
	try:
		elapsed = time.monotonic() - started
		assert mount.info.mount_type is p.MountType.FORK
		assert elapsed < 2.0, f"connecting to a fork mount took {elapsed:.2f}s"
	finally:
		mount.disconnect()
