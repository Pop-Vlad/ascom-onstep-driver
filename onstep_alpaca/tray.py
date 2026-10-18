"""Tray icon for the driver, so it can be left running without a terminal.
"""

from __future__ import annotations

import logging
import sys
import threading
import webbrowser
from pathlib import Path

from . import autostart
from .__main__ import (
	Service,
	apply_overrides,
	build_parser,
	build_service,
	configure_logging,
	default_log_path,
)
from .config import Config, default_config_path

log = logging.getLogger("onstep_alpaca.tray")

POLL_SECONDS = 2.0
"""How often the icon re-reads the driver's state. Cached reads cost ~0.2 us, so this is
free; it only needs to be fast enough to look live."""

IDLE = (0x9E, 0xA3, 0xAA)
CONNECTED = (0x2E, 0xA0, 0x43)
FAULT = (0xD1, 0x34, 0x2F)


def _image(colour, size: int = 64):
	"""A filled circle in the given colour, drawn rather than shipped as a file."""
	from PIL import Image, ImageDraw

	image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
	draw = ImageDraw.Draw(image)
	margin = size // 8
	draw.ellipse((margin, margin, size - margin, size - margin), fill=colour + (255,))
	return image


def _state(service: Service) -> tuple[tuple[int, int, int], str]:
	"""Icon colour and tooltip for how the driver is doing right now."""
	scope = service.scope
	try:
		if not scope.connected:
			return IDLE, f"OnStep Alpaca - idle\n{service.url}"
		link = scope._require_link()
		if link.fault is not None:
			return FAULT, f"OnStep Alpaca - link fault\n{link.fault}"
		return CONNECTED, f"OnStep Alpaca - {link.info.description}\n{service.url}"
	except Exception as exc:  # pragma: no cover - the icon must never take the driver down
		return FAULT, f"OnStep Alpaca - {exc}"


def _build_icon(service: Service):
	import pystray

	def on_setup(icon, item):
		webbrowser.open(service.url.rstrip("/") + "/setup")

	def on_logs(icon, item):
		path = Path(service.config.log_file or default_log_path())
		webbrowser.open(str(path.parent))

	def autostart_checked(item):
		return autostart.is_enabled()

	def on_autostart(icon, item):
		autostart.set_enabled(not autostart.is_enabled())

	def on_quit(icon, item):
		log.info("quitting from the tray")
		icon.stop()

	menu = [
		pystray.MenuItem(lambda item: _state(service)[1].splitlines()[0],
		                 lambda icon, item: None, enabled=False),
		pystray.Menu.SEPARATOR,
		pystray.MenuItem("Open setup page", on_setup, default=True),
		pystray.MenuItem("Open log folder", on_logs),
		pystray.Menu.SEPARATOR,
		pystray.MenuItem("Start at login", on_autostart, checked=autostart_checked,
		                 enabled=autostart.supported()),
		pystray.Menu.SEPARATOR,
		pystray.MenuItem("Quit", on_quit),
	]
	colour, title = _state(service)
	return pystray.Icon("onstep-alpaca", _image(colour), title, pystray.Menu(*menu))


def _watch(icon, service: Service, stop: threading.Event) -> None:
	"""Keep the colour and tooltip honest while the driver runs."""
	current = None
	while not stop.wait(POLL_SECONDS):
		colour, title = _state(service)
		if colour != current:
			icon.icon = _image(colour)
			current = colour
		icon.title = title


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)

	persisted = Config.load(args.config or default_config_path())
	config = apply_overrides(persisted, args)
	# Windowless means no console, so a log file is the only way to see anything go wrong.
	if not config.log_file:
		config.log_file = str(default_log_path())
		args.log_file = config.log_file
	configure_logging(config)

	service = build_service(args)
	if service is None:
		log.error("not starting a second tray icon")
		return 1

	try:
		import pystray  # noqa: F401
		from PIL import Image  # noqa: F401
	except ImportError:
		log.warning("pystray and pillow are not installed, serving without a tray icon "
		            "(pip install 'onstep-alpaca[tray]' to get one)")
		return service.run()

	service.announce()
	worker = threading.Thread(target=service.serve, name="alpaca-server", daemon=True)
	worker.start()

	icon = _build_icon(service)
	stop = threading.Event()
	watcher = threading.Thread(target=_watch, args=(icon, service, stop),
	                           name="tray-watch", daemon=True)
	watcher.start()
	try:
		icon.run()
	finally:
		stop.set()
		service.shutdown()
	return 0


if __name__ == "__main__":  # pragma: no cover - manual launch
	sys.exit(main())
