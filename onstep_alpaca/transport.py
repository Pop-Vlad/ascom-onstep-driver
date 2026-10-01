"""Byte-level links to an OnStep controller: USB serial, or TCP over the WiFi addon.

Reply framing lives here because it is a property of the protocol (see
``protocol.ReplyShape``) and all links share it.

The WiFi addon's two channels decide the transport shape: port 9999 force-disconnects
its client after 2000 ms (``WiFi-Bluetooth.ino:418``) so it must reconnect per command,
while 9998 holds for 120 s. Both accept exactly one client at a time.
"""

from __future__ import annotations

import abc
import enum
import logging
import socket
import time

from . import protocol
from .protocol import Cmd, ProtocolError, ReplyShape

log = logging.getLogger(__name__)

BAUD_RATES = (9600, 19200, 38400, 57600, 115200)
"""Rates to try when probing a serial port, slowest first because 9600 is OnStep's
``SERIAL_A_BAUD_DEFAULT`` (``Config.h:24``) and therefore the most likely to answer."""

STANDARD_COMMAND_PORT = 9999
PERSISTENT_COMMAND_PORT = 9998

ONESHOT_CLIENT_BUDGET_S = 2.0
"""How long port 9999 keeps a client before dropping it."""

OPTIONAL_FAILURE_GRACE = 0.08
"""How long to wait for a terminator after a lone ``0`` before calling it a refusal."""

OPTIONAL_POLL_INTERVAL = 0.01
"""Granularity of the grace-period wait."""


class TransportError(Exception):
	"""The link failed: not open, disconnected, or no reply within the timeout."""


class ReplyTimeout(TransportError):
	"""No complete reply arrived in time."""


class Transport(abc.ABC):
	"""A bidirectional byte link to one OnStep controller."""

	#: Human-readable identity, e.g. ``COM5 @ 9600`` -- used in logs and in the Alpaca
	#: driver's reported connection string.
	name: str = "unconfigured"

	@abc.abstractmethod
	def open(self) -> None:
		...

	@abc.abstractmethod
	def close(self) -> None:
		...

	@property
	@abc.abstractmethod
	def is_open(self) -> bool:
		...

	# -- subclass hooks -----------------------------------------------------------

	@abc.abstractmethod
	def _send(self, payload: bytes) -> None:
		...

	@abc.abstractmethod
	def _recv(self, max_bytes: int, timeout: float) -> bytes:
		"""Read up to ``max_bytes``, returning ``b""`` if nothing arrived in time."""

	@abc.abstractmethod
	def _drain(self) -> None:
		"""Discard buffered input left over from a previous, timed-out exchange."""

	def _begin_exchange(self) -> None:
		"""Hook for links that must connect per command (port 9999)."""

	def _end_exchange(self) -> None:
		"""Counterpart to :meth:`_begin_exchange`."""

	# -- the protocol ------------------------------------------------------------

	def exchange(self, cmd: Cmd) -> str | None:
		"""Send one command and read exactly the reply its shape promises."""
		if not self.is_open:
			raise TransportError(f"{self.name} is not open")

		self._begin_exchange()
		try:
			self._drain()
			log.debug("%s -> %s", self.name, cmd.text)
			self._send(cmd.text.encode("ascii"))

			if cmd.shape is ReplyShape.NONE:
				return None

			deadline = time.monotonic() + cmd.timeout
			if cmd.shape in (ReplyShape.BOOLEAN, ReplyShape.DIGIT):
				reply = self._read_one(deadline, cmd)
			elif cmd.shape is ReplyShape.OPTIONAL:
				reply = self._read_optional(deadline, cmd)
			else:
				reply = self._read_terminated(deadline, cmd)
			log.debug("%s <- %r", self.name, reply)
			return reply
		finally:
			self._end_exchange()

	def _read_one(self, deadline: float, cmd: Cmd) -> str:
		"""Read the single unterminated character of a boolean or digit reply."""
		while True:
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				raise ReplyTimeout(f"no reply to {cmd.text} within {cmd.timeout}s")
			chunk = self._recv(1, remaining)
			if not chunk:
				continue
			char = chunk.decode("ascii", "replace")
			if cmd.shape is ReplyShape.BOOLEAN and char not in "01":
				raise ProtocolError(f"{cmd.text} answered {char!r}, expected 0 or 1")
			if cmd.shape is ReplyShape.DIGIT and not char.isdigit():
				raise ProtocolError(f"{cmd.text} answered {char!r}, expected a digit")
			return char

	def _read_terminated(self, deadline: float, cmd: Cmd) -> str:
		"""Accumulate until ``#``."""
		buf = bytearray()
		while True:
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				raise ReplyTimeout(f"no terminated reply to {cmd.text} within {cmd.timeout}s" + (
					f" (partial: {bytes(buf)!r})" if buf else ""))
			chunk = self._recv(64, remaining)
			if not chunk:
				continue
			for index, byte in enumerate(chunk):
				if byte == 0x23:  # '#'
					if index + 1 < len(chunk):
						# Nothing should follow a terminator; if something does the link is out of
						# step, so say so rather than return plausible-looking data.
						log.warning("%s: %d stray byte(s) after terminator for %s", self.name, len(chunk) - index - 1,
						            cmd.text, )
					return buf.decode("ascii", "replace")
				buf.append(byte)
			if len(buf) > 128:
				raise ProtocolError(f"{cmd.text} reply exceeded 128 bytes with no terminator: " f"{bytes(buf[:64])!r}")

	def _read_optional(self, deadline: float, cmd: Cmd) -> str | None:
		"""Read a terminated reply, or detect the bare ``0`` that means "refused"."""
		buf = bytearray()
		grace_until: float | None = None
		while True:
			now = time.monotonic()
			if now > deadline:
				raise ReplyTimeout(
					f"no reply to {cmd.text} within {cmd.timeout}s" + (f" (partial: {bytes(buf)!r})" if buf else ""))
			chunk = self._recv(64, min(deadline - now, OPTIONAL_POLL_INTERVAL))
			if chunk:
				for index, byte in enumerate(chunk):
					if byte == 0x23:  # '#'
						return buf.decode("ascii", "replace")
					buf.append(byte)
				# A lone '0' might still be the start of "0#", so start the clock
				# rather than deciding now. Anything longer cannot be a refusal.
				grace_until = (
					time.monotonic() + OPTIONAL_FAILURE_GRACE
					if bytes(buf) == b"0"
					else None
				)
				continue
			if grace_until is not None and time.monotonic() >= grace_until:
				log.debug("%s: %s refused (bare 0)", self.name, cmd.text)
				return None
			if len(buf) > 128:
				raise ProtocolError(f"{cmd.text} reply exceeded 128 bytes with no terminator")

	# -- convenience -------------------------------------------------------------

	def __enter__(self):
		self.open()
		return self

	def __exit__(self, *exc_info):
		self.close()
		return False

	def __repr__(self) -> str:  # pragma: no cover - debugging aid
		state = "open" if self.is_open else "closed"
		return f"<{type(self).__name__} {self.name} {state}>"


