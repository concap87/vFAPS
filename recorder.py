"""
Recorder Module
================
Manages recording sessions, undo/redo, and data processing.
"""

import copy
import time
from dataclasses import dataclass, field
from typing import Optional
from funscript_io import FunscriptAction, FunscriptAxis, FunscriptProject, AXIS_DEFINITIONS


@dataclass
class RecordingSegment:
    """A single recording segment with metadata."""
    axis_name: str
    actions: list[FunscriptAction]
    start_ms: int
    end_ms: int
    timestamp: float = 0.0  # When this was recorded

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()


class UndoManager:
    """Manages undo/redo history for recorded segments."""

    def __init__(self, max_history: int = 100):
        self.max_history = max_history
        self._undo_stack: list[tuple[str, RecordingSegment, list[FunscriptAction]]] = []
        self._redo_stack: list[tuple[str, RecordingSegment, list[FunscriptAction]]] = []

    def push(self, action_type: str, segment: RecordingSegment,
             previous_actions: list[FunscriptAction]):
        """Push an action onto the undo stack."""
        self._undo_stack.append((action_type, segment, previous_actions))
        if len(self._undo_stack) > self.max_history:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def can_undo(self) -> bool:
        return len(self._undo_stack) > 0

    def can_redo(self) -> bool:
        return len(self._redo_stack) > 0

    def undo(self) -> Optional[tuple[str, RecordingSegment, list[FunscriptAction]]]:
        """Pop from undo stack, push to redo stack."""
        if not self._undo_stack:
            return None
        item = self._undo_stack.pop()
        self._redo_stack.append(item)
        return item

    def redo(self) -> Optional[tuple[str, RecordingSegment, list[FunscriptAction]]]:
        """Pop from redo stack, push to undo stack."""
        if not self._redo_stack:
            return None
        item = self._redo_stack.pop()
        self._undo_stack.append(item)
        return item

    def clear(self):
        self._undo_stack.clear()
        self._redo_stack.clear()


