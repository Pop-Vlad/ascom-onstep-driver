"""Finding the mount: which serial port, or which address, actually has an OnStep.

USB is tried first and exhaustively; the network only if nothing answered over USB. A
serial link has lower latency, does not contend with a phone app for the controller's
single client slot, and cannot be confused by some other device listening on 9999.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from . import transport as t
from .transport import BAUD_RATES, PERSISTENT_COMMAND_PORT, STANDARD_COMMAND_PORT, SerialTransport, TcpTransport, \
	Transport

log = logging.getLogger(__name__)

DEFAULT_WIFI_HOST = "192.168.0.1"
"""What the ESP addon uses for its own access point, and as its static fallback in station
mode. Documented in the addon's ``Config.h:15``."""

NETWORK_PORTS = (PERSISTENT_COMMAND_PORT, STANDARD_COMMAND_PORT)
"""Persistent (9998) first: it holds a session, so it is strictly better when present. It is
OFF by default in the addon's config, hence the fallback to 9999."""

LIKELY_USB_VENDOR_IDS = {
	0x10C4: "Silicon Labs CP210x",
	0x1A86: "WCH CH340/CH9102",
	0x0403: "FTDI",
	0x303A: "Espressif ESP32-S2/S3",
	0x16C0: "PJRC Teensy",
	0x2341: "Arduino",
	0x2A03: "Arduino",
	0x1B4F: "SparkFun",
}
"""USB-serial bridges OnStep controllers are actually built with. Used to *order* candidates,
never to exclude them, so a home-built controller is found rather than skipped."""

SUBNET_CONNECT_TIMEOUT = 0.2
"""Per-host TCP connect budget during a subnet sweep. Only hosts whose port actually opens
get a full protocol probe, so this stays cheap."""

MAX_SUBNET_HOSTS = 254
DEFAULT_WORKERS = 32


@dataclass(frozen=True, slots=True)
class SerialTarget:
	"""A serial port and the baud rate the mount was found at."""

	port: str
	baudrate: int = 9600

	kind = "serial"

	def transport(self) -> Transport:
		return SerialTransport(self.port, self.baudrate)

	def describe(self) -> str:
		return f"{self.port} at {self.baudrate} baud"

	def to_dict(self) -> dict:
		return {"kind": "serial", "port": self.port, "baudrate": self.baudrate}


@dataclass(frozen=True, slots=True)
class TcpTarget:
	"""A host and command-channel port."""

	host: str
	port: int = PERSISTENT_COMMAND_PORT

	kind = "tcp"

	def transport(self) -> Transport:
		# TcpTransport picks one-shot vs persistent from the port number.
		return TcpTransport(self.host, self.port)

	def describe(self) -> str:
		channel = "persistent" if self.port == PERSISTENT_COMMAND_PORT else "one-shot"
		return f"{self.host}:{self.port} ({channel})"

	def to_dict(self) -> dict:
		return {"kind": "tcp", "host": self.host, "port": self.port}


Target = SerialTarget | TcpTarget
TransportOpener = Callable[[Target], Transport]


def target_from_dict(data: dict) -> Target:
	"""Rebuild a target from persisted config."""
	kind = data.get("kind")
	if kind == "serial":
		return SerialTarget(port=data["port"], baudrate=int(data.get("baudrate", 9600)))
	if kind == "tcp":
		return TcpTarget(
			host=data["host"], port=int(data.get("port", PERSISTENT_COMMAND_PORT))
		)
	raise ValueError(f"unknown target kind: {kind!r}")


@dataclass(frozen=True, slots=True)
class Discovered:
	"""A target that answered, and what it said."""

	target: Target
	identity: dict[str, str]

	@property
	def product(self) -> str:
		return self.identity.get("product", "OnStep")

	@property
	def firmware_version(self) -> str:
		return self.identity.get("version", "unknown")

	def describe(self) -> str:
		return f"{self.product} {self.firmware_version} on {self.target.describe()}"


DISCOVERY_CONNECT_TIMEOUT = 1.0
"""TCP connect budget while *probing*, as opposed to deliberately connecting."""


def default_opener(target: Target) -> Transport:
	"""Build a transport for probing. Not the same as one built to be used."""
	if isinstance(target, TcpTarget):
		return TcpTransport(target.host, target.port, connect_timeout=DISCOVERY_CONNECT_TIMEOUT)
	return target.transport()


