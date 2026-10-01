"""Alpaca REST conformance tests.

These target the conventions that decide whether a driver works with every client or
only with the one it was tried against: a driver error is an HTTP 200 with the error in
the body, parameter names are case-insensitive, and ServerTransactionID climbs.
"""

from __future__ import annotations

import json

import pytest

from onstep_alpaca import errors
from onstep_alpaca.alpaca import create_app
from onstep_alpaca.config import Config
from onstep_alpaca.link import MountLink
from onstep_alpaca.setup_page import render
from onstep_alpaca.simulator import OnStepSimulator, SimulatedTransport
from onstep_alpaca.telescope import Telescope

BASE = "/api/v1/telescope/0"


@pytest.fixture
def sim():
	return OnStepSimulator(
		mount_type="E", latitude=44.43, longitude=26.10, slew_rate_deg_s=2000.0
	)


@pytest.fixture
def scope(sim):
	return Telescope(
		lambda: MountLink(SimulatedTransport(sim), poll_interval=0.05),
		unique_id="test-unique-id",
	)


@pytest.fixture
def client(scope):
	config = Config(unique_id="test-unique-id")
	app = create_app(
		scope,
		device_number=0,
		device_name="OnStep",
		setup_page=lambda: render(scope, config, 11111),
	)
	app.config.update(TESTING=True)
	with app.test_client() as client:
		yield client
	if scope.connected:
		scope.connected = False


@pytest.fixture
def connected(client):
	assert client.put(f"{BASE}/connected", data={"Connected": "True"}).status_code == 200
	return client


def body(response):
	return json.loads(response.data)


def value(response):
	payload = body(response)
	assert payload["ErrorNumber"] == 0, payload["ErrorMessage"]
	return payload["Value"]


# --------------------------------------------------------------------------------------
# The envelope


def test_every_response_carries_the_alpaca_envelope(connected):
	payload = body(connected.get(f"{BASE}/declination"))
	assert set(payload) >= {
		"Value", "ClientTransactionID", "ServerTransactionID",
		"ErrorNumber", "ErrorMessage",
	}


def test_client_transaction_id_is_echoed(connected):
	payload = body(connected.get(f"{BASE}/declination?ClientTransactionID=4242"))
	assert payload["ClientTransactionID"] == 4242


def test_a_missing_client_transaction_id_becomes_zero(connected):
	assert body(connected.get(f"{BASE}/declination"))["ClientTransactionID"] == 0


def test_a_junk_client_transaction_id_is_tolerated_not_rejected(connected):
	"""Not worth refusing a request over, and the conformance tools check that a non-numeric
	value does not break the driver."""
	response = connected.get(f"{BASE}/declination?ClientTransactionID=banana")
	assert response.status_code == 200
	assert body(response)["ClientTransactionID"] == 0


def test_server_transaction_id_climbs(connected):
	seen = [
		body(connected.get(f"{BASE}/declination"))["ServerTransactionID"]
		for _ in range(5)
	]
	assert seen == sorted(seen)
	assert len(set(seen)) == 5


def test_parameter_names_are_case_sensitive(client):
	"""PUT parameter names are case sensitive: `connected` instead of `Connected` must be
	refused, not quietly accepted."""
	assert client.put(f"{BASE}/connected", data={"Connected": "True"}).status_code == 200
	assert value(client.get(f"{BASE}/connected")) is True

	for wrong in ("connected", "CONNECTED", "ConnecteD"):
		response = client.put(f"{BASE}/connected", data={wrong: "False"})
		assert response.status_code == 400, f"{wrong} was accepted"
	# ...and none of those badly-cased requests changed anything.
	assert value(client.get(f"{BASE}/connected")) is True


def test_a_differently_cased_client_transaction_id_is_still_echoed(connected):
	"""ClientID and ClientTransactionID are case insensitive, and ConformU sends a
	differently-cased name expecting the value back regardless."""
	for name in ("ClientTransactionID", "clienttransactionid", "CLIENTTRANSACTIONID"):
		response = connected.get(f"{BASE}/declination?{name}=67890")
		assert response.status_code == 200
		assert body(response)["ClientTransactionID"] == 67890, name


