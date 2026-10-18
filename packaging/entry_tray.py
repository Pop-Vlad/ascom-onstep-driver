"""Frozen entry point for the tray app.

PyInstaller runs its entry script as ``__main__``, which is not a package, so the relative
imports inside ``onstep_alpaca`` would fail if the module were pointed at directly. This
stub keeps the real module importable as part of its package.
"""

import sys

from onstep_alpaca.tray import main

if __name__ == "__main__":
	sys.exit(main())
