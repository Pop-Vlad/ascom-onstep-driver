# -*- mode: python ; coding: utf-8 -*-
"""Freeze the driver into a folder of two executables, one windowed and one console.

Both are built here rather than only the tray app, because when something does not work
the first thing anyone needs is output on a screen: ``onstep-alpaca --scan`` cannot say
anything from a process that has no console at all.

They share one COLLECT, so the interpreter and the libraries are stored once.
"""

import tomllib
from pathlib import Path

ROOT = Path(SPECPATH).parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
ICON = str(ROOT / "packaging" / "onstep-alpaca.ico")

HIDDEN = [
	# pystray and pyserial both pick a platform backend behind a runtime check, which the
	# import graph does not always follow.
	"pystray._win32",
	"serial.tools.list_ports_windows",
]

EXCLUDES = [
	# Nothing here draws a GUI beyond a 64px circle, and these are the usual stowaways.
	"tkinter", "numpy", "matplotlib", "pytest", "setuptools", "pip",
	"PIL.ImageTk", "PIL.ImageQt",
]


def version_resource(name: str, description: str) -> str:
	"""Write a Windows version resource, so the file's Properties tab is not blank.

	An unsigned executable with no publisher metadata is what every piece of malware also
	looks like; this is the cheap half of not looking like one.
	"""
	parts = tuple(int(p) for p in VERSION.split(".")) + (0, 0, 0, 0)
	numeric = parts[:4]
	path = Path(workpath) / f"{name}.version.txt"
	path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numeric}, prodvers={numeric}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Vlad Pop'),
      StringStruct('FileDescription', {description!r}),
      StringStruct('FileVersion', {VERSION!r}),
      StringStruct('InternalName', {name!r}),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 Vlad Pop. MIT licence.'),
      StringStruct('OriginalFilename', {name + '.exe'!r}),
      StringStruct('ProductName', 'OnStep Alpaca'),
      StringStruct('ProductVersion', {VERSION!r}),
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
""", encoding="utf-8")
	return str(path)


def analyse(script: str):
	return Analysis(
		[str(ROOT / "packaging" / script)],
		pathex=[str(ROOT)],
		hiddenimports=HIDDEN,
		excludes=EXCLUDES,
		noarchive=False,
	)


tray = analyse("entry_tray.py")
cli = analyse("entry_cli.py")

tray_exe = EXE(
	PYZ(tray.pure, tray.zipped_data), tray.scripts, [],
	exclude_binaries=True,
	name="onstep-alpaca-tray",
	console=False,  # the whole point: no console window at login
	icon=ICON,
	version=version_resource("onstep-alpaca-tray", "OnStep Alpaca driver (tray)"),
)

cli_exe = EXE(
	PYZ(cli.pure, cli.zipped_data), cli.scripts, [],
	exclude_binaries=True,
	name="onstep-alpaca",
	console=True,
	icon=ICON,
	version=version_resource("onstep-alpaca", "OnStep Alpaca driver (console)"),
)

COLLECT(
	tray_exe, tray.binaries, tray.datas,
	cli_exe, cli.binaries, cli.datas,
	strip=False,
	upx=False,  # UPX-packed binaries trip antivirus heuristics for no real gain here
	name="onstep-alpaca",
)
