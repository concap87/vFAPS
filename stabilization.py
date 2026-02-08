"""
Stabilization Pipeline
=======================
Per-axis signal processing stack applied *before* calibration mapping.
Each axis gets its own independent pipeline instance.

Pipeline order (each stage is optional and independently togglable):
  1. Spike Rejection   — clamp single-frame outliers
  2. One Euro Filter   — adaptive low-pass (fast moves stay sharp, slow moves get smoothed)
  3. Slew-Rate Limiter — cap maximum velocity (units/sec)
  4. Jerk Limiter      — cap maximum acceleration change (units/sec²)

Additionally, *after* calibration mapping (0-100), two post-map stages:
  5. Deadzone          — ignore mapped changes smaller than threshold
  6. Hysteresis        — require movement past threshold before changing direction

The pre-map pipeline lives here; deadzone and hysteresis are integrated
into AxisCalibration.map_value() in vr_controller.py.

Presets
-------
  Off    — all filters bypassed
  Light  — gentle smoothing, wide slew limit
  Medium — moderate smoothing, moderate slew limit (default)
  Heavy  — aggressive smoothing, tight slew limit, jerk limiter on
"""

import math
import time
from dataclasses import dataclass, field
from typing import Optional


# ============================================================
# Individual filter stages
# ============================================================

class OneEuroFilter:
    """
    The 1-Euro Filter — an adaptive low-pass filter.

    When the signal moves slowly the cutoff frequency drops (more smoothing).
    When the signal moves fast the cutoff drops less (less lag).

    Parameters
    ----------
    min_cutoff : float
        Minimum cutoff frequency in Hz.  Lower = more smoothing at rest.
    beta : float
        Speed coefficient.  Higher = less lag during fast movement.
    d_cutoff : float
        Cutoff frequency for the derivative filter (usually leave at 1.0).
    """

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.007,
                 d_cutoff: float = 1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff

        self._x_prev: Optional[float] = None
        self._dx_prev: float = 0.0
        self._t_prev: Optional[float] = None

    def _smoothing_factor(self, te: float, cutoff: float) -> float:
        r = 2.0 * math.pi * cutoff * te
        return r / (r + 1.0)

    def filter(self, x: float, t: Optional[float] = None) -> float:
        if t is None:
            t = time.monotonic()

        if self._x_prev is None:
            self._x_prev = x
            self._dx_prev = 0.0
            self._t_prev = t
            return x

        te = t - self._t_prev
        if te <= 0:
            te = 1e-6  # avoid division by zero
        self._t_prev = t

        # Estimate derivative
        a_d = self._smoothing_factor(te, self.d_cutoff)
        dx = (x - self._x_prev) / te
        dx_hat = a_d * dx + (1.0 - a_d) * self._dx_prev

        # Adaptive cutoff
        cutoff = self.min_cutoff + self.beta * abs(dx_hat)

        # Filter the signal
        a = self._smoothing_factor(te, cutoff)
        x_hat = a * x + (1.0 - a) * self._x_prev

        self._x_prev = x_hat
        self._dx_prev = dx_hat
        return x_hat

    def reset(self):
        self._x_prev = None
        self._dx_prev = 0.0
        self._t_prev = None


class SlewRateLimiter:
    """
    Caps the maximum rate of change (units per second).
    Prevents sudden jumps from appearing in the output.

    Parameters
    ----------
    max_rate : float
        Maximum change per second in raw units.
    """

    def __init__(self, max_rate: float = 5.0):
        self.max_rate = max_rate
        self._prev: Optional[float] = None
        self._t_prev: Optional[float] = None

    def filter(self, x: float, t: Optional[float] = None) -> float:
        if t is None:
            t = time.monotonic()

        if self._prev is None:
            self._prev = x
            self._t_prev = t
            return x

        dt = t - self._t_prev
        if dt <= 0:
            return self._prev
        self._t_prev = t

        max_delta = self.max_rate * dt
        delta = x - self._prev
        if abs(delta) > max_delta:
            delta = math.copysign(max_delta, delta)

        result = self._prev + delta
        self._prev = result
        return result

    def reset(self):
        self._prev = None
        self._t_prev = None


