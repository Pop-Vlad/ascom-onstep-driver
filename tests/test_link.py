"""Tests for the owner thread and status cache.

Two properties carry most of the weight here. First, reading state must cost nothing --
that is the entire reason this layer exists, and it is easy to regress by adding an
innocent-looking read to a property. Second, state must not lie immediately after a
command, which is what the sample-time latch is for.
"""

from __future__ import annotations

import dataclasses
import threading
import time

import pytest

from onstep_alpaca import link as L
from onstep_alpaca import protocol as p
from onstep_alpaca import transport as t
from onstep_alpaca.link import MountLink, Motion
from onstep_alpaca.simulator import OnStepSimulator, SimulatedTransport, SimulatorServer


@pytest.fixture
def sim():
	return OnStepSimulator(
		mount_type="E", latitude=44.43, longitude=26.10, slew_rate_deg_s=2000.0
	)


@pytest.fixture
def mount(sim):
	link = MountLink(SimulatedTransport(sim), poll_interval=0.05)
	link.connect()
	yield link
	link.disconnect()


def wait_until(predicate, timeout=5.0, message="condition never held"):
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		if predicate():
			return
		time.sleep(0.01)
	raise AssertionError(message)


# --------------------------------------------------------------------------------------
# Connecting


def test_connect_identifies_the_mount_and_reads_its_capabilities(mount):
	info = mount.info
	assert info.product == "On-Step"
	assert info.firmware_version == "10.23a"
	assert info.mount_type is p.MountType.GEM
	assert info.is_gem is True
	assert info.coordinate_mode is p.CoordinateMode.TOPOCENTRIC
	assert info.is_onstepx is True  # 10.x
	assert "GEM" in info.description


def test_connect_publishes_a_complete_state_before_returning(mount):
	"""A client that connects and immediately reads a property must not race the first poll."""
	state = mount.state
	assert state.ra_hours == pytest.approx(12.0, abs=0.01)
	assert state.dec_degrees == pytest.approx(45.0, abs=0.01)
	assert state.sidereal_time > 0.0
	assert state.status.mount_type is p.MountType.GEM
	assert state.age < 1.0


def test_connect_reads_the_site_with_longitude_corrected_to_ascom_sign(mount):
	# The simulator sits at 26.10 deg EAST; OnStep reports that as negative.
	assert mount.info.site_latitude == pytest.approx(44.43, abs=1 / 60)
	assert mount.info.site_longitude == pytest.approx(26.10, abs=1 / 60)


def test_connect_refuses_something_that_is_not_an_onstep():
	sim = OnStepSimulator(product_name="u-blox GNSS")
	link = MountLink(SimulatedTransport(sim))
	with pytest.raises(L.LinkError, match="did not identify"):
		link.connect()
	assert link.connected is False
	# And it must not leave the transport open behind it.
	assert link._transport.is_open is False


def test_connect_propagates_a_transport_that_will_not_open():
	link = MountLink(t.TcpTransport("127.0.0.1", 1, connect_timeout=0.3))
	with pytest.raises(t.TransportError):
		link.connect()
	assert link.connected is False


def test_properties_raise_before_connecting(sim):
	link = MountLink(SimulatedTransport(sim))
	for getter in (lambda: link.info, lambda: link.state):
		with pytest.raises(L.LinkError, match="not connected"):
			getter()
	with pytest.raises(L.LinkError, match="not connected"):
		link.execute(p.GET_STATUS)


def test_disconnect_is_idempotent_and_closes_the_transport(sim):
	link = MountLink(SimulatedTransport(sim), poll_interval=0.05)
	link.connect()
	assert link.connected is True
	link.disconnect()
	assert link.connected is False
	assert link._transport.is_open is False
	link.disconnect()  # must not raise


def test_fork_mount_skips_the_gem_only_capabilities():
	"""Meridian limits are compiled out of non-GEM builds, so asking must not fail the
	connect -- it must just leave them unknown."""
	sim = OnStepSimulator(mount_type="K")
	link = MountLink(SimulatedTransport(sim), poll_interval=0.05)
	link.connect()
	try:
		assert link.info.mount_type is p.MountType.FORK
		assert link.info.is_gem is False
		assert link.info.meridian_limit_east_minutes is None
	finally:
		link.disconnect()


