"""ASCOM error numbers and the exceptions that carry them.

Alpaca reports failures in the JSON body with ``ErrorNumber``/``ErrorMessage`` and an
HTTP 200; only an unparseable request is an HTTP 400. Numbers are from ASCOM's reserved
0x400-0x4FF range.
"""

from __future__ import annotations


class AscomError(Exception):
	"""Base for anything that should become an Alpaca error envelope."""

	number: int = 0x500
	default_message = "driver error"

	def __init__(self, message: str | None = None):
		self.message = message or self.default_message
		super().__init__(self.message)


class NotImplementedError_(AscomError):
	"""The property or method is not supported by this driver at all."""

	number = 0x400
	default_message = "not implemented by this driver"


class InvalidValueError(AscomError):
	number = 0x401
	default_message = "invalid value"


class ValueNotSetError(AscomError):
	number = 0x402
	default_message = "value has not been set"


class NotConnectedError(AscomError):
	number = 0x407
	default_message = "the driver is not connected to a mount"


class ParkedError(AscomError):
	number = 0x408
	default_message = "the mount is parked"


class SlavedError(AscomError):
	number = 0x409
	default_message = "the mount is slaved"


class InvalidOperationError(AscomError):
	"""The request is valid but not possible in the mount's current state."""

	number = 0x40B
	default_message = "not valid in the current state"


class ActionNotImplementedError(AscomError):
	number = 0x40C
	default_message = "action not implemented"


class DriverError(AscomError):
	"""Something went wrong talking to the mount."""

	number = 0x500
	default_message = "driver error"


class BadRequest(Exception):
	"""The request itself was wrong: a missing or unparseable parameter."""

	def __init__(self, message: str):
		self.message = message
		super().__init__(message)
