"""
Controller 3D Visualization
=============================
Renders a real-time 3D wireframe representation of the Valve Index controller
using QPainter with perspective projection.  Comes in two flavours:

- ControllerVizPanel: docked in the right sidebar with an "overlay" button
- ControllerVizOverlay: floating transparent widget that sits on top of the
  video, draggable + resizable + adjustable transparency

Includes Calibration (Ctrl+R) to recenter the view.
NOTE: Uses a shared global state for calibration so all views stay in sync.
"""

import math
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSlider, QFrame, QSizePolicy, QSizeGrip
)
from PyQt6.QtCore import Qt, pyqtSignal, QPointF, QPoint, QSize, QRectF, QObject
from PyQt6.QtGui import (
    QPainter, QPen, QColor, QBrush, QFont, QPainterPath,
    QMouseEvent, QLinearGradient
)


# ============================================================
# Shared State Management (The Fix for Mismatched Views)
# ============================================================

class _VizSharedState(QObject):
    """
    Singleton-style state to ensure Panel and Overlay share
    the exact same calibration offsets.
    """
    state_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        # Offsets (Pitch, Yaw, Roll)
        self.offsets = (0.0, 0.0, 0.0)

    def set_offsets(self, p, y, r):
        self.offsets = (p, y, r)
        self.state_changed.emit()

    def get_offsets(self):
        return self.offsets

# Global instance shared by all widgets in this module
_SHARED_STATE = _VizSharedState()


# ============================================================
# 3D math helpers
# ============================================================

def _rot_x(pts, angle_deg):
    """Rotate list of (x,y,z) around X axis."""
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    return [(x, y * ca - z * sa, y * sa + z * ca) for x, y, z in pts]

def _rot_y(pts, angle_deg):
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    return [(x * ca + z * sa, y, -x * sa + z * ca) for x, y, z in pts]

def _rot_z(pts, angle_deg):
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    return [(x * ca - y * sa, x * sa + y * ca, z) for x, y, z in pts]

def _translate(pts, dx, dy, dz):
    return [(x + dx, y + dy, z + dz) for x, y, z in pts]

def _project(pts, cx, cy, fov=400, camera_z=5.0):
    """Perspective project 3D points to 2D screen coords."""
    result = []
    for x, y, z in pts:
        zz = z + camera_z
        if zz < 0.1:
            zz = 0.1
        sx = cx + x * fov / zz
        sy = cy - y * fov / zz  # y-up in 3D, y-down on screen
        result.append((sx, sy, zz))
    return result


# ============================================================
# Controller 3D model (wireframe vertices + edges)
# ============================================================

