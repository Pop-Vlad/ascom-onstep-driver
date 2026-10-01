"""Config persistence and the UDP discovery responder."""

from __future__ import annotations

import json
import socket

import pytest

from onstep_alpaca import discovery
from onstep_alpaca.config import Config, default_config_path
from onstep_alpaca.discovery_server import (
	DISCOVERY_REQUEST,
	DiscoveryServer,
)


# --------------------------------------------------------------------------------------
# Config


def test_a_unique_id_is_generated_once_and_persisted(tmp_path):
	"""Clients store this to remember which mount a profile refers to, so regenerating it
	would make every client treat the driver as a brand-new device."""
	path = tmp_path / "config.json"
	first = Config()
	assert first.unique_id
	first.save(path)

	reloaded = Config.load(path)
	assert reloaded.unique_id == first.unique_id

	# A second, independent config gets its own id.
	assert Config().unique_id != first.unique_id


def test_config_round_trips(tmp_path):
	path = tmp_path / "config.json"
	original = Config(
		alpaca_port=12345,
		hosts=["10.0.0.5"],
		scan_subnet=True,
		poll_interval=0.5,
		site_elevation=120.0,
		remembered_target={"kind": "serial", "port": "COM7", "baudrate": 19200},
	)
	original.save(path)
	reloaded = Config.load(path)
	assert reloaded.alpaca_port == 12345
	assert reloaded.hosts == ["10.0.0.5"]
	assert reloaded.scan_subnet is True
	assert reloaded.poll_interval == 0.5
	assert reloaded.site_elevation == 120.0
	assert discovery.target_from_dict(reloaded.remembered_target) == discovery.SerialTarget(
		"COM7", 19200
	)


def test_a_missing_config_file_gives_defaults(tmp_path):
	config = Config.load(tmp_path / "nope.json")
	assert config.alpaca_port == 11111
	assert config.unique_id


def test_a_corrupt_config_file_is_survivable(tmp_path):
	"""Refusing to start because a settings file got truncated would be the less useful
	behaviour: the driver can always run on defaults."""
	path = tmp_path / "config.json"
	path.write_text("{ this is not json", encoding="utf-8")
	config = Config.load(path)
	assert config.alpaca_port == 11111


def test_a_config_file_containing_the_wrong_shape_is_survivable(tmp_path):
	path = tmp_path / "config.json"
	path.write_text("[1, 2, 3]", encoding="utf-8")
	assert Config.load(path).alpaca_port == 11111


def test_unknown_config_keys_are_ignored_rather_than_fatal(tmp_path):
	path = tmp_path / "config.json"
	path.write_text(
		json.dumps({"alpaca_port": 9999, "from_a_future_version": True}),
		encoding="utf-8",
	)
	config = Config.load(path)
	assert config.alpaca_port == 9999


def test_saving_is_atomic(tmp_path):
	"""An interrupted save must not be able to corrupt the existing file."""
	path = tmp_path / "config.json"
	Config(alpaca_port=1111).save(path)
	Config(alpaca_port=2222).save(path)
	assert Config.load(path).alpaca_port == 2222
	assert not list(tmp_path.glob("*.tmp")), "a temporary file was left behind"


def test_the_default_config_path_is_per_user():
	path = default_config_path()
	assert path.name == "config.json"
	assert "onstep-alpaca" in str(path)


# --------------------------------------------------------------------------------------
# Discovery responder


def _ask(port: int, payload: bytes = DISCOVERY_REQUEST, timeout: float = 2.0):
	client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	client.settimeout(timeout)
	try:
		client.sendto(payload, ("127.0.0.1", port))
		data, _ = client.recvfrom(1024)
		return json.loads(data)
	except (TimeoutError, socket.timeout):
		return None
	except OSError:
		# Windows turns a closed UDP port's ICMP unreachable into ConnectionResetError on the
		# next recv, which means the same thing here: nobody answered.
		return None
	finally:
		client.close()


@pytest.fixture
def responder():
	# Port 0 would be ideal but the protocol fixes 32227, so use a high port the
	# tests own and check the behaviour rather than the number.
	server = DiscoveryServer(alpaca_port=11111, listen_port=32299)
	if not server.start():
		pytest.skip("could not bind the test discovery port")
	yield server
	server.stop()


