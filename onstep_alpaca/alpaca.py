"""The Alpaca REST surface: one Flask app over one :class:`Telescope`.
"""

from __future__ import annotations

import datetime as dt
import itertools
import logging
import threading

from flask import Flask, Response, jsonify, request

from . import errors
from .errors import AscomError, BadRequest
from .telescope import Telescope

log = logging.getLogger(__name__)

API_VERSION = 1
DEVICE_TYPE = "telescope"
MANUFACTURER = "ascom-onstep-driver"


class _Transactions:
	"""Hands out ``ServerTransactionID`` values."""

	def __init__(self):
		self._counter = itertools.count(1)
		self._lock = threading.Lock()

	def next(self) -> int:
		with self._lock:
			return next(self._counter)


class _FoldedParams(dict):
	"""A parameter view whose lookups ignore case."""

	def __init__(self, source):
		super().__init__(source)
		self._folded = {key.lower(): value for key, value in source.items()}

	def get(self, key, default=None):
		return self._folded.get(key.lower(), default)


def _params() -> dict[str, str]:
	"""All request parameters, with their names exactly as sent."""
	merged: dict[str, str] = {}
	for source in (request.args, request.form):
		for key in source:
			merged[key] = source[key]
	return merged


def _client_transaction_id(params: dict[str, str]) -> int:
	"""Echo the client's transaction id, or 0 if it sent nothing usable."""
	raw = _FoldedParams(params).get("ClientTransactionID")
	if raw is None:
		return 0
	try:
		value = int(raw)
	except (TypeError, ValueError):
		return 0
	return value if value >= 0 else 0


def _as_bool(name: str, raw: str) -> bool:
	"""Parse Alpaca's booleans, which arrive as ``True``/``False`` strings."""
	lowered = raw.strip().lower()
	if lowered in ("true", "1", "yes"):
		return True
	if lowered in ("false", "0", "no"):
		return False
	raise BadRequest(f"{name} must be true or false, not {raw!r}")


def _as_float(name: str, raw: str) -> float:
	try:
		return float(raw)
	except (TypeError, ValueError):
		raise BadRequest(f"{name} must be a number, not {raw!r}") from None


def _as_utc_date(name: str, raw: str) -> "dt.datetime":
	"""Parse the ISO 8601 instant ASCOM sends for ``UTCDate``."""
	text = raw.strip()
	try:
		parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
	except ValueError:
		raise BadRequest(f"{name} must be an ISO 8601 date and time, not {raw!r}") from None
	if parsed.tzinfo is None:
		parsed = parsed.replace(tzinfo=dt.timezone.utc)
	return parsed


def _as_int(name: str, raw: str) -> int:
	try:
		return int(raw)
	except (TypeError, ValueError):
		raise BadRequest(f"{name} must be an integer, not {raw!r}") from None


def _required(params: dict[str, str], name: str) -> str:
	value = params.get(name)
	if value is None:
		raise BadRequest(f"missing required parameter {name}")
	return value
	return value


def _float_param(params: dict[str, str], name: str) -> float:
	return _as_float(name, _required(params, name))


def _int_param(params: dict[str, str], name: str) -> int:
	return _as_int(name, _required(params, name))


# --------------------------------------------------------------------------------------
# The ASCOM surface, as data. IntEnum members serialise as plain integers, which is what
# clients expect for AlignmentMode, EquatorialSystem, SideOfPier and friends.