def _build_controller_model():
    """
    Build a simplified Valve Index controller wireframe.
    """
    verts = []
    edges = []
    faces = []

    # --- Handle body (rectangular prism) ---
    hw, hh, hd = 0.20, 0.60, 0.15
    box = [
        (-hw, -hh, -hd), ( hw, -hh, -hd), ( hw,  hh, -hd), (-hw,  hh, -hd),
        (-hw, -hh,  hd), ( hw, -hh,  hd), ( hw,  hh,  hd), (-hw,  hh,  hd),
    ]
    base = len(verts)
    verts.extend(box)
    for i, j in [(0,1),(1,2),(2,3),(3,0), (4,5),(5,6),(6,7),(7,4), (0,4),(1,5),(2,6),(3,7)]:
        edges.append((base+i, base+j))
    faces.append([base+4, base+5, base+6, base+7])
    faces.append([base+0, base+3, base+2, base+1])

    # --- Trigger ---
    tw, td = 0.12, 0.10
    trig = [
        (-tw,  -hh+0.05,  hd), ( tw,  -hh+0.05,  hd),
        ( tw,  -hh-0.15,  hd+td), (-tw,  -hh-0.15,  hd+td),
    ]
    base_t = len(verts)
    verts.extend(trig)
    for i, j in [(0,1),(1,2),(2,3),(3,0)]:
        edges.append((base_t+i, base_t+j))
    faces.append([base_t+0, base_t+1, base_t+2, base_t+3])

    # --- Tracking ring ---
    ring_r = 0.35
    ring_y = hh + 0.10
    ring_h = 0.08
    n_ring = 12
    base_r = len(verts)
    for i in range(n_ring):
        a = 2 * math.pi * i / n_ring
        rx = ring_r * math.cos(a)
        rz = ring_r * math.sin(a)
        verts.append((rx, ring_y, rz))
        verts.append((rx, ring_y + ring_h, rz))
    for i in range(n_ring):
        i2 = (i + 1) % n_ring
        edges.append((base_r + i*2, base_r + i2*2))
        edges.append((base_r + i*2+1, base_r + i2*2+1))
        edges.append((base_r + i*2, base_r + i*2+1))

    # --- Thumbstick ---
    stick_r = 0.06
    stick_y = hh + 0.01
    stick_cx = 0.0
    stick_cz = -0.02
    n_stick = 8
    base_s = len(verts)
    for i in range(n_stick):
        a = 2 * math.pi * i / n_stick
        verts.append((stick_cx + stick_r * math.cos(a), stick_y, stick_cz + stick_r * math.sin(a)))
    for i in range(n_stick):
        edges.append((base_s + i, base_s + (i+1) % n_stick))

    # --- Buttons ---
    for bx, bz, label in [(0.08, 0.05, "A"), (-0.08, 0.05, "B")]:
        bs = 0.03
        base_b = len(verts)
        verts.extend([
            (bx-bs, hh+0.01, bz-bs), (bx+bs, hh+0.01, bz-bs),
            (bx+bs, hh+0.01, bz+bs), (bx-bs, hh+0.01, bz+bs),
        ])
        for i, j in [(0,1),(1,2),(2,3),(3,0)]:
            edges.append((base_b+i, base_b+j))

    return verts, edges, faces


_MODEL_VERTS, _MODEL_EDGES, _MODEL_FACES = _build_controller_model()


# ============================================================
# Core renderer
# ============================================================

