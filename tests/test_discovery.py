"""Auto-detection tests.

Everything here runs against injected transports rather than real hardware, via the
``opener`` seam. The behaviours worth pinning are the orderings -- USB before network,
remembered target before any scan, persistent channel before one-shot -- and the
refusal to mistake "this port opened" for "a mount is here".
"""

from __future__ import annotations

import time
import types

import pytest

from onstep_alpaca import discovery as d
from onstep_alpaca import transport as t
from onstep_alpaca.simulator import OnStepSimulator, SimulatedTransport


def target_key(target):
	if isinstance(target, d.SerialTarget):
		return ("serial", target.port, target.baudrate)
	return ("tcp", target.host, target.port)


class FakeWorld:
	"""A set of places a mount might be, and a record of everything probed."""

	def __init__(self, mounts: dict):
		self.mounts = {target_key(k): v for k, v in mounts.items()}
		self.attempts: list[tuple] = []

	def opener(self, target):
		key = target_key(target)
		self.attempts.append(key)
		sim = self.mounts.get(key)
		if sim is None:
			raise t.TransportError(f"nothing at {target.describe()}")
		return SimulatedTransport(sim)

	@property
	def probe_count(self) -> int:
		return len(self.attempts)


def onstep(**kwargs) -> OnStepSimulator:
	return OnStepSimulator(**kwargs)


# --------------------------------------------------------------------------------------
# Targets


def test_serial_target_round_trips_through_config():
	target = d.SerialTarget("COM7", 57600)
	assert d.target_from_dict(target.to_dict()) == target


def test_tcp_target_round_trips_through_config():
	target = d.TcpTarget("192.168.1.50", 9998)
	assert d.target_from_dict(target.to_dict()) == target


def test_tcp_target_defaults_to_the_persistent_channel():
	assert d.TcpTarget("host").port == t.PERSISTENT_COMMAND_PORT


def test_target_from_dict_rejects_nonsense():
	with pytest.raises(ValueError, match="unknown target kind"):
		d.target_from_dict({"kind": "carrier-pigeon"})


def test_tcp_target_builds_a_transport_with_the_right_channel_mode():
	oneshot = d.TcpTarget("h", t.STANDARD_COMMAND_PORT).transport()
	persistent = d.TcpTarget("h", t.PERSISTENT_COMMAND_PORT).transport()
	assert oneshot.mode is t.TcpMode.ONESHOT
	assert persistent.mode is t.TcpMode.PERSISTENT


# --------------------------------------------------------------------------------------
# probe_target


def test_probe_target_identifies_a_mount():
	world = FakeWorld({d.SerialTarget("COM3", 9600): onstep()})
	found = d.probe_target(d.SerialTarget("COM3", 9600), opener=world.opener)
	assert found is not None
	assert found.product == "On-Step"
	assert found.firmware_version == "3.16q"
	assert "COM3 at 9600 baud" in found.describe()


def test_probe_target_rejects_a_port_that_opens_but_is_not_a_mount():
	"""A serial port opening tells you nothing -- identification is by :GVP# content."""
	world = FakeWorld({d.SerialTarget("COM3", 9600): onstep(product_name="u-blox GNSS")})
	assert d.probe_target(d.SerialTarget("COM3", 9600), opener=world.opener) is None


def test_probe_target_returns_none_rather_than_raising_when_nothing_is_there():
	"""An absent mount is the expected case during a scan, not an error."""
	world = FakeWorld({})
	assert d.probe_target(d.SerialTarget("COM9", 9600), opener=world.opener) is None


def test_probe_target_closes_what_it_opened():
	sim = onstep()
	opened = []

	def opener(target):
		link = SimulatedTransport(sim)
		opened.append(link)
		return link

	d.probe_target(d.SerialTarget("COM3"), opener=opener)
	assert opened and all(not link.is_open for link in opened), "probe leaked a port"


# --------------------------------------------------------------------------------------
# Serial scanning


def test_discover_serial_finds_the_mount_among_several_ports():
	world = FakeWorld(
		{
			d.SerialTarget("COM1", 9600): onstep(product_name="u-blox GNSS"),
			d.SerialTarget("COM5", 9600): onstep(),
		}
	)
	found = d.discover_serial(["COM1", "COM3", "COM5"], opener=world.opener)
	assert len(found) == 1
	assert found[0].target == d.SerialTarget("COM5", 9600)


def test_discover_serial_walks_baud_rates_until_one_answers():
	"""A controller reconfigured away from the 9600 default still has to be found."""
	world = FakeWorld({d.SerialTarget("COM4", 115200): onstep()})
	found = d.discover_serial(["COM4"], opener=world.opener)
	assert len(found) == 1
	assert found[0].target.baudrate == 115200
	# And it tried the default first, because that is the likeliest.
	assert world.attempts[0] == ("serial", "COM4", 9600)