GETTERS: dict[str, callable] = {
	# -- common to all devices
	"connected": lambda s: s.connected,
	"description": lambda s: s.description,
	"driverinfo": lambda s: s.driver_info,
	"driverversion": lambda s: s.driver_version,
	"interfaceversion": lambda s: s.interface_version,
	"name": lambda s: s.name,
	"supportedactions": lambda s: s.supported_actions,
	# -- capabilities
	"alignmentmode": lambda s: int(s.alignment_mode),
	"equatorialsystem": lambda s: int(s.equatorial_system),
	"canfindhome": lambda s: s.can_find_home,
	"canpark": lambda s: s.can_park,
	"cansetpark": lambda s: s.can_set_park,
	"canunpark": lambda s: s.can_unpark,
	"canpulseguide": lambda s: s.can_pulse_guide,
	"cansettracking": lambda s: s.can_set_tracking,
	"canslew": lambda s: s.can_slew,
	"canslewasync": lambda s: s.can_slew_async,
	"canslewaltaz": lambda s: s.can_slew_altaz,
	"canslewaltazasync": lambda s: s.can_slew_altaz_async,
	"cansync": lambda s: s.can_sync,
	"cansyncaltaz": lambda s: s.can_sync_altaz,
	"cansetguiderates": lambda s: s.can_set_guide_rates,
	"cansetdeclinationrate": lambda s: s.can_set_declination_rate,
	"cansetrightascensionrate": lambda s: s.can_set_right_ascension_rate,
	"cansetpierside": lambda s: s.can_set_pier_side,
	# -- position
	"rightascension": lambda s: s.right_ascension,
	"declination": lambda s: s.declination,
	"altitude": lambda s: s.altitude,
	"azimuth": lambda s: s.azimuth,
	"siderealtime": lambda s: s.sidereal_time,
	"athome": lambda s: s.at_home,
	"atpark": lambda s: s.at_park,
	"slewing": lambda s: s.slewing,
	"sideofpier": lambda s: int(s.side_of_pier),
	# -- site
	"sitelatitude": lambda s: s.site_latitude,
	"sitelongitude": lambda s: s.site_longitude,
	"siteelevation": lambda s: s.site_elevation,
	"utcdate": lambda s: s.utc_date.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
	# -- tracking
	"tracking": lambda s: s.tracking,
	"trackingrate": lambda s: int(s.tracking_rate),
	"trackingrates": lambda s: [int(rate) for rate in s.tracking_rates],
	"doesrefraction": lambda s: s.does_refraction,
	"rightascensionrate": lambda s: s.right_ascension_rate,
	"declinationrate": lambda s: s.declination_rate,
	# -- guiding
	"ispulseguiding": lambda s: s.is_pulse_guiding,
	"guideraterightascension": lambda s: s.guide_rate_right_ascension,
	"guideratedeclination": lambda s: s.guide_rate_declination,
	# -- targets and misc
	"targetrightascension": lambda s: s.target_right_ascension,
	"targetdeclination": lambda s: s.target_declination,
	"slewsettletime": lambda s: s.slew_settle_time,
	# -- optics: this driver does not know the telescope attached to the mount, and
	# ASCOM is explicit that these should throw rather than report a made-up number.
	"aperturearea": lambda s: _not_implemented("ApertureArea"),
	"aperturediameter": lambda s: _not_implemented("ApertureDiameter"),
	"focallength": lambda s: _not_implemented("FocalLength"),
}


def _not_implemented(name: str):
	raise errors.NotImplementedError_(f"{name} describes the telescope, which the mount driver has no way to know")


SETTERS: dict[str, tuple[str, callable]] = {
	# name -> (parameter name, parser); the attribute is derived from the key.
	"connected": ("Connected", _as_bool),
	"tracking": ("Tracking", _as_bool),
	"trackingrate": ("TrackingRate", _as_int),
	"doesrefraction": ("DoesRefraction", _as_bool),
	"sitelatitude": ("SiteLatitude", _as_float),
	"sitelongitude": ("SiteLongitude", _as_float),
	"siteelevation": ("SiteElevation", _as_float),
	"slewsettletime": ("SlewSettleTime", _as_float),
	"targetrightascension": ("TargetRightAscension", _as_float),
	"targetdeclination": ("TargetDeclination", _as_float),
	"guideraterightascension": ("GuideRateRightAscension", _as_float),
	"guideratedeclination": ("GuideRateDeclination", _as_float),
	"rightascensionrate": ("RightAscensionRate", _as_float),
	"declinationrate": ("DeclinationRate", _as_float),
	"utcdate": ("UTCDate", _as_utc_date),
}