class _ControllerRenderer:
    """Stateless renderer: call render() with a QPainter and current angles."""

    @staticmethod
    def render(painter: QPainter, width: int, height: int,
               pitch: float, yaw: float, roll: float,
               pos_x: float, pos_y: float, pos_z: float,
               is_tracked: bool, alpha: int = 255,
               raw_pitch: float = 0, raw_yaw: float = 0, raw_roll: float = 0):
        
        cx = width / 2
        cy = height / 2
        fov = min(width, height) * 0.9

        # Start from base model
        pts = list(_MODEL_VERTS)

        # Apply controller rotation (order: roll -> pitch -> yaw)
        # Using VISUAL (calibrated) angles
        pts = _rot_z(pts, roll)
        pts = _rot_x(pts, pitch)
        pts = _rot_y(pts, yaw)

        dx = max(-1, min(1, pos_x)) * 0.4
        dy = max(-1, min(1, pos_y - 0.7)) * 0.4
        dz = max(-1, min(1, pos_z)) * 0.4
        pts = _translate(pts, dx, dy, dz)

        projected = _project(pts, cx, cy, fov, camera_z=4.0)

        if not is_tracked:
            _ControllerRenderer._draw_not_tracked(painter, width, height, alpha)
            return

        # Faces
        for face_indices in _MODEL_FACES:
            face_pts = [projected[i] for i in face_indices]
            if len(face_pts) >= 3:
                path = QPainterPath()
                path.moveTo(face_pts[0][0], face_pts[0][1])
                for fp in face_pts[1:]:
                    path.lineTo(fp[0], fp[1])
                path.closeSubpath()
                face_color = QColor(69, 71, 90, min(alpha, 80))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(face_color))
                painter.drawPath(path)

        # Edges
        for i, j in _MODEL_EDGES:
            x1, y1, z1 = projected[i]
            x2, y2, z2 = projected[j]
            avg_z = (z1 + z2) / 2
            depth_alpha = max(40, min(alpha, int(alpha * (1.0 - (avg_z - 3.0) / 4.0))))
            pen = QPen(QColor(137, 180, 250, depth_alpha), 1.5)
            painter.setPen(pen)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        # Axes
        origin = [(0, 0, 0)]
        ax_x = [(0.3, 0, 0)]
        ax_y = [(0, 0.3, 0)]
        ax_z = [(0, 0, 0.3)]

        for ax_pts, color in [(ax_x, QColor(243, 139, 168, alpha)),
                               (ax_y, QColor(166, 227, 161, alpha)),
                               (ax_z, QColor(137, 180, 250, alpha))]:
            o = _rot_z(_rot_x(_rot_y(origin, yaw), pitch), roll)
            a = _rot_z(_rot_x(_rot_y(ax_pts, yaw), pitch), roll)
            o = _translate(o, dx, dy, dz)
            a = _translate(a, dx, dy, dz)
            po = _project(o, cx, cy, fov, 4.0)
            pa = _project(a, cx, cy, fov, 4.0)
            painter.setPen(QPen(color, 2))
            painter.drawLine(QPointF(po[0][0], po[0][1]),
                             QPointF(pa[0][0], pa[0][1]))

        # Text - Always display RAW values
        font = QFont("Consolas", 9)
        painter.setFont(font)
        text_color = QColor(166, 173, 200, alpha)
        painter.setPen(text_color)
        text = f"P:{raw_pitch:+.0f}° Y:{raw_yaw:+.0f}° R:{raw_roll:+.0f}°"
        painter.drawText(QRectF(4, height - 20, width - 8, 18),
                         Qt.AlignmentFlag.AlignCenter, text)

    @staticmethod
    def _draw_not_tracked(painter, width, height, alpha):
        painter.setPen(QPen(QColor(243, 139, 168, alpha), 2))
        font = QFont("Segoe UI", 12)
        painter.setFont(font)
        painter.drawText(
            QRectF(0, 0, width, height),
            Qt.AlignmentFlag.AlignCenter,
            "Controller\nNot Tracked"
        )


# ============================================================
# Shared canvas widget
# ============================================================