class JerkLimiter:
    """
    Caps the maximum acceleration change (second derivative) per second.
    Smooths out abrupt velocity changes.

    Parameters
    ----------
    max_jerk : float
        Maximum acceleration change per second² in raw units.
    """

    def __init__(self, max_jerk: float = 20.0):
        self.max_jerk = max_jerk
        self._prev: Optional[float] = None
        self._vel_prev: float = 0.0
        self._t_prev: Optional[float] = None

    def filter(self, x: float, t: Optional[float] = None) -> float:
        if t is None:
            t = time.monotonic()

        if self._prev is None:
            self._prev = x
            self._vel_prev = 0.0
            self._t_prev = t
            return x

        dt = t - self._t_prev
        if dt <= 0:
            return self._prev
        self._t_prev = t

        # Current velocity
        vel = (x - self._prev) / dt

        # Acceleration (jerk = change in acceleration, but we simplify to
        # capping the velocity change rate)
        accel = (vel - self._vel_prev) / dt
        max_accel = self.max_jerk * dt
        if abs(accel) > max_accel:
            accel = math.copysign(max_accel, accel)
            vel = self._vel_prev + accel * dt

        result = self._prev + vel * dt
        self._prev = result
        self._vel_prev = vel
        return result

    def reset(self):
        self._prev = None
        self._vel_prev = 0.0
        self._t_prev = None


class SpikeRejector:
    """
    Reject single-frame outlier spikes.
    If a sample deviates more than `threshold` from the previous sample
    AND the *next* sample returns close to the pre-spike value, the spike
    is replaced with the previous value.

    Since we work in a streaming fashion (no lookahead), we use a 1-frame
    delay: hold the suspicious sample and only emit it if the next sample
    confirms it wasn't a spike.

    Parameters
    ----------
    threshold : float
        Maximum expected single-frame change.  Anything larger is suspect.
    """

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self._prev: Optional[float] = None
        self._held: Optional[float] = None  # suspicious sample waiting for confirmation
        self._held_t: Optional[float] = None

    def filter(self, x: float, t: Optional[float] = None) -> float:
        if self._prev is None:
            self._prev = x
            return x

        if self._held is not None:
            # We have a suspicious sample from last frame
            # Check if current sample confirms the jump or reverts
            if abs(x - self._held) < self.threshold:
                # Current sample is close to the held value → jump was real
                self._prev = self._held
                self._held = None
                # Now process current sample normally (fall through)
            else:
                # Current sample reverted → the held sample was a spike
                # Discard the held sample, emit previous value
                discarded = self._held
                self._held = None
                # Process current sample against the pre-spike value
                # (fall through with _prev unchanged)

        # Normal processing
        delta = abs(x - self._prev)
        if delta > self.threshold:
            # Suspicious — hold it for one frame
            self._held = x
            self._held_t = t
            return self._prev  # emit previous value for now
        else:
            self._prev = x
            return x

    def reset(self):
        self._prev = None
        self._held = None
        self._held_t = None


# ============================================================
# Deadzone and Hysteresis (post-mapping, applied to 0-100 values)
# ============================================================

class DeadzoneFilter:
    """
    Ignore mapped value changes smaller than `threshold`.
    Output only updates when the input has moved more than `threshold`
    away from the last emitted value.

    Parameters
    ----------
    threshold : float
        Minimum change in mapped units (0-100 scale) to emit a new value.
    """

    def __init__(self, threshold: float = 1.0):
        self.threshold = threshold
        self._last_emitted: Optional[int] = None

    def filter(self, value: int) -> int:
        if self._last_emitted is None:
            self._last_emitted = value
            return value

        if abs(value - self._last_emitted) >= self.threshold:
            self._last_emitted = value
            return value
        return self._last_emitted

    def reset(self):
        self._last_emitted = None


class HysteresisFilter:
    """
    Require the signal to cross a threshold before changing direction.
    Prevents tiny oscillations at a boundary from producing jittery output.

    Parameters
    ----------
    band : float
        Half-width of the hysteresis band in mapped units (0-100 scale).
    """

    def __init__(self, band: float = 1.5):
        self.band = band
        self._last_emitted: Optional[int] = None
        self._direction: int = 0  # +1 rising, -1 falling, 0 unknown

    def filter(self, value: int) -> int:
        if self._last_emitted is None:
            self._last_emitted = value
            return value

        delta = value - self._last_emitted

        if self._direction == 0:
            # No established direction yet
            if abs(delta) >= self.band:
                self._direction = 1 if delta > 0 else -1
                self._last_emitted = value
                return value
            return self._last_emitted

        # Moving in same direction: always accept
        if (self._direction > 0 and delta > 0) or (self._direction < 0 and delta < 0):
            self._last_emitted = value
            return value

        # Trying to reverse: require crossing the band
        if abs(delta) >= self.band * 2:
            self._direction = 1 if delta > 0 else -1
            self._last_emitted = value
            return value

        return self._last_emitted

    def reset(self):
        self._last_emitted = None
        self._direction = 0