# --------------------------------------------------------------------------------------
# Serial


@dataclass(frozen=True, slots=True)
class SerialPortInfo:
	device: str
	description: str
	vendor_id: int | None
	product_id: int | None
	likely: bool
	"""True when the USB vendor is one OnStep controllers are commonly built on."""


def enumerate_serial_ports(skip_bluetooth: bool = True) -> list[SerialPortInfo]:
	"""List serial ports, most likely first."""
	try:
		from serial.tools import list_ports
	except ImportError:  # pragma: no cover - pyserial is a declared dependency
		log.warning("pyserial is not installed; cannot scan serial ports")
		return []

	found: list[SerialPortInfo] = []
	for port in list_ports.comports():
		text = f"{port.description or ''} {getattr(port, 'hwid', '') or ''}".lower()
		if skip_bluetooth and "bluetooth" in text:
			log.debug("skipping %s (Bluetooth virtual port)", port.device)
			continue
		vid = getattr(port, "vid", None)
		found.append(
			SerialPortInfo(
				device=port.device,
				description=port.description or "",
				vendor_id=vid,
				product_id=getattr(port, "pid", None),
				likely=vid in LIKELY_USB_VENDOR_IDS,
			)
		)

	# Likely adapters first; stable ordering otherwise so results are reproducible.
	found.sort(key=lambda info: (not info.likely, info.device))
	return found


def probe_target(target: Target, timeout: float = t.PROBE_TIMEOUT,
                 opener: TransportOpener = default_opener) -> Discovered | None:
	"""Open one target, ask what it is, close it again."""
	try:
		link = opener(target)
	except Exception as exc:  # a factory failure is still just "not here"
		log.debug("cannot build a transport for %s: %s", target.describe(), exc)
		return None
	try:
		link.open()
	except Exception as exc:
		log.debug("cannot open %s: %s", target.describe(), exc)
		return None
	try:
		identity = t.probe(link, timeout=timeout)
	except Exception as exc:  # pragma: no cover - probe swallows its own failures
		log.debug("probe of %s failed: %s", target.describe(), exc)
		return None
	finally:
		link.close()

	if identity is None:
		return None
	found = Discovered(target=target, identity=identity)
	log.info("found %s", found.describe())
	return found


def _probe_serial_port(device: str, baud_rates: Sequence[int], timeout: float,
                       opener: TransportOpener) -> Discovered | None:
	"""Walk one port's baud rates in order. Sequential by necessity: a serial port cannot be
	opened twice at once."""
	for baudrate in baud_rates:
		found = probe_target(SerialTarget(device, baudrate), timeout, opener)
		if found is not None:
			return found
	return None


def _baud_passes(baud_rates: Sequence[int]) -> list[Sequence[int]]:
	"""Split the baud ladder so the likeliest rate is tried on every port first."""
	if not baud_rates:
		return []
	if len(baud_rates) == 1:
		return [baud_rates]
	return [baud_rates[:1], baud_rates[1:]]


def _probe_serial_pass(devices: Sequence[str], baud_rates: Sequence[int], timeout: float, opener: TransportOpener,
                       workers: int) -> list[Discovered]:
	"""One pass: every device, in parallel, over this pass's baud rates."""
	if not devices or not baud_rates:
		return []
	results: list[Discovered] = []
	with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(devices)),
	                                           thread_name_prefix="onstep-scan") as pool:
		futures = [
			pool.submit(_probe_serial_port, device, baud_rates, timeout, opener)
			for device in devices
		]
		for future in concurrent.futures.as_completed(futures):
			found = future.result()
			if found is not None:
				results.append(found)

	# Completion order is scheduling luck; the caller's port order is intentional.
	order = {device: index for index, device in enumerate(devices)}
	results.sort(key=lambda found: order.get(found.target.port, len(order)))
	return results


