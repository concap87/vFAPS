"""
VR Controller Input Module
===========================
Handles VR controller tracking via OpenVR/SteamVR.
Supports Valve Index, Meta Quest Touch (via Quest Link/Air Link),
and other SteamVR-compatible controllers.
Provides position and rotation data mapped to 0-100 scale.
Includes mouse fallback mode for development/testing without VR.
"""

import math
import time
import threading
from dataclasses import dataclass, field
from typing import Optional, Callable
from collections import deque

try:
    from stabilization import StabilizationManager
    HAS_STABILIZATION = True
except ImportError:
    HAS_STABILIZATION = False

try:
    import openvr
    HAS_OPENVR = True
except ImportError:
    HAS_OPENVR = False


# ============================================================
# Controller Profiles
# ============================================================
# Each profile defines how to read buttons and analog axes from
# a specific controller type through the OpenVR legacy API.
# Fields:
#   name            - Human-readable display name
#   trigger_bits    - Bitmask positions for digital trigger (list of bit indices)
#   trigger_axis    - (axis_index, threshold) for analog trigger fallback
#   a_button_bits   - Bitmask positions for A/X button
#   b_button_bits   - Bitmask positions for B/Y button
#   grip_bits       - Bitmask positions for digital grip
#   grip_axis       - (axis_index, threshold) for analog grip fallback
#   thumbstick_axis - Axis index for thumbstick X/Y
# ============================================================

@dataclass
class ControllerProfile:
    """Button/axis mapping for a specific controller type."""
    name: str = "Unknown"
    trigger_bits: list[int] = field(default_factory=lambda: [33])
    trigger_axis: tuple[int, float] = (1, 0.8)     # (rAxis index, threshold)
    a_button_bits: list[int] = field(default_factory=lambda: [7])
    b_button_bits: list[int] = field(default_factory=lambda: [1])
    grip_bits: list[int] = field(default_factory=lambda: [2])
    grip_axis: Optional[tuple[int, float]] = None   # (rAxis index, threshold)
    thumbstick_axis: int = 0                         # rAxis index for stick


# --- Built-in profiles ---

PROFILE_VALVE_INDEX = ControllerProfile(
    name="Valve Index",
    trigger_bits=[33],
    trigger_axis=(1, 0.8),
    a_button_bits=[7],              # A button is bit 7 only (bit 2 is grip!)
    b_button_bits=[1],
    grip_bits=[2, 4],               # Grip/squeeze handle
    grip_axis=None,                 # Index grip is capacitive/force, digital bit works
    thumbstick_axis=0,
)

PROFILE_QUEST_TOUCH = ControllerProfile(
    name="Meta Quest Touch",
    trigger_bits=[33],
    trigger_axis=(1, 0.8),      # Analog trigger on rAxis[1]
    a_button_bits=[7],          # A/X button
    b_button_bits=[1, 2],       # B/Y - some Quest Link versions use bit 1 or 2
    grip_bits=[],               # Quest grip is fully analog, no reliable digital bit
    grip_axis=(2, 0.7),         # Analog grip on rAxis[2], threshold 70%
    thumbstick_axis=0,
)

PROFILE_VIVE_WAND = ControllerProfile(
    name="HTC Vive Wand",
    trigger_bits=[33],
    trigger_axis=(1, 0.8),
    a_button_bits=[7],              # Trackpad click / menu
    b_button_bits=[1],              # Menu/Application button
    grip_bits=[2],                  # Grip is bit 2
    grip_axis=None,
    thumbstick_axis=0,              # Trackpad reports as axis 0
)

# Universal fallback: probes all known bit positions
PROFILE_GENERIC = ControllerProfile(
    name="Generic Controller",
    trigger_bits=[33],
    trigger_axis=(1, 0.8),
    a_button_bits=[7],              # A button
    b_button_bits=[1, 0],           # Try both common B button positions
    grip_bits=[2, 4],               # Try both common grip positions
    grip_axis=(2, 0.7),             # Also check analog grip
    thumbstick_axis=0,
)