# ============================================================
# Per-axis stabilization pipeline
# ============================================================

@dataclass
class StabilizationConfig:
    """Configuration for a single axis's stabilization pipeline."""

    # Spike rejection
    spike_rejection_enabled: bool = True
    spike_threshold: float = 0.5  # raw units

    # One Euro filter
    one_euro_enabled: bool = True
    one_euro_min_cutoff: float = 1.5   # Hz — lower = more smoothing at rest
    one_euro_beta: float = 0.007       # speed coefficient

    # Slew-rate limiter
    slew_rate_enabled: bool = True
    slew_max_rate: float = 5.0         # raw units/sec

    # Jerk limiter
    jerk_limiter_enabled: bool = False
    jerk_max_jerk: float = 20.0        # raw units/sec²

    # Post-map deadzone
    deadzone_enabled: bool = True
    deadzone_threshold: float = 1.0    # mapped 0-100 units

    # Post-map hysteresis
    hysteresis_enabled: bool = True
    hysteresis_band: float = 1.5       # mapped 0-100 units


# Preset configurations
STABILIZATION_PRESETS = {
    "off": StabilizationConfig(
        spike_rejection_enabled=False,
        one_euro_enabled=False,
        slew_rate_enabled=False,
        jerk_limiter_enabled=False,
        deadzone_enabled=False,
        hysteresis_enabled=False,
    ),
    "light": StabilizationConfig(
        spike_rejection_enabled=True,
        spike_threshold=0.8,
        one_euro_enabled=True,
        one_euro_min_cutoff=2.5,
        one_euro_beta=0.01,
        slew_rate_enabled=True,
        slew_max_rate=8.0,
        jerk_limiter_enabled=False,
        deadzone_enabled=True,
        deadzone_threshold=0.5,
        hysteresis_enabled=True,
        hysteresis_band=1.0,
    ),
    "medium": StabilizationConfig(
        spike_rejection_enabled=True,
        spike_threshold=0.5,
        one_euro_enabled=True,
        one_euro_min_cutoff=1.5,
        one_euro_beta=0.007,
        slew_rate_enabled=True,
        slew_max_rate=5.0,
        jerk_limiter_enabled=False,
        deadzone_enabled=True,
        deadzone_threshold=1.0,
        hysteresis_enabled=True,
        hysteresis_band=1.5,
    ),
    "heavy": StabilizationConfig(
        spike_rejection_enabled=True,
        spike_threshold=0.3,
        one_euro_enabled=True,
        one_euro_min_cutoff=0.8,
        one_euro_beta=0.004,
        slew_rate_enabled=True,
        slew_max_rate=3.0,
        jerk_limiter_enabled=True,
        jerk_max_jerk=15.0,
        deadzone_enabled=True,
        deadzone_threshold=2.0,
        hysteresis_enabled=True,
        hysteresis_band=2.5,
    ),
}


# Axis-type-specific defaults — rotation axes need different thresholds
# than position axes because their raw units differ (degrees vs meters).
ROTATION_AXES = {"pitch", "yaw", "roll"}

def _adjust_config_for_axis(config: StabilizationConfig, axis_name: str) -> StabilizationConfig:
    """Return a copy of config with thresholds scaled for rotation axes."""
    if axis_name not in ROTATION_AXES:
        return config

    # Rotation axes are in degrees (-180 to 180) vs position in meters (~-1.5 to 1.5)
    # Scale thresholds by roughly 60x for comparable behavior
    import copy as _copy
    c = _copy.copy(config)
    c.spike_threshold = config.spike_threshold * 60.0
    c.slew_max_rate = config.slew_max_rate * 60.0
    c.jerk_max_jerk = config.jerk_max_jerk * 60.0
    return c