def discover_serial(ports: Iterable[str] | None = None, baud_rates: Sequence[int] = BAUD_RATES,
                    timeout: float = t.PROBE_TIMEOUT, opener: TransportOpener = default_opener,
                    workers: int = DEFAULT_WORKERS, first_only: bool = False) -> list[Discovered]:
	"""Probe serial ports, in parallel across ports and in baud-rate passes."""
	devices = (
		list(ports)
		if ports is not None
		else [info.device for info in enumerate_serial_ports()]
	)
	if not devices:
		return []

	log.debug("scanning serial ports: %s", ", ".join(devices))
	results: list[Discovered] = []
	remaining = list(devices)
	for pass_bauds in _baud_passes(baud_rates):
		if not remaining:
			break
		hits = _probe_serial_pass(remaining, pass_bauds, timeout, opener, workers)
		if hits and first_only:
			return hits
		results.extend(hits)
		answered = {found.target.port for found in hits}
		remaining = [device for device in remaining if device not in answered]

	order = {device: index for index, device in enumerate(devices)}
	results.sort(key=lambda found: order.get(found.target.port, len(order)))
	return results


# --------------------------------------------------------------------------------------
# Network


def local_subnets() -> list[ipaddress.IPv4Network]:
	"""Work out which /24s this machine is on."""
	addresses: set[str] = set()

	probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	try:
		probe.connect(("192.0.2.1", 9))  # TEST-NET-1; no packets leave the host
		addresses.add(probe.getsockname()[0])
	except OSError:
		pass
	finally:
		probe.close()

	try:
		for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
			addresses.add(info[4][0])
	except OSError:  # pragma: no cover - depends on local name resolution
		pass

	networks: list[ipaddress.IPv4Network] = []
	for text in sorted(addresses):
		try:
			address = ipaddress.IPv4Address(text)
		except ipaddress.AddressValueError:
			continue
		if address.is_loopback or address.is_link_local or address.is_multicast:
			continue
		network = ipaddress.IPv4Network(f"{address}/24", strict=False)
		if network not in networks:
			networks.append(network)
	return networks


def _port_is_open(host: str, port: int, timeout: float) -> bool:
	"""Cheap reachability check, so only live hosts get a full protocol probe."""
	try:
		with socket.create_connection((host, port), timeout=timeout):
			return True
	except OSError:
		return False


def discover_network(hosts: Iterable[str] = (), ports: Sequence[int] = NETWORK_PORTS, timeout: float = t.PROBE_TIMEOUT,
                     opener: TransportOpener = default_opener, scan_subnet: bool = False,
                     workers: int = DEFAULT_WORKERS, include_default_host: bool = True, first_only: bool = False) -> \
		list[Discovered]:
	"""Probe network candidates: configured hosts, the firmware default, then a sweep."""
	configured: list[str] = []
	for host in hosts:
		if host and host not in configured:
			configured.append(host)

	# Tiers rather than one flat batch: a configured host that answers immediately
	# should not be made to wait out the firmware default's connect timeout.
	tiers: list[list[str]] = []
	if configured:
		tiers.append(configured)
	if include_default_host and DEFAULT_WIFI_HOST not in configured:
		tiers.append([DEFAULT_WIFI_HOST])

	results: list[Discovered] = []
	for tier in tiers:
		hits = _probe_hosts(tier, ports, timeout, opener, workers, first_only)
		if hits and first_only:
			return hits
		results.extend(hits)
	if results or not scan_subnet:
		return _dedupe_by_host(results)

	known = configured + [DEFAULT_WIFI_HOST]
	sweep = [
		str(address)
		for network in local_subnets()
		for address in list(network.hosts())[:MAX_SUBNET_HOSTS]
		if str(address) not in known
	]
	if not sweep:
		return []

	log.info("sweeping %d addresses for a command channel", len(sweep))
	live = _filter_reachable(sweep, ports, workers)
	if not live:
		return []
	log.debug("addresses with an open command port: %s", ", ".join(h for h, _ in live))
	return _probe_pairs(live, timeout, opener, workers)


def _probe_hosts(hosts: Sequence[str], ports: Sequence[int], timeout: float, opener: TransportOpener, workers: int,
                 first_only: bool = False) -> list[Discovered]:
	if not hosts:
		return []
	pairs = [(host, port) for host in hosts for port in ports]
	return _probe_pairs(pairs, timeout, opener, workers, host_order=hosts, first_only=first_only)