_SETTER_ATTRIBUTES = {
	"connected": "connected",
	"tracking": "tracking",
	"trackingrate": "tracking_rate",
	"doesrefraction": "does_refraction",
	"sitelatitude": "site_latitude",
	"sitelongitude": "site_longitude",
	"siteelevation": "site_elevation",
	"slewsettletime": "slew_settle_time",
	"targetrightascension": "target_right_ascension",
	"targetdeclination": "target_declination",
	"guideraterightascension": "guide_rate_right_ascension",
	"guideratedeclination": "guide_rate_declination",
	"rightascensionrate": "right_ascension_rate",
	"declinationrate": "declination_rate",
	"utcdate": "utc_date",
}


def _method_slew_to_coordinates(scope: Telescope, params):
	scope.slew_to_coordinates(_float_param(params, "RightAscension"), _float_param(params, "Declination"))


def _method_slew_to_coordinates_async(scope: Telescope, params):
	scope.slew_to_coordinates_async(_float_param(params, "RightAscension"), _float_param(params, "Declination"))


def _method_slew_to_altaz(scope: Telescope, params):
	scope.slew_to_altaz(_float_param(params, "Azimuth"), _float_param(params, "Altitude"))


def _method_slew_to_altaz_async(scope: Telescope, params):
	scope.slew_to_altaz_async(_float_param(params, "Azimuth"), _float_param(params, "Altitude"))


def _method_sync_to_coordinates(scope: Telescope, params):
	scope.sync_to_coordinates(_float_param(params, "RightAscension"), _float_param(params, "Declination"))


def _method_pulse_guide(scope: Telescope, params):
	scope.pulse_guide(_int_param(params, "Direction"), _int_param(params, "Duration"))


def _method_move_axis(scope: Telescope, params):
	scope.move_axis(_int_param(params, "Axis"), _float_param(params, "Rate"))


def _method_action(scope: Telescope, params):
	return scope.action(_required(params, "Action"), params.get("Parameters", ""))


def _method_command_blind(scope: Telescope, params):
	# The legacy pass-through. Rather than guess an arbitrary command's reply shape,
	# point callers at the Action that declares one.
	raise errors.NotImplementedError_("use Action('onstep:command', ':GVP#') instead, which declares a reply shape")


PUT_METHODS: dict[str, callable] = {
	"slewtocoordinates": _method_slew_to_coordinates,
	"slewtocoordinatesasync": _method_slew_to_coordinates_async,
	"slewtotarget": lambda s, p: s.slew_to_target(),
	"slewtotargetasync": lambda s, p: s.slew_to_target_async(),
	"slewtoaltaz": _method_slew_to_altaz,
	"slewtoaltazasync": _method_slew_to_altaz_async,
	"synctocoordinates": _method_sync_to_coordinates,
	"synctotarget": lambda s, p: s.sync_to_target(),
	"abortslew": lambda s, p: s.abort_slew(),
	"findhome": lambda s, p: s.find_home(),
	"park": lambda s, p: s.park(),
	"unpark": lambda s, p: s.unpark(),
	"setpark": lambda s, p: s.set_park(),
	"pulseguide": _method_pulse_guide,
	"moveaxis": _method_move_axis,
	"action": _method_action,
	# The legacy pass-through. Rather than guess an arbitrary command's reply shape,
	# point callers at the Action that declares one.
	"commandblind": _method_command_blind,
	"commandbool": _method_command_blind,
	"commandstring": _method_command_blind,
	# ASCOM has no command for "flip now", and OnStep's flip is its own business.
	# Both spellings: ASCOM's member is SideOfPier, and some clients send SetPierSide.
	"sideofpier": lambda s, p: _not_implemented("SideOfPier (set)"),
	"setpierside": lambda s, p: _not_implemented("SideOfPier (set)"),
	"synctoaltaz": lambda s, p: _not_implemented("SyncToAltAz"),
}

GET_METHODS: dict[str, callable] = {
	# Properties that take an argument, so they cannot live in GETTERS.
	"canmoveaxis": lambda s, p: s.can_move_axis(_int_param(p, "Axis")),
	"axisrates": lambda s, p: [
		{"Minimum": rate.minimum, "Maximum": rate.maximum}
		for rate in s.axis_rates(_int_param(p, "Axis"))
	],
	"destinationsideofpier": lambda s, p: int(
		s.destination_side_of_pier(
			_float_param(p, "RightAscension"), _float_param(p, "Declination")
		)
	),
}


# --------------------------------------------------------------------------------------
# The app