# Lookup table for auto-detection (lowercase substrings -> profile)
_PROFILE_MATCH_TABLE = [
    # (manufacturer_substring, model_substring, profile)
    ("valve",    "index",     PROFILE_VALVE_INDEX),
    ("valve",    "knuckles",  PROFILE_VALVE_INDEX),
    ("oculus",   "",          PROFILE_QUEST_TOUCH),
    ("meta",     "",          PROFILE_QUEST_TOUCH),
    ("facebook", "",          PROFILE_QUEST_TOUCH),
    ("htc",      "vive",      PROFILE_VIVE_WAND),
]


@dataclass
class ControllerState:
    """Current state of all tracked axes and buttons."""
    # Position in meters (raw)
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    # Rotation in degrees (raw)
    pitch: float = 0.0
    yaw: float = 0.0
    roll: float = 0.0
    # Mapped positions (0-100)
    mapped: dict = None
    # Timestamp
    timestamp: float = 0.0
    # Is controller tracked
    is_tracked: bool = False
    # Button states (True = currently pressed)
    trigger_pressed: bool = False
    a_button_pressed: bool = False
    b_button_pressed: bool = False
    grip_pressed: bool = False
    # Thumbstick axes (-1.0 to 1.0)
    thumbstick_x: float = 0.0
    thumbstick_y: float = 0.0

    def __post_init__(self):
        if self.mapped is None:
            self.mapped = {}


@dataclass
class AxisCalibration:
    """Calibration settings for mapping raw input to 0-100."""
    min_val: float = 0.0
    max_val: float = 1.0
    inverted: bool = False
    deadzone: float = 0.02  # Ignore movements smaller than this
    sensitivity: float = 1.0  # Multiplier for movement scaling (1.0 = linear)

    def map_value(self, raw: float) -> int:
        """Map a raw value to 0-100 range with sensitivity scaling."""
        range_val = self.max_val - self.min_val
        if range_val == 0:
            return 50

        # 1. Normalize to 0.0 - 1.0 based on calibration min/max
        normalized = (raw - self.min_val) / range_val

        # 2. Apply Sensitivity (scale around center point 0.5)
        # Formula: new_val = (val - center) * sensitivity + center
        if self.sensitivity != 1.0:
            normalized = (normalized - 0.5) * self.sensitivity + 0.5

        # 3. Clamp to valid range
        normalized = max(0.0, min(1.0, normalized))

        # 4. Invert if necessary
        if self.inverted:
            normalized = 1.0 - normalized

        return int(round(normalized * 100))


class KalmanFilter1D:
    """Simple 1D Kalman filter for smoothing controller jitter."""

    def __init__(self, process_noise=0.01, measurement_noise=0.1):
        self.process_noise = process_noise
        self.measurement_noise = measurement_noise
        self.estimate = 0.0
        self.error = 1.0
        self.initialized = False

    def update(self, measurement: float) -> float:
        if not self.initialized:
            self.estimate = measurement
            self.initialized = True
            return measurement

        # Prediction
        prediction = self.estimate
        pred_error = self.error + self.process_noise

        # Update
        gain = pred_error / (pred_error + self.measurement_noise)
        self.estimate = prediction + gain * (measurement - prediction)
        self.error = (1 - gain) * pred_error

        return self.estimate

    def reset(self):
        self.estimate = 0.0
        self.error = 1.0
        self.initialized = False


