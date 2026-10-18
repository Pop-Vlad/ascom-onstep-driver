"""Start the driver when the user logs in, as a toggle the tray icon can flip.

A per-user ``Run`` key rather than a scheduled task or a service: it needs no admin
rights, takes effect immediately, and shows up in Task Manager's Startup tab, so the user
can turn it off from outside this program too. It only fires at interactive logon, which
is the right scope for a tray app -- anything needing to run before login wants a service
instead.
"""

from __future__ import annotations

import logging
import shlex
import sys
from pathlib import Path

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "OnStep Alpaca"

LAUNCHER = "onstep-alpaca-tray.exe"
"""The gui-script wrapper from pyproject.toml, which runs without a console window."""


def supported() -> bool:
	"""Only Windows has the Run key. Elsewhere this is systemd's or launchd's job."""
	return sys.platform == "win32"


def launch_command() -> str:
	"""The command line to register, which is always the windowless tray launcher.
	"""
	beside = Path(sys.executable).with_name(LAUNCHER)
	if beside.exists():
		return f'"{beside}"'

	if getattr(sys, "frozen", False):
		# A frozen build has no interpreter to fall back on, so the only honest answer left
		# is to re-run ourselves -- correct whenever the tray build is the one installed.
		return f'"{Path(sys.executable)}"'

	interpreter = Path(sys.executable)
	windowless = interpreter.with_name("pythonw.exe")
	if windowless.exists():
		interpreter = windowless
	return f'"{interpreter}" -m onstep_alpaca.tray'


class _RegistryStore:
	"""The real HKCU Run key. Separated so the toggle can be tested without touching it."""

	def read(self) -> str | None:
		import winreg

		try:
			with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
				value, _ = winreg.QueryValueEx(key, VALUE_NAME)
				return value
		except FileNotFoundError:
			return None

	def write(self, value: str) -> None:
		import winreg

		with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
			winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, value)

	def remove(self) -> None:
		import winreg

		try:
			with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
			                    winreg.KEY_SET_VALUE) as key:
				winreg.DeleteValue(key, VALUE_NAME)
		except FileNotFoundError:
			pass


def _default_store() -> _RegistryStore:
	return _RegistryStore()


def is_enabled(store=None) -> bool:
	if not supported():
		return False
	store = store or _default_store()
	try:
		return store.read() is not None
	except OSError as exc:  # pragma: no cover - a locked-down registry
		log.debug("could not read the autostart entry: %s", exc)
		return False


def enable(store=None) -> bool:
	"""Register the launcher. Returns whether it is now on."""
	if not supported():
		log.info("autostart at login is a Windows feature; use systemd or launchd instead")
		return False
	store = store or _default_store()
	try:
		store.write(launch_command())
	except OSError as exc:
		log.warning("could not enable autostart: %s", exc)
		return False
	log.info("autostart enabled: %s", launch_command())
	return True


def disable(store=None) -> bool:
	"""Remove the entry. Returns whether it is now on, so False means success."""
	if not supported():
		return False
	store = store or _default_store()
	try:
		store.remove()
	except OSError as exc:
		log.warning("could not disable autostart: %s", exc)
		return is_enabled(store)
	log.info("autostart disabled")
	return False


def set_enabled(enabled: bool, store=None) -> bool:
	return enable(store) if enabled else disable(store)


def describe() -> str:
	"""One line for the setup page and the CLI."""
	if not supported():
		return "not available on this platform"
	return f"on, running {shlex.quote(launch_command())}" if is_enabled() else "off"
