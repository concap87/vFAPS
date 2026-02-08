"""
Timeline Widget
================
Custom PyQt6 widget for visualizing and interacting with funscript data
along a timeline synchronized with the video player.

Supports: zoom, pan, selection, click-to-seek, and direct point editing
(click-drag points to adjust time and position).
"""

from PyQt6.QtWidgets import QWidget, QMenu
from PyQt6.QtCore import Qt, QRectF, pyqtSignal, QPointF, QTimer
from PyQt6.QtGui import (QPainter, QPen, QColor, QBrush, QPainterPath,
                          QLinearGradient, QMouseEvent, QWheelEvent,
                          QContextMenuEvent, QFont)

from funscript_io import FunscriptAction
import copy


class TimelineWidget(QWidget):
    """
    Timeline visualization showing funscript action data as a graph.
    Supports zooming, panning, selection, click-to-seek, and direct
    point editing via click-and-drag.
    """

    # Signals
    seek_requested = pyqtSignal(int)              # Seek to timestamp (ms)
    selection_changed = pyqtSignal(int, int)       # Selection range (start_ms, end_ms)
    clear_selection_requested = pyqtSignal()
    clear_range_requested = pyqtSignal(int, int)   # Clear data in range
    point_moved = pyqtSignal(int, int, int)        # (action_index, new_at, new_pos)
    point_deleted = pyqtSignal(int)                # action_index to delete
    point_added = pyqtSignal(int, int)             # (at_ms, pos) - add a new point
    points_modified = pyqtSignal()                 # Generic "data changed" notification

    # Colors
    COLOR_BG = QColor(24, 24, 37)
    COLOR_GRID = QColor(49, 50, 68, 120)
    COLOR_GRID_TEXT = QColor(166, 173, 200, 180)
    COLOR_LINE = QColor(137, 180, 250)
    COLOR_LINE_FILL = QColor(137, 180, 250, 30)
    COLOR_PLAYHEAD = QColor(243, 139, 168)
    COLOR_SELECTION = QColor(137, 180, 250, 40)
    COLOR_SELECTION_BORDER = QColor(137, 180, 250, 120)
    COLOR_RECORDING = QColor(243, 139, 168, 50)
    COLOR_BUFFER_LINE = QColor(243, 139, 168, 180)
    COLOR_POINT = QColor(137, 180, 250)
    COLOR_POINT_HOVER = QColor(250, 179, 135)
    COLOR_POINT_SELECTED = QColor(166, 227, 161)       # Green for selected
    COLOR_POINT_DRAGGING = QColor(245, 224, 220)        # Light for dragging
    COLOR_POINT_RING = QColor(255, 255, 255, 80)        # Outer ring on hover

    # Overlay lane colors (per-axis, with transparency)
    OVERLAY_COLORS = {
        "stroke":  QColor(137, 180, 250, 90),   # blue
        "surge":   QColor(166, 227, 161, 90),    # green
        "sway":    QColor(250, 179, 135, 90),    # peach
        "twist":   QColor(203, 166, 247, 90),    # mauve
        "roll":    QColor(245, 194, 231, 90),    # pink
        "pitch":   QColor(148, 226, 213, 90),    # teal
    }

    # Beat marker color
    COLOR_BEAT = QColor(249, 226, 175, 200)         # yellow/gold - bright
    COLOR_BEAT_SUB = QColor(249, 226, 175, 80)       # subdivisions

    # Hit-test radius in pixels
    POINT_HIT_RADIUS = 10
    POINT_DRAW_RADIUS = 4
    POINT_HOVER_RADIUS = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setMaximumHeight(250)
        self.setSizePolicy(self.sizePolicy())
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        # Data - this is a REFERENCE to the axis's action list, kept in sync
        self._actions: list[FunscriptAction] = []
        self._buffer_actions: list[FunscriptAction] = []  # Live recording preview

        # View state
        self._duration_ms: int = 60000  # Total video duration
        self._view_start_ms: int = 0     # Left edge of visible window
        self._view_end_ms: int = 60000   # Right edge of visible window
        self._playhead_ms: int = 0       # Current playback position

        # Zoom/pan state
        self._zoom_level: float = 1.0
        self._min_zoom: float = 1.0
        self._max_zoom: float = 100.0
        self._is_panning: bool = False
        self._pan_start_x: int = 0
        self._pan_start_view: int = 0

        # Selection state
        self._selection_start_ms: int = -1
        self._selection_end_ms: int = -1
        self._is_selecting: bool = False

        # Recording indicator
        self._is_recording: bool = False

        # --- Point editing state ---
        self._hover_idx: int = -1            # Index of point under mouse
        self._selected_idx: int = -1         # Currently selected (clicked) point
        self._dragging_idx: int = -1         # Index of point being dragged
        self._is_dragging: bool = False
        self._drag_start_action: FunscriptAction = None  # Snapshot before drag
        self._drag_offset_x: float = 0       # Pixel offset from point center at grab
        self._drag_offset_y: float = 0

        # Layout
        self._margin_left = 45
        self._margin_right = 10
        self._margin_top = 10
        self._margin_bottom = 25

        # --- Multi-lane overlays (Phase 3) ---
        self._primary_axis_id: str = "stroke"
        self._overlay_lanes: dict[str, dict] = {}  # {axis_id: {"actions": list, "visible": bool}}

        # --- Heatmap (Phase 4) ---
        self._show_heatmap: bool = False

        # --- Beat / analysis lanes (Phase 5) ---
        self._analysis_lanes: dict[str, dict] = {}  # {lane_id: {"data": any, "visible": bool}}

    # ---- Public API ----

    def set_actions(self, actions: list[FunscriptAction]):
        """Set the funscript actions to display (keeps reference). Backward-compat."""
        self._actions = actions
        # Reset editing state when data reloads
        self._hover_idx = -1
        self._selected_idx = -1
        self._dragging_idx = -1
        self._is_dragging = False
        self.update()

    # --- Multi-lane API (Phase 3) ---

    def set_primary_lane(self, axis_id: str, actions: list):
        """Set the primary (editable) lane."""
        self._primary_axis_id = axis_id
        self._actions = actions
        self._hover_idx = -1
        self._selected_idx = -1
        self._dragging_idx = -1
        self._is_dragging = False
        self.update()

    def set_overlay_lanes(self, lanes: dict, visibility: dict | None = None):
        """Set overlay lanes: {axis_id: actions_list}."""
        for axis_id, actions in lanes.items():
            self._overlay_lanes[axis_id] = {
                "actions": actions,
                "visible": visibility.get(axis_id, True) if visibility else True,
            }
        self.update()

    def set_overlay_visibility(self, axis_id: str, visible: bool):
        """Toggle a single overlay lane on/off."""
        if axis_id in self._overlay_lanes:
            self._overlay_lanes[axis_id]["visible"] = visible
            self.update()

    def set_analysis_lane(self, lane_id: str, data, visible: bool = True):
        """Set an analysis lane (beats, vision, etc.)."""
        self._analysis_lanes[lane_id] = {"data": data, "visible": visible}
        self.update()

    def set_analysis_visibility(self, lane_id: str, visible: bool):
        if lane_id in self._analysis_lanes:
            self._analysis_lanes[lane_id]["visible"] = visible
            self.update()

    # --- Heatmap API (Phase 4) ---

    def set_heatmap_visible(self, visible: bool):
        self._show_heatmap = visible
        self.update()

    def set_buffer_actions(self, actions: list[FunscriptAction]):
        """Set live recording buffer actions (shown in different color)."""
        self._buffer_actions = actions
        self.update()

    def set_duration(self, duration_ms: int):
        """Set total video duration."""
        self._duration_ms = max(1, duration_ms)
        self._view_end_ms = min(self._view_end_ms, self._duration_ms)
        self.update()

    def set_playhead(self, time_ms: int):
        """Update playback position - always accepted, always repaints."""
        self._playhead_ms = time_ms
        # Auto-scroll to keep playhead visible (only when NOT dragging a point)
        if not self._is_dragging:
            if time_ms < self._view_start_ms or time_ms > self._view_end_ms:
                view_width = self._view_end_ms - self._view_start_ms
                self._view_start_ms = max(0, time_ms - view_width // 4)
                self._view_end_ms = self._view_start_ms + view_width
                if self._view_end_ms > self._duration_ms:
                    self._view_end_ms = self._duration_ms
                    self._view_start_ms = max(0, self._view_end_ms - view_width)
        self.update()

    def set_recording(self, is_recording: bool):
        """Set recording indicator state."""
        self._is_recording = is_recording
        self.update()

    def zoom_to_fit(self):
        """Reset zoom to show entire timeline."""
        self._view_start_ms = 0
        self._view_end_ms = self._duration_ms
        self._zoom_level = 1.0
        self.update()

    def get_selection(self) -> tuple[int, int]:
        """Get current selection range, or (-1, -1) if no selection."""
        if self._selection_start_ms < 0:
            return (-1, -1)
        return (min(self._selection_start_ms, self._selection_end_ms),
                max(self._selection_start_ms, self._selection_end_ms))

    def clear_selection(self):
        """Clear the current selection."""
        self._selection_start_ms = -1
        self._selection_end_ms = -1
        self.update()

    def get_selected_point_index(self) -> int:
        """Return the currently selected point index, or -1."""
        return self._selected_idx

    def deselect_point(self):
        """Deselect the current point."""
        self._selected_idx = -1
        self.update()

    # ---- Coordinate conversion ----

    def _get_plot_rect(self) -> QRectF:
        """Get the plotting area rectangle."""
        return QRectF(
            self._margin_left,
            self._margin_top,
            self.width() - self._margin_left - self._margin_right,
            self.height() - self._margin_top - self._margin_bottom
        )

    def _ms_to_x(self, time_ms: int) -> float:
        """Convert timestamp to x pixel coordinate."""
        rect = self._get_plot_rect()
        view_range = max(1, self._view_end_ms - self._view_start_ms)
        return rect.left() + (time_ms - self._view_start_ms) / view_range * rect.width()

    def _x_to_ms(self, x: float) -> int:
        """Convert x pixel coordinate to timestamp."""
        rect = self._get_plot_rect()
        view_range = self._view_end_ms - self._view_start_ms
        t = (x - rect.left()) / max(1, rect.width()) * view_range + self._view_start_ms
        return max(0, min(self._duration_ms, int(t)))

    def _pos_to_y(self, pos: int) -> float:
        """Convert funscript position (0-100) to y pixel coordinate."""
        rect = self._get_plot_rect()
        return rect.bottom() - (pos / 100.0) * rect.height()

    def _y_to_pos(self, y: float) -> int:
        """Convert y pixel coordinate to funscript position (0-100)."""
        rect = self._get_plot_rect()
        pos = (rect.bottom() - y) / max(1, rect.height()) * 100
        return max(0, min(100, int(round(pos))))

    # ---- Hit testing ----

    def _hit_test_point(self, mx: float, my: float) -> int:
        """
        Find the index of the action point nearest to pixel (mx, my).
        Returns -1 if nothing is within POINT_HIT_RADIUS.
        """
        best_idx = -1
        best_dist_sq = self.POINT_HIT_RADIUS ** 2

        for i, action in enumerate(self._actions):
            if action.at < self._view_start_ms - 500 or action.at > self._view_end_ms + 500:
                continue
            px = self._ms_to_x(action.at)
            py = self._pos_to_y(action.pos)
            dx = mx - px
            dy = my - py
            dist_sq = dx * dx + dy * dy
            if dist_sq < best_dist_sq:
                best_dist_sq = dist_sq
                best_idx = i

        return best_idx

    # ---- Painting ----

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self._get_plot_rect()

        # Background
        p.fillRect(self.rect(), self.COLOR_BG)

        # Grid
        self._draw_grid(p, rect)

        # Heatmap (behind everything else)
        if self._show_heatmap and self._actions:
            self._draw_heatmap(p, rect)

        # Selection highlight
        if self._selection_start_ms >= 0:
            self._draw_selection(p, rect)

        # Recording indicator
        if self._is_recording:
            self._draw_recording_indicator(p, rect)

        # Beat markers (analysis lane)
        beat_lane = self._analysis_lanes.get("beats")
        if beat_lane and beat_lane.get("visible") and beat_lane.get("data"):
            self._draw_beat_markers(p, rect, beat_lane["data"])

        # Overlay lanes (read-only, rendered before primary)
        for axis_id, lane in self._overlay_lanes.items():
            if lane.get("visible") and lane.get("actions"):
                color = self.OVERLAY_COLORS.get(axis_id, QColor(180, 180, 180, 70))
                fill = QColor(color.red(), color.green(), color.blue(), 20)
                self._draw_actions_line(p, rect, lane["actions"], color, fill)

        # Committed action data (primary lane: line + fill + points)
        if self._actions:
            self._draw_actions_line(p, rect, self._actions, self.COLOR_LINE, self.COLOR_LINE_FILL)
            self._draw_action_points(p, rect, self._actions)

        # Live recording buffer
        if self._buffer_actions:
            self._draw_actions_line(p, rect, self._buffer_actions, self.COLOR_BUFFER_LINE, self.COLOR_RECORDING)

        # Playhead
        self._draw_playhead(p, rect)

        # Tooltip overlay for hovered/selected point
        self._draw_point_tooltip(p, rect)

        p.end()

    def _draw_grid(self, p: QPainter, rect: QRectF):
        """Draw time and position grid lines."""
        pen = QPen(self.COLOR_GRID, 1, Qt.PenStyle.DotLine)
        p.setPen(pen)
        font = QFont("Segoe UI", 8)
        p.setFont(font)

        # Horizontal lines (position 0, 25, 50, 75, 100)
        for pos in [0, 25, 50, 75, 100]:
            y = self._pos_to_y(pos)
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            p.setPen(QPen(self.COLOR_GRID_TEXT))
            p.drawText(QRectF(0, y - 8, self._margin_left - 5, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       str(pos))
            p.setPen(pen)

        # Vertical lines (time markers)
        view_range = self._view_end_ms - self._view_start_ms
        intervals = [100, 250, 500, 1000, 2000, 5000, 10000, 30000, 60000, 300000]
        target_px = 80
        time_per_px = view_range / max(1, rect.width())
        target_interval = target_px * time_per_px

        interval = intervals[0]
        for iv in intervals:
            if iv >= target_interval:
                interval = iv
                break
        else:
            interval = intervals[-1]

        start_t = int(self._view_start_ms / interval) * interval
        t = start_t
        while t <= self._view_end_ms:
            if t >= self._view_start_ms:
                x = self._ms_to_x(t)
                p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
                p.setPen(QPen(self.COLOR_GRID_TEXT))
                label = self._format_time(t)
                p.drawText(QRectF(x - 30, rect.bottom() + 2, 60, 20),
                           Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                           label)
                p.setPen(pen)
            t += interval

    def _draw_actions_line(self, p: QPainter, rect: QRectF,
                           actions: list[FunscriptAction],
                           line_color: QColor, fill_color: QColor):
        """Draw action data as a filled line graph (no points - those are separate)."""
        visible = [a for a in actions
                    if self._view_start_ms - 1000 <= a.at <= self._view_end_ms + 1000]
        if not visible:
            return

        path = QPainterPath()
        fill_path = QPainterPath()

        first_x = self._ms_to_x(visible[0].at)
        first_y = self._pos_to_y(visible[0].pos)
        path.moveTo(first_x, first_y)
        fill_path.moveTo(first_x, rect.bottom())
        fill_path.lineTo(first_x, first_y)

        for action in visible[1:]:
            x = self._ms_to_x(action.at)
            y = self._pos_to_y(action.pos)
            path.lineTo(x, y)
            fill_path.lineTo(x, y)

        last_x = self._ms_to_x(visible[-1].at)
        fill_path.lineTo(last_x, rect.bottom())
        fill_path.closeSubpath()

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(fill_color))
        p.drawPath(fill_path)

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(line_color, 2))
        p.drawPath(path)

    def _draw_action_points(self, p: QPainter, rect: QRectF,
                            actions: list[FunscriptAction]):
        """Draw individual action points with hover/select/drag states."""
        visible_count = sum(
            1 for a in actions
            if self._view_start_ms - 500 <= a.at <= self._view_end_ms + 500
        )
        if visible_count > 500:
            return

        for i, action in enumerate(actions):
            if action.at < self._view_start_ms - 500 or action.at > self._view_end_ms + 500:
                continue

            x = self._ms_to_x(action.at)
            y = self._pos_to_y(action.pos)
            center = QPointF(x, y)

            is_hover = (i == self._hover_idx and not self._is_dragging)
            is_selected = (i == self._selected_idx)
            is_dragging = (i == self._dragging_idx and self._is_dragging)

            if is_dragging:
                radius = self.POINT_HOVER_RADIUS + 1
                fill_color = self.COLOR_POINT_DRAGGING
                border_color = QColor(255, 255, 255, 200)
                border_width = 2
            elif is_selected:
                radius = self.POINT_HOVER_RADIUS
                fill_color = self.COLOR_POINT_SELECTED
                border_color = QColor(255, 255, 255, 160)
                border_width = 2
            elif is_hover:
                radius = self.POINT_HOVER_RADIUS
                fill_color = self.COLOR_POINT_HOVER
                border_color = self.COLOR_POINT_RING
                border_width = 2
            else:
                radius = self.POINT_DRAW_RADIUS
                fill_color = self.COLOR_POINT
                border_color = QColor(0, 0, 0, 0)
                border_width = 0

            # Outer glow ring for interactive points
            if is_hover or is_selected or is_dragging:
                ring_color = QColor(fill_color)
                ring_color.setAlpha(30)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(ring_color))
                p.drawEllipse(center, radius + 5, radius + 5)

            # Main dot
            if border_width > 0:
                p.setPen(QPen(border_color, border_width))
            else:
                p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(fill_color))
            p.drawEllipse(center, radius, radius)

    def _draw_point_tooltip(self, p: QPainter, rect: QRectF):
        """Draw a small info tooltip near the hovered or dragged point."""
        idx = self._dragging_idx if self._is_dragging else self._hover_idx
        if idx < 0 or idx >= len(self._actions):
            return

        action = self._actions[idx]
        x = self._ms_to_x(action.at)
        y = self._pos_to_y(action.pos)

        text = f"{self._format_time(action.at)}  pos:{action.pos}"
        font = QFont("Consolas", 8)
        p.setFont(font)

        tw = 120
        th = 18
        tx = x - tw / 2
        ty = y - th - 14

        if tx < rect.left():
            tx = rect.left()
        if tx + tw > rect.right():
            tx = rect.right() - tw
        if ty < rect.top():
            ty = y + 14

        bg = QColor(49, 50, 68, 220)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(bg))
        p.drawRoundedRect(QRectF(tx, ty, tw, th), 3, 3)

        p.setPen(QPen(QColor(205, 214, 244)))
        p.drawText(QRectF(tx + 4, ty, tw - 8, th),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter,
                   text)

    def _draw_playhead(self, p: QPainter, rect: QRectF):
        """Draw the playhead line."""
        x = self._ms_to_x(self._playhead_ms)
        if rect.left() <= x <= rect.right():
            pen = QPen(self.COLOR_PLAYHEAD, 2)
            p.setPen(pen)
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom() + 20))

            p.setBrush(QBrush(self.COLOR_PLAYHEAD))
            p.setPen(Qt.PenStyle.NoPen)
            triangle = QPainterPath()
            triangle.moveTo(x - 6, rect.top() - 2)
            triangle.lineTo(x + 6, rect.top() - 2)
            triangle.lineTo(x, rect.top() + 6)
            triangle.closeSubpath()
            p.drawPath(triangle)

    def _draw_selection(self, p: QPainter, rect: QRectF):
        """Draw selection highlight."""
        s1, s2 = self.get_selection()
        x1 = self._ms_to_x(s1)
        x2 = self._ms_to_x(s2)
        sel_rect = QRectF(x1, rect.top(), x2 - x1, rect.height())
        p.fillRect(sel_rect, QBrush(self.COLOR_SELECTION))
        p.setPen(QPen(self.COLOR_SELECTION_BORDER, 1, Qt.PenStyle.DashLine))
        p.drawRect(sel_rect)

    def _draw_recording_indicator(self, p: QPainter, rect: QRectF):
        """Draw recording zone indicator."""
        x = self._ms_to_x(self._playhead_ms)
        rec_rect = QRectF(x - 2, rect.top(), 4, rect.height())
        p.fillRect(rec_rect, QBrush(self.COLOR_RECORDING))

    def _draw_heatmap(self, p: QPainter, rect: QRectF):
        """Draw time-density heatmap behind the primary data."""
        if not self._actions:
            return
        # Aim for ~8px wide buckets for a clear visual
        bucket_w = max(4, int(rect.width() / 120))
        n_buckets = max(1, int(rect.width() / bucket_w))
        buckets = [0] * n_buckets
        view_range = max(1, self._view_end_ms - self._view_start_ms)

        for a in self._actions:
            if a.at < self._view_start_ms or a.at > self._view_end_ms:
                continue
            frac = (a.at - self._view_start_ms) / view_range
            bi = min(n_buckets - 1, int(frac * n_buckets))
            buckets[bi] += 1

        max_count = max(buckets) if buckets else 1
        if max_count == 0:
            return

        p.setPen(Qt.PenStyle.NoPen)
        for i, count in enumerate(buckets):
            if count == 0:
                continue
            intensity = count / max_count
            # Color ramp: dark blue -> purple -> orange
            if intensity < 0.5:
                t = intensity * 2
                r = int(49 + (137 - 49) * t)
                g = int(50 + (70 - 50) * t)
                b = int(120 + (180 - 120) * t)
            else:
                t = (intensity - 0.5) * 2
                r = int(137 + (250 - 137) * t)
                g = int(70 + (179 - 70) * t)
                b = int(180 + (135 - 180) * t)
            alpha = int(60 + 140 * intensity)
            color = QColor(r, g, b, alpha)
            p.setBrush(QBrush(color))
            bx = rect.left() + i * bucket_w
            p.drawRect(QRectF(bx, rect.top(), bucket_w, rect.height()))

    def _draw_beat_markers(self, p: QPainter, rect: QRectF, beat_data):
        """Draw beat markers as top/bottom ticks so they don't obscure the waveform."""
        # Get beat timestamps
        if hasattr(beat_data, 'beats'):
            beats = beat_data.beats or []
        elif isinstance(beat_data, dict):
            beats = beat_data.get('beats', [])
        else:
            beats = []

        if not beats:
            return

        # Get BPM for display
        bpm = 0.0
        if hasattr(beat_data, 'bpm'):
            bpm = beat_data.bpm
        elif isinstance(beat_data, dict):
            bpm = beat_data.get('bpm', 0.0)

        # Calculate density
        visible_beats = [b for b in beats
                         if self._view_start_ms <= b <= self._view_end_ms]

        if len(visible_beats) >= 2:
            avg_px_spacing = rect.width() / max(1, len(visible_beats))
        else:
            avg_px_spacing = rect.width()

        # Skip if way too dense
        if len(visible_beats) > 500:
            self._draw_bpm_badge(p, rect, bpm, len(beats))
            return

        # Tick heights — short markers at top and bottom edges
        tick_h = min(12, rect.height() * 0.12)

        # Subdivision grid (faint dashes in the middle)
        draw_subs = avg_px_spacing > 50
        if draw_subs:
            beat_set = set(beats)
            if hasattr(beat_data, 'get_beat_grid'):
                grid = beat_data.get_beat_grid()
            else:
                grid = []

            if grid:
                sub_pen = QPen(self.COLOR_BEAT_SUB, 1, Qt.PenStyle.DotLine)
                p.setPen(sub_pen)
                for t_ms in grid:
                    if t_ms in beat_set:
                        continue
                    if t_ms < self._view_start_ms or t_ms > self._view_end_ms:
                        continue
                    x = self._ms_to_x(t_ms)
                    # Only draw small ticks at top/bottom for subdivisions
                    p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.top() + tick_h * 0.5))
                    p.drawLine(QPointF(x, rect.bottom() - tick_h * 0.5), QPointF(x, rect.bottom()))

        # Main beat markers
        beat_color = self.COLOR_BEAT
        for t_ms in beats:
            if t_ms < self._view_start_ms or t_ms > self._view_end_ms:
                continue
            x = self._ms_to_x(t_ms)

            # Top tick (solid, prominent)
            p.setPen(QPen(beat_color, 2))
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.top() + tick_h))

            # Bottom tick
            p.drawLine(QPointF(x, rect.bottom() - tick_h), QPointF(x, rect.bottom()))

            # Faint connecting dash through the data area (very subtle)
            faint_color = QColor(beat_color.red(), beat_color.green(), beat_color.blue(), 35)
            p.setPen(QPen(faint_color, 1, Qt.PenStyle.DotLine))
            p.drawLine(QPointF(x, rect.top() + tick_h), QPointF(x, rect.bottom() - tick_h))

            # Small dot at top when zoomed in
            if avg_px_spacing > 30:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(beat_color))
                p.drawEllipse(QPointF(x, rect.top() + 3), 2.5, 2.5)

        # BPM badge
        self._draw_bpm_badge(p, rect, bpm, len(beats))

    def _draw_bpm_badge(self, p: QPainter, rect: QRectF, bpm: float, n_beats: int):
        """Draw BPM indicator badge in the timeline."""
        if bpm <= 0:
            return
        text = f"♩ {bpm:.0f} BPM ({n_beats} beats)"
        font = QFont("Segoe UI", 8, QFont.Weight.Bold)
        p.setFont(font)

        tw = 140
        th = 18
        tx = rect.right() - tw - 5
        ty = rect.top() + 4

        # Background pill
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(49, 50, 68, 200)))
        p.drawRoundedRect(QRectF(tx, ty, tw, th), 4, 4)

        # Text
        p.setPen(QPen(self.COLOR_BEAT))
        p.drawText(QRectF(tx + 4, ty, tw - 8, th),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter,
                   text)

    @staticmethod
    def _format_time(ms: int) -> str:
        """Format milliseconds as MM:SS.s"""
        total_s = ms / 1000.0
        minutes = int(total_s // 60)
        seconds = total_s % 60
        if minutes > 0:
            return f"{minutes}:{seconds:04.1f}"
        return f"{seconds:.1f}s"

    # ---- Mouse events ----

    def mousePressEvent(self, event: QMouseEvent):
        rect = self._get_plot_rect()
        if not rect.contains(event.position()):
            return

        mx = event.position().x()
        my = event.position().y()

        if event.button() == Qt.MouseButton.LeftButton:
            # First, check if we clicked on an existing point
            hit_idx = self._hit_test_point(mx, my)

            if hit_idx >= 0:
                # Start dragging this point
                self._selected_idx = hit_idx
                self._dragging_idx = hit_idx
                self._is_dragging = True
                a = self._actions[hit_idx]
                self._drag_start_action = FunscriptAction(at=a.at, pos=a.pos)
                px = self._ms_to_x(a.at)
                py = self._pos_to_y(a.pos)
                self._drag_offset_x = px - mx
                self._drag_offset_y = py - my
                self.setCursor(Qt.CursorShape.SizeAllCursor)
                self.update()

            elif event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                # Start selection
                self._is_selecting = True
                self._selection_start_ms = self._x_to_ms(mx)
                self._selection_end_ms = self._selection_start_ms
                self._selected_idx = -1

            elif event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                # Ctrl+click: add a new point at this location
                at_ms = self._x_to_ms(mx)
                pos = self._y_to_pos(my)
                self.point_added.emit(at_ms, pos)
                self._selected_idx = -1

            else:
                # Plain click: seek video + deselect
                ms = self._x_to_ms(mx)
                self.seek_requested.emit(ms)
                self._selected_idx = -1
                self.clear_selection()

        elif event.button() == Qt.MouseButton.MiddleButton:
            self._is_panning = True
            self._pan_start_x = int(mx)
            self._pan_start_view = self._view_start_ms
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent):
        mx = event.position().x()
        my = event.position().y()

        if self._is_dragging and self._dragging_idx >= 0:
            new_at = self._x_to_ms(mx + self._drag_offset_x)
            new_pos = self._y_to_pos(my + self._drag_offset_y)
            new_at = max(0, min(self._duration_ms, new_at))
            new_pos = max(0, min(100, new_pos))

            if self._dragging_idx < len(self._actions):
                self._actions[self._dragging_idx].at = new_at
                self._actions[self._dragging_idx].pos = new_pos
            self.update()

        elif self._is_panning:
            dx = int(mx) - self._pan_start_x
            view_range = self._view_end_ms - self._view_start_ms
            rect = self._get_plot_rect()
            dt = int(-dx / max(1, rect.width()) * view_range)
            new_start = max(0, min(self._duration_ms - view_range, self._pan_start_view + dt))
            self._view_start_ms = new_start
            self._view_end_ms = new_start + view_range
            self.update()

        elif self._is_selecting:
            self._selection_end_ms = self._x_to_ms(mx)
            self.update()

        else:
            # Hover detection
            old_hover = self._hover_idx
            self._hover_idx = self._hit_test_point(mx, my)
            if self._hover_idx != old_hover:
                if self._hover_idx >= 0:
                    self.setCursor(Qt.CursorShape.PointingHandCursor)
                else:
                    self.setCursor(Qt.CursorShape.CrossCursor)
                self.update()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._is_dragging and self._dragging_idx >= 0:
            idx = self._dragging_idx
            if idx < len(self._actions) and self._drag_start_action:
                new_action = self._actions[idx]
                if (new_action.at != self._drag_start_action.at or
                        new_action.pos != self._drag_start_action.pos):
                    self.point_moved.emit(idx, new_action.at, new_action.pos)
                    self.points_modified.emit()
            self._is_dragging = False
            self._dragging_idx = -1
            self._drag_start_action = None
            if self._hover_idx >= 0:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.setCursor(Qt.CursorShape.CrossCursor)
            self.update()

        if self._is_panning:
            self._is_panning = False
            self.setCursor(Qt.CursorShape.CrossCursor)

        if self._is_selecting:
            self._is_selecting = False
            s1, s2 = self.get_selection()
            if abs(s2 - s1) > 50:
                self.selection_changed.emit(s1, s2)
            else:
                self.clear_selection()

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        """Double-click on empty area adds a new point; on a point selects & seeks."""
        rect = self._get_plot_rect()
        if not rect.contains(event.position()):
            return

        mx = event.position().x()
        my = event.position().y()
        hit_idx = self._hit_test_point(mx, my)

        if hit_idx >= 0:
            self._selected_idx = hit_idx
            self.seek_requested.emit(self._actions[hit_idx].at)
            self.update()
        else:
            at_ms = self._x_to_ms(mx)
            pos = self._y_to_pos(my)
            self.point_added.emit(at_ms, pos)

    def keyPressEvent(self, event):
        """Handle Delete key to remove selected point."""
        if event.key() == Qt.Key.Key_Delete and self._selected_idx >= 0:
            self.point_deleted.emit(self._selected_idx)
            self._selected_idx = -1
            self.update()
        else:
            super().keyPressEvent(event)

    def wheelEvent(self, event: QWheelEvent):
        """Zoom in/out centered on mouse position."""
        delta = event.angleDelta().y()
        if delta == 0:
            return

        factor = 1.15 if delta > 0 else 1 / 1.15

        mouse_x = event.position().x()
        mouse_time = self._x_to_ms(int(mouse_x))

        view_range = self._view_end_ms - self._view_start_ms
        new_range = max(500, min(self._duration_ms, int(view_range / factor)))

        rect = self._get_plot_rect()
        mouse_frac = (mouse_x - rect.left()) / max(1, rect.width())
        new_start = int(mouse_time - mouse_frac * new_range)
        new_start = max(0, min(self._duration_ms - new_range, new_start))

        self._view_start_ms = new_start
        self._view_end_ms = new_start + new_range
        self._zoom_level = self._duration_ms / max(1, new_range)
        self.update()

    def contextMenuEvent(self, event: QContextMenuEvent):
        """Right-click context menu."""
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #1e1e2e;
                color: #cdd6f4;
                border: 1px solid #313244;
            }
            QMenu::item:selected {
                background-color: #45475a;
            }
        """)

        mx = event.pos().x()
        my = event.pos().y()
        hit_idx = self._hit_test_point(mx, my)

        if hit_idx >= 0:
            action = self._actions[hit_idx]
            info = menu.addAction(f"Point: {self._format_time(action.at)}, pos {action.pos}")
            info.setEnabled(False)
            menu.addSeparator()
            delete_pt = menu.addAction("Delete This Point")
            delete_pt.triggered.connect(lambda: self._emit_delete_point(hit_idx))
        else:
            at_ms = self._x_to_ms(mx)
            pos_val = self._y_to_pos(my)
            add_pt = menu.addAction(f"Add Point Here ({self._format_time(at_ms)}, pos {pos_val})")
            add_pt.triggered.connect(lambda: self.point_added.emit(at_ms, pos_val))

        menu.addSeparator()

        zoom_fit = menu.addAction("Zoom to Fit")
        zoom_fit.triggered.connect(self.zoom_to_fit)

        s1, s2 = self.get_selection()
        if s1 >= 0:
            menu.addSeparator()
            clear_sel = menu.addAction("Clear Selection")
            clear_sel.triggered.connect(self.clear_selection)
            clear_range = menu.addAction(
                f"Delete Points in Selection ({self._format_time(s1)} - {self._format_time(s2)})"
            )
            clear_range.triggered.connect(lambda: self.clear_range_requested.emit(s1, s2))

        menu.exec(event.globalPos())

    def _emit_delete_point(self, idx: int):
        self.point_deleted.emit(idx)
        if self._selected_idx == idx:
            self._selected_idx = -1
        if self._hover_idx == idx:
            self._hover_idx = -1
        self.update()