def test_gem_mount_reads_the_meridian_limits(mount):
	assert mount.info.meridian_limit_east_minutes == pytest.approx(60.0)
	assert mount.info.meridian_limit_west_minutes == pytest.approx(60.0)


def test_onstepx_is_detected_from_the_product_name():
	sim = OnStepSimulator(product_name="OnStepX", firmware_version="10.24")
	link = MountLink(SimulatedTransport(sim), poll_interval=0.05)
	link.connect()
	try:
		assert link.info.is_onstepx is True
	finally:
		link.disconnect()


# --------------------------------------------------------------------------------------
# The cache is the point


def test_reading_state_costs_no_link_traffic(mount):
	"""The property that makes NINA and PHD2 polling together viable at 9600 baud."""
	mount.poll_interval = 60.0  # stop background refreshes interfering
	time.sleep(0.1)
	before = mount.commands_sent
	for _ in range(500):
		state = mount.state
		_ = state.ra_hours, state.dec_degrees, state.status.tracking, mount.slewing
	assert mount.commands_sent == before, "reading cached state hit the mount"


def test_the_cache_refreshes_on_its_own(mount):
	first = mount.state.sampled_at
	wait_until(lambda: mount.state.sampled_at > first, message="cache never refreshed")
	assert mount.state.age < 1.0


def test_poll_traffic_is_bounded_by_the_interval(sim):
	"""Four commands per cycle (status, RA, Dec, one rotating slow field), so the mount sees
	a predictable rate rather than one scaled by how many clients poll."""
	link = MountLink(SimulatedTransport(sim), poll_interval=0.1)
	link.connect()
	try:
		baseline = link.commands_sent
		time.sleep(0.55)
		sent = link.commands_sent - baseline
		# ~5 cycles of 4 commands; allow generous slack for scheduling.
		assert 8 <= sent <= 40, sent
	finally:
		link.disconnect()


def test_slow_fields_are_filled_in_at_connect_then_rotate(mount):
	# All three are read during connect, so none is left at its zero value.
	state = mount.state
	assert state.sidereal_time > 0.0
	assert state.azimuth != 0.0 or state.altitude != 0.0


def test_azimuth_is_normalised_into_zero_to_three_sixty(mount):
	wait_until(lambda: 0.0 <= mount.state.azimuth < 360.0)


# --------------------------------------------------------------------------------------
# Commands


def test_execute_returns_the_reply(mount):
	assert mount.execute(p.GET_PRODUCT_NAME) == "On-Step"
	assert mount.execute(p.TRACKING_ON) == "1"


def test_execute_returns_none_for_a_command_with_no_reply(mount):
	assert mount.execute(p.ABORT_SLEW) is None


def test_a_refused_boolean_command_raises_with_the_mounts_own_reason():
	""":hP# with no park position set answers a bare 0; the driver turns that into the real
	cause by asking :GE#."""
	link = MountLink(
		SimulatedTransport(OnStepSimulator(park_position=None)), poll_interval=0.05
	)
	link.connect()
	mount = link
	with pytest.raises(p.CommandRejected) as excinfo:
		mount.execute(p.PARK)
	assert excinfo.value.error is p.CommandError.NO_PARK_POSITION_SET
	assert "no park position" in str(excinfo.value)
	link.disconnect()


def test_a_refused_goto_raises_with_the_mapped_digit(mount, sim):
	mount.execute(p.SET_PARK)
	mount.execute(p.PARK, motion=Motion.START)
	wait_until(lambda: mount.state.status.parked)
	with pytest.raises(p.CommandRejected) as excinfo:
		mount.execute(p.GOTO_TARGET, motion=Motion.START)
	assert excinfo.value.error is p.CommandError.SLEW_ERR_IN_PARK


def test_a_refusal_with_no_recorded_reason_is_still_reported(mount, sim):
	"""The mount may say no without recording why; the driver must not invent a cause."""
	exc = p.CommandRejected(":Te#", None)
	assert "no reason reported" in str(exc)
	assert exc.error is None