def _filter_reachable(hosts: Sequence[str], ports: Sequence[int], workers: int) -> list[tuple[str, int]]:
	"""Narrow a sweep to the addresses that actually have one of the ports open."""
	pairs = [(host, port) for host in hosts for port in ports]
	live: list[tuple[str, int]] = []
	with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(pairs)),
	                                           thread_name_prefix="onstep-sweep") as pool:
		futures = {
			pool.submit(_port_is_open, host, port, SUBNET_CONNECT_TIMEOUT): (host, port)
			for host, port in pairs
		}
		for future in concurrent.futures.as_completed(futures):
			if future.result():
				live.append(futures[future])
	return live


def _probe_pairs(pairs: Sequence[tuple[str, int]], timeout: float, opener: TransportOpener, workers: int,
                 host_order: Sequence[str] | None = None, first_only: bool = False) -> list[Discovered]:
	if not pairs:
		return []
	best_port = NETWORK_PORTS[0]
	results: list[Discovered] = []
	# Not a `with` block: its shutdown(wait=True) joins already-running workers, so an
	# early return would still block for the timeout it is skipping. Cancelling cannot help.
	pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(pairs)), thread_name_prefix="onstep-scan")
	try:
		futures = [
			pool.submit(probe_target, TcpTarget(host, port), timeout, opener)
			for host, port in pairs
		]
		for future in concurrent.futures.as_completed(futures):
			found = future.result()
			if found is None:
				continue
			results.append(found)
			if first_only and found.target.port == best_port:
				# A hit on the preferred channel cannot be beaten, and a firewall that drops SYNs
				# makes a dead port cost the whole timeout. Stragglers just close a socket.
				return [found]
	finally:
		pool.shutdown(wait=False, cancel_futures=True)

	# A mount reachable on both ports should be reported on the persistent one, and
	# configured hosts should outrank the firmware default.
	order = (
		{host: index for index, host in enumerate(host_order)} if host_order else {}
	)
	port_rank = {port: index for index, port in enumerate(NETWORK_PORTS)}
	results.sort(
		key=lambda found: (
			order.get(found.target.host, len(order)),
			port_rank.get(found.target.port, len(port_rank)),
		)
	)
	return _dedupe_by_host(results)


def _dedupe_by_host(results: Sequence[Discovered]) -> list[Discovered]:
	"""One entry per host: the same mount answering on 9998 and 9999 is one mount."""
	seen: set[str] = set()
	unique: list[Discovered] = []
	for found in results:
		host = found.target.host
		if host in seen:
			continue
		seen.add(host)
		unique.append(found)
	return unique


# --------------------------------------------------------------------------------------
# The whole thing


def discover(preferred: Target | None = None, serial_ports: Iterable[str] | None = None, hosts: Iterable[str] = (),
             baud_rates: Sequence[int] = BAUD_RATES, timeout: float = t.PROBE_TIMEOUT,
             opener: TransportOpener = default_opener, include_usb: bool = True, include_network: bool = True,
             scan_subnet: bool = False, workers: int = DEFAULT_WORKERS) -> Discovered | None:
	"""Find one mount, trying USB before the network."""
	if preferred is not None:
		found = probe_target(preferred, timeout, opener)
		if found is not None:
			log.info("reconnected to the remembered mount: %s", found.describe())
			return found
		log.info("remembered mount at %s did not answer; scanning", preferred.describe())

	if include_usb:
		for found in discover_serial(serial_ports, baud_rates, timeout, opener, workers, first_only=True):
			return found

	if include_network:
		for found in discover_network(hosts=hosts, timeout=timeout, opener=opener, scan_subnet=scan_subnet,
		                              workers=workers, first_only=True):
			return found

	log.warning("no OnStep mount found")
	return None


def discover_all(serial_ports: Iterable[str] | None = None, hosts: Iterable[str] = (),
                 baud_rates: Sequence[int] = BAUD_RATES, timeout: float = t.PROBE_TIMEOUT,
                 opener: TransportOpener = default_opener, scan_subnet: bool = False,
                 workers: int = DEFAULT_WORKERS) -> list[Discovered]:
	"""Every mount that answers, USB first. Used by the setup page's scan button."""
	found = list(discover_serial(serial_ports, baud_rates, timeout, opener, workers))
	found.extend(
		discover_network(hosts=hosts, timeout=timeout, opener=opener, scan_subnet=scan_subnet, workers=workers)
	)
	return found
