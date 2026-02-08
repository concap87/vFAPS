# Changelog — vFAPS v2.0

## Overview

Seven enhancement phases implemented to improve recording quality, editing tools, and workflow:

---

## Phase 1 — Realtime Stabilization Pipeline

**Files changed:** `stabilization.py` (NEW, 611 lines), `vr_controller.py` (modified), `main_window.py` (modified)

### What it does
A per-axis signal processing stack applied *before* calibration mapping, ensuring both the 3D viz and the recorder benefit from stabilized data.

### Pipeline order (each stage independently togglable):
1. **Spike Rejection** — Detects and discards single-frame outlier spikes using 1-frame lookahead
2. **One Euro Filter** — Adaptive low-pass: slow movements get heavy smoothing, fast movements stay sharp
3. **Slew-Rate Limiter** — Caps maximum velocity (units/sec) to prevent jumps
4. **Jerk Limiter** — Caps acceleration changes for even smoother motion (heavy preset only)
5. **Deadzone** (post-map) — Ignores mapped value changes smaller than threshold
6. **Hysteresis** (post-map) — Requires crossing a direction-change band before reversing

### Presets
| Preset | Spike | OneEuro | Slew | Jerk | Deadzone | Hysteresis |
|--------|-------|---------|------|------|----------|------------|
| Off    | ✗     | ✗       | ✗    | ✗    | ✗        | ✗          |
| Light  | ✓     | min_cutoff=2.5 | 8.0/s | ✗ | 0.5  | 1.0        |
| Medium | ✓     | min_cutoff=1.5 | 5.0/s | ✗ | 1.0  | 1.5        |
| Heavy  | ✓     | min_cutoff=0.8 | 3.0/s | ✓ | 2.0  | 2.5        |

### Axis-aware scaling
Rotation axes (pitch/yaw/roll, in degrees) automatically get threshold scaling (~60×) vs position axes (in meters).

### UI
Settings panel: **Stabilization** dropdown — Off / Light / Medium / Heavy (default: Medium)

---

## Phase 2 — Axis Control Surfaces (Lock + Record Toggles)

**Files changed:** `vr_controller.py` (modified), `main_window.py` (modified)

### Axis Lock
- **Lock at raw state level** — freezes the raw value for any axis before calibration mapping
- Locked axis shows frozen value in 3D viz, position bar, and recording
- **Unlock resumes live** — resets stabilization filters for that axis to prevent discontinuity
- UI: 🔓/🔒 toggle buttons per axis in the right panel

### Recording Per-Axis Toggle
- Multi-axis checkboxes can be toggled during live recording
- Toggle off immediately stops sampling for that axis
- Toggle on resumes cleanly
- Export reflects only what was actually recorded

---

## Phase 3 — Multi-Lane Timeline

**Files changed:** `timeline_widget.py` (major refactor, 877 lines)

### New API
```python
set_primary_lane(axis_id, actions)     # Editable lane
set_overlay_lanes({axis_id: actions})  # Read-only overlay lanes
set_analysis_lanes({lane_id: data})    # Analysis data (beats, etc.)
set_overlay_visibility(axis_id, bool)  # Toggle per-overlay
set_analysis_visibility(lane_id, bool) # Toggle per-analysis lane
```

### Per-axis overlay colors
| Axis   | Color  |
|--------|--------|
| Stroke | Blue   |
| Surge  | Green  |
| Sway   | Peach  |
| Twist  | Mauve  |
| Roll   | Pink   |
| Pitch  | Teal   |

### UI
View → Timeline Overlays submenu with checkboxes for all 6 axes. Overlays are read-only (no accidental editing of non-primary axes).

---

## Phase 4 — Heatmap Visualization

**Files changed:** `timeline_widget.py` (added heatmap rendering)

### What it does
Time-density heatmap drawn behind the primary data lane. Computes point density per pixel bucket across the visible view window.

### Color ramp
Dark blue → Purple → Orange (low to high density), with transparency.

### UI
View → Show Heatmap (Ctrl+H) toggle. Helps identify over-sampled or sparse regions for editing.

---

## Phase 5 — Beat Detection & Alignment

**Files changed:** `beat_detection.py` (NEW, 381 lines), `timeline_widget.py` (beat markers), `main_window.py` (tools menu)

### Beat Detection Pipeline
1. Extract audio from video using `ffmpeg` subprocess
2. Read WAV data and compute spectral flux (onset strength)
3. Peak-pick onsets above adaptive threshold
4. Estimate BPM using inter-onset interval histogram
5. Build a regular beat grid from estimated BPM
6. Store results as `BeatData` (beats, onsets, bpm, confidence, subdivisions)

### BeatData methods
- `snap_to_beat(time_ms, tolerance)` — Snap to nearest beat
- `snap_to_grid(time_ms, tolerance)` — Snap to nearest subdivision
- `get_beat_grid()` — Full grid including subdivisions
- Serializable to/from dict for project persistence

### Timeline rendering
- Solid vertical lines for beats
- Dotted lines for subdivisions
- Colors: beats = green-gold, subdivisions = subtle green

### UI
- Tools → Detect Beats from Audio... (runs detection with progress)
- Tools → Show Beat Markers (Ctrl+B) toggle
- Tools → Snap Selection to Beats
- Graceful fallback if numpy or ffmpeg unavailable

---

## Phase 6 — Vision Tracking Foundation

**Files changed:** `vision_tracking.py` (NEW, 272 lines)

### Module ready, UI integration deferred
Provides a `VisionTracker` class that can analyze video frames for motion. Currently a foundation module — the UI hookup is not yet wired since it requires OpenCV and more extensive integration work.

### Capabilities
- Frame differencing for motion detection
- ROI (region of interest) tracking
- Motion magnitude → funscript position mapping
- Configurable sensitivity and smoothing

---

## Phase 7 — Unified Export (Bundle Format)

**Files changed:** `funscript_io.py` (modified), `main_window.py` (modified)

### Bundle Export
Single-file export containing all axes + metadata in one JSON file (`.fsbundle`). Useful for project sharing.

### Updated Project Format (v2.0)
- `save_project(filepath, extra_data=None)` — Persists beat data, settings alongside project
- `load_project(filepath)` → `(project, extra_data)` — Backward compatible with v1.0 projects
- `export_bundle(output_path, extra_data)` — Combined export
- `import_bundle(filepath)` → `(project, extra_data)` — Combined import

### UI
File → Export Bundle...

---

## File Summary

| File | Lines | Status |
|------|-------|--------|
| `stabilization.py` | 611 | **NEW** — Phase 1 |
| `beat_detection.py` | 381 | **NEW** — Phase 5 |
| `vision_tracking.py` | 272 | **NEW** — Phase 6 |
| `vr_controller.py` | 708 | Modified — Phases 1, 2 |
| `timeline_widget.py` | 877 | Modified — Phases 3, 4, 5 |
| `main_window.py` | 1562 | Modified — All phases UI |
| `funscript_io.py` | 254 | Modified — Phase 7 |
| `recorder.py` | 338 | Unchanged |
| `calibration_wizard.py` | 424 | Unchanged |
| `controller_viz.py` | 585 | Unchanged |
| `position_display.py` | 509 | Unchanged |
| `video_player.py` | 354 | Unchanged |
| `main.py` | 174 | Unchanged |