class AxisStabilizer:
    """
    Complete stabilization pipeline for a single axis.
    Call `process(raw_value)` to get the stabilized output.
    Call `post_map(mapped_value)` after calibration mapping for deadzone/hysteresis.
    """

    def __init__(self, axis_name: str, config: Optional[StabilizationConfig] = None):
        self.axis_name = axis_name
        self._config = config or STABILIZATION_PRESETS["medium"]
        self._effective_config = _adjust_config_for_axis(self._config, axis_name)

        # Build filter instances
        self._spike = SpikeRejector()
        self._one_euro = OneEuroFilter()
        self._slew = SlewRateLimiter()
        self._jerk = JerkLimiter()
        self._deadzone = DeadzoneFilter()
        self._hysteresis = HysteresisFilter()

        self._apply_config()

    def _apply_config(self):
        """Re-configure filter instances from current config."""
        c = self._effective_config

        self._spike.threshold = c.spike_threshold
        self._one_euro.min_cutoff = c.one_euro_min_cutoff
        self._one_euro.beta = c.one_euro_beta
        self._slew.max_rate = c.slew_max_rate
        self._jerk.max_jerk = c.jerk_max_jerk
        self._deadzone.threshold = c.deadzone_threshold
        self._hysteresis.band = c.hysteresis_band

    def set_config(self, config: StabilizationConfig):
        """Update configuration and re-apply to filters."""
        self._config = config
        self._effective_config = _adjust_config_for_axis(config, self.axis_name)
        self._apply_config()

    def get_config(self) -> StabilizationConfig:
        return self._config

    def process(self, raw: float, t: Optional[float] = None) -> float:
        """
        Run the pre-mapping stabilization pipeline.
        Returns the stabilized raw value.
        """
        if t is None:
            t = time.monotonic()

        val = raw
        c = self._effective_config

        # 1. Spike rejection
        if c.spike_rejection_enabled:
            val = self._spike.filter(val, t)

        # 2. One Euro filter
        if c.one_euro_enabled:
            val = self._one_euro.filter(val, t)

        # 3. Slew-rate limiter
        if c.slew_rate_enabled:
            val = self._slew.filter(val, t)

        # 4. Jerk limiter
        if c.jerk_limiter_enabled:
            val = self._jerk.filter(val, t)

        return val

    def post_map(self, mapped: int) -> int:
        """
        Run the post-mapping stabilization (deadzone + hysteresis).
        Call this on the 0-100 mapped value after AxisCalibration.map_value().
        """
        c = self._effective_config
        val = mapped

        if c.deadzone_enabled:
            val = self._deadzone.filter(val)

        if c.hysteresis_enabled:
            val = self._hysteresis.filter(val)

        return val

    def reset(self):
        """Reset all filter states (e.g. after calibration)."""
        self._spike.reset()
        self._one_euro.reset()
        self._slew.reset()
        self._jerk.reset()
        self._deadzone.reset()
        self._hysteresis.reset()


class StabilizationManager:
    """
    Manages per-axis stabilizers and preset selection.
    One instance is owned by the controller input class.
    """

    AXIS_NAMES = ["x", "y", "z", "pitch", "yaw", "roll"]

    def __init__(self, preset: str = "medium"):
        self._preset_name = preset
        self._config = STABILIZATION_PRESETS.get(preset, STABILIZATION_PRESETS["medium"])

        self.stabilizers: dict[str, AxisStabilizer] = {
            name: AxisStabilizer(name, self._config)
            for name in self.AXIS_NAMES
        }

    @property
    def preset_name(self) -> str:
        return self._preset_name

    def set_preset(self, preset: str):
        """Switch all axes to a named preset."""
        if preset not in STABILIZATION_PRESETS:
            return
        self._preset_name = preset
        self._config = STABILIZATION_PRESETS[preset]
        for name, stab in self.stabilizers.items():
            stab.set_config(self._config)

    def set_axis_config(self, axis_name: str, config: StabilizationConfig):
        """Set a custom config for a specific axis."""
        if axis_name in self.stabilizers:
            self.stabilizers[axis_name].set_config(config)
            self._preset_name = "custom"

    def process(self, axis_name: str, raw: float, t: Optional[float] = None) -> float:
        """Run pre-map stabilization for one axis."""
        if axis_name in self.stabilizers:
            return self.stabilizers[axis_name].process(raw, t)
        return raw

    def post_map(self, axis_name: str, mapped: int) -> int:
        """Run post-map stabilization for one axis."""
        if axis_name in self.stabilizers:
            return self.stabilizers[axis_name].post_map(mapped)
        return mapped

    def reset_all(self):
        """Reset all filter states."""
        for stab in self.stabilizers.values():
            stab.reset()

    def reset_axis(self, axis_name: str):
        """Reset a single axis's filter state."""
        if axis_name in self.stabilizers:
            self.stabilizers[axis_name].reset()
