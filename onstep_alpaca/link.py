"""The owner thread: one holder of the link, a request queue, and a status cache.

OnStep's serial default is 9600 baud and its WiFi channel costs a TCP connect per
command, while NINA and PHD2 both poll continuously. So reads and writes are separated:
writes go through a queue served by one thread (OnStep answers one command at a time,
with no identifier to match replies against), and reads come from a cached snapshot the
same thread refreshes whenever the queue is empty.
"""

from __future__ import annotations

import enum
import logging
import queue
import threading
import time
from dataclasses import dataclass, replace

from . import protocol, transport
from .protocol import Cmd, CommandError, CommandRejected, CoordinateMode, MountType, ProtocolError, ReplyShape, Status
from .transport import Transport, TransportError

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 0.25
"""Seconds between status refreshes. OnStep caches its own coordinates for 50 ms (500 ms on
an AVR), so polling faster only steals link time from real commands."""

RECONNECT_BACKOFF = 2.0
"""Seconds to wait before retrying a link that failed."""

MAX_QUEUE_WAIT = 0.1
"""Longest the owner thread blocks on its queue in one go. ``queue.get`` cannot watch the
stop event, so this bounds how long ``disconnect()`` takes."""

_SHUTDOWN = object()
"""Sentinel queued by :meth:`MountLink.disconnect` to wake the thread immediately."""

SLOW_GROUP = (protocol.GET_ALTITUDE, protocol.GET_AZIMUTH, protocol.GET_SIDEREAL_TIME)
"""One of these per cycle, round-robin. Nothing polls them hard, and alt/az is derivable from
the equatorial position anyway, so a refresh every few cycles is ample."""


class LinkError(Exception):
	"""The link is unusable: not connected, or it failed and has not recovered."""


class Motion(enum.Enum):
	"""What a command does to the mount's motion, for snapshot-staleness purposes."""

	START = "start"
	"""Begins motion. Hold ``slewing`` True until a newer sample proves otherwise."""

	STOP = "stop"
	"""Ends motion. Drop any latch so ``slewing`` can go False immediately."""


@dataclass(frozen=True, slots=True)
class MountInfo:
	"""What the mount is. Read once at connect; none of it changes while running."""

	product: str
	firmware_version: str
	firmware_date: str
	mount_type: MountType
	coordinate_mode: CoordinateMode
	transport_name: str
	is_onstepx: bool = False
	meridian_limit_east_minutes: float | None = None
	meridian_limit_west_minutes: float | None = None
	dec_limit_min: float | None = None
	dec_limit_max: float | None = None
	steps_per_degree_axis1: float | None = None
	steps_per_degree_axis2: float | None = None
	max_rate_ms: float | None = None
	site_latitude: float = 0.0
	site_longitude: float = 0.0
	time_zone: float = 0.0

	@property
	def is_gem(self) -> bool:
		"""Only a GEM has a pier side, a meridian to flip across, or a counterweight
		orientation -- several ASCOM capability flags hang off this."""
		return self.mount_type is MountType.GEM

	@property
	def description(self) -> str:
		return f"{self.product} {self.firmware_version} ({self.mount_type.name})"


@dataclass(frozen=True, slots=True)
class MountState:
	"""One coherent observation of the mount. Immutable, so readers never tear."""

	status: Status
	ra_hours: float
	dec_degrees: float
	altitude: float
	azimuth: float
	sidereal_time: float
	sampled_at: float
	"""``time.monotonic()`` when the fast group finished. The motion latch compares against
	this, so it must mean "status is at least this fresh"."""

	@property
	def age(self) -> float:
		return time.monotonic() - self.sampled_at


