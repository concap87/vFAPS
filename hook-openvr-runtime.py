# PyInstaller runtime hook: ensure openvr's native DLL can be found
# This runs before the main script, adding the bundled openvr directory
# to the DLL search path so ctypes.LoadLibrary() can find libopenvr_api_64.dll.

import os
import sys

def _setup_openvr_dll_path():
    """Add likely openvr DLL locations to the DLL search path."""
    # In a PyInstaller bundle, sys._MEIPASS is the extraction root
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))

    # Possible locations where the DLL might end up
    search_dirs = [
        base,                                    # bundle root
        os.path.join(base, 'openvr'),            # openvr subdir
        os.path.join(base, '_internal'),         # _internal root
        os.path.join(base, '_internal', 'openvr'),  # _internal/openvr
    ]

    for d in search_dirs:
        if os.path.isdir(d):
            # os.add_dll_directory() is the modern way (Python 3.8+, Windows)
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass

            # Also prepend to PATH as a fallback for older ctypes behavior
            if d not in os.environ.get('PATH', ''):
                os.environ['PATH'] = d + os.pathsep + os.environ.get('PATH', '')

_setup_openvr_dll_path()