def test_discover_serial_returns_results_in_port_order_not_completion_order():
	"""Parallel probing must not make the winner depend on scheduling luck."""
	world = FakeWorld(
		{
			d.SerialTarget("COM2", 9600): onstep(),
			d.SerialTarget("COM8", 9600): onstep(),
		}
	)
	found = d.discover_serial(["COM2", "COM8"], opener=world.opener)
	assert [f.target.port for f in found] == ["COM2", "COM8"]


def test_discover_serial_with_no_ports_does_nothing():
	world = FakeWorld({})
	assert d.discover_serial([], opener=world.opener) == []
	assert world.probe_count == 0


def test_discover_serial_probes_each_port_but_stops_at_the_first_baud_that_works():
	world = FakeWorld({d.SerialTarget("COM4", 9600): onstep()})
	d.discover_serial(["COM4"], opener=world.opener)
	assert world.attempts == [("serial", "COM4", 9600)], world.attempts


# --------------------------------------------------------------------------------------
# Port enumeration


def _fake_port(device, description="USB Serial", vid=None, pid=None, hwid=""):
	return types.SimpleNamespace(
		device=device, description=description, vid=vid, pid=pid, hwid=hwid
	)


def test_enumerate_skips_bluetooth_virtual_ports(monkeypatch):
	"""Opening one can block for many seconds on Windows while it tries to reach a device
	that is not there, which would swamp the scan budget."""
	from serial.tools import list_ports

	monkeypatch.setattr(
		list_ports,
		"comports",
		lambda: [
			_fake_port("COM3", "Standard Serial over Bluetooth link"),
			_fake_port("COM5", "Silicon Labs CP210x USB to UART Bridge", vid=0x10C4),
		],
	)
	ports = d.enumerate_serial_ports()
	assert [p.device for p in ports] == ["COM5"]


def test_enumerate_puts_likely_adapters_first_without_excluding_the_rest(monkeypatch):
	"""An unrecognised adapter is still probed, just later -- a home-built controller must be
	findable."""
	from serial.tools import list_ports

	monkeypatch.setattr(
		list_ports,
		"comports",
		lambda: [
			_fake_port("COM1", "Some unknown gadget", vid=0xDEAD),
			_fake_port("COM9", "CH340", vid=0x1A86),
		],
	)
	ports = d.enumerate_serial_ports()
	assert [p.device for p in ports] == ["COM9", "COM1"]
	assert ports[0].likely is True
	assert ports[1].likely is False


def test_enumerate_keeps_bluetooth_when_asked(monkeypatch):
	from serial.tools import list_ports

	monkeypatch.setattr(
		list_ports,
		"comports",
		lambda: [_fake_port("COM3", "Bluetooth link")],
	)
	assert len(d.enumerate_serial_ports(skip_bluetooth=False)) == 1


# --------------------------------------------------------------------------------------
# Network scanning


def test_discover_network_tries_the_firmware_default_address():
	"""192.168.0.1 is what the addon uses for its own AP and as its station fallback, so it
	is worth a probe before any sweep."""
	world = FakeWorld({d.TcpTarget(d.DEFAULT_WIFI_HOST, 9998): onstep()})
	found = d.discover_network(opener=world.opener)
	assert len(found) == 1
	assert found[0].target.host == d.DEFAULT_WIFI_HOST


def test_discover_network_prefers_the_persistent_channel():
	"""A mount answering on both 9998 and 9999 should be used over 9998, which holds a
	session instead of dropping the client every two seconds."""
	sim = onstep()
	world = FakeWorld(
		{d.TcpTarget("10.0.0.5", 9998): sim, d.TcpTarget("10.0.0.5", 9999): sim}
	)
	found = d.discover_network(hosts=["10.0.0.5"], opener=world.opener)
	assert len(found) == 1, "the same mount on two ports is one mount"
	assert found[0].target.port == t.PERSISTENT_COMMAND_PORT


def test_discover_network_falls_back_to_the_oneshot_channel():
	"""Persistent is OFF by default in the addon's config, so 9999-only is the common case
	rather than a fault."""
	world = FakeWorld({d.TcpTarget("10.0.0.5", 9999): onstep()})
	found = d.discover_network(hosts=["10.0.0.5"], opener=world.opener)
	assert len(found) == 1
	assert found[0].target.port == t.STANDARD_COMMAND_PORT


