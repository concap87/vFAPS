# vFAPS v2.0

**VR Funscript Authoring & Playback Studio**

Create multi-axis funscripts by tracking Valve Index controller movements synchronized to video playback.

☕ [Buy Me a Coffee](https://buymeacoffee.com/vfaps)

## What's New in v2.0

- **Realtime Stabilization** — Per-axis signal processing pipeline (spike rejection, One Euro filter, slew/jerk limiters, deadzone, hysteresis) with Off/Light/Medium/Heavy presets
- **Axis Lock** — Freeze any axis at its current value; recording and 3D viz reflect the frozen state
- **Multi-Lane Timeline** — View overlay lanes for all 6 axes simultaneously with per-axis colors
- **Heatmap Visualization** — Time-density heatmap behind the timeline shows over-sampled regions
- **Beat Detection** — Extract audio beats from video, display markers on timeline, snap points to beats
- **Vision Tracking** — Foundation module for video-based motion analysis (module ready)
- **Bundle Export** — Single-file combined export of all axes + metadata for project sharing

See [CHANGELOG.md](CHANGELOG.md) for detailed per-phase technical descriptions.

## Features

- **VR Controller Tracking** — Maps Valve Index 6DOF controller position/rotation at ~90Hz
- **Multi-Axis Support** — Record stroke, surge, sway, twist, roll, and pitch simultaneously
- **Stabilization Pipeline** — 4-stage pre-map filtering + deadzone/hysteresis post-map, with presets
- **Real-Time Visualization** — Position indicator bar with trail effect and numeric readout
- **Interactive Timeline** — Zoom, pan, select, click-to-seek, drag points, overlay lanes
- **Editing** — Drag points, add/delete, undo/redo, delete regions, smoothing, point reduction
- **Guided Calibration** — Step-by-step wizard for setting min/max per axis
- **3D Controller View** — Real-time 3D wireframe, sidebar panel or draggable overlay
- **Beat Detection** — Audio beat extraction, timeline markers, snap-to-beat
- **Controller Buttons** — Trigger=record, A=calibrate, thumbstick=scrub
- **Project Files** — Save/load with all axis data + beat data + settings
- **Export** — Standard funscript JSON + multi-axis naming + unified bundle export
- **Mouse Fallback** — Works without VR hardware
- **Dark Theme UI** — Clean Catppuccin-inspired interface

## Requirements

- **Windows 10/11** (64-bit)
- **Python 3.10+**
- **mpv** media player (for video playback)
- **SteamVR** + **Valve Index Controller(s)** (for VR input; optional with mouse fallback)

### Optional
- **ffmpeg** (system binary) for beat detection audio extraction
- **numpy** for beat detection signal processing

## Quick Start

### 1. Install Dependencies
```bash
pip install PyQt6 python-mpv openvr numpy
```
Or run `install.bat`.

### 2. Install mpv
- **winget**: `winget install mpv`
- **Chocolatey**: `choco install mpv`
- **Manual**: Download from [mpv.io](https://mpv.io/installation/) and add to PATH

### 3. Launch
```bash
python main.py
```
Or double-click `run.bat`.

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Space` | Play / Pause |
| `R` | Start / Stop recording |
| `Escape` | Cancel recording |
| `←` / `→` | Skip back/forward 5s |
| `Ctrl+←` / `Ctrl+→` | Skip back/forward 10s |
| `,` / `.` | Previous / Next frame |
| `Ctrl+Z` / `Ctrl+Y` | Undo / Redo |
| `Delete` | Delete selected data |
| `Ctrl+O` | Open video |
| `Ctrl+S` | Save project |
| `Ctrl+E` | Export funscript |
| `Ctrl+H` | Toggle heatmap |
| `Ctrl+B` | Toggle beat markers |
| `Ctrl+3` | Toggle 3D controller overlay |
| `Ctrl+4` | Toggle position bar overlay |

## Processing Pipeline

1. **Raw Input** — Controller polled at ~90Hz
2. **Kalman Filter** — Reduces tracking jitter
3. **Stabilization Pre-Map** — Spike rejection → One Euro → Slew-rate → Jerk limit
4. **Axis Lock** — Frozen axes use held value
5. **Calibration Mapping** — Raw → 0-100
6. **Stabilization Post-Map** — Deadzone + Hysteresis
7. **Recording** — Samples stored with video timestamps
8. **Point Reduction** — Ramer-Douglas-Peucker algorithm
9. **Optional Smoothing** — Moving average filter

## Architecture

```
main.py                — Entry point, dark theme
main_window.py         — Main window, integrates all components
video_player.py        — mpv/Qt video player
vr_controller.py       — OpenVR tracking, stabilization, axis lock
stabilization.py       — Per-axis stabilization pipeline
recorder.py            — Recording engine with undo/redo
timeline_widget.py     — Timeline with overlays, heatmap, beats
position_display.py    — Position indicator with trail effect
calibration_wizard.py  — Guided calibration overlay
controller_viz.py      — 3D controller visualization
funscript_io.py        — Funscript format + project + bundle I/O
beat_detection.py      — Audio beat detection pipeline
vision_tracking.py     — Video motion tracking foundation
```

## License

MIT License — Free for personal and commercial use.