class _VizCanvas(QWidget):
    """
    Widget that paints the 3D controller.
    Subscribes to _SHARED_STATE for calibration offsets.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pitch = self._yaw = self._roll = 0.0
        self._px = self._py = self._pz = 0.0
        self._is_tracked = False
        self._alpha = 255
        
        # Connect to shared global calibration
        _SHARED_STATE.state_changed.connect(self.update)

    def set_state(self, pitch, yaw, roll, x, y, z, is_tracked, alpha=255):
        self._pitch = pitch
        self._yaw = yaw
        self._roll = roll
        self._px = x
        self._py = y
        self._pz = z
        self._is_tracked = is_tracked
        self._alpha = alpha
        self.update()

    def calibrate(self):
        """Update global state with current rotation as new zero."""
        _SHARED_STATE.set_offsets(self._pitch, self._yaw, self._roll)
        # No need to call update(), signal will trigger it.

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        bg_alpha = min(self._alpha, 40)
        p.fillRect(self.rect(), QColor(24, 24, 37, bg_alpha))

        # Retrieve shared offsets
        off_p, off_y, off_r = _SHARED_STATE.get_offsets()

        # Calculate visual rotation
        vis_p = self._pitch - off_p
        vis_y = self._yaw - off_y
        vis_r = self._roll - off_r

        _ControllerRenderer.render(
            p, self.width(), self.height(),
            vis_p, vis_y, vis_r,  # Visual
            self._px, self._py, self._pz,
            self._is_tracked, self._alpha,
            raw_pitch=self._pitch, # Raw for text
            raw_yaw=self._yaw,
            raw_roll=self._roll
        )
        p.end()


# ============================================================
# Floating overlay widget
# ============================================================

class ControllerVizOverlay(QWidget):
    
    closed = pyqtSignal()

    TITLE_HEIGHT = 28
    MIN_SIZE = QSize(160, 160)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(self.MIN_SIZE)
        self.resize(260, 280)

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

        self._title_bar = QWidget()
        self._title_bar.setFixedHeight(self.TITLE_HEIGHT)
        title_layout = QHBoxLayout(self._title_bar)
        title_layout.setContentsMargins(8, 2, 4, 2)
        title_layout.setSpacing(4)

        title = QLabel("3D Controller")
        title.setFont(QFont("Segoe UI", 9))
        title.setStyleSheet("color: #cdd6f4;")
        title_layout.addWidget(title)
        
        cal_btn = QPushButton("Cal")
        cal_btn.setFixedSize(20, 20)
        cal_btn.setToolTip("Calibrate (Ctrl+R)")
        cal_btn.setStyleSheet("QPushButton { background: transparent; color: #fab387; border: none; font-weight: bold; } QPushButton:hover { color: #f9e2af; }")
        cal_btn.clicked.connect(self._calibrate)
        title_layout.addWidget(cal_btn)
        
        title_layout.addStretch()

        opacity_label = QLabel("Op:")
        opacity_label.setStyleSheet("color: #a6adc8; font-size: 11px;")
        title_layout.addWidget(opacity_label)

        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(30, 255)
        self._opacity_slider.setValue(self._opacity)
        self._opacity_slider.setFixedWidth(60)
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

        self._canvas = _VizCanvas()
        layout.addWidget(self._canvas, 1)

        self._grip = QSizeGrip(self)
        self._grip.setFixedSize(14, 14)
        self._grip.setStyleSheet("background: transparent;")

    def set_controller_state(self, pitch, yaw, roll, x, y, z, is_tracked):
        self._canvas.set_state(pitch, yaw, roll, x, y, z, is_tracked,
                               alpha=self._opacity)

    def _calibrate(self):
        self._canvas.calibrate()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_R and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self._calibrate()
        else:
            super().keyPressEvent(event)

    def _on_opacity_changed(self, val):
        self._opacity = val
        self.update()

    def _close(self):
        self.hide()
        self.closed.emit()

    # ---- Mouse Events ----

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


# ============================================================
# Docked panel widget
# ============================================================

class ControllerVizPanel(QWidget):
    
    overlay_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        header = QHBoxLayout()
        title = QLabel("3D Controller")
        title.setFont(QFont("Segoe UI", 10))
        title.setStyleSheet("color: #a6adc8;")
        header.addWidget(title)
        header.addStretch()

        self.cal_btn = QPushButton("Cal")
        self.cal_btn.setToolTip("Calibrate Center (Ctrl+R)")
        self.cal_btn.setFixedSize(24, 24)
        self.cal_btn.setStyleSheet("""
            QPushButton { background: #313244; color: #fab387; border: none;
                border-radius: 4px; font-size: 16px; }
            QPushButton:hover { background: #45475a; color: #f9e2af; }
        """)
        self.cal_btn.clicked.connect(self.calibrate_now)
        header.addWidget(self.cal_btn)

        overlay_btn = QPushButton("[^]")
        overlay_btn.setToolTip("Pop out as floating overlay (Ctrl+4)")
        overlay_btn.setFixedSize(24, 24)
        overlay_btn.setStyleSheet("""
            QPushButton { background: #313244; color: #cdd6f4; border: none; border-radius: 4px; font-size: 14px; }
            QPushButton:hover { background: #45475a; }
        """)
        overlay_btn.clicked.connect(self.overlay_requested.emit)
        header.addWidget(overlay_btn)
        layout.addLayout(header)

        self._canvas = _VizCanvas()
        layout.addWidget(self._canvas, 1)

    def set_controller_state(self, pitch, yaw, roll, x, y, z, is_tracked):
        self._canvas.set_state(pitch, yaw, roll, x, y, z, is_tracked)

    def calibrate_now(self):
        self._canvas.calibrate()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_R and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self.calibrate_now()
        else:
            super().keyPressEvent(event)