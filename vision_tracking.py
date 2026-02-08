"""
Vision Tracking Module
=======================
Extracts motion signals from video frames by tracking a user-selected
region of interest (ROI).  Produces a time-series of 0-100 values
that can be displayed as an analysis lane and optionally blended
with recorded controller data.

Approach:
  1. User selects an ROI rectangle on a video frame
  2. Track ROI across frames using template correlation or optical flow
  3. Map motion to a 0-100 scalar series on a chosen axis (Y for vertical, X for horizontal)

Dependencies (optional, not required at install time):
  - ffmpeg (system binary) for frame extraction
  - numpy (already required)

This module is designed to run as a background task and produce results
that get loaded into the timeline as an analysis lane.
"""

import os
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Optional, Callable

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


@dataclass
class ROI:
    """Region of interest in normalized coordinates (0.0 to 1.0)."""
    x: float = 0.0      # Left edge
    y: float = 0.0      # Top edge
    width: float = 0.1   # Width
    height: float = 0.1  # Height

    def to_pixel(self, frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
        """Convert to pixel coordinates (x, y, w, h)."""
        return (
            int(self.x * frame_w),
            int(self.y * frame_h),
            int(self.width * frame_w),
            int(self.height * frame_h),
        )


@dataclass
class TrackingResult:
    """Result of vision tracking — a time series of position values."""
    # List of (time_ms, value_0_100) tuples
    series: list[tuple[int, int]] = field(default_factory=list)
    # Which axis this maps to
    axis: str = "y"  # "y" for vertical, "x" for horizontal
    # Tracking metadata
    fps: float = 0.0
    frame_count: int = 0
    roi: Optional[ROI] = None

    def to_dict(self) -> dict:
        return {
            "series": self.series,
            "axis": self.axis,
            "fps": self.fps,
            "frame_count": self.frame_count,
            "roi": {"x": self.roi.x, "y": self.roi.y,
                    "width": self.roi.width, "height": self.roi.height}
                   if self.roi else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TrackingResult":
        roi = None
        if d.get("roi"):
            roi = ROI(**d["roi"])
        return cls(
            series=[(t, v) for t, v in d.get("series", [])],
            axis=d.get("axis", "y"),
            fps=d.get("fps", 0.0),
            frame_count=d.get("frame_count", 0),
            roi=roi,
        )

    def to_actions(self):
        """Convert to FunscriptAction list for timeline display."""
        from funscript_io import FunscriptAction
        return [FunscriptAction(at=t, pos=v) for t, v in self.series]


def _check_ffmpeg() -> bool:
    """Check if ffmpeg is available."""
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _extract_frames_raw(video_path: str, fps: float = 10.0,
                        width: int = 320, height: int = 240,
                        max_frames: int = 10000) -> tuple[Optional[bytes], int, int, int]:
    """
    Extract frames as raw RGB bytes using ffmpeg.
    Returns (raw_bytes, width, height, n_frames) or (None, 0, 0, 0) on failure.
    """
    if not _check_ffmpeg():
        return None, 0, 0, 0

    try:
        result = subprocess.run([
            "ffmpeg", "-i", video_path,
            "-vf", f"fps={fps},scale={width}:{height}",
            "-pix_fmt", "rgb24",
            "-f", "rawvideo",
            "-frames:v", str(max_frames),
            "-"
        ], capture_output=True, timeout=300)

        raw = result.stdout
        frame_size = width * height * 3
        n_frames = len(raw) // frame_size

        if n_frames == 0:
            return None, 0, 0, 0

        # Trim to exact frame boundaries
        raw = raw[:n_frames * frame_size]
        return raw, width, height, n_frames

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None, 0, 0, 0


def track_roi(video_path: str, roi: ROI, fps: float = 10.0,
              axis: str = "y", progress_callback: Callable = None) -> Optional[TrackingResult]:
    """
    Track an ROI across video frames and produce a position series.

    Parameters
    ----------
    video_path : str
        Path to the video file.
    roi : ROI
        Region of interest to track (normalized coords).
    fps : float
        Analysis frame rate (lower = faster, less precise).
    axis : str
        "y" to track vertical motion, "x" for horizontal.
    progress_callback : callable
        Called with float 0.0-1.0 progress.

    Returns TrackingResult or None on failure.
    """
    if not HAS_NUMPY:
        return None

    if progress_callback:
        progress_callback(0.0)

    # Extract frames at reduced resolution for speed
    raw_bytes, fw, fh, n_frames = _extract_frames_raw(
        video_path, fps=fps, width=320, height=240
    )

    if raw_bytes is None or n_frames == 0:
        return None

    frame_size = fw * fh * 3
    frame_interval_ms = 1000.0 / fps

    # Convert ROI to pixel coords
    rx, ry, rw, rh = roi.to_pixel(fw, fh)
    rx = max(0, min(fw - rw, rx))
    ry = max(0, min(fh - rh, ry))
    rw = max(8, rw)
    rh = max(8, rh)

    if progress_callback:
        progress_callback(0.1)

    # Extract initial template
    frame0 = np.frombuffer(raw_bytes[:frame_size], dtype=np.uint8).reshape(fh, fw, 3)
    template = frame0[ry:ry+rh, rx:rx+rw].copy().astype(np.float32)
    template_gray = np.mean(template, axis=2)  # Convert to grayscale

    # Search area (expanded region around ROI)
    search_margin = max(rw, rh)  # Search this many pixels beyond ROI
    search_x1 = max(0, rx - search_margin)
    search_y1 = max(0, ry - search_margin)
    search_x2 = min(fw, rx + rw + search_margin)
    search_y2 = min(fh, ry + rh + search_margin)

    series = []
    prev_pos = (rx + rw // 2, ry + rh // 2)  # Center of ROI

    for i in range(n_frames):
        offset = i * frame_size
        frame = np.frombuffer(
            raw_bytes[offset:offset + frame_size],
            dtype=np.uint8
        ).reshape(fh, fw, 3)

        # Extract search region
        search_region = frame[search_y1:search_y2, search_x1:search_x2]
        search_gray = np.mean(search_region.astype(np.float32), axis=2)

        # Template matching via normalized cross-correlation (simplified)
        best_score = -1
        best_pos = prev_pos

        # Slide template over search region
        sh, sw = search_gray.shape
        th, tw = template_gray.shape

        if sh >= th and sw >= tw:
            # Use vectorized sum-of-squared-differences for speed
            for dy in range(0, sh - th + 1, 2):  # step=2 for speed
                for dx in range(0, sw - tw + 1, 2):
                    patch = search_gray[dy:dy+th, dx:dx+tw]
                    # Normalized cross-correlation
                    p_mean = np.mean(patch)
                    t_mean = np.mean(template_gray)
                    num = np.sum((patch - p_mean) * (template_gray - t_mean))
                    den = np.sqrt(
                        np.sum((patch - p_mean)**2) *
                        np.sum((template_gray - t_mean)**2) + 1e-10
                    )
                    score = num / den

                    if score > best_score:
                        best_score = score
                        best_pos = (
                            search_x1 + dx + tw // 2,
                            search_y1 + dy + th // 2
                        )

        # Map position to 0-100
        if axis == "y":
            val = int((1.0 - best_pos[1] / fh) * 100)
        else:
            val = int(best_pos[0] / fw * 100)
        val = max(0, min(100, val))

        time_ms = int(i * frame_interval_ms)
        series.append((time_ms, val))
        prev_pos = best_pos

        # Update search area around found position
        search_x1 = max(0, best_pos[0] - rw // 2 - search_margin)
        search_y1 = max(0, best_pos[1] - rh // 2 - search_margin)
        search_x2 = min(fw, best_pos[0] + rw // 2 + search_margin)
        search_y2 = min(fh, best_pos[1] + rh // 2 + search_margin)

        if progress_callback and i % 10 == 0:
            progress_callback(0.1 + 0.9 * i / n_frames)

    if progress_callback:
        progress_callback(1.0)

    return TrackingResult(
        series=series,
        axis=axis,
        fps=fps,
        frame_count=n_frames,
        roi=roi,
    )