def test_execute_all_runs_commands_back_to_back(mount):
	replies = mount.execute_all(
		p.set_target_ra(6.0), p.set_target_dec(30.0), p.GOTO_TARGET, motion=Motion.START
	)
	assert replies == ["1", "1", "0"]


def test_execute_all_stops_at_the_first_refusal(mount, sim):
	"""A bad declination must not be followed by a goto to a half-set target."""
	sim.target_ra_hours = 1.0
	with pytest.raises(p.CommandRejected):
		mount.execute_all(
			p.Cmd(":Sd+45*30:0.0#", p.ReplyShape.BOOLEAN),  # refused length
			p.GOTO_TARGET,
		)
	assert ":MS#" not in sim.log[-3:], "goto ran after the target was refused"


def test_execute_from_the_link_thread_is_refused_rather_than_deadlocking(mount):
	captured = {}

	def from_worker():
		try:
			mount.execute(p.GET_STATUS)
		except Exception as exc:
			captured["error"] = exc

	# Stand in for a callback that accidentally runs on the owner thread.
	real_thread = mount._thread
	try:
		mount._thread = threading.current_thread()
		from_worker()
	finally:
		mount._thread = real_thread
	assert isinstance(captured.get("error"), L.LinkError)
	assert "deadlock" in str(captured["error"])


def test_commands_are_served_promptly_while_polling_continues(mount):
	"""Requests win over background polling, so a command never queues behind a backlog of
	refreshes."""
	for _ in range(10):
		started = time.monotonic()
		assert mount.execute(p.GET_PRODUCT_NAME) == "On-Step"
		assert time.monotonic() - started < 1.0


def test_refresh_forces_a_fresh_sample(mount):
	mount.poll_interval = 60.0
	time.sleep(0.1)
	before = mount.state.sampled_at
	state = mount.refresh()
	assert state.sampled_at > before


# --------------------------------------------------------------------------------------
# Motion coherence: the cache must not lie right after a command


def test_slewing_is_true_immediately_after_an_async_goto_is_accepted():
	"""What NINA depends on: start a slew, poll Slewing, see True. A stale snapshot saying
	'not slewing' would read as a slew that finished instantly."""
	# Slow enough to still be moving when we look, close enough to finish quickly:
	# the simulator starts at 12 h / +45 deg, so this is a ~3 degree move at 2 deg/s.
	sim = OnStepSimulator(slew_rate_deg_s=2.0)
	link = MountLink(SimulatedTransport(sim), poll_interval=0.05)
	link.connect()
	try:
		link.execute_all(
			p.set_target_ra(12.0), p.set_target_dec(42.0), p.GOTO_TARGET,
			motion=Motion.START,
		)
		assert link.slewing is True
		wait_until(lambda: link.slewing is False, timeout=20.0)
	finally:
		link.disconnect()


def test_the_motion_latch_holds_slewing_true_until_a_newer_sample_exists(mount):
	"""Unit-level check of the mechanism: with the cached status saying 'not slewing', a
	motion stamp newer than the sample still reports motion."""
	mount.poll_interval = 60.0
	state = mount.refresh()
	assert state.status.slewing is False
	assert mount.slewing is False

	mount._motion_started_at = time.monotonic()
	assert mount.slewing is True, "latch did not hold after a motion command"

	time.sleep(0.02)  # so the next sample is unambiguously newer than the stamp
	mount.refresh()
	assert mount.slewing is False, "latch did not clear once a newer sample arrived"


def test_abort_clears_the_latch_so_slewing_can_drop_immediately(mount):
	mount.poll_interval = 60.0
	mount._motion_started_at = time.monotonic() + 10.0  # a latch far in the future
	assert mount.slewing is True
	mount.execute(p.ABORT_SLEW, motion=Motion.STOP)
	wait_until(lambda: mount.slewing is False, message="abort left the latch set")


def test_guiding_does_not_show_up_as_slewing(mount):
	"""ASCOM requires Slewing to stay False during a guide pulse, or PHD2 waits forever for
	the mount to settle."""
	mount.execute(p.TRACKING_ON)
	mount.pulse_guide(0, 400)
	assert mount.is_pulse_guiding is True
	assert mount.slewing is False