def test_a_badly_cased_put_parameter_is_a_400(connected):
	"""PUT body parameter names are case sensitive -- ConformU sends each one with
	deliberately wrong casing and expects an HTTP 400 for every one."""
	assert connected.put(
		f"{BASE}/slewtocoordinatesasync",
		data={"rightascension": "6.0", "Declination": "30.0"},
	).status_code == 400
	assert connected.put(
		f"{BASE}/pulseguide", data={"Direction": "0", "duration": "100"}
	).status_code == 400
	assert connected.put(
		f"{BASE}/moveaxis", data={"axis": "0", "Rate": "0.1"}
	).status_code == 400


def test_a_differently_cased_get_parameter_is_accepted(connected):
	"""GET query-string names are case insensitive, which is the opposite of a PUT body.
	ConformU enforces both, so the two cannot share one rule."""
	for name in ("Axis", "axis", "AXIS"):
		assert value(connected.get(f"{BASE}/canmoveaxis?{name}=0")) is True, name
		assert connected.get(f"{BASE}/axisrates?{name}=0").status_code == 200, name
	for ra, dec in (("RightAscension", "Declination"), ("rightascension", "declination")):
		assert connected.get(
			f"{BASE}/destinationsideofpier?{ra}=6&{dec}=30"
		).status_code == 200, ra


# --------------------------------------------------------------------------------------
# Errors: in the body, not the status line


def test_a_driver_error_is_an_http_200_with_the_error_in_the_body(client):
	"""The single most important compatibility rule in Alpaca. Reading a property while
	disconnected is an error, not an HTTP failure."""
	response = client.get(f"{BASE}/declination")
	assert response.status_code == 200
	payload = body(response)
	assert payload["ErrorNumber"] == errors.NotConnectedError.number
	assert payload["ErrorMessage"]


def test_an_unimplemented_property_reports_the_not_implemented_number(connected):
	payload = body(connected.get(f"{BASE}/focallength"))
	assert payload["ErrorNumber"] == 0x400


def test_an_invalid_value_reports_the_invalid_value_number(connected):
	response = connected.put(
		f"{BASE}/slewtocoordinatesasync",
		data={"RightAscension": "99", "Declination": "0"},
	)
	assert response.status_code == 200
	assert body(response)["ErrorNumber"] == 0x401


def test_a_malformed_request_is_the_one_case_that_is_an_http_400(connected):
	"""There is no valid Alpaca response to a request that cannot be parsed."""
	assert connected.put(f"{BASE}/slewtocoordinatesasync", data={}).status_code == 400
	assert connected.put(
		f"{BASE}/slewtocoordinatesasync",
		data={"RightAscension": "abc", "Declination": "0"},
	).status_code == 400
	assert connected.put(f"{BASE}/connected", data={"Connected": "maybe"}).status_code == 400


def test_an_unknown_member_is_a_400(connected):
	assert connected.get(f"{BASE}/teleportation").status_code == 400


def test_the_wrong_http_verb_is_a_400(connected):
	assert connected.put(f"{BASE}/declination", data={}).status_code == 400
	assert connected.get(f"{BASE}/park").status_code == 400


def test_the_wrong_device_number_is_a_400(connected):
	assert connected.get("/api/v1/telescope/3/declination").status_code == 400


# --------------------------------------------------------------------------------------
# Management


def test_api_versions(client):
	assert value(client.get("/management/apiversions")) == [1]


def test_server_description(client):
	description = value(client.get("/management/v1/description"))
	assert "ServerName" in description
	assert description["Manufacturer"]


def test_configured_devices_reports_a_stable_unique_id(client):
	devices = value(client.get("/management/v1/configureddevices"))
	assert len(devices) == 1
	assert devices[0]["DeviceType"] == "Telescope"
	assert devices[0]["DeviceNumber"] == 0
	assert devices[0]["UniqueID"] == "test-unique-id"


# --------------------------------------------------------------------------------------
# The setup page


def test_setup_page_renders_while_disconnected(client):
	"""It is what people open when the driver is misbehaving, so it has to work in exactly
	the state where everything else does not."""
	response = client.get("/setup")
	assert response.status_code == 200
	assert b"OnStep Alpaca Driver" in response.data
	assert b"not connected" in response.data