class SerialTransport(Transport):
	"""USB serial link. The preferred transport: lower latency and no single-client
	contention with phone apps."""

	def __init__(self, port: str, baudrate: int = 9600):
		self.port = port
		self.baudrate = baudrate
		self.name = f"{port} @ {baudrate}"
		self._serial = None

	def open(self) -> None:
		import serial  # imported lazily so the package works without pyserial

		if self._serial is not None:
			return
		handle = serial.Serial()
		handle.port = self.port
		handle.baudrate = self.baudrate
		handle.timeout = 0  # non-blocking; the framing loop owns all timing
		handle.write_timeout = 2.0
		# Held low so opening the port does not reset an ESP32/Teensy controller.
		# Assigning before open() is honoured by pyserial and applied at open time.
		handle.dtr = False
		handle.rts = False
		handle.open()
		self._serial = handle
		log.info("opened %s", self.name)

	def close(self) -> None:
		if self._serial is not None:
			try:
				self._serial.close()
			except Exception:  # pragma: no cover - closing must not raise
				log.debug("error closing %s", self.name, exc_info=True)
			self._serial = None

	@property
	def is_open(self) -> bool:
		return self._serial is not None and self._serial.is_open

	def _send(self, payload: bytes) -> None:
		try:
			self._serial.write(payload)
			self._serial.flush()
		except Exception as exc:
			raise TransportError(f"{self.name} write failed: {exc}") from exc

	def _recv(self, max_bytes: int, timeout: float) -> bytes:
		# timeout=0 makes read() return immediately with whatever is buffered, so a
		# short sleep is what actually paces this loop and keeps it off a busy spin.
		try:
			data = self._serial.read(max_bytes)
		except Exception as exc:
			raise TransportError(f"{self.name} read failed: {exc}") from exc
		if not data:
			time.sleep(min(0.005, max(timeout, 0.0)))
		return data

	def _drain(self) -> None:
		try:
			self._serial.reset_input_buffer()
		except Exception:  # pragma: no cover - a failed drain is not fatal
			log.debug("could not drain %s", self.name, exc_info=True)


class TcpMode(enum.Enum):
	PERSISTENT = "persistent"
	"""Port 9998. One connection held across commands."""

	ONESHOT = "oneshot"
	"""Port 9999. Reconnect per command, because the firmware drops the client after 2 s
	whether it is idle or not."""