class MountLink:
	"""Owns a :class:`~onstep_alpaca.transport.Transport` and everything on it."""

	def __init__(self, link_transport: Transport, poll_interval: float = DEFAULT_POLL_INTERVAL, ):
		self._transport = link_transport
		self.poll_interval = poll_interval

		self._queue: queue.Queue[_Request] = queue.Queue()
		self._thread: threading.Thread | None = None
		self._stop = threading.Event()

		self._info: MountInfo | None = None
		self._state: MountState | None = None
		self._next_poll = 0.0
		self._slow_index = 0

		self._motion_started_at = 0.0
		self._pulse_deadline = 0.0

		self._fault: BaseException | None = None
		self._fault_at = 0.0

		#: Count of commands actually put on the wire, for diagnostics and for tests
		#: that assert the cache is doing its job.
		self.commands_sent = 0

	# -- lifecycle ---------------------------------------------------------------

	def connect(self) -> MountInfo:
		"""Open the link, identify the mount, read its capabilities, start polling."""
		if self._thread is not None:
			return self.info

		self._transport.open()
		try:
			identity = transport.probe(self._transport)
			if identity is None:
				raise LinkError(f"{self._transport.name} did not identify as an OnStep mount")
			self._info = self._read_capabilities(identity)
			self._poll_cycle(read_all_slow=True)
			if self._state is None:
				# _poll_cycle records faults rather than raising, so connect has to
				# check: a mount that identifies but cannot be read is not connected.
				raise LinkError(
					f"{self._transport.name} identified as "f"{self._info.description} but its state could not be read: "f"{self._fault}")
		except Exception:
			self._transport.close()
			self._info = None
			self._state = None
			raise

		self._stop.clear()
		self._thread = threading.Thread(target=self._run, name="onstep-link", daemon=True)
		self._thread.start()
		log.info("connected to %s via %s", self.info.description, self._transport.name)
		return self.info

	def disconnect(self) -> None:
		self._stop.set()
		thread, self._thread = self._thread, None
		if thread is not None:
			self._queue.put(_SHUTDOWN)  # wake it now rather than after MAX_QUEUE_WAIT
			thread.join(timeout=5.0)
			if thread.is_alive():  # pragma: no cover - the worker never blocks forever
				log.warning("link thread did not stop within 5s")
		self._transport.close()
		self._info = None
		self._state = None

	@property
	def connected(self) -> bool:
		return self._thread is not None and self._info is not None

	@property
	def info(self) -> MountInfo:
		info = self._info
		if info is None:
			raise LinkError("not connected")
		return info

	@property
	def state(self) -> MountState:
		"""The most recent observation. Never blocks; never None while connected."""
		state = self._state
		if state is None:
			raise LinkError("not connected")
		return state

	@property
	def fault(self) -> BaseException | None:
		"""The last link failure, if the link is currently broken."""
		return self._fault

	def note_site(self, latitude: float | None = None, longitude: float | None = None) -> None:
		"""Record a site change already written to the mount (longitude east-positive)."""
		updates: dict[str, float] = {}
		if latitude is not None:
			updates["site_latitude"] = latitude
		if longitude is not None:
			updates["site_longitude"] = longitude
		if updates:
			self._info = replace(self.info, **updates)

	def __enter__(self) -> "MountLink":
		self.connect()
		return self

	def __exit__(self, *exc_info):
		self.disconnect()
		return False

	# -- motion-aware state ------------------------------------------------------

	@property
	def slewing(self) -> bool:
		"""True while the mount is moving under a goto, park or home command."""
		state = self.state
		if state.sampled_at < self._motion_started_at:
			return True
		return state.status.slewing

	@property
	def is_pulse_guiding(self) -> bool:
		"""True while a guide pulse is outstanding."""
		now = time.monotonic()
		if now < self._pulse_deadline:
			return True
		state = self.state
		if state.sampled_at <= self._pulse_deadline:
			return False
		return state.status.guiding

	# -- commands ----------------------------------------------------------------

	def execute(self, cmd: Cmd, *, motion: Motion | None = None, timeout: float | None = None) -> str | None:
		"""Send one command and return its reply, raising if the mount refuses it."""
		if self._thread is None:
			raise LinkError("not connected")
		if threading.current_thread() is self._thread:
			raise LinkError("execute() called from the link thread would deadlock")

		request = _Request(cmd=cmd, motion=motion)
		self._queue.put(request)
		budget = timeout if timeout is not None else cmd.timeout * 2 + 5.0
		if not request.done.wait(budget):
			raise LinkError(f"timed out waiting for the link to run {cmd.text}")
		if request.error is not None:
			raise request.error
		return request.result

	def execute_all(self, *cmds: Cmd, motion: Motion | None = None) -> list[str | None]:
		"""Run several commands back to back, aborting at the first refusal."""
		if not cmds:
			return []
		request = _Request(cmd=cmds[0], extra=cmds[1:], motion=motion)
		if self._thread is None:
			raise LinkError("not connected")
		if threading.current_thread() is self._thread:
			raise LinkError("execute_all() called from the link thread would deadlock")
		self._queue.put(request)
		budget = sum(c.timeout for c in cmds) * 2 + 5.0
		if not request.done.wait(budget):
			raise LinkError("timed out waiting for the link")
		if request.error is not None:
			raise request.error
		return request.results

	def pulse_guide(self, direction: int, duration_ms: int) -> None:
		"""Issue a guide pulse and start the local ``IsPulseGuiding`` clock."""
		cmd = protocol.pulse_guide(direction, duration_ms)
		self.execute(cmd)
		self._pulse_deadline = time.monotonic() + duration_ms / 1000.0

	def refresh(self, timeout: float = 3.0) -> MountState:
		"""Force a status sample and wait for it. Used where staleness is unacceptable."""
		if self._thread is None:
			raise LinkError("not connected")
		request = _Request(cmd=None)
		self._queue.put(request)
		if not request.done.wait(timeout):
			raise LinkError("timed out waiting for a refresh")
		if request.error is not None:
			raise request.error
		return self.state

	# -- the worker --------------------------------------------------------------

	def _run(self) -> None:
		while not self._stop.is_set():
			# Capped rather than the full time to the next poll: queue.get() does not watch
			# the stop event, so a long interval would stall disconnect() for all of it.
			due_in = max(0.0, self._next_poll - time.monotonic())
			try:
				request = self._queue.get(timeout=min(due_in, MAX_QUEUE_WAIT))
			except queue.Empty:
				if time.monotonic() >= self._next_poll:
					self._poll_cycle()
				continue
			if request is _SHUTDOWN:
				return
			# Requests always win over polling: the loop comes back here immediately
			# and only polls once the queue has drained.
			try:
				self._serve(request)
			finally:
				request.done.set()

	def _serve(self, request: "_Request") -> None:
		try:
			self._ensure_link()
			if request.cmd is None:  # a bare refresh
				self._poll_cycle()
				return

			for cmd in (request.cmd, *request.extra):
				request.results.append(self._exchange_checked(cmd))

			if request.motion is Motion.START:
				# Stamp the latch and force the next cycle to run now, so `slewing`
				# becomes trustworthy after one round trip rather than one interval.
				self._motion_started_at = time.monotonic()
				self._next_poll = 0.0
			elif request.motion is Motion.STOP:
				self._motion_started_at = 0.0
				self._next_poll = 0.0
		except Exception as exc:  # delivered to the caller, not swallowed
			request.error = exc
			if isinstance(exc, TransportError):
				self._note_fault(exc)

	def _exchange_checked(self, cmd: Cmd) -> str | None:
		"""One exchange, with the mount's refusals turned into exceptions."""
		reply = self._raw_exchange(cmd)

		if cmd.shape is ReplyShape.BOOLEAN and reply == "0":
			raise CommandRejected(cmd.text, self._query_last_error())
		if cmd.shape is ReplyShape.DIGIT and reply is not None and reply != "0":
			raise CommandRejected(
				cmd.text, protocol.GOTO_RESULT_ERRORS.get(reply)
			)
		return reply

	def _query_last_error(self) -> CommandError | None:
		"""Ask ``:GE#`` why the previous command was refused."""
		try:
			reply = self._raw_exchange(protocol.GET_LAST_ERROR)
		except (TransportError, ProtocolError):
			return None
		if not reply:
			return None
		try:
			return protocol.parse_last_error(reply)
		except ProtocolError:
			return None

	def _raw_exchange(self, cmd: Cmd) -> str | None:
		self.commands_sent += 1
		return self._transport.exchange(cmd)

	def _poll_cycle(self, read_all_slow: bool = False) -> None:
		"""Refresh the cache: status, RA and Dec every time, plus one slow field."""
		self._next_poll = time.monotonic() + self.poll_interval
		try:
			self._ensure_link()
			status = protocol.parse_status(self._raw_exchange(protocol.GET_STATUS))
			ra = protocol.parse_hms(self._raw_exchange(protocol.GET_RA))
			dec = protocol.parse_dms(self._raw_exchange(protocol.GET_DEC))
			sampled_at = time.monotonic()

			previous = self._state
			state = MountState(status=status, ra_hours=ra, dec_degrees=dec,
			                   altitude=previous.altitude if previous else 0.0,
			                   azimuth=previous.azimuth if previous else 0.0,
			                   sidereal_time=previous.sidereal_time if previous else 0.0, sampled_at=sampled_at)
			self._state = state
			self._state = self._poll_slow(state, read_all_slow)
			self._clear_fault()
		except (TransportError, ProtocolError) as exc:
			self._note_fault(exc)
		except LinkError as exc:
			# Down and still inside its backoff; _ensure_link already recorded the fault.
			# Must not escape, or one dropout would kill the owner thread for good.
			log.debug("poll skipped: %s", exc)

	def _poll_slow(self, state: MountState, read_all: bool) -> MountState:
		"""Read one slow field (or all of them, at connect)."""
		fields = (
			SLOW_GROUP
			if read_all
			else (SLOW_GROUP[self._slow_index % len(SLOW_GROUP)],)
		)
		self._slow_index += 1
		updates: dict[str, float] = {}
		for cmd in fields:
			reply = self._raw_exchange(cmd)
			if reply is None:
				continue
			if cmd is protocol.GET_ALTITUDE:
				updates["altitude"] = protocol.parse_dms(reply)
			elif cmd is protocol.GET_AZIMUTH:
				# :GZ# is unsigned DDD*MM; parse_dms handles the missing sign.
				updates["azimuth"] = protocol.parse_dms(reply) % 360.0
			elif cmd is protocol.GET_SIDEREAL_TIME:
				updates["sidereal_time"] = protocol.parse_hms(reply)
		return replace(state, **updates) if updates else state

	# -- capabilities ------------------------------------------------------------

	def _read_capabilities(self, identity: dict[str, str]) -> MountInfo:
		"""Read the fixed properties once, so nothing has to ask again later."""
		product = identity.get("product", "OnStep")
		version = identity.get("version", "unknown")
		is_onstepx = "onstepx" in product.lower().replace(" ", "").replace("-", "")

		status = protocol.parse_status(self._raw_exchange(protocol.GET_STATUS))

		mode = CoordinateMode.TOPOCENTRIC
		raw_mode = self._optional(protocol.GET_COORDINATE_MODE)
		if raw_mode is not None:
			try:
				mode = CoordinateMode(int(raw_mode))
			except ValueError:
				log.warning("unrecognised coordinate mode %r; assuming topocentric", raw_mode)

		info = MountInfo(
			product=product,
			firmware_version=version,
			firmware_date=identity.get("date", ""),
			mount_type=status.mount_type,
			coordinate_mode=mode,
			transport_name=self._transport.name,
			is_onstepx=is_onstepx,
			dec_limit_min=self._optional_float(protocol.GET_DEC_LIMIT_MIN),
			dec_limit_max=self._optional_float(protocol.GET_DEC_LIMIT_MAX),
			steps_per_degree_axis1=self._optional_float(protocol.GET_AXIS1_STEPS_PER_DEG),
			steps_per_degree_axis2=self._optional_float(protocol.GET_AXIS2_STEPS_PER_DEG),
			max_rate_ms=self._optional_float(protocol.GET_MAX_RATE),
			site_latitude=self._optional_dms(protocol.GET_LATITUDE) or 0.0,
			# OnStep reports longitude west-positive; ASCOM wants east-positive.
			site_longitude=-(self._optional_dms(protocol.GET_LONGITUDE) or 0.0),
			time_zone=self._optional_zone(protocol.GET_UTC_OFFSET),
		)

		if status.mount_type is MountType.GEM:
			# Only compiled in for GEM builds; asking a fork mount returns an error.
			info = replace(info,
			               meridian_limit_east_minutes=self._optional_float(protocol.GET_MERIDIAN_LIMIT_EAST),
			               meridian_limit_west_minutes=self._optional_float(protocol.GET_MERIDIAN_LIMIT_WEST),
			               )
		return info

	def _optional(self, cmd: Cmd) -> str | None:
		"""Read a value the firmware build may not implement."""
		try:
			reply = self._raw_exchange(cmd)
		except (TransportError, ProtocolError) as exc:
			log.debug("%s unavailable: %s", cmd.text, exc)
			return None
		if reply in (None, "", "0"):
			return None
		return reply

	def _optional_float(self, cmd: Cmd) -> float | None:
		reply = self._optional(cmd)
		if reply is None:
			return None
		try:
			return float(reply)
		except ValueError:
			log.debug("%s returned non-numeric %r", cmd.text, reply)
			return None

	def _optional_dms(self, cmd: Cmd) -> float | None:
		reply = self._optional(cmd)
		if reply is None:
			return None
		try:
			return protocol.parse_dms(reply)
		except ProtocolError:
			return None

	def _optional_zone(self, cmd: Cmd) -> float:
		reply = self._optional(cmd)
		if reply is None:
			return 0.0
		try:
			return protocol.parse_time_zone(reply)
		except ProtocolError:
			return 0.0

	# -- faults ------------------------------------------------------------------

	def _ensure_link(self) -> None:
		"""Reopen a link that failed, at most once per backoff interval."""
		if self._transport.is_open:
			return
		if time.monotonic() - self._fault_at < RECONNECT_BACKOFF:
			raise LinkError(f"link is down: {self._fault}")
		log.info("reopening %s", self._transport.name)
		try:
			self._transport.open()
		except TransportError as exc:
			self._note_fault(exc)
			raise LinkError(f"cannot reopen the link: {exc}") from exc
		self._clear_fault()

	def _note_fault(self, exc: BaseException) -> None:
		if self._fault is None:
			log.warning("link fault on %s: %s", self._transport.name, exc)
		self._fault = exc
		self._fault_at = time.monotonic()
		self._transport.close()

	def _clear_fault(self) -> None:
		if self._fault is not None:
			log.info("link recovered on %s", self._transport.name)
		self._fault = None


@dataclass
class _Request:
	"""One unit of work for the owner thread."""

	cmd: Cmd | None
	"""``None`` means "just refresh the cache"."""

	extra: tuple[Cmd, ...] = ()
	motion: Motion | None = None
	results: list[str | None] = None
	error: BaseException | None = None
	done: threading.Event = None

	def __post_init__(self):
		if self.results is None:
			self.results = []
		if self.done is None:
			self.done = threading.Event()

	@property
	def result(self) -> str | None:
		return self.results[0] if self.results else None
