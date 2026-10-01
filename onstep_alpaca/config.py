"""Persisted settings, and the stable identity ASCOM requires.

JSON rather than TOML so the standard library can both read and write it; the intended
editing surface is the ``/setup`` page. ``unique_id`` is the field that matters: clients
store it to remember which mount a profile refers to, so it is generated once and never
changes.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_ALPACA_PORT = 11111
DISCOVERY_PORT = 32227
"""Fixed by the Alpaca discovery protocol; not ours to choose."""


def default_config_path() -> Path:
	"""Somewhere per-user and writable, so the driver needs no install step."""
	base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CONFIG_HOME")
	if base:
		return Path(base) / "onstep-alpaca" / "config.json"
	return Path.home() / ".config" / "onstep-alpaca" / "config.json"


@dataclass
class Config:
	# -- server
	alpaca_port: int = DEFAULT_ALPACA_PORT
	bind_address: str = "0.0.0.0"
	"""Default is every interface, so NINA or PHD2 on another machine can reach it. Set to
	127.0.0.1 to keep the driver local to this PC."""

	discovery_enabled: bool = True
	device_number: int = 0
	device_name: str = "OnStep"
	unique_id: str = ""
	location: str = ""

	# -- mount connection
	remembered_target: dict | None = None
	"""How the last successful connection was made, so reconnecting costs one probe instead
	of a scan. Written by the driver, not normally edited by hand."""

	hosts: list[str] = field(default_factory=list)
	"""Extra addresses to try before the firmware default, for a mount on a fixed IP."""

	scan_subnet: bool = False
	"""Sweep the local subnet when nothing else answers. Off because it is slow and the most
	intrusive thing the driver can do to a network."""

	prefer_usb: bool = True
	poll_interval: float = 0.25
	site_elevation: float = 0.0
	"""ASCOM wants an elevation; OnStep has nowhere to store one."""

	log_level: str = "INFO"

	def __post_init__(self):
		if not self.unique_id:
			# Generated once, then persisted forever: clients key their saved profiles
			# off this value.
			self.unique_id = f"onstep-alpaca-{uuid.uuid4()}"

	# -- persistence -------------------------------------------------------------

	@classmethod
	def load(cls, path: Path | None = None) -> "Config":
		"""Read the config, falling back to defaults for anything missing."""
		path = path or default_config_path()
		if not path.exists():
			log.info("no config at %s; using defaults", path)
			return cls()
		try:
			data = json.loads(path.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError) as exc:
			log.warning("could not read %s (%s); using defaults", path, exc)
			return cls()
		if not isinstance(data, dict):
			log.warning("%s does not contain an object; using defaults", path)
			return cls()

		known = {f for f in cls.__dataclass_fields__}
		unknown = set(data) - known
		if unknown:
			log.debug("ignoring unknown config keys: %s", ", ".join(sorted(unknown)))
		return cls(**{k: v for k, v in data.items() if k in known})

	def save(self, path: Path | None = None) -> Path:
		"""Write the config atomically, so an interrupted save cannot corrupt it."""
		path = path or default_config_path()
		path.parent.mkdir(parents=True, exist_ok=True)
		temporary = path.with_suffix(".json.tmp")
		temporary.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n", encoding="utf-8")
		temporary.replace(path)
		log.debug("saved config to %s", path)
		return path
