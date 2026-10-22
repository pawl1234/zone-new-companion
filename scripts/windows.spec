# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for dist\zone-new-companion.exe; scripts\build_windows.ps1 runs it:
#   python -m PyInstaller scripts\windows.spec -- [--libmpv <path to libmpv-2.dll>]
# A spec file rather than command-line options because only a spec can leave out
# files the PyQt6 hook collects that the app never uses.
import argparse
import os

parser = argparse.ArgumentParser()
parser.add_argument('--libmpv', help='libmpv-2.dll to bundle; without it the app plays streams in VLC')
options = parser.parse_args()

ROOT = os.path.dirname(SPECPATH)
NAME = 'zone-new-companion'
ICON = os.path.join(ROOT, 'zone_new_companion', 'icon', 'icon.ico')

binaries = []
if options.libmpv:
    # Bundled under the name python-mpv looks for; extracted to _MEIPASS at startup.
    binaries.append((options.libmpv, '.'))

a = Analysis(
    [os.path.join(ROOT, 'main.py')],
    pathex=[ROOT],
    binaries=binaries,
    datas=[
        (ICON, 'zone_new_companion/icon'),
        (os.path.join(ROOT, 'zone_new_companion', 'ui', 'theme'), 'zone_new_companion/ui/theme'),
    ],
    hiddenimports=['mpv'],
    excludes=['setuptools', 'pkg_resources'],
    optimize=2,
)

# Qt files the app never loads (about 12 MB compressed):
# - opengl32sw.dll: software OpenGL for machines without a GPU driver (VMs, some
#   RDP sessions); there the built-in player cannot start and streams play in VLC
# - Qt6Pdf.dll, which only the PDF image plugin needs, and the other unused image formats
# - Qt translations: the app installs no QTranslator
UNUSED_QT = (
    'opengl32sw.dll',
    'qt6pdf.dll',
    'imageformats/qpdf.dll',
    'imageformats/qtiff.dll',
    'imageformats/qtga.dll',
    'imageformats/qicns.dll',
    'imageformats/qwbmp.dll',
)


def used(entry):
    dest = entry[0].replace('\\', '/').lower()
    if not dest.startswith('pyqt6/'):
        return True
    return '/translations/' not in dest and not dest.endswith(UNUSED_QT)


a.binaries = [entry for entry in a.binaries if used(entry)]
a.datas = [entry for entry in a.datas if used(entry)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=NAME,
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=[ICON],
)