def test_setup_page_shows_the_mount_once_connected(connected):
	response = connected.get("/setup")
	assert response.status_code == 200
	assert b"On-Step" in response.data
	assert b"GEM" in response.data


def test_setup_page_is_served_at_the_alpaca_device_path(client):
	assert client.get("/setup/v1/telescope/0/setup").status_code == 200


# --------------------------------------------------------------------------------------
# Properties and capabilities over HTTP


def test_connect_and_read_position(connected):
	assert 0.0 <= value(connected.get(f"{BASE}/rightascension")) < 24.0
	assert -90.0 <= value(connected.get(f"{BASE}/declination")) <= 90.0
	assert 0.0 <= value(connected.get(f"{BASE}/siderealtime")) < 24.0


def test_capability_flags_are_booleans(connected):
	for name in (
			"canfindhome", "canpark", "canpulseguide", "cansettracking", "canslew",
			"canslewasync", "cansync", "cansetguiderates", "cansetdeclinationrate",
			"cansetpierside", "cansyncaltaz",
	):
		assert isinstance(value(connected.get(f"{BASE}/{name}")), bool), name


def test_enums_serialise_as_integers(connected):
	"""Clients expect plain numbers for AlignmentMode, EquatorialSystem and friends."""
	assert value(connected.get(f"{BASE}/alignmentmode")) == 2  # GERMAN_POLAR
	assert value(connected.get(f"{BASE}/equatorialsystem")) == 1  # TOPOCENTRIC
	assert value(connected.get(f"{BASE}/sideofpier")) in (0, 1)
	assert value(connected.get(f"{BASE}/trackingrate")) == 0  # SIDEREAL
	assert value(connected.get(f"{BASE}/trackingrates")) == [0, 1, 2, 3]


def test_axis_rates_are_reported_as_min_max_objects(connected):
	rates = value(connected.get(f"{BASE}/axisrates?Axis=0"))
	assert len(rates) == 1
	assert set(rates[0]) == {"Minimum", "Maximum"}
	assert rates[0]["Maximum"] > 0

	assert value(connected.get(f"{BASE}/axisrates?Axis=2")) == []


def test_can_move_axis_takes_its_axis_argument(connected):
	assert value(connected.get(f"{BASE}/canmoveaxis?Axis=0")) is True
	assert value(connected.get(f"{BASE}/canmoveaxis?Axis=1")) is True
	assert value(connected.get(f"{BASE}/canmoveaxis?Axis=2")) is False


def test_destination_side_of_pier_takes_coordinates(connected):
	answer = value(
		connected.get(f"{BASE}/destinationsideofpier?RightAscension=6&Declination=30")
	)
	assert answer in (0, 1)


def test_utc_date_is_iso_8601_with_a_z(connected):
	text = value(connected.get(f"{BASE}/utcdate"))
	assert text.endswith("Z")
	assert "T" in text


def test_site_round_trips_over_http(connected):
	assert connected.put(f"{BASE}/sitelongitude", data={"SiteLongitude": "-122.5"}).status_code == 200
	assert value(connected.get(f"{BASE}/sitelongitude")) == pytest.approx(-122.5, abs=0.02)
	assert connected.put(f"{BASE}/siteelevation", data={"SiteElevation": "150"}).status_code == 200
	assert value(connected.get(f"{BASE}/siteelevation")) == 150.0


def test_setting_an_unsupported_rate_reports_not_implemented(connected):
	response = connected.put(f"{BASE}/declinationrate", data={"DeclinationRate": "0.5"})
	assert response.status_code == 200
	assert body(response)["ErrorNumber"] == 0x400
	# The getter still works and reads zero, as ASCOM requires.
	assert value(connected.get(f"{BASE}/declinationrate")) == 0.0


# --------------------------------------------------------------------------------------
# Methods over HTTP


def test_tracking_can_be_set_over_http(connected):
	assert connected.put(f"{BASE}/tracking", data={"Tracking": "True"}).status_code == 200
	assert value(connected.get(f"{BASE}/tracking")) is True


