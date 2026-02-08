"""
Position Display Widget
========================
Visual indicator showing current controller position on a 0-100 scale
with trail effect showing recent movement history.

Comes in two forms:
- PositionDisplay: docked in the sidebar with a pop-out button
- PositionDisplayOverlay: floating transparent overlay on the video
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSlider, QSizeGrip, QSizePolicy
)
from PyQt6.QtCore import Qt, QRectF, QPointF, QPoint, QSize, pyqtSignal
from PyQt6.QtGui import (QPainter, QPen, QColor, QBrush, QPainterPath,
                          QLinearGradient, QRadialGradient, QFont, QMouseEvent)

from collections import deque
import time


# ============================================================
# Shared painting canvas
# ============================================================

class _PositionCanvas(QWidget):
    """
    Core painting widget for the position bar.  Used by both the
    docked panel and the floating overlay.
    """

    # Colors
    COLOR_BG = QColor(24, 24, 37)
    COLOR_BAR_BG = QColor(49, 50, 68)
    COLOR_BAR_BORDER = QColor(69, 71, 90)
    COLOR_INDICATOR = QColor(137, 180, 250)
    COLOR_INDICATOR_GLOW = QColor(137, 180, 250, 60)
    COLOR_TRAIL = QColor(137, 180, 250, 80)
    COLOR_TEXT = QColor(205, 214, 244)
    COLOR_RECORDING = QColor(243, 139, 168)
    COLOR_GRID = QColor(69, 71, 90, 80)
    COLOR_LABEL = QColor(166, 173, 200)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._position: int = 50
        self._axis_label: str = "Stroke"
        self._trail = deque(maxlen=30)
        self._last_trail_time = 0
        self._is_recording = False
        self._is_tracked = False
        self._secondary_positions: dict = {}
        self._alpha = 255  # For overlay transparency

    def set_position(self, pos: int):
        self._position = max(0, min(100, pos))
        now = time.time()
        if now - self._last_trail_time > 0.016:
            self._trail.append((pos, now))
            self._last_trail_time = now
        self.update()

    def set_secondary_positions(self, positions: dict):
        self._secondary_positions = positions
        self.update()

    def set_recording(self, recording: bool):
        self._is_recording = recording
        self.update()

    def set_tracked(self, tracked: bool):
        self._is_tracked = tracked
        self.update()

    def set_axis_label(self, label: str):
        self._axis_label = label
        self.update()

    def set_alpha(self, alpha: int):
        self._alpha = alpha
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        alpha = self._alpha

        # Background
        p.fillRect(self.rect(), QColor(24, 24, 37, min(alpha, 40)))

        # Layout
        top_margin = 30
        bottom_margin = 40
        bar_left = 20
        bar_width = 30
        bar_height = h - top_margin - bottom_margin
        bar_right = bar_left + bar_width

        if bar_height < 20:
            p.end()
            return

        # Axis label
        font = QFont("Segoe UI", 9, QFont.Weight.Bold)
        p.setFont(font)
        p.setPen(QPen(QColor(166, 173, 200, alpha)))
        p.drawText(QRectF(0, 5, w, 20), Qt.AlignmentFlag.AlignHCenter, self._axis_label)

        # Main bar background
        bar_rect = QRectF(bar_left, top_margin, bar_width, bar_height)
        p.setPen(QPen(QColor(69, 71, 90, alpha), 1))
        p.setBrush(QBrush(QColor(49, 50, 68, alpha)))
        p.drawRoundedRect(bar_rect, 4, 4)

        # Grid lines at 25% intervals
        p.setPen(QPen(QColor(69, 71, 90, min(alpha, 80)), 1, Qt.PenStyle.DotLine))
        for pct in [25, 50, 75]:
            y = top_margin + bar_height * (1 - pct / 100.0)
            p.drawLine(QPointF(bar_left + 2, y), QPointF(bar_right - 2, y))

        # Scale labels
        small_font = QFont("Segoe UI", 7)
        p.setFont(small_font)
        p.setPen(QPen(QColor(69, 71, 90, alpha)))
        for val in [0, 50, 100]:
            y = top_margin + bar_height * (1 - val / 100.0)
            p.drawText(QRectF(bar_right + 3, y - 7, 25, 14),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       str(val))

        # Trail
        now = time.time()
        for pos, t in self._trail:
            age = now - t
            if age > 0.5:
                continue
            trail_alpha = int(min(alpha, 80) * (1 - age / 0.5))
            y = top_margin + bar_height * (1 - pos / 100.0)
            color = QColor(137, 180, 250, trail_alpha)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            size = 3 + 3 * (1 - age / 0.5)
            p.drawEllipse(QPointF(bar_left + bar_width / 2, y), size, size)

        # Fill bar up to position
        if self._is_tracked:
            fill_height = bar_height * (self._position / 100.0)
            fill_rect = QRectF(bar_left + 2, top_margin + bar_height - fill_height,
                              bar_width - 4, fill_height)

            indicator_color = self.COLOR_RECORDING if self._is_recording else self.COLOR_INDICATOR
            gradient = QLinearGradient(fill_rect.topLeft(), fill_rect.bottomLeft())
            gradient.setColorAt(0, QColor(indicator_color.red(), indicator_color.green(),
                                          indicator_color.blue(), min(alpha, 120)))
            gradient.setColorAt(1, QColor(indicator_color.red(), indicator_color.green(),
                                          indicator_color.blue(), min(alpha, 40)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(gradient))
            p.drawRoundedRect(fill_rect, 2, 2)

            # Position indicator
            y = top_margin + bar_height * (1 - self._position / 100.0)

            # Glow
            glow_color = QColor(indicator_color.red(), indicator_color.green(),
                               indicator_color.blue(), min(alpha, 40))
            glow = QRadialGradient(QPointF(bar_left + bar_width / 2, y), 20)
            glow.setColorAt(0, glow_color)
            glow.setColorAt(1, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(glow))
            p.drawEllipse(QPointF(bar_left + bar_width / 2, y), 20, 20)

            # Line
            p.setPen(QPen(QColor(indicator_color.red(), indicator_color.green(),
                                 indicator_color.blue(), alpha), 3))
            p.drawLine(QPointF(bar_left, y), QPointF(bar_right, y))

            # Circle
            p.setBrush(QBrush(QColor(indicator_color.red(), indicator_color.green(),
                                     indicator_color.blue(), alpha)))
            p.setPen(QPen(QColor(24, 24, 37, alpha), 2))
            p.drawEllipse(QPointF(bar_left + bar_width / 2, y), 7, 7)

        # Numeric readout
        font = QFont("Segoe UI", 14, QFont.Weight.Bold)
        p.setFont(font)
        text_color = self.COLOR_RECORDING if self._is_recording else self.COLOR_TEXT
        p.setPen(QPen(QColor(text_color.red(), text_color.green(),
                             text_color.blue(), alpha)))
        p.drawText(QRectF(0, h - bottom_margin + 5, w, 30),
                   Qt.AlignmentFlag.AlignHCenter,
                   str(self._position) if self._is_tracked else "---")

        # Recording dot
        if self._is_recording:
            p.setBrush(QBrush(QColor(243, 139, 168, alpha)))
            p.setPen(Qt.PenStyle.NoPen)
            blink = int(time.time() * 3) % 2 == 0
            if blink:
                p.drawEllipse(QPointF(w - 12, top_margin - 12), 5, 5)

        # Not tracked warning
        if not self._is_tracked:
            font = QFont("Segoe UI", 8)
            p.setFont(font)
            p.setPen(QPen(QColor(243, 139, 168, min(alpha, 180))))
            p.drawText(QRectF(0, h - 15, w, 15),
                       Qt.AlignmentFlag.AlignHCenter, "No tracking")

        # Secondary axis mini-bars
        if self._secondary_positions:
            mini_left = bar_right + 25
            mini_width = 8
            mini_height = bar_height * 0.6
            mini_top = top_margin + bar_height * 0.2
            idx = 0
            for name, pos in list(self._secondary_positions.items())[:3]:
                x = mini_left + idx * (mini_width + 6)
                if x + mini_width > w:
                    break
                p.setPen(QPen(QColor(69, 71, 90, alpha), 1))
                p.setBrush(QBrush(QColor(49, 50, 68, alpha)))
                mini_rect = QRectF(x, mini_top, mini_width, mini_height)
                p.drawRoundedRect(mini_rect, 2, 2)

                fill_h = mini_height * (pos / 100.0)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(QColor(166, 173, 200, min(alpha, 100))))
                p.drawRoundedRect(QRectF(x + 1, mini_top + mini_height - fill_h,
                                         mini_width - 2, fill_h), 1, 1)

                p.setFont(QFont("Segoe UI", 6))
                p.setPen(QPen(QColor(166, 173, 200, alpha)))
                p.drawText(QRectF(x - 2, mini_top - 12, mini_width + 4, 10),
                           Qt.AlignmentFlag.AlignHCenter, name[0].upper())
                idx += 1

        p.end()


# ============================================================
# Docked panel (sidebar)
# ============================================================

class PositionDisplay(QWidget):
    """
    Vertical bar widget showing the current mapped position (0-100).
    Includes a trail of recent positions, numeric readout, and a
    pop-out button to create a floating overlay.
    """

    overlay_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(80)
        self.setMaximumWidth(120)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # Header with pop-out button
        header = QHBoxLayout()
        title = QLabel("Position")
        title.setFont(QFont("Segoe UI", 10))
        title.setStyleSheet("color: #a6adc8;")
        header.addWidget(title)
        header.addStretch()

        overlay_btn = QPushButton("[^]")
        overlay_btn.setToolTip("Pop out as floating overlay (Ctrl+4)")
        overlay_btn.setFixedSize(24, 24)
        overlay_btn.setStyleSheet("""
            QPushButton { background: #313244; color: #cdd6f4; border: none;
                border-radius: 4px; font-size: 14px; }
            QPushButton:hover { background: #45475a; }
        """)
        overlay_btn.clicked.connect(self.overlay_requested.emit)
        header.addWidget(overlay_btn)
        layout.addLayout(header)

        # Canvas
        self._canvas = _PositionCanvas()
        layout.addWidget(self._canvas, 1)

    # Forward API to canvas
    def set_position(self, pos: int):
        self._canvas.set_position(pos)

    def set_secondary_positions(self, positions: dict):
        self._canvas.set_secondary_positions(positions)

    def set_recording(self, recording: bool):
        self._canvas.set_recording(recording)

    def set_tracked(self, tracked: bool):
        self._canvas.set_tracked(tracked)

    def set_axis_label(self, label: str):
        self._canvas.set_axis_label(label)


# ============================================================
# Floating overlay
# ============================================================

class PositionDisplayOverlay(QWidget):
    """
    Floating semi-transparent overlay showing the position bar.
    Draggable, resizable, with adjustable transparency.
    """

    closed = pyqtSignal()

    TITLE_HEIGHT = 28
    MIN_SIZE = QSize(90, 200)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(self.MIN_SIZE)
        self.resize(120, 350)

        self._anchor_parent = parent
        self._opacity = 200
        self._dragging = False
        self._resizing = False
        self._drag_offset = QPoint()
        self._resize_start_size = QSize()
        self._resize_start_pos = QPoint()

        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Title bar
        self._title_bar = QWidget()
        self._title_bar.setFixedHeight(self.TITLE_HEIGHT)
        title_layout = QHBoxLayout(self._title_bar)
        title_layout.setContentsMargins(6, 2, 4, 2)
        title_layout.setSpacing(4)

        title = QLabel("Position")
        title.setFont(QFont("Segoe UI", 9))
        title.setStyleSheet("color: #cdd6f4;")
        title_layout.addWidget(title)
        title_layout.addStretch()

        # Transparency slider
        opacity_label = QLabel("Op:")
        opacity_label.setStyleSheet("color: #a6adc8; font-size: 11px;")
        opacity_label.setToolTip("Transparency")
        title_layout.addWidget(opacity_label)

        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(30, 255)
        self._opacity_slider.setValue(self._opacity)
        self._opacity_slider.setFixedWidth(50)
        self._opacity_slider.setStyleSheet("""
            QSlider::groove:horizontal { background: #313244; height: 4px; border-radius: 2px; }
            QSlider::handle:horizontal { background: #89b4fa; width: 10px; margin: -3px 0; border-radius: 5px; }
        """)
        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        title_layout.addWidget(self._opacity_slider)

        close_btn = QPushButton("X")
        close_btn.setFixedSize(20, 20)
        close_btn.setStyleSheet("""
            QPushButton { background: transparent; color: #6c7086; border: none; font-size: 12px; }
            QPushButton:hover { color: #f38ba8; }
        """)
        close_btn.clicked.connect(self._close)
        title_layout.addWidget(close_btn)

        layout.addWidget(self._title_bar)

        # Canvas
        self._canvas = _PositionCanvas()
        self._canvas.set_alpha(self._opacity)
        layout.addWidget(self._canvas, 1)

        # Size grip
        self._grip = QSizeGrip(self)
        self._grip.setFixedSize(14, 14)
        self._grip.setStyleSheet("background: transparent;")

    # ---- Public API (forward to canvas) ----

    def set_position(self, pos: int):
        self._canvas.set_position(pos)

    def set_secondary_positions(self, positions: dict):
        self._canvas.set_secondary_positions(positions)

    def set_recording(self, recording: bool):
        self._canvas.set_recording(recording)

    def set_tracked(self, tracked: bool):
        self._canvas.set_tracked(tracked)

    def set_axis_label(self, label: str):
        self._canvas.set_axis_label(label)

    # ---- Internal ----

    def _on_opacity_changed(self, val):
        self._opacity = val
        self._canvas.set_alpha(val)
        self.update()

    def _close(self):
        self.hide()
        self.closed.emit()

    # ---- Drag / Resize ----

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            if event.position().y() < self.TITLE_HEIGHT:
                self._dragging = True
                self._drag_offset = event.globalPosition().toPoint() - self.pos()
            elif (event.position().x() > self.width() - 16 and
                  event.position().y() > self.height() - 16):
                self._resizing = True
                self._resize_start_size = self.size()
                self._resize_start_pos = event.globalPosition().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._dragging:
            new_pos = event.globalPosition().toPoint() - self._drag_offset
            if self._anchor_parent:
                parent_global = self._anchor_parent.mapToGlobal(
                    self._anchor_parent.rect().topLeft()
                )
                pw = self._anchor_parent.width()
                ph = self._anchor_parent.height()
                new_pos.setX(max(parent_global.x(),
                                 min(parent_global.x() + pw - self.width(), new_pos.x())))
                new_pos.setY(max(parent_global.y(),
                                 min(parent_global.y() + ph - self.height(), new_pos.y())))
            self.move(new_pos)
        elif self._resizing:
            delta = event.globalPosition().toPoint() - self._resize_start_pos
            new_w = max(self.MIN_SIZE.width(), self._resize_start_size.width() + delta.x())
            new_h = max(self.MIN_SIZE.height(), self._resize_start_size.height() + delta.y())
            self.resize(new_w, new_h)
        else:
            if (event.position().x() > self.width() - 16 and
                    event.position().y() > self.height() - 16):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif event.position().y() < self.TITLE_HEIGHT:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._dragging = False
        self._resizing = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseReleaseEvent(event)

    # ---- Painting ----

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        bg = QColor(24, 24, 37, min(self._opacity, 220))
        p.fillRect(self.rect(), bg)

        title_bg = QColor(30, 30, 46, min(self._opacity, 240))
        p.fillRect(0, 0, self.width(), self.TITLE_HEIGHT, title_bg)

        border = QColor(69, 71, 90, min(self._opacity, 160))
        p.setPen(QPen(border, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 6, 6)

        # Resize grip dots
        grip_color = QColor(88, 91, 112, min(self._opacity, 120))
        p.setPen(QPen(grip_color, 1))
        bx, by = self.width() - 12, self.height() - 12
        for i in range(3):
            for j in range(3 - i):
                p.drawPoint(bx + i * 4, by + j * 4)

        p.end()

    def resizeEvent(self, event):
        self._grip.move(self.width() - self._grip.width(),
                        self.height() - self._grip.height())
        super().resizeEvent(event)