class Recorder:
    """
    Main recording engine.
    Handles recording controller input synchronized with video timestamps.
    """

    def __init__(self, project: FunscriptProject):
        self.project = project
        self.undo_manager = UndoManager()

        # Recording state
        self.is_recording = False
        self.active_axis: str = "stroke"  # Which axis to record to
        self.active_axes: set[str] = {"stroke"}  # Multi-axis recording

        # Current recording buffer
        self._buffer: dict[str, list[FunscriptAction]] = {}
        self._record_start_ms: int = 0

        # Recording settings
        self.sample_rate: int = 90  # Target samples per second
        self.min_interval_ms: int = 11  # Minimum ms between samples (~90Hz)
        self._last_sample_time: dict[str, int] = {}

        # Point reduction settings
        self.point_reduction_enabled: bool = True
        self.rdp_epsilon: float = 1.5  # Ramer-Douglas-Peucker tolerance

    def start_recording(self, video_time_ms: int):
        """Begin a recording session."""
        self.is_recording = True
        self._record_start_ms = video_time_ms
        self._buffer = {axis: [] for axis in self.active_axes}
        self._last_sample_time = {axis: -1 for axis in self.active_axes}

    def stop_recording(self) -> dict[str, RecordingSegment]:
        """
        End recording session. Apply processing and commit to project.
        Returns the recorded segments.
        """
        self.is_recording = False
        segments = {}

        for axis_name, actions in self._buffer.items():
            if not actions:
                continue

            # Apply point reduction if enabled
            if self.point_reduction_enabled and len(actions) > 3:
                actions = self._reduce_points(actions)

            segment = RecordingSegment(
                axis_name=axis_name,
                actions=actions,
                start_ms=actions[0].at,
                end_ms=actions[-1].at,
            )

            # Save previous state for undo
            axis = self.project.get_axis(axis_name)
            previous = axis.get_actions_in_range(segment.start_ms, segment.end_ms)

            # Commit to project
            axis.add_actions(actions)

            # Push to undo stack
            self.undo_manager.push("record", segment, copy.deepcopy(previous))
            segments[axis_name] = segment

        self._buffer.clear()
        return segments

    def cancel_recording(self):
        """Cancel current recording without saving."""
        self.is_recording = False
        self._buffer.clear()

    def add_sample(self, video_time_ms: int, mapped_positions: dict[str, int]):
        """
        Add a sample during recording.
        mapped_positions: dict of axis_name -> 0-100 value
        """
        if not self.is_recording:
            return

        for axis_name in self.active_axes:
            # Map funscript axis name (e.g. "stroke") to controller axis (e.g. "y")
            axis_def = AXIS_DEFINITIONS.get(axis_name)
            if axis_def:
                controller_axis = axis_def["controller_axis"]
            else:
                controller_axis = axis_name

            if controller_axis not in mapped_positions:
                continue

            pos = mapped_positions.get(controller_axis, 50)

            # Enforce minimum interval
            last_time = self._last_sample_time.get(axis_name, -1)
            if last_time >= 0 and (video_time_ms - last_time) < self.min_interval_ms:
                continue

            action = FunscriptAction(at=video_time_ms, pos=max(0, min(100, pos)))

            if axis_name not in self._buffer:
                self._buffer[axis_name] = []
            self._buffer[axis_name].append(action)
            self._last_sample_time[axis_name] = video_time_ms

    def get_buffer_preview(self) -> dict[str, list[FunscriptAction]]:
        """Get current recording buffer for live preview."""
        return dict(self._buffer)

    def undo(self) -> bool:
        """Undo the last recording action."""
        result = self.undo_manager.undo()
        if result is None:
            return False

        action_type, segment, previous_actions = result
        axis = self.project.get_axis(segment.axis_name)

        # Remove the recorded segment
        axis.remove_actions_in_range(segment.start_ms, segment.end_ms)

        # Restore previous actions
        if previous_actions:
            axis.actions.extend(previous_actions)
            axis.sort_actions()

        return True

    def redo(self) -> bool:
        """Redo the last undone action."""
        result = self.undo_manager.redo()
        if result is None:
            return False

        action_type, segment, _ = result
        axis = self.project.get_axis(segment.axis_name)
        axis.add_actions(segment.actions)
        return True

    def clear_range(self, axis_name: str, start_ms: int, end_ms: int):
        """Clear all actions in a time range (with undo support)."""
        axis = self.project.get_axis(axis_name)
        previous = copy.deepcopy(axis.get_actions_in_range(start_ms, end_ms))

        if not previous:
            return

        axis.remove_actions_in_range(start_ms, end_ms)

        segment = RecordingSegment(
            axis_name=axis_name,
            actions=[],
            start_ms=start_ms,
            end_ms=end_ms,
        )
        self.undo_manager.push("clear", segment, previous)

    def _reduce_points(self, actions: list[FunscriptAction]) -> list[FunscriptAction]:
        """
        Apply Ramer-Douglas-Peucker algorithm to reduce point count
        while preserving movement shape.
        """
        if len(actions) <= 2:
            return actions

        points = [(a.at, a.pos) for a in actions]
        reduced = self._rdp(points, self.rdp_epsilon)
        return [FunscriptAction(at=int(p[0]), pos=int(p[1])) for p in reduced]

    def _rdp(self, points: list[tuple], epsilon: float) -> list[tuple]:
        """Ramer-Douglas-Peucker algorithm implementation."""
        if len(points) <= 2:
            return points

        # Find point with max distance from line between first and last
        start = points[0]
        end = points[-1]
        max_dist = 0
        max_idx = 0

        for i in range(1, len(points) - 1):
            dist = self._perpendicular_distance(points[i], start, end)
            if dist > max_dist:
                max_dist = dist
                max_idx = i

        if max_dist > epsilon:
            left = self._rdp(points[:max_idx + 1], epsilon)
            right = self._rdp(points[max_idx:], epsilon)
            return left[:-1] + right
        else:
            return [start, end]

    @staticmethod
    def _perpendicular_distance(point, line_start, line_end):
        """Calculate perpendicular distance from a point to a line segment."""
        # Normalize time axis to be comparable with position axis
        t_range = max(1, line_end[0] - line_start[0])
        p_range = 100  # funscript range

        px = (point[0] - line_start[0]) / t_range * p_range
        py = point[1]
        lx1 = 0
        ly1 = line_start[1]
        lx2 = p_range
        ly2 = line_end[1]

        dx = lx2 - lx1
        dy = ly2 - ly1
        denom = (dx * dx + dy * dy) ** 0.5

        if denom == 0:
            return ((px - lx1) ** 2 + (py - ly1) ** 2) ** 0.5

        return abs(dy * px - dx * py + lx2 * ly1 - ly2 * lx1) / denom

    def apply_smoothing(self, axis_name: str, window_size: int = 5,
                         start_ms: Optional[int] = None, end_ms: Optional[int] = None):
        """
        Apply moving average smoothing to an axis.
        Optionally only within a time range.
        """
        axis = self.project.get_axis(axis_name)
        if not axis.actions:
            return

        # Save for undo
        if start_ms is not None and end_ms is not None:
            original = copy.deepcopy(axis.get_actions_in_range(start_ms, end_ms))
            target_actions = axis.get_actions_in_range(start_ms, end_ms)
        else:
            original = copy.deepcopy(axis.actions)
            target_actions = axis.actions
            start_ms = target_actions[0].at if target_actions else 0
            end_ms = target_actions[-1].at if target_actions else 0

        if len(target_actions) < window_size:
            return

        positions = [a.pos for a in target_actions]
        smoothed = self._moving_average(positions, window_size)

        for action, new_pos in zip(target_actions, smoothed):
            action.pos = max(0, min(100, int(round(new_pos))))

        segment = RecordingSegment(
            axis_name=axis_name,
            actions=copy.deepcopy(target_actions),
            start_ms=start_ms,
            end_ms=end_ms,
        )
        self.undo_manager.push("smooth", segment, original)

    @staticmethod
    def _moving_average(data: list[float], window: int) -> list[float]:
        """Simple moving average."""
        result = []
        half = window // 2
        for i in range(len(data)):
            start = max(0, i - half)
            end = min(len(data), i + half + 1)
            result.append(sum(data[start:end]) / (end - start))
        return result