def test_a_full_slew_over_http(connected):
	connected.put(f"{BASE}/tracking", data={"Tracking": "True"})
	response = connected.put(
		f"{BASE}/slewtocoordinatesasync",
		data={"RightAscension": "6.5", "Declination": "22.0"},
	)
	assert body(response)["ErrorNumber"] == 0
	# Slewing must be true the moment the async call returns.
	assert value(connected.get(f"{BASE}/slewing")) is True

	import time
	deadline = time.monotonic() + 10
	while value(connected.get(f"{BASE}/slewing")):
		assert time.monotonic() < deadline, "slew never finished"
		time.sleep(0.05)
	assert value(connected.get(f"{BASE}/rightascension")) == pytest.approx(6.5, abs=0.01)


def test_pulse_guide_over_http(connected):
	connected.put(f"{BASE}/tracking", data={"Tracking": "True"})
	response = connected.put(
		f"{BASE}/pulseguide", data={"Direction": "0", "Duration": "400"}
	)
	assert body(response)["ErrorNumber"] == 0
	assert value(connected.get(f"{BASE}/ispulseguiding")) is True
	# And guiding is not slewing.
	assert value(connected.get(f"{BASE}/slewing")) is False


def test_move_axis_over_http(connected):
	connected.put(f"{BASE}/tracking", data={"Tracking": "True"})
	assert body(
		connected.put(f"{BASE}/moveaxis", data={"Axis": "0", "Rate": "0.25"})
	)["ErrorNumber"] == 0
	assert value(connected.get(f"{BASE}/slewing")) is True
	assert body(
		connected.put(f"{BASE}/moveaxis", data={"Axis": "0", "Rate": "0"})
	)["ErrorNumber"] == 0


def test_abort_slew_over_http(connected):
	assert body(connected.put(f"{BASE}/abortslew", data={}))["ErrorNumber"] == 0


def test_park_and_unpark_over_http(connected):
	assert body(connected.put(f"{BASE}/setpark", data={}))["ErrorNumber"] == 0
	assert body(connected.put(f"{BASE}/park", data={}))["ErrorNumber"] == 0
	assert value(connected.get(f"{BASE}/atpark")) is True
	assert body(connected.put(f"{BASE}/unpark", data={}))["ErrorNumber"] == 0


def test_slewing_while_parked_reports_the_parked_error_number(connected):
	connected.put(f"{BASE}/setpark", data={})
	connected.put(f"{BASE}/park", data={})
	response = connected.put(
		f"{BASE}/slewtocoordinatesasync",
		data={"RightAscension": "6", "Declination": "30"},
	)
	assert body(response)["ErrorNumber"] == 0x408  # ParkedException


def test_sync_over_http(connected):
	connected.put(f"{BASE}/tracking", data={"Tracking": "True"})
	response = connected.put(
		f"{BASE}/synctocoordinates",
		data={"RightAscension": "3.25", "Declination": "48.0"},
	)
	assert body(response)["ErrorNumber"] == 0


def test_action_passes_through_to_the_mount(connected):
	response = connected.put(
		f"{BASE}/action", data={"Action": "onstep:command", "Parameters": ":GVP#"}
	)
	assert value(response) == "On-Step"


def test_supported_actions_is_listed(connected):
	actions = value(connected.get(f"{BASE}/supportedactions"))
	assert "onstep:command" in actions


def test_command_blind_points_at_the_action_instead(connected):
	"""Rather than guess the reply shape of an arbitrary command, say where to go."""
	response = connected.put(f"{BASE}/commandblind", data={"Command": ":Q#"})
	payload = body(response)
	assert payload["ErrorNumber"] == 0x400
	assert "Action" in payload["ErrorMessage"]


def test_target_coordinates_report_value_not_set_before_use(connected):
	payload = body(connected.get(f"{BASE}/targetrightascension"))
	assert payload["ErrorNumber"] == 0x402  # ValueNotSet
	connected.put(f"{BASE}/targetrightascension", data={"TargetRightAscension": "5.5"})
	assert value(connected.get(f"{BASE}/targetrightascension")) == pytest.approx(5.5)


def test_disconnecting_over_http(connected):
	assert connected.put(f"{BASE}/connected", data={"Connected": "False"}).status_code == 200
	assert value(connected.get(f"{BASE}/connected")) is False
	assert body(connected.get(f"{BASE}/declination"))["ErrorNumber"] == 0x407