class TcpTransport(Transport):
	"""WiFi link to the ESP addon's command channel."""

	def __init__(self, host: str, port: int = PERSISTENT_COMMAND_PORT, mode: TcpMode | None = None,
	             connect_timeout: float = 3.0):
		self.host = host
		self.port = port
		self.mode = mode or (
			TcpMode.ONESHOT if port == STANDARD_COMMAND_PORT else TcpMode.PERSISTENT
		)
		self.connect_timeout = connect_timeout
		self.name = f"{host}:{port} ({self.mode.value})"
		self._sock: socket.socket | None = None
		self._open = False
		self._connected_at = 0.0

	def open(self) -> None:
		self._open = True
		if self.mode is TcpMode.PERSISTENT:
			self._connect()
		else:
			# Prove the endpoint answers now rather than at the first real command, so
			# a wrong address surfaces as a connect error instead of a timeout.
			self._connect()
			self._disconnect()
		log.info("opened %s", self.name)

	def close(self) -> None:
		self._open = False
		self._disconnect()

	@property
	def is_open(self) -> bool:
		return self._open

	def _connect(self) -> None:
		try:
			sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
		except OSError as exc:
			raise TransportError(f"cannot reach {self.host}:{self.port}: {exc}") from exc
		sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
		sock.settimeout(0.0)
		self._sock = sock
		self._connected_at = time.monotonic()

	def _disconnect(self) -> None:
		if self._sock is not None:
			try:
				self._sock.close()
			except OSError:  # pragma: no cover - closing must not raise
				pass
			self._sock = None

	def _begin_exchange(self) -> None:
		if self.mode is TcpMode.ONESHOT:
			self._connect()
			return
		# Persistent mode: the 120 s idle timer is generous next to our poll interval,
		# but reconnect anyway if the peer went away (addon reboot, WiFi drop).
		if self._sock is None:
			self._connect()

	def _end_exchange(self) -> None:
		if self.mode is TcpMode.ONESHOT:
			self._disconnect()

	def _send(self, payload: bytes) -> None:
		if self._sock is None:
			raise TransportError(f"{self.name} has no connection")
		try:
			self._sock.sendall(payload)
		except OSError as exc:
			self._disconnect()
			raise TransportError(f"{self.name} send failed: {exc}") from exc

	def _recv(self, max_bytes: int, timeout: float) -> bytes:
		if self._sock is None:
			raise TransportError(f"{self.name} has no connection")
		if self.mode is TcpMode.ONESHOT:
			elapsed = time.monotonic() - self._connected_at
			if elapsed > ONESHOT_CLIENT_BUDGET_S:
				raise ReplyTimeout(f"{self.name}: firmware drops the client after "
				                   f"{ONESHOT_CLIENT_BUDGET_S}s and {elapsed:.1f}s have passed")
		try:
			self._sock.settimeout(max(timeout, 0.001))
			data = self._sock.recv(max_bytes)
		except (TimeoutError, socket.timeout):
			return b""
		except BlockingIOError:
			return b""
		except OSError as exc:
			self._disconnect()
			raise TransportError(f"{self.name} recv failed: {exc}") from exc
		if not data:
			self._disconnect()
			raise TransportError(f"{self.name}: peer closed the connection")
		return data

	def _drain(self) -> None:
		if self._sock is None or self.mode is TcpMode.ONESHOT:
			return  # a fresh connection has nothing stale in it
		try:
			self._sock.settimeout(0.0)
			while self._sock.recv(256):
				pass
		except (BlockingIOError, TimeoutError, socket.timeout, OSError):
			pass


PROBE_TIMEOUT = 0.6
"""Auto-detection walks every port and address, so the per-try budget must be small. A real
OnStep answers ``:GVP#`` from a string constant well inside this even at 9600."""


def probe(transport: Transport, timeout: float = PROBE_TIMEOUT) -> dict[str, str] | None:
	"""Ask an open transport whether an OnStep is on the other end."""
	identify = Cmd(protocol.GET_PRODUCT_NAME.text, ReplyShape.TERMINATED, timeout)
	try:
		name = transport.exchange(identify)
	except (TransportError, ProtocolError) as exc:
		log.debug("probe of %s failed: %s", transport.name, exc)
		return None
	if not name or "onstep" not in name.lower().replace(" ", "").replace("-", ""):
		log.debug("probe of %s: not OnStep (%r)", transport.name, name)
		return None

	identity = {"product": name}
	for key, cmd in (
			("version", protocol.GET_FIRMWARE_NUMBER),
			("date", protocol.GET_FIRMWARE_DATE),
			("time", protocol.GET_FIRMWARE_TIME),
	):
		try:
			value = transport.exchange(Cmd(cmd.text, ReplyShape.TERMINATED, timeout))
		except (TransportError, ProtocolError):
			continue
		if value:
			identity[key] = value
	return identity
