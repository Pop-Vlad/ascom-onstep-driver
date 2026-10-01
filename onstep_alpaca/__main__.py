"""Command line entry point: ``python -m onstep_alpaca``.

Connection is lazy: the server starts and answers discovery immediately, and the mount
is only opened when a client sets ``Connected``. So the driver can be left running
between sessions with the mount powered off.

``--scan`` skips all that and reports what is findable -- the first thing to reach for
when a mount is not being detected.
"""

from __future__ import annotations

import argparse
import copy
import logging
import sys
from pathlib import Path

from . import discovery
from .alpaca import create_app
from .config import Config, default_config_path
from .discovery_server import DiscoveryServer
from .link import MountLink
from .setup_page import render
from .telescope import Telescope

log = logging.getLogger("onstep_alpaca")


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(prog="python -m onstep_alpaca",
	                                 description="ASCOM Alpaca telescope driver for OnStep mounts.")
	parser.add_argument("--config", type=Path, default=None,
	                    help=f"config file (default: {default_config_path()})")
	parser.add_argument("--port", type=int, default=None, help="Alpaca API port")
	parser.add_argument("--bind", default=None,
	                    help="address to bind (default 0.0.0.0; use 127.0.0.1 to keep "
	                         "the driver on this machine only)")
	parser.add_argument("--serial-port", default=None,
	                    help="force a serial port, e.g. COM5, skipping detection")
	parser.add_argument("--baud", type=int, default=None, help="baud rate for --serial-port")
	parser.add_argument("--host", action="append", default=None, metavar="ADDRESS",
	                    help="a mount address to try before the firmware default "
	                         "(repeatable)")
	parser.add_argument("--scan-subnet", action="store_true", default=None,
	                    help="sweep the local subnet if nothing else answers")
	parser.add_argument("--no-discovery", action="store_true",
	                    help="do not answer Alpaca discovery broadcasts")
	parser.add_argument("--poll-interval", type=float, default=None,
	                    help="seconds between mount status refreshes")
	parser.add_argument("--log-level", default=None,
	                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
	parser.add_argument("--scan", action="store_true",
	                    help="list the mounts that can be found, then exit")
	parser.add_argument("--simulate-slew-rate", type=float, default=6.0,
	                    metavar="DEG_PER_S",
	                    help="how fast the simulated mount slews (default 6). Lower values "
	                         "make a conformance run take realistic time; only meaningful with --simulate")
	parser.add_argument("--simulate", action="store_true",
	                    help="serve a built-in simulated mount instead of real "
	                         "hardware, for testing a client end to end")
	return parser


def apply_overrides(config: Config, args: argparse.Namespace) -> Config:
	"""Command line beats config file, for everything the user actually passed."""
	if args.port is not None:
		config.alpaca_port = args.port
	if args.bind is not None:
		config.bind_address = args.bind
	if args.host is not None:
		config.hosts = list(args.host)
	if args.scan_subnet is not None:
		config.scan_subnet = args.scan_subnet
	if args.no_discovery:
		config.discovery_enabled = False
	if args.poll_interval is not None:
		config.poll_interval = args.poll_interval
	if args.log_level is not None:
		config.log_level = args.log_level
	return config


def make_link_factory(config: Config, args: argparse.Namespace, remember: "callable[[dict], None]"):
	"""Build the callable the telescope uses to get a fresh link."""
	if args.simulate:
		from .simulator import OnStepSimulator, SimulatedTransport
		shared = OnStepSimulator(mount_type="E", latitude=44.43, longitude=26.10,
		                         slew_rate_deg_s=args.simulate_slew_rate)

		def simulated() -> MountLink:
			return MountLink(SimulatedTransport(shared), config.poll_interval)

		return simulated

	def connect() -> MountLink:
		if args.serial_port:
			target = discovery.SerialTarget(args.serial_port, args.baud or 9600)
			log.info("using the serial port given on the command line: %s", target.describe())
			return MountLink(target.transport(), config.poll_interval)

		remembered = None
		if config.remembered_target:
			try:
				remembered = discovery.target_from_dict(config.remembered_target)
			except (ValueError, KeyError):
				log.debug("ignoring an unreadable remembered target")

		found = discovery.discover(preferred=remembered, hosts=config.hosts, include_usb=config.prefer_usb,
		                           scan_subnet=config.scan_subnet)
		if found is None:
			raise discovery.t.TransportError(
				"no OnStep mount found on USB or the network. Check it is powered and ""connected, or pass --serial-port / --host.")

		remember(found.target.to_dict())
		return MountLink(found.target.transport(), config.poll_interval)

	return connect


def run_scan() -> int:
	found = discovery.discover_all(scan_subnet=True)
	ports = discovery.enumerate_serial_ports()

	print("Serial ports:")
	if not ports:
		print("  (none)")
	for port in ports:
		tag = "likely" if port.likely else "other "
		print(f"  [{tag}] {port.device:8} {port.description}")

	print("\nMounts found:")
	if not found:
		print("  (none)")
		print("\nNothing answered. Things worth checking:")
		print("  - the mount is powered on and the USB cable is a data cable")
		print("  - no other program holds the port (only one client at a time)")
		print("  - for WiFi, that this machine is on the mount's network")
		return 1
	for mount in found:
		print(f"  {mount.describe()}")
		print(f"      connect with: {mount.target.to_dict()}")
	return 0


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	config_path = args.config or default_config_path()

	# Two configs: `persisted` is the file, `config` is that plus this run's command line.
	# Saving the merged version would make a one-off `--no-discovery` permanent.
	persisted = Config.load(config_path)
	config = apply_overrides(copy.deepcopy(persisted), args)

	logging.basicConfig(level=getattr(logging, config.log_level, logging.INFO),
	                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

	if args.scan:
		return run_scan()

	# Write the file version out, so the generated unique_id survives and a first run
	# leaves an editable file behind.
	def save_persisted() -> None:
		try:
			persisted.save(config_path)
		except OSError as exc:
			log.warning("could not write %s: %s", config_path, exc)

	save_persisted()

	def remember(target: dict) -> None:
		"""Record where the mount was found -- a thing the driver learned, not a thing the
		user passed, so this one does belong in the file."""
		config.remembered_target = target
		if persisted.remembered_target != target:
			persisted.remembered_target = target
			save_persisted()

	scope = Telescope(make_link_factory(config, args, remember), unique_id=config.unique_id, name=config.device_name,
	                  site_elevation=config.site_elevation, )

	app = create_app(scope, device_number=config.device_number, device_name=config.device_name,
	                 location=config.location, setup_page=lambda: render(scope, config, config.alpaca_port), )

	responder = None
	if config.discovery_enabled:
		responder = DiscoveryServer(config.alpaca_port)
		responder.start()

	log.info("serving Alpaca on http://%s:%d/  (setup page at /setup)", config.bind_address, config.alpaca_port, )
	if args.simulate:
		log.warning("serving a SIMULATED mount; no hardware is being controlled")

	try:
		# threaded=True matters: a synchronous slew holds its request open for minutes, and
		# a single-threaded server would stop answering Slewing polls for all of it.
		app.run(host=config.bind_address, port=config.alpaca_port, threaded=True, use_reloader=False, )
	except KeyboardInterrupt:  # pragma: no cover - interactive
		log.info("stopping")
	finally:
		if responder is not None:
			responder.stop()
		try:
			scope.connected = False
		except Exception:  # pragma: no cover - shutdown must not raise
			pass
	return 0


if __name__ == "__main__":
	sys.exit(main())