# --------------------------------------------------------------------------------------
# Pulse guiding


def test_is_pulse_guiding_is_true_for_a_pulse_shorter_than_the_poll_interval(mount):
	"""PHD2 sends tens of milliseconds; a purely cache-driven answer would miss it."""
	mount.poll_interval = 10.0
	mount.execute(p.TRACKING_ON)
	mount.pulse_guide(2, 120)
	assert mount.is_pulse_guiding is True
	wait_until(lambda: mount.is_pulse_guiding is False, timeout=3.0)


def test_pulse_guide_moves_the_mount(mount, sim):
	mount.execute(p.TRACKING_ON)
	before = mount.refresh().dec_degrees
	mount.pulse_guide(0, 1000)  # north
	wait_until(lambda: mount.refresh().dec_degrees > before, timeout=3.0)


def test_pulse_guide_rejects_an_over_long_duration(mount):
	with pytest.raises(ValueError):
		mount.pulse_guide(0, p.MAX_PULSE_GUIDE_MS + 1)


def test_pulse_guide_refusal_does_not_start_the_local_clock(mount, sim):
	"""If the mount refuses the pulse, IsPulseGuiding must not claim one is running."""
	mount.execute(p.SET_PARK)
	mount.execute(p.PARK, motion=Motion.START)
	wait_until(lambda: mount.state.status.parked)
	with pytest.raises(p.CommandRejected):
		mount.pulse_guide(0, 500)
	assert mount.is_pulse_guiding is False


# --------------------------------------------------------------------------------------
# Faults and recovery


def test_a_link_that_dies_is_reported_then_recovered():
	sim = OnStepSimulator()
	server = SimulatorServer(sim, oneshot_drop=False).start()
	link = MountLink(
		t.TcpTransport("127.0.0.1", server.port, mode=t.TcpMode.PERSISTENT),
		poll_interval=0.05,
	)
	link.connect()
	try:
		assert link.fault is None
		server.stop()
		wait_until(lambda: link.fault is not None, message="fault was never noticed")

		# Bring the same address back up; the link must reopen on its own.
		server = SimulatorServer(sim, port=server.port, oneshot_drop=False).start()
		wait_until(
			lambda: link.fault is None,
			timeout=15.0,
			message="link never recovered",
		)
		assert link.execute(p.GET_PRODUCT_NAME) == "On-Step"
	finally:
		link.disconnect()
		server.stop()


def test_commands_fail_fast_while_the_link_is_down():
	sim = OnStepSimulator()
	server = SimulatorServer(sim, oneshot_drop=False).start()
	link = MountLink(
		t.TcpTransport("127.0.0.1", server.port, mode=t.TcpMode.PERSISTENT),
		poll_interval=0.05,
	)
	link.connect()
	try:
		server.stop()
		wait_until(lambda: link.fault is not None)
		started = time.monotonic()
		with pytest.raises((L.LinkError, t.TransportError)):
			link.execute(p.GET_PRODUCT_NAME)
		assert time.monotonic() - started < 5.0, "a down link should fail fast"
	finally:
		link.disconnect()
		server.stop()


def test_state_stays_readable_while_the_link_is_down():
	"""A client polling properties during a dropout should get the last known values, not an
	exception storm -- the ASCOM layer decides how to present staleness."""
	sim = OnStepSimulator()
	server = SimulatorServer(sim, oneshot_drop=False).start()
	link = MountLink(
		t.TcpTransport("127.0.0.1", server.port, mode=t.TcpMode.PERSISTENT),
		poll_interval=0.05,
	)
	link.connect()
	try:
		server.stop()
		wait_until(lambda: link.fault is not None)
		state = link.state
		assert state.ra_hours >= 0.0
		assert state.age >= 0.0
	finally:
		link.disconnect()
		server.stop()


# --------------------------------------------------------------------------------------
# A poll cycle must not block a guide pulse


