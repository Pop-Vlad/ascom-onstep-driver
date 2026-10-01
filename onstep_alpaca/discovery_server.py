"""The Alpaca discovery responder: how clients find this driver by themselves.

A client broadcasts ``alpacadiscovery1`` to UDP 32227 and every Alpaca server answers
with its REST port. Needs SO_REUSEADDR (several servers may share the port on one
machine) and must reply to the sender, not the broadcast address.
"""

from __future__ import annotations

import json
import logging
import socket
import threading

log = logging.getLogger(__name__)

DISCOVERY_PORT = 32227
DISCOVERY_REQUEST = b"alpacadiscovery1"
"""The exact payload clients broadcast. Anything else on the port is not for us."""


class DiscoveryServer:
	"""Answers Alpaca discovery broadcasts with this driver's REST port."""

	def __init__(self, alpaca_port: int, listen_port: int = DISCOVERY_PORT):
		self.alpaca_port = alpaca_port
		self.listen_port = listen_port
		self._socket: socket.socket | None = None
		self._thread: threading.Thread | None = None
		self._stop = threading.Event()

	def start(self) -> bool:
		"""Begin answering. Returns False if the port could not be opened."""
		try:
			sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
			sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
			sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
			sock.bind(("0.0.0.0", self.listen_port))
			sock.settimeout(0.3)
		except OSError as exc:
			log.warning("Alpaca discovery is unavailable on UDP %d (%s); clients will need ""the address typed in",
			            self.listen_port, exc)
			return False

		self._socket = sock
		self._stop.clear()
		self._thread = threading.Thread(target=self._serve, name="alpaca-discovery", daemon=True)
		self._thread.start()
		log.info("answering Alpaca discovery on UDP %d with port %d", self.listen_port, self.alpaca_port, )
		return True

	def stop(self) -> None:
		self._stop.set()
		thread, self._thread = self._thread, None
		if thread is not None:
			thread.join(timeout=2.0)
		if self._socket is not None:
			self._socket.close()
			self._socket = None

	def __enter__(self) -> "DiscoveryServer":
		self.start()
		return self

	def __exit__(self, *exc_info):
		self.stop()
		return False

	def _serve(self) -> None:
		reply = json.dumps({"AlpacaPort": self.alpaca_port}).encode("ascii")
		while not self._stop.is_set():
			try:
				data, sender = self._socket.recvfrom(1024)
			except (TimeoutError, socket.timeout):
				continue
			except OSError:
				return
			if DISCOVERY_REQUEST not in data:
				log.debug("ignoring %r on the discovery port from %s", data[:32], sender)
				continue
			try:
				# Back to the sender, not to the broadcast address.
				self._socket.sendto(reply, sender)
				log.debug("answered discovery from %s", sender)
			except OSError as exc:  # pragma: no cover - transient network failure
				log.debug("could not answer discovery from %s: %s", sender, exc)