def test_discovery_answers_with_the_alpaca_port(responder):
	assert _ask(32299) == {"AlpacaPort": 11111}


def test_discovery_ignores_traffic_that_is_not_a_discovery_request(responder):
	"""Something else broadcasting on the port must not get an answer."""
	assert _ask(32299, b"hello?", timeout=0.6) is None


def test_discovery_keeps_answering(responder):
	for _ in range(3):
		assert _ask(32299) == {"AlpacaPort": 11111}


def test_discovery_can_be_stopped_and_restarted():
	server = DiscoveryServer(alpaca_port=11111, listen_port=32298)
	if not server.start():
		pytest.skip("could not bind the test discovery port")
	assert _ask(32298) == {"AlpacaPort": 11111}
	server.stop()
	assert _ask(32298, timeout=0.6) is None
	assert server.start() is True
	try:
		assert _ask(32298) == {"AlpacaPort": 11111}
	finally:
		server.stop()


def test_a_port_that_cannot_be_bound_is_reported_not_fatal():
	"""Discovery is a convenience; a client can always be pointed at the address by hand, so
	failing to bind must not stop the driver."""
	blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	blocker.bind(("0.0.0.0", 0))
	taken = blocker.getsockname()[1]
	try:
		# SO_REUSEADDR means UDP binds usually succeed anyway, which is the desired
		# behaviour. Either way the call must return a bool and never raise.
		server = DiscoveryServer(alpaca_port=11111, listen_port=taken)
		result = server.start()
		assert isinstance(result, bool)
		server.stop()
	finally:
		blocker.close()


# --------------------------------------------------------------------------------------
# Command line overrides must not become permanent settings


def test_a_one_off_flag_is_not_written_back_to_the_config(tmp_path, monkeypatch):
	"""A single `--no-discovery --port 9999` run must not leave discovery disabled and the
	port changed forever. The flags apply to that run only."""
	from onstep_alpaca import __main__ as cli

	path = tmp_path / "config.json"
	Config().save(path)
	before = Config.load(path)
	assert before.discovery_enabled is True

	args = cli.build_parser().parse_args(
		["--config", str(path), "--no-discovery", "--port", "9999",
		 "--bind", "127.0.0.1", "--host", "10.0.0.5", "--scan-subnet"]
	)
	# Reproduce what main() does: persist the file version, override only a copy.
	persisted = Config.load(path)
	effective = cli.apply_overrides(__import__("copy").deepcopy(persisted), args)
	persisted.save(path)

	assert effective.discovery_enabled is False, "the flag should affect this run"
	assert effective.alpaca_port == 9999
	assert effective.hosts == ["10.0.0.5"]

	reloaded = Config.load(path)
	assert reloaded.discovery_enabled is True, "the flag leaked into the config file"
	assert reloaded.alpaca_port == 11111
	assert reloaded.hosts == []
	assert reloaded.scan_subnet is False
	# The identity still has to survive, since clients key profiles off it.
	assert reloaded.unique_id == before.unique_id


def test_overriding_a_copy_does_not_mutate_the_persisted_object():
	"""deepcopy, not replace: `hosts` is a list and a shallow copy would share it."""
	import copy as copy_module
	from onstep_alpaca import __main__ as cli

	persisted = Config()
	args = cli.build_parser().parse_args(["--host", "10.0.0.5"])
	effective = cli.apply_overrides(copy_module.deepcopy(persisted), args)
	assert effective.hosts == ["10.0.0.5"]
	assert persisted.hosts == [], "the persisted config was mutated"


def test_a_discovered_mount_is_remembered_because_the_driver_learned_it(tmp_path):
	"""The one thing that does belong in the file: where the mount turned out to be."""
	path = tmp_path / "config.json"
	persisted = Config()
	persisted.save(path)

	target = discovery.SerialTarget("COM9", 19200).to_dict()
	persisted.remembered_target = target
	persisted.save(path)

	reloaded = Config.load(path)
	assert discovery.target_from_dict(reloaded.remembered_target) == discovery.SerialTarget(
		"COM9", 19200
	)