def create_app(
		scope: Telescope,
		device_number: int = 0,
		device_name: str = "OnStep",
		location: str = "",
		setup_page: callable | None = None,
) -> Flask:
	"""Build the Flask app serving one telescope."""
	app = Flask(__name__)
	transactions = _Transactions()

	def envelope(value=None, error: AscomError | None = None) -> Response:
		params = _params()
		body = {
			"ClientTransactionID": _client_transaction_id(params),
			"ServerTransactionID": transactions.next(),
			"ErrorNumber": error.number if error else 0,
			"ErrorMessage": error.message if error else "",
		}
		if error is None and value is not None:
			body["Value"] = value
		elif error is None:
			body["Value"] = value
		return jsonify(body)

	@app.errorhandler(BadRequest)
	def _bad_request(exc: BadRequest):
		# The one failure that is an HTTP error: there is no valid Alpaca response to a
		# request that cannot be parsed.
		return Response(exc.message, status=400, mimetype="text/plain")

	# -- management --------------------------------------------------------------

	@app.get("/management/apiversions")
	def api_versions():
		return envelope([API_VERSION])

	@app.get("/management/v1/description")
	def description():
		return envelope(
			{
				"ServerName": "OnStep Alpaca Driver",
				"Manufacturer": MANUFACTURER,
				"ManufacturerVersion": scope.driver_version,
				"Location": location,
			}
		)

	@app.get("/management/v1/configureddevices")
	def configured_devices():
		return envelope(
			[
				{
					"DeviceName": device_name,
					"DeviceType": "Telescope",
					"DeviceNumber": device_number,
					"UniqueID": scope.unique_id,
				}
			]
		)

	# -- setup -------------------------------------------------------------------

	@app.get("/setup")
	@app.get(f"/setup/v1/{DEVICE_TYPE}/<int:device>/setup")
	def setup(device: int = 0):
		if setup_page is None:
			return Response("<p>No setup page is configured.</p>", mimetype="text/html")
		return Response(setup_page(), mimetype="text/html")

	# -- the device --------------------------------------------------------------

	@app.route(
		f"/api/v{API_VERSION}/{DEVICE_TYPE}/<int:device>/<method>",
		methods=["GET", "PUT"],
	)
	def device_request(device: int, method: str):
		if device != device_number:
			# Alpaca treats an unknown device number as a malformed request.
			raise BadRequest(f"this server serves {DEVICE_TYPE} {device_number}, not {device}")

		name = method.lower()
		params = _params()
		try:
			if request.method == "GET":
				return envelope(_handle_get(name, params))
			return envelope(_handle_put(name, params))
		except AscomError as exc:
			# A driver error is a 200 with the error in the body. This is the single
			# most important line in the file for client compatibility.
			log.debug("%s %s -> %s", request.method, method, exc)
			return envelope(error=exc)
		except NotImplementedError as exc:  # pragma: no cover - defensive
			return envelope(error=errors.NotImplementedError_(str(exc)))
		except BadRequest:
			raise
		except Exception as exc:  # pragma: no cover - never leak a traceback
			log.exception("unhandled error serving %s %s", request.method, method)
			return envelope(error=errors.DriverError(str(exc)))

	def _handle_get(name: str, params: dict[str, str]):
		if name in GETTERS:
			return GETTERS[name](scope)
		if name in GET_METHODS:
			# GET query-string names are case insensitive, unlike a PUT body.
			return GET_METHODS[name](scope, _FoldedParams(params))
		if name in PUT_METHODS or name in SETTERS:
			raise BadRequest(f"{name} must be called with PUT, not GET")
		raise BadRequest(f"unknown telescope member {name!r}")

	def _handle_put(name: str, params: dict[str, str]):
		if name in SETTERS:
			parameter, parse = SETTERS[name]
			value = parse(parameter, _required(params, parameter))
			setattr(scope, _SETTER_ATTRIBUTES[name], value)
			return None
		if name in PUT_METHODS:
			return PUT_METHODS[name](scope, params)
		if name in GETTERS or name in GET_METHODS:
			raise BadRequest(f"{name} must be read with GET, not PUT")
		raise BadRequest(f"unknown telescope member {name!r}")

	return app
