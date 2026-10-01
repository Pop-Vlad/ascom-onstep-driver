"""The ``/setup`` page Alpaca expects every device to serve.

One self-contained HTML string: no templates, no static files, no JavaScript build, so
it works from whatever browser is to hand over a LAN. Read-only -- a form that could
reconfigure the mount mid-session is a good way to lose an exposure.
"""

from __future__ import annotations

import html

from .config import Config
from .telescope import Telescope

_STYLE = """
:root { color-scheme: light dark; }
body { font: 15px/1.55 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
       margin: 0; padding: 2rem 1.25rem; background: #f7f7f8; color: #16181d; }
@media (prefers-color-scheme: dark) {
  body { background: #15171c; color: #e8eaed; }
  .card { background: #1e2127 !important; border-color: #2c313a !important; }
  th { color: #9aa3b0 !important; }
  code { background: #2a2f38 !important; }
}
main { max-width: 46rem; margin: 0 auto; }
h1 { font-size: 1.35rem; margin: 0 0 .25rem; }
.sub { color: #6b7280; margin: 0 0 1.5rem; }
.card { background: #fff; border: 1px solid #e4e6ea; border-radius: 10px;
        padding: 1rem 1.15rem; margin-bottom: 1rem; }
h2 { font-size: .8rem; text-transform: uppercase; letter-spacing: .06em;
     color: #6b7280; margin: 0 0 .75rem; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: .3rem 0; vertical-align: top; }
th { font-weight: 500; color: #6b7280; width: 42%; }
code { background: #f0f1f3; padding: .1rem .35rem; border-radius: 4px;
       font-size: .9em; }
.pill { display: inline-block; padding: .12rem .5rem; border-radius: 999px;
        font-size: .8rem; font-weight: 500; }
.ok { background: #dcfce7; color: #14532d; }
.off { background: #f1f3f5; color: #495057; }
.warn { background: #fef3c7; color: #713f12; }
"""


def _row(label: str, value: object) -> str:
	return f"<tr><th>{html.escape(label)}</th><td>{value}</td></tr>"


def _text(value: object) -> str:
	return html.escape(str(value))


def _code(value: object) -> str:
	return f"<code>{html.escape(str(value))}</code>"


def render(scope: Telescope, config: Config, alpaca_port: int) -> str:
	"""Build the page. Must never raise: it is the thing people open when the driver is
	misbehaving, so it has to work while disconnected or faulted."""
	connected = scope.connected

	status = (
		'<span class="pill ok">connected</span>'
		if connected
		else '<span class="pill off">not connected</span>'
	)

	server_rows = [
		_row("Status", status),
		_row("Alpaca API", _code(f"http://<this-host>:{alpaca_port}/api/v1/telescope/"
		                         f"{config.device_number}/")),
		_row("Device number", _code(config.device_number)),
		_row("Unique ID", _code(config.unique_id)),
		_row(
			"Discovery",
			'<span class="pill ok">on</span>'
			if config.discovery_enabled
			else '<span class="pill off">off</span>',
		),
		_row("Driver", _text(scope.driver_version)),
	]

	mount_rows: list[str] = []
	if connected:
		try:
			link = scope._require_link()
			info = link.info
			state = link.state
			mount_rows = [
				_row("Firmware", _text(f"{info.product} {info.firmware_version}")),
				_row("Mount type", _text(info.mount_type.name)),
				_row("Connection", _code(info.transport_name)),
				_row("Coordinate frame", _text(info.coordinate_mode.name)),
				_row(
					"Meridian",
					_text("never flips (tracks through)")
					if scope.never_flips
					else _text("flips at the configured limit"),
				),
				_row(
					"Site",
					_text(
						f"{info.site_latitude:+.3f}°, {info.site_longitude:+.3f}° "
						f"(east positive)"
					),
				),
				_row(
					"Position",
					_text(
						f"RA {state.ra_hours:.4f} h, Dec {state.dec_degrees:+.4f}°"
					),
				),
				_row(
					"Alt / Az",
					_text(f"{state.altitude:+.2f}° / {state.azimuth:.2f}°"),
				),
				_row("Sidereal time", _text(f"{state.sidereal_time:.4f} h")),
				_row(
					"Tracking",
					'<span class="pill ok">on</span>'
					if state.status.tracking
					else '<span class="pill off">off</span>',
				),
				_row(
					"Slewing",
					_text("yes") if link.slewing else _text("no"),
				),
				_row("Park state", _text(state.status.park.name.replace("_", " ").lower())),
				_row("Commands sent", _text(link.commands_sent)),
				_row("Snapshot age", _text(f"{state.age:.2f} s")),
			]
			if link.fault is not None:
				mount_rows.insert(
					0,
					_row(
						"Link",
						f'<span class="pill warn">reconnecting</span> '
						f"{_text(link.fault)}",
					),
				)
			error = state.status.general_error
			if error:
				from .protocol import GENERAL_ERROR_TEXT

				mount_rows.insert(
					0,
					_row(
						"Mount error",
						f'<span class="pill warn">'
						f"{_text(GENERAL_ERROR_TEXT.get(error, error))}</span>",
					),
				)
		except Exception as exc:  # the page must render even when everything is wrong
			mount_rows = [_row("Error", _text(exc))]
	else:
		remembered = config.remembered_target
		mount_rows = [
			_row(
				"Last known mount",
				_code(remembered) if remembered else _text("none recorded yet"),
			),
			_row(
				"Connect",
				"set <code>Connected</code> to true from your ASCOM or Alpaca client",
			),
		]

	config_rows = [
		_row("USB tried first", _text(config.prefer_usb)),
		_row("Extra hosts", _code(", ".join(config.hosts)) if config.hosts else _text("none")),
		_row("Subnet sweep", _text(config.scan_subnet)),
		_row("Poll interval", _text(f"{config.poll_interval:.2f} s")),
		_row("Site elevation", _text(f"{config.site_elevation:.0f} m")),
	]

	return f"""<title>OnStep Alpaca Driver</title>
<style>{_STYLE}</style>
<main>
  <h1>OnStep Alpaca Driver</h1>
  <p class="sub">ASCOM Alpaca telescope driver for OnStep and OnStepX mounts.</p>

  <div class="card">
    <h2>Server</h2>
    <table>{"".join(server_rows)}</table>
  </div>

  <div class="card">
    <h2>Mount</h2>
    <table>{"".join(mount_rows)}</table>
  </div>

  <div class="card">
    <h2>Settings</h2>
    <table>{"".join(config_rows)}</table>
    <p class="sub" style="margin:.75rem 0 0">
      Edit these in the config file or on the command line
      (<code>python -m onstep_alpaca --help</code>). This page is read-only on
      purpose: reconfiguring a mount mid-session is a good way to lose an exposure.
    </p>
  </div>
</main>
"""
