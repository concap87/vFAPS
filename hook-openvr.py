"""PyInstaller hook for openvr — bundles the native DLL."""
import os
import openvr

# Find where openvr is installed
openvr_dir = os.path.dirname(openvr.__file__)

# Bundle the native DLL into the openvr folder in the package
binaries = []
for dll_name in ['libopenvr_api_64.dll', 'libopenvr_api_64.so', 'libopenvr_api_32.dll']:
    dll_path = os.path.join(openvr_dir, dll_name)
    if os.path.exists(dll_path):
        binaries.append((dll_path, 'openvr'))

datas = []