def test_configured_hosts_outrank_the_firmware_default():
	sim = onstep()
	world = FakeWorld(
		{
			d.TcpTarget("10.0.0.5", 9998): sim,
			d.TcpTarget(d.DEFAULT_WIFI_HOST, 9998): sim,
		}
	)
	found = d.discover_network(hosts=["10.0.0.5"], opener=world.opener)
	assert found[0].target.host == "10.0.0.5"


def test_discover_network_does_not_sweep_the_subnet_unless_asked():
	"""The sweep is the slowest path and the most intrusive, so it must be opt-in."""
	world = FakeWorld({})
	assert d.discover_network(opener=world.opener) == []
	# Only the default host, on both ports -- nothing resembling 254 addresses.
	assert world.probe_count <= len(d.NETWORK_PORTS)


def test_local_subnets_are_plausible_and_exclude_loopback():
	for network in d.local_subnets():
		assert network.prefixlen == 24
		assert not network.network_address.is_loopback
		assert not network.network_address.is_link_local


# --------------------------------------------------------------------------------------
# The whole thing: ordering


def test_usb_is_preferred_over_the_network():
	"""Lower latency, and no contention with a phone app for the controller's single command-
	channel client slot."""
	world = FakeWorld(
		{
			d.SerialTarget("COM5", 9600): onstep(),
			d.TcpTarget("10.0.0.5", 9998): onstep(),
		}
	)
	found = d.discover(
		serial_ports=["COM5"], hosts=["10.0.0.5"], opener=world.opener
	)
	assert isinstance(found.target, d.SerialTarget)
	assert not any(key[0] == "tcp" for key in world.attempts), (
		"the network was probed even though USB answered"
	)


def test_network_is_used_when_usb_has_nothing():
	world = FakeWorld({d.TcpTarget("10.0.0.5", 9998): onstep()})
	found = d.discover(
		serial_ports=["COM5", "COM6"], hosts=["10.0.0.5"], opener=world.opener
	)
	assert isinstance(found.target, d.TcpTarget)
	assert found.target.host == "10.0.0.5"


def test_a_remembered_target_is_tried_first_and_alone():
	"""Reconnecting to the same mount is the common case; it should cost one probe, not a
	scan."""
	world = FakeWorld({d.SerialTarget("COM5", 19200): onstep()})
	found = d.discover(
		preferred=d.SerialTarget("COM5", 19200),
		serial_ports=["COM1", "COM2", "COM3"],
		hosts=["10.0.0.5"],
		opener=world.opener,
	)
	assert found.target == d.SerialTarget("COM5", 19200)
	assert world.probe_count == 1, world.attempts


def test_a_remembered_target_that_moved_falls_back_to_a_scan():
	world = FakeWorld({d.SerialTarget("COM7", 9600): onstep()})
	found = d.discover(
		preferred=d.SerialTarget("COM5", 19200),
		serial_ports=["COM7"],
		opener=world.opener,
	)
	assert found.target == d.SerialTarget("COM7", 9600)
	assert world.attempts[0] == ("serial", "COM5", 19200), "remembered target not first"


def test_discover_returns_none_when_there_is_no_mount():
	world = FakeWorld({})
	assert d.discover(serial_ports=["COM1"], hosts=["10.0.0.5"], opener=world.opener) is None


def test_usb_can_be_turned_off():
	world = FakeWorld({d.SerialTarget("COM5", 9600): onstep()})
	assert (
			d.discover(
				serial_ports=["COM5"], include_usb=False, include_network=False,
				opener=world.opener,
			)
			is None
	)
	assert world.probe_count == 0


def test_discover_all_lists_everything_usb_first():
	world = FakeWorld(
		{
			d.SerialTarget("COM5", 9600): onstep(),
			d.TcpTarget("10.0.0.5", 9998): onstep(),
		}
	)
	found = d.discover_all(
		serial_ports=["COM5"], hosts=["10.0.0.5"], opener=world.opener
	)
	assert len(found) == 2
	assert isinstance(found[0].target, d.SerialTarget)
	assert isinstance(found[1].target, d.TcpTarget)


def test_discovered_description_is_human_readable():
	world = FakeWorld({d.SerialTarget("COM5", 9600): onstep(product_name="OnStepX",
	                                                        firmware_version="10.24")})
	found = d.discover(serial_ports=["COM5"], opener=world.opener)
	assert found.describe() == "OnStepX 10.24 on COM5 at 9600 baud"


def test_the_probing_opener_uses_a_short_connect_budget():
	"""A scan must not inherit the 3 s connect timeout meant for a deliberate connect, or
	looking for an absent mount feels like a hang."""
	link = d.default_opener(d.TcpTarget("10.0.0.5", 9998))
	assert link.connect_timeout == d.DISCOVERY_CONNECT_TIMEOUT
	assert d.DISCOVERY_CONNECT_TIMEOUT < 3.0
	# A target built to be *used* keeps the longer, more forgiving budget.
	assert d.TcpTarget("10.0.0.5", 9998).transport().connect_timeout == 3.0