def test_a_command_interrupts_the_poll_cycle():
	"""A pulse arriving mid-cycle used to wait for all four of the cycle's round trips --
	about 67 ms at 9600 baud, and the whole of the PulseGuide tail. It must now be served
	between commands, so it lands *inside* the cycle's own sequence rather than after it."""
	sim = OnStepSimulator()
	# Slow enough that the pulse reliably arrives while the cycle is still running.
	link = MountLink(SimulatedTransport(sim, byte_delay=0.002), poll_interval=10.0)
	link.connect()
	try:
		wait_until(lambda: link.state is not None)
		sim.log.clear()
		done = threading.Event()

		def refresh():
			link.refresh(timeout=10)
			done.set()

		threading.Thread(target=refresh, daemon=True).start()
		wait_until(lambda: any(c == ":GU#" for c in sim.log))  # the cycle has started
		link.execute(p.pulse_guide(0, 200))
		assert done.wait(10), "the refresh never completed"

		log = list(sim.log)
		pulse = next(i for i, c in enumerate(log) if c.startswith(":MG"))
		after = [c for c in log[pulse + 1:]]
		assert after, "the pulse was served after the whole cycle, not inside it"
		assert any(c in (":GRa#", ":GDe#", ":GA#", ":GZ#", ":GS#") for c in after), (
				"expected cycle reads after the pulse, got %r" % (after,))
	finally:
		link.disconnect()


def test_an_interrupting_command_does_not_make_the_sample_lie_about_motion():
	"""The cycle stamps its sample when the status is read. If it stamped later, a goto
	served mid-cycle would set the motion latch *before* the stamp, and Slewing would read
	False for a mount that had just been told to move."""
	sim = OnStepSimulator(slew_rate_deg_s=0.5)
	link = MountLink(SimulatedTransport(sim, byte_delay=0.002), poll_interval=10.0)
	link.connect()
	try:
		wait_until(lambda: link.state is not None)
		sim.log.clear()
		sampled = {}
		done = threading.Event()

		def refresh():
			sampled["state"] = link.refresh(timeout=10)
			done.set()

		threading.Thread(target=refresh, daemon=True).start()
		wait_until(lambda: any(c == ":GU#" for c in sim.log))
		link.execute(p.set_target_dec(10.0))
		link.execute(p.GOTO_TARGET, motion=Motion.START)
		assert done.wait(10), "the refresh never completed"

		# That cycle read the status before the goto was sent, so the sample it published
		# describes a mount that was not yet moving. Stamping it after the interruption
		# would put it past the latch, and `slewing` would answer False.
		state = sampled["state"]
		assert state.sampled_at < link._motion_started_at, (
			"the sample is stamped after the goto it predates, so the motion latch cannot fire")
	finally:
		link.disconnect()


def test_sidereal_time_is_carried_forward_between_reads():
	""":GS# is read one cycle in three, so served as stored it was up to three intervals
	stale -- measured at 49% of samples beyond ConformU's 1 second tolerance at a 0.5 s
	poll. It is a clock, so it can be advanced exactly instead of re-read."""
	sim = OnStepSimulator()
	link = MountLink(SimulatedTransport(sim), poll_interval=10.0)
	link.connect()
	try:
		wait_until(lambda: link.state.sidereal_time > 0.0)
		state = link.state
		stored = state.sidereal_time
		assert state.sidereal_time_at > 0.0, "the read was never timestamped"
		time.sleep(0.5)
		advanced = state.sidereal_time_now
		# Half a second of wall clock is a touch more in sidereal seconds.
		moved = (advanced - stored) * 3600.0
		assert 0.4 < moved < 0.7, "expected ~0.5 sidereal seconds, got %.3f" % moved
		assert link.state.sidereal_time == stored, "the stored sample must not be mutated"
	finally:
		link.disconnect()


def test_sidereal_time_wraps_at_24_hours():
	sim = OnStepSimulator()
	link = MountLink(SimulatedTransport(sim), poll_interval=10.0)
	link.connect()
	try:
		wait_until(lambda: link.state.sidereal_time > 0.0)
		near_midnight = dataclasses.replace(
			link.state, sidereal_time=23.9999, sidereal_time_at=time.monotonic() - 2.0)
		assert 0.0 <= near_midnight.sidereal_time_now < 24.0
	finally:
		link.disconnect()