class VRControllerInput:
    """
    Handles VR controller tracking via OpenVR.
    Polls controller position/rotation and maps to 0-100 for each axis.
    """

    AXIS_NAMES = ["x", "y", "z", "pitch", "yaw", "roll"]

    def __init__(self):
        self.vr_system: Optional[openvr.IVRSystem] = None
        self.is_initialized = False
        self.controller_index: int = -1
        self.preferred_hand: str = "right"  # "left" or "right"

        # Controller profile (auto-detected or manually set)
        self.profile: ControllerProfile = PROFILE_GENERIC

        # Calibration per axis
        self.calibrations: dict[str, AxisCalibration] = {
            "x":     AxisCalibration(min_val=-0.3, max_val=0.3),
            "y":     AxisCalibration(min_val=0.3, max_val=1.2),
            "z":     AxisCalibration(min_val=-0.3, max_val=0.3),
            "pitch": AxisCalibration(min_val=-90.0, max_val=90.0),
            "yaw":   AxisCalibration(min_val=-90.0, max_val=90.0),
            "roll":  AxisCalibration(min_val=-90.0, max_val=90.0),
        }

        # Kalman filters for each axis
        self.filters: dict[str, KalmanFilter1D] = {
            name: KalmanFilter1D(process_noise=0.005, measurement_noise=0.05)
            for name in self.AXIS_NAMES
        }

        # Polling thread
        self._polling = False
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_rate = 90  # Hz
        self._current_state = ControllerState()
        self._state_lock = threading.Lock()
        self._callbacks: list[Callable] = []

        # Recent states for visualization trail
        self._history = deque(maxlen=50)

        # Button edge detection (previous frame state)
        self._prev_trigger = False
        self._prev_a_button = False
        self._prev_b_button = False
        self._prev_grip = False

        # Button event callbacks: called once on press (rising edge)
        self._button_callbacks: dict[str, list[Callable]] = {
            "trigger": [],
            "a_button": [],
            "b_button": [],
            "grip": [],
        }

        # Stabilization pipeline (pre-map + post-map filtering)
        self._stabilization: StabilizationManager | None = None
        if HAS_STABILIZATION:
            self._stabilization = StabilizationManager(preset="medium")

        # Axis lock state: locked axes hold a frozen raw value
        self._axis_locks: dict[str, bool] = {n: False for n in self.AXIS_NAMES}
        self._axis_lock_values: dict[str, float] = {n: 0.0 for n in self.AXIS_NAMES}

    def initialize(self) -> tuple[bool, str]:
        """Initialize OpenVR and find controller. Returns (success, message)."""
        if not HAS_OPENVR:
            return False, "openvr package not installed. Install with: pip install openvr"

        try:
            self.vr_system = openvr.init(openvr.VRApplication_Other)
            self.is_initialized = True
        except openvr.OpenVRError as e:
            return False, f"Failed to initialize OpenVR: {e}\nMake sure SteamVR is running."

        # Find controller
        found = self._find_controller()
        if not found:
            return False, "No controller found. Make sure your controller is on and tracked in SteamVR."

        # Auto-detect controller type and select button profile
        self.profile = self._detect_controller_profile()

        return True, (f"Connected to {self.profile.name} controller "
                      f"(device {self.controller_index})")

    def _find_controller(self) -> bool:
        """Find the preferred controller device index."""
        if not self.vr_system:
            return False

        left_idx = -1
        right_idx = -1

        for i in range(openvr.k_unMaxTrackedDeviceCount):
            device_class = self.vr_system.getTrackedDeviceClass(i)
            if device_class == openvr.TrackedDeviceClass_Controller:
                role = self.vr_system.getControllerRoleForTrackedDeviceIndex(i)
                if role == openvr.TrackedControllerRole_LeftHand:
                    left_idx = i
                elif role == openvr.TrackedControllerRole_RightHand:
                    right_idx = i

        if self.preferred_hand == "right" and right_idx >= 0:
            self.controller_index = right_idx
        elif self.preferred_hand == "left" and left_idx >= 0:
            self.controller_index = left_idx
        elif right_idx >= 0:
            self.controller_index = right_idx
        elif left_idx >= 0:
            self.controller_index = left_idx
        else:
            # Try to find any controller
            for i in range(openvr.k_unMaxTrackedDeviceCount):
                if self.vr_system.getTrackedDeviceClass(i) == openvr.TrackedDeviceClass_Controller:
                    self.controller_index = i
                    break

        return self.controller_index >= 0

    def _detect_controller_profile(self) -> ControllerProfile:
        """
        Auto-detect the controller type by reading OpenVR device properties
        (manufacturer and model strings) and selecting the matching profile.
        Falls back to PROFILE_GENERIC if no match is found.
        """
        manufacturer = self._get_device_string(
            openvr.Prop_ManufacturerName_String
        ).lower()
        model = self._get_device_string(
            openvr.Prop_ModelNumber_String
        ).lower()
        tracking = self._get_device_string(
            openvr.Prop_TrackingSystemName_String
        ).lower()

        combined = f"{manufacturer} {model} {tracking}"
        print(f"Controller detected: manufacturer='{manufacturer}', "
              f"model='{model}', tracking='{tracking}'")

        for mfr_substr, model_substr, profile in _PROFILE_MATCH_TABLE:
            if mfr_substr and mfr_substr in combined:
                if not model_substr or model_substr in combined:
                    print(f"Matched profile: {profile.name}")
                    return profile

        # Additional heuristic: Quest Link/Air Link often reports
        # "oculus" in the tracking system name even if manufacturer
        # string is different
        if "oculus" in tracking or "quest" in combined:
            print(f"Matched profile via tracking system: Meta Quest Touch")
            return PROFILE_QUEST_TOUCH

        print(f"No specific match, using generic profile")
        return PROFILE_GENERIC

    def _get_device_string(self, prop) -> str:
        """Read a string property from the tracked controller device."""
        if not self.vr_system or self.controller_index < 0:
            return ""
        try:
            value = self.vr_system.getStringTrackedDeviceProperty(
                self.controller_index, prop
            )
            return value if isinstance(value, str) else value.decode("utf-8", errors="ignore")
        except Exception:
            return ""

    def get_controller_name(self) -> str:
        """Return the human-readable name of the detected controller type."""
        return self.profile.name

    def _matrix_to_position(self, mat) -> tuple[float, float, float]:
        """Extract position from HmdMatrix34_t."""
        return (mat[0][3], mat[1][3], mat[2][3])

    def _matrix_to_euler(self, mat) -> tuple[float, float, float]:
        """Extract euler angles (pitch, yaw, roll) from HmdMatrix34_t in degrees."""
        # Extract rotation matrix elements
        r00, r01, r02 = mat[0][0], mat[0][1], mat[0][2]
        r10, r11, r12 = mat[1][0], mat[1][1], mat[1][2]
        r20, r21, r22 = mat[2][0], mat[2][1], mat[2][2]

        # Calculate euler angles
        pitch = math.degrees(math.atan2(-r12, math.sqrt(r02**2 + r22**2)))
        yaw = math.degrees(math.atan2(r02, r22))
        roll = math.degrees(math.atan2(r10, r11))

        return (pitch, yaw, roll)

    def poll_once(self) -> ControllerState:
        """Poll controller state once. Returns ControllerState."""
        state = ControllerState(timestamp=time.time())

        if not self.is_initialized or self.controller_index < 0:
            return state

        try:
            poses = self.vr_system.getDeviceToAbsoluteTrackingPose(
                openvr.TrackingUniverseStanding,
                0,
                openvr.k_unMaxTrackedDeviceCount
            )

            pose = poses[self.controller_index]
            if not pose.bPoseIsValid:
                return state

            state.is_tracked = True
            mat = pose.mDeviceToAbsoluteTracking
            ts = state.timestamp

            # Extract position
            x, y, z = self._matrix_to_position(mat)
            # Extract rotation
            pitch, yaw, roll = self._matrix_to_euler(mat)

            # 1. Kalman filter (existing)
            raw = {
                "x": self.filters["x"].update(x),
                "y": self.filters["y"].update(y),
                "z": self.filters["z"].update(z),
                "pitch": self.filters["pitch"].update(pitch),
                "yaw": self.filters["yaw"].update(yaw),
                "roll": self.filters["roll"].update(roll),
            }

            # 2. Stabilization pre-map filter
            if self._stabilization:
                for ax in self.AXIS_NAMES:
                    raw[ax] = self._stabilization.process(ax, raw[ax], ts)

            # 3. Axis lock: freeze locked axes at held value
            for ax in self.AXIS_NAMES:
                if self._axis_locks.get(ax, False):
                    raw[ax] = self._axis_lock_values[ax]

            state.x, state.y, state.z = raw["x"], raw["y"], raw["z"]
            state.pitch, state.yaw, state.roll = raw["pitch"], raw["yaw"], raw["roll"]

            # 4. Calibration mapping to 0-100
            mapped = {}
            for ax in self.AXIS_NAMES:
                mapped[ax] = self.calibrations[ax].map_value(raw[ax])

            # 5. Stabilization post-map filter (deadzone + hysteresis)
            if self._stabilization:
                for ax in self.AXIS_NAMES:
                    mapped[ax] = self._stabilization.post_map(ax, mapped[ax])

            state.mapped = mapped

            # Read button states and thumbstick via controller state
            # Uses the auto-detected controller profile for correct bitmask mapping
            try:
                success, ctrl_state = self.vr_system.getControllerState(self.controller_index)
                if success:
                    pressed = ctrl_state.ulButtonPressed
                    profile = self.profile

                    # --- Trigger ---
                    state.trigger_pressed = any(
                        bool(pressed & (1 << bit)) for bit in profile.trigger_bits
                    )
                    # Analog trigger fallback (works on all controllers)
                    if not state.trigger_pressed and profile.trigger_axis:
                        ax_idx, threshold = profile.trigger_axis
                        if ctrl_state.rAxis[ax_idx].x > threshold:
                            state.trigger_pressed = True

                    # --- A button (X on left Quest Touch) ---
                    state.a_button_pressed = any(
                        bool(pressed & (1 << bit)) for bit in profile.a_button_bits
                    )

                    # --- B button (Y on left Quest Touch) ---
                    state.b_button_pressed = any(
                        bool(pressed & (1 << bit)) for bit in profile.b_button_bits
                    )

                    # --- Grip ---
                    state.grip_pressed = any(
                        bool(pressed & (1 << bit)) for bit in profile.grip_bits
                    )
                    # Analog grip fallback (essential for Quest Touch)
                    if not state.grip_pressed and profile.grip_axis:
                        ax_idx, threshold = profile.grip_axis
                        if ctrl_state.rAxis[ax_idx].x > threshold:
                            state.grip_pressed = True

                    # --- Thumbstick ---
                    stick_idx = profile.thumbstick_axis
                    state.thumbstick_x = ctrl_state.rAxis[stick_idx].x
                    state.thumbstick_y = ctrl_state.rAxis[stick_idx].y
            except Exception:
                pass

        except Exception:
            pass

        return state

    def start_polling(self):
        """Start background polling thread."""
        if self._polling:
            return
        self._polling = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def stop_polling(self):
        """Stop background polling thread."""
        self._polling = False
        if self._poll_thread:
            self._poll_thread.join(timeout=1.0)
            self._poll_thread = None

    def _poll_loop(self):
        """Main polling loop running in background thread."""
        interval = 1.0 / self._poll_rate
        while self._polling:
            state = self.poll_once()
            with self._state_lock:
                self._current_state = state
                self._history.append(state)

            # Button edge detection - fire callbacks on rising edge only
            if state.trigger_pressed and not self._prev_trigger:
                for cb in self._button_callbacks.get("trigger", []):
                    try:
                        cb()
                    except Exception:
                        pass
            if state.a_button_pressed and not self._prev_a_button:
                for cb in self._button_callbacks.get("a_button", []):
                    try:
                        cb()
                    except Exception:
                        pass
            if state.b_button_pressed and not self._prev_b_button:
                for cb in self._button_callbacks.get("b_button", []):
                    try:
                        cb()
                    except Exception:
                        pass
            if state.grip_pressed and not self._prev_grip:
                for cb in self._button_callbacks.get("grip", []):
                    try:
                        cb()
                    except Exception:
                        pass

            self._prev_trigger = state.trigger_pressed
            self._prev_a_button = state.a_button_pressed
            self._prev_b_button = state.b_button_pressed
            self._prev_grip = state.grip_pressed

            # Notify general callbacks
            for cb in self._callbacks:
                try:
                    cb(state)
                except Exception:
                    pass
            time.sleep(interval)

    def get_current_state(self) -> ControllerState:
        """Get the most recent controller state (thread-safe)."""
        with self._state_lock:
            return self._current_state

    def get_history(self) -> list[ControllerState]:
        """Get recent state history for trail visualization."""
        with self._state_lock:
            return list(self._history)

    def add_callback(self, callback: Callable):
        """Add a callback that receives ControllerState on each poll."""
        self._callbacks.append(callback)

    def remove_callback(self, callback: Callable):
        """Remove a polling callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def on_button_press(self, button: str, callback: Callable):
        """
        Register a callback for a button press event (rising edge only).
        button: "trigger", "a_button", "b_button", or "grip"
        callback: no-argument callable, called from the polling thread
        """
        if button in self._button_callbacks:
            self._button_callbacks[button].append(callback)

    def calibrate_axis(self, axis: str, min_val: float, max_val: float):
        """Set calibration range for an axis."""
        if axis in self.calibrations:
            self.calibrations[axis].min_val = min_val
            self.calibrations[axis].max_val = max_val

    def set_sensitivity(self, sensitivity: float):
        """Update sensitivity multiplier for all axes."""
        for calibration in self.calibrations.values():
            calibration.sensitivity = sensitivity

    def recenter(self):
        """
        Re-center all axes so the current controller position maps to 50.
        Shifts the calibration window so the midpoint aligns with the
        current raw reading, preserving the total range.
        """
        state = self.get_current_state()
        raw = {
            "x": state.x, "y": state.y, "z": state.z,
            "pitch": state.pitch, "yaw": state.yaw, "roll": state.roll,
        }
        for axis_name, current_raw in raw.items():
            if axis_name in self.calibrations:
                cal = self.calibrations[axis_name]
                half_range = (cal.max_val - cal.min_val) / 2.0
                if half_range > 0:
                    cal.min_val = current_raw - half_range
                    cal.max_val = current_raw + half_range

    def auto_calibrate_start(self):
        """Begin auto-calibration (track min/max of each axis)."""
        self._cal_data = {name: {"min": float('inf'), "max": float('-inf')} for name in self.AXIS_NAMES}
        self._auto_cal = True

    def auto_calibrate_update(self, state: ControllerState):
        """Update auto-calibration with a new state."""
        if not hasattr(self, '_auto_cal') or not self._auto_cal:
            return
        raw = {"x": state.x, "y": state.y, "z": state.z,
               "pitch": state.pitch, "yaw": state.yaw, "roll": state.roll}
        for name, val in raw.items():
            self._cal_data[name]["min"] = min(self._cal_data[name]["min"], val)
            self._cal_data[name]["max"] = max(self._cal_data[name]["max"], val)

    def auto_calibrate_finish(self):
        """Apply auto-calibration results."""
        if not hasattr(self, '_auto_cal') or not self._auto_cal:
            return
        self._auto_cal = False
        for name, data in self._cal_data.items():
            if data["min"] < data["max"]:
                margin = (data["max"] - data["min"]) * 0.05
                self.calibrations[name].min_val = data["min"] - margin
                self.calibrations[name].max_val = data["max"] + margin

    def reset_filters(self):
        """Reset all Kalman filters."""
        for f in self.filters.values():
            f.reset()
        if self._stabilization:
            self._stabilization.reset_all()

    # ---- Stabilization API ----

    def set_stabilization_preset(self, preset: str):
        """Set stabilization preset: off, light, medium, heavy."""
        if self._stabilization:
            self._stabilization.set_preset(preset)

    def get_stabilization_preset(self) -> str:
        if self._stabilization:
            return self._stabilization.preset_name
        return "off"

    # ---- Axis Lock API ----

    def lock_axis(self, axis: str):
        """Freeze an axis at its current raw value."""
        if axis not in self._axis_locks:
            return
        state = self.get_current_state()
        raw_map = {"x": state.x, "y": state.y, "z": state.z,
                   "pitch": state.pitch, "yaw": state.yaw, "roll": state.roll}
        self._axis_lock_values[axis] = raw_map.get(axis, 0.0)
        self._axis_locks[axis] = True

    def unlock_axis(self, axis: str):
        """Resume live tracking for an axis."""
        if axis in self._axis_locks:
            self._axis_locks[axis] = False
            # Reset stabilization filter so there's no snap
            if self._stabilization:
                self._stabilization.reset(axis)

    def is_axis_locked(self, axis: str) -> bool:
        return self._axis_locks.get(axis, False)

    def toggle_axis_lock(self, axis: str) -> bool:
        """Toggle lock, return new lock state."""
        if self.is_axis_locked(axis):
            self.unlock_axis(axis)
            return False
        else:
            self.lock_axis(axis)
            return True

    def shutdown(self):
        """Clean up OpenVR resources."""
        self.stop_polling()
        if self.is_initialized:
            try:
                openvr.shutdown()
            except Exception:
                pass
            self.is_initialized = False


class MouseFallbackInput:
    """
    Mouse-based input for testing without VR hardware.
    Vertical mouse movement maps to the primary axis (stroke).
    Horizontal mouse movement maps to secondary axis (sway).
    """

    def __init__(self):
        self.is_initialized = True
        self.controller_index = 0
        self._current_y = 50  # 0-100
        self._current_x = 50
        self._state_lock = threading.Lock()
        self._callbacks: list[Callable] = []
        self._polling = False
        self._poll_thread = None
        self._history = deque(maxlen=50)
        self.sensitivity = 1.0  # Default sensitivity

    def initialize(self) -> tuple[bool, str]:
        return True, "Mouse fallback mode active. Move mouse vertically in video area to control position."

    def set_sensitivity(self, sensitivity: float):
        """Update sensitivity multiplier."""
        self.sensitivity = sensitivity

    def update_from_mouse(self, normalized_x: float, normalized_y: float):
        """
        Update position from mouse coordinates.
        normalized_x, normalized_y should be 0.0 to 1.0
        """
        with self._state_lock:
            sens = self.sensitivity
            
            # Apply sensitivity centered around 0.5
            # Formula: new_val = (val - 0.5) * sens + 0.5
            sx = (normalized_x - 0.5) * sens + 0.5
            sy = (normalized_y - 0.5) * sens + 0.5
            
            # Clamp to valid 0-1 range before converting to 0-100
            sx = max(0.0, min(1.0, sx))
            sy = max(0.0, min(1.0, sy))

            self._current_x = int(sx * 100)
            self._current_y = int((1.0 - sy) * 100)  # Invert Y for UI logic (up=100)

    def get_current_state(self) -> ControllerState:
        with self._state_lock:
            state = ControllerState(
                y=self._current_y / 100.0,
                x=self._current_x / 100.0,
                timestamp=time.time(),
                is_tracked=True,
                mapped={
                    "x": self._current_x,
                    "y": self._current_y,
                    "z": 50,
                    "pitch": 50,
                    "yaw": 50,
                    "roll": 50,
                }
            )
            return state

    def get_history(self) -> list[ControllerState]:
        with self._state_lock:
            return list(self._history)

    def start_polling(self):
        if self._polling:
            return
        self._polling = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def stop_polling(self):
        self._polling = False
        if self._poll_thread:
            self._poll_thread.join(timeout=1.0)

    def _poll_loop(self):
        while self._polling:
            state = self.get_current_state()
            with self._state_lock:
                self._history.append(state)
            for cb in self._callbacks:
                try:
                    cb(state)
                except Exception:
                    pass
            time.sleep(1.0 / 60)

    def add_callback(self, callback):
        self._callbacks.append(callback)

    def remove_callback(self, callback):
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def poll_once(self):
        return self.get_current_state()

    def reset_filters(self):
        pass

    def shutdown(self):
        self.stop_polling()

    def auto_calibrate_start(self):
        pass

    def auto_calibrate_update(self, state):
        pass

    def auto_calibrate_finish(self):
        pass

    def calibrate_axis(self, axis: str, min_val: float, max_val: float):
        pass

    def recenter(self):
        """No-op for mouse fallback."""
        pass

    def on_button_press(self, button: str, callback):
        """No-op for mouse fallback."""
        pass

    def get_controller_name(self) -> str:
        return "Mouse Fallback"

    # Stabilization stubs
    def set_stabilization_preset(self, preset: str):
        pass

    def get_stabilization_preset(self) -> str:
        return "off"

    # Axis lock stubs
    def lock_axis(self, axis: str):
        pass

    def unlock_axis(self, axis: str):
        pass

    def is_axis_locked(self, axis: str) -> bool:
        return False

    def toggle_axis_lock(self, axis: str) -> bool:
        return False