# --------------------------------------------------------------------------------------
# Latency: a mount that answers must not wait for candidates that will not


def test_a_configured_host_that_answers_skips_the_firmware_default():
	"""Tiered probing: a reachable configured host must not pay the default address's connect
	timeout before being reported."""
	world = FakeWorld({d.TcpTarget("10.0.0.5", 9998): onstep()})
	found = d.discover(hosts=["10.0.0.5"], include_usb=False, opener=world.opener)
	assert found.target.host == "10.0.0.5"
	assert not any(
		key[1] == d.DEFAULT_WIFI_HOST for key in world.attempts
	), "the firmware default was probed even though a configured host answered"


def test_the_default_baud_is_swept_across_every_port_before_the_others():
	"""One unresponsive port must not cost five timeouts before the next port is tried, so
	the ladder is walked in passes rather than per-port."""
	world = FakeWorld({d.SerialTarget("COM9", 9600): onstep()})
	found = d.discover(serial_ports=["COM1", "COM5", "COM9"], opener=world.opener)
	assert found.target == d.SerialTarget("COM9", 9600)
	# Every attempt in the winning pass was at the default rate.
	assert {key[2] for key in world.attempts} == {9600}, world.attempts


def test_a_non_default_baud_is_still_found_in_the_second_pass():
	world = FakeWorld({d.SerialTarget("COM5", 57600): onstep()})
	found = d.discover(serial_ports=["COM1", "COM5"], opener=world.opener)
	assert found.target == d.SerialTarget("COM5", 57600)


def test_a_port_that_answered_is_not_reprobed_in_later_passes():
	world = FakeWorld({d.SerialTarget("COM5", 9600): onstep()})
	d.discover_all(serial_ports=["COM5"], opener=world.opener)
	serial_attempts = [key for key in world.attempts if key[0] == "serial"]
	assert serial_attempts == [("serial", "COM5", 9600)], serial_attempts


def test_discover_all_still_enumerates_everything():
	"""The setup page's scan button wants the full list, not the first hit."""
	world = FakeWorld(
		{
			d.SerialTarget("COM2", 9600): onstep(),
			d.SerialTarget("COM8", 115200): onstep(),
		}
	)
	found = d.discover_all(serial_ports=["COM2", "COM8"], opener=world.opener)
	assert [(f.target.port, f.target.baudrate) for f in found] == [
		("COM2", 9600),
		("COM8", 115200),
	]


def test_a_hit_on_the_preferred_channel_does_not_wait_for_the_other_port():
	"""A firewall that drops SYNs rather than rejecting them makes a dead port cost a full
	connect timeout, so a hit on 9998 must short-circuit rather than wait for 9999."""
	import threading

	# The slow probe must be *running* before the fast one finishes, or cancelling would
	# suffice and the test would pass even with a pool that joins on the way out.
	slow_started = threading.Event()

	class SlowWorld(FakeWorld):
		def opener(self, target):
			key = target_key(target)
			self.attempts.append(key)
			if key == ("tcp", "10.0.0.5", t.STANDARD_COMMAND_PORT):
				slow_started.set()
				time.sleep(5.0)  # stands in for a SYN that is dropped, not rejected
			else:
				assert slow_started.wait(3.0), "slow probe never started"
			sim = self.mounts.get(key)
			if sim is None:
				raise t.TransportError("nothing here")
			return SimulatedTransport(sim)

	world = SlowWorld({d.TcpTarget("10.0.0.5", t.PERSISTENT_COMMAND_PORT): onstep()})
	started = time.monotonic()
	found = d.discover(hosts=["10.0.0.5"], include_usb=False, opener=world.opener)
	elapsed = time.monotonic() - started

	assert found.target.port == t.PERSISTENT_COMMAND_PORT
	assert elapsed < 2.0, f"waited {elapsed:.1f}s for a port it did not need"


def test_discover_all_still_waits_for_both_ports_to_report():
	"""Enumeration must not inherit the short-circuit: the setup page wants the whole
	picture, including a mount that only answers on 9999."""
	world = FakeWorld(
		{
			d.TcpTarget("10.0.0.5", t.PERSISTENT_COMMAND_PORT): onstep(),
			d.TcpTarget("10.0.0.9", t.STANDARD_COMMAND_PORT): onstep(),
		}
	)
	found = d.discover_all(
		serial_ports=[], hosts=["10.0.0.5", "10.0.0.9"], opener=world.opener
	)
	assert {f.target.host for f in found} == {"10.0.0.5", "10.0.0.9"}
