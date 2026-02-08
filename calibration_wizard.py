"""
Calibration Wizard
===================
Step-by-step guided calibration overlay.  The user moves their controller
to specific positions and presses the A button (or clicks the on-screen
button) to capture each endpoint.  Each axis can also be skipped.

Emits calibration_finished(dict) with {axis_name: {"min": float, "max": float}}
for every axis the user confirmed.
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QFrame
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QRectF
from PyQt6.QtGui import QPainter, QPen, QColor, QBrush, QFont


# Each calibration step: axis, label, instruction, icon, endpoint (min/max), color
CALIBRATION_STEPS = [
    # Stroke (y)
    {"axis": "y",     "label": "Stroke - Top",      "instruction": "Move the controller to the\nTOP of your stroke range",         "hint": "↑", "endpoint": "max", "color": QColor(137, 180, 250)},
    {"axis": "y",     "label": "Stroke - Bottom",   "instruction": "Move the controller to the\nBOTTOM of your stroke range",      "hint": "↓", "endpoint": "min", "color": QColor(137, 180, 250)},
    # Surge (z)
    {"axis": "z",     "label": "Surge - Forward",   "instruction": "Push the controller\nFORWARD as far as you'll go",          "hint": "↑", "endpoint": "max", "color": QColor(166, 227, 161)},
    {"axis": "z",     "label": "Surge - Back",      "instruction": "Pull the controller\nBACK to your rear limit",              "hint": "↓", "endpoint": "min", "color": QColor(166, 227, 161)},
    # Sway (x)
    {"axis": "x",     "label": "Sway - Left",       "instruction": "Move the controller\nto your LEFT limit",                   "hint": "←", "endpoint": "min", "color": QColor(250, 179, 135)},
    {"axis": "x",     "label": "Sway - Right",      "instruction": "Move the controller\nto your RIGHT limit",                  "hint": "→", "endpoint": "max", "color": QColor(250, 179, 135)},
    # Twist (yaw)
    {"axis": "yaw",   "label": "Twist - Left",      "instruction": "Twist the controller\nfully to the LEFT",                   "hint": "←", "endpoint": "min", "color": QColor(203, 166, 247)},
    {"axis": "yaw",   "label": "Twist - Right",     "instruction": "Twist the controller\nfully to the RIGHT",                  "hint": "→", "endpoint": "max", "color": QColor(203, 166, 247)},
    # Roll
    {"axis": "roll",  "label": "Roll - Left",       "instruction": "Roll/tilt the controller\nfully to the LEFT",               "hint": "←", "endpoint": "min", "color": QColor(245, 194, 231)},
    {"axis": "roll",  "label": "Roll - Right",      "instruction": "Roll/tilt the controller\nfully to the RIGHT",              "hint": "→", "endpoint": "max", "color": QColor(245, 194, 231)},
    # Pitch
    {"axis": "pitch", "label": "Pitch - Up",        "instruction": "Tilt the controller\nUP (point it at the ceiling)",         "hint": "↑", "endpoint": "max", "color": QColor(148, 226, 213)},
    {"axis": "pitch", "label": "Pitch - Down",      "instruction": "Tilt the controller\nDOWN (point it at the floor)",         "hint": "↓", "endpoint": "min", "color": QColor(148, 226, 213)},
]

TOTAL_STEPS = len(CALIBRATION_STEPS)


class CalibrationWizard(QWidget):
    """
    Full-screen overlay that guides the user through controller calibration
    one axis endpoint at a time.
    """

    calibration_finished = pyqtSignal(dict)   # {axis: {"min": float, "max": float}}
    calibration_cancelled = pyqtSignal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._step_index = 0
        self._results: dict = {}
        self._skipped_axes: set = set()
        self._raw_value: float = 0.0

        # Make this a frameless tool window so it floats above all child widgets
        # (including native video surfaces like mpv)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setAutoFillBackground(True)

        # Live value polling
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(33)
        self._poll_timer.timeout.connect(self._poll_value)

        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(12)

        # Title bar
        title_row = QHBoxLayout()
        self._title = QLabel("Controller Calibration")
        self._title.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
        self._title.setStyleSheet("color: #cdd6f4;")
        title_row.addWidget(self._title)
        title_row.addStretch()
        self._step_counter = QLabel("Step 1 / 12")
        self._step_counter.setFont(QFont("Segoe UI", 14))
        self._step_counter.setStyleSheet("color: #a6adc8;")
        title_row.addWidget(self._step_counter)
        layout.addLayout(title_row)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setRange(0, TOTAL_STEPS)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(6)
        self._progress.setStyleSheet("""
            QProgressBar { background: #313244; border: none; border-radius: 3px; }
            QProgressBar::chunk { background: #89b4fa; border-radius: 3px; }
        """)
        layout.addWidget(self._progress)

        layout.addSpacing(10)

        # Central area
        center = QHBoxLayout()
        center.setSpacing(30)

        # Left: big hint icon
        self._hint_label = QLabel("↑")
        self._hint_label.setFont(QFont("Segoe UI", 72))
        self._hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint_label.setFixedWidth(140)
        self._hint_label.setStyleSheet("color: #89b4fa;")
        center.addWidget(self._hint_label)

        # Middle: instruction text
        mid = QVBoxLayout()
        mid.setSpacing(8)

        self._axis_label = QLabel("Stroke - Top")
        self._axis_label.setFont(QFont("Segoe UI", 18, QFont.Weight.DemiBold))
        self._axis_label.setStyleSheet("color: #89b4fa;")
        mid.addWidget(self._axis_label)

        self._instruction = QLabel("Move the controller to the\nTOP of your stroke range")
        self._instruction.setFont(QFont("Segoe UI", 14))
        self._instruction.setStyleSheet("color: #cdd6f4;")
        self._instruction.setWordWrap(True)
        mid.addWidget(self._instruction)

        mid.addSpacing(6)

        self._value_label = QLabel("Current value: 0.0000")
        self._value_label.setFont(QFont("Consolas", 16))
        self._value_label.setStyleSheet("color: #f9e2af;")
        mid.addWidget(self._value_label)

        # Captured values summary
        self._captured_label = QLabel("")
        self._captured_label.setFont(QFont("Consolas", 11))
        self._captured_label.setStyleSheet("color: #a6adc8;")
        mid.addWidget(self._captured_label)

        mid.addStretch()
        center.addLayout(mid, 1)

        # Right: live gauge
        self._gauge = _CalibrationGauge()
        self._gauge.setFixedSize(120, 220)
        center.addWidget(self._gauge, 0, Qt.AlignmentFlag.AlignVCenter)

        layout.addLayout(center, 1)

        # Bottom buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)

        btn_style_secondary = """
            QPushButton { background: #45475a; color: #cdd6f4; border: none;
                padding: 10px 20px; border-radius: 6px; font-size: 14px; }
            QPushButton:hover { background: #585b70; }
        """
        btn_style_primary = """
            QPushButton { background: #89b4fa; color: #1e1e2e; border: none;
                padding: 10px 24px; border-radius: 6px; font-size: 14px; font-weight: bold; }
            QPushButton:hover { background: #b4d0fb; }
        """

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setFixedWidth(100)
        self._cancel_btn.clicked.connect(self._cancel)
        self._cancel_btn.setStyleSheet(btn_style_secondary)
        btn_row.addWidget(self._cancel_btn)

        self._back_btn = QPushButton("<  Back")
        self._back_btn.setFixedWidth(100)
        self._back_btn.clicked.connect(self._go_back_step)
        self._back_btn.setStyleSheet(btn_style_secondary)
        self._back_btn.setEnabled(False)  # Disabled on first step
        btn_row.addWidget(self._back_btn)

        btn_row.addStretch()

        self._skip_btn = QPushButton("Skip Axis")
        self._skip_btn.setFixedWidth(180)
        self._skip_btn.clicked.connect(self._skip_axis)
        self._skip_btn.setStyleSheet(btn_style_secondary)
        btn_row.addWidget(self._skip_btn)

        self._confirm_btn = QPushButton("Confirm  ✓")
        self._confirm_btn.setFixedWidth(200)
        self._confirm_btn.clicked.connect(self._confirm_step)
        self._confirm_btn.setStyleSheet(btn_style_primary)
        self._btn_style_primary = btn_style_primary
        btn_row.addWidget(self._confirm_btn)

        layout.addLayout(btn_row)

        tip = QLabel("A / X = confirm  |  B / Y = go back  |  Or click the buttons above.")
        tip.setFont(QFont("Segoe UI", 11))
        tip.setStyleSheet("color: #6c7086;")
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(tip)

    # ---- Public API ----

    def start(self):
        """Reset and start the calibration sequence."""
        self._step_index = 0
        self._results = {}
        self._skipped_axes = set()
        self._update_step_display()
        self._poll_timer.start()

        # Position over the parent window's central area
        if self.parent():
            parent = self.parent()
            # Map parent's top-left to global coords
            global_pos = parent.mapToGlobal(parent.rect().topLeft())
            self.setGeometry(
                global_pos.x(), global_pos.y(),
                parent.width(), parent.height()
            )
        self.show()
        self.raise_()
        self.activateWindow()

    def confirm_via_controller(self):
        """Called when the A button is pressed on the controller."""
        self._confirm_step()

    def go_back_via_controller(self):
        """Called when the B button is pressed on the controller."""
        self._go_back_step()

    # ---- Internals ----

    def _get_raw_value_for_step(self) -> float:
        state = self._controller.get_current_state()
        step = CALIBRATION_STEPS[self._step_index]
        axis = step["axis"]
        raw_map = {
            "x": state.x, "y": state.y, "z": state.z,
            "pitch": state.pitch, "yaw": state.yaw, "roll": state.roll,
        }
        return raw_map.get(axis, 0.0)

    def _poll_value(self):
        if not self.isVisible() or self._step_index >= TOTAL_STEPS:
            return
        val = self._get_raw_value_for_step()
        self._raw_value = val
        self._value_label.setText(f"Current value: {val:.4f}")

        step = CALIBRATION_STEPS[self._step_index]
        axis = step["axis"]
        if axis in ("pitch", "yaw", "roll"):
            norm = max(0.0, min(1.0, (val + 180) / 360))
        else:
            norm = max(0.0, min(1.0, (val + 1.5) / 3.0))
        self._gauge.set_value(norm, step["color"])

        # Update captured summary
        self._update_captured_summary()

    def _update_captured_summary(self):
        step = CALIBRATION_STEPS[self._step_index]
        axis = step["axis"]
        if axis in self._results:
            vals = self._results[axis]
            parts = []
            if "min" in vals:
                parts.append(f"min: {vals['min']:.4f}")
            if "max" in vals:
                parts.append(f"max: {vals['max']:.4f}")
            self._captured_label.setText(f"Captured: {', '.join(parts)}")
        else:
            self._captured_label.setText("")

    def _update_step_display(self):
        if self._step_index >= TOTAL_STEPS:
            self._finish()
            return

        step = CALIBRATION_STEPS[self._step_index]
        axis = step["axis"]

        # Auto-skip if this axis was skipped
        if axis in self._skipped_axes:
            self._step_index += 1
            self._update_step_display()
            return

        color_hex = step["color"].name()
        self._step_counter.setText(f"Step {self._step_index + 1} / {TOTAL_STEPS}")
        self._progress.setValue(self._step_index)
        self._axis_label.setText(step["label"])
        self._axis_label.setStyleSheet(f"color: {color_hex};")
        self._instruction.setText(step["instruction"])
        self._hint_label.setText(step["hint"])
        self._hint_label.setStyleSheet(f"color: {color_hex};")
        self._captured_label.setText("")
        self._back_btn.setEnabled(self._step_index > 0)

    def _confirm_step(self):
        if self._step_index >= TOTAL_STEPS:
            return

        step = CALIBRATION_STEPS[self._step_index]
        axis = step["axis"]
        endpoint = step["endpoint"]
        val = self._get_raw_value_for_step()

        if axis not in self._results:
            self._results[axis] = {}
        self._results[axis][endpoint] = val

        # Flash
        self._confirm_btn.setText("✓  Captured!")
        self._confirm_btn.setStyleSheet("""
            QPushButton { background: #a6e3a1; color: #1e1e2e; border: none;
                padding: 10px 24px; border-radius: 6px; font-size: 14px; font-weight: bold; }
        """)
        QTimer.singleShot(350, self._advance_step)

    def _advance_step(self):
        self._step_index += 1
        self._confirm_btn.setText("Confirm  ✓")
        self._confirm_btn.setStyleSheet(self._btn_style_primary)
        self._update_step_display()

    def _go_back_step(self):
        """Go back to the previous step, clearing any captured data for it."""
        if self._step_index <= 0:
            return

        self._step_index -= 1

        # If the previous step's axis was skipped, un-skip it
        step = CALIBRATION_STEPS[self._step_index]
        axis = step["axis"]
        self._skipped_axes.discard(axis)

        # Clear the captured endpoint so the user can redo it
        endpoint = step["endpoint"]
        if axis in self._results and endpoint in self._results[axis]:
            del self._results[axis][endpoint]
            # If both endpoints are gone, remove the axis entry entirely
            if not self._results[axis]:
                del self._results[axis]

        self._confirm_btn.setText("Confirm  ✓")
        self._confirm_btn.setStyleSheet(self._btn_style_primary)
        self._update_step_display()

    def _skip_axis(self):
        if self._step_index >= TOTAL_STEPS:
            return
        step = CALIBRATION_STEPS[self._step_index]
        axis = step["axis"]
        self._skipped_axes.add(axis)
        if axis in self._results:
            del self._results[axis]
        # Advance past both steps for this axis
        while (self._step_index < TOTAL_STEPS and
               CALIBRATION_STEPS[self._step_index]["axis"] == axis):
            self._step_index += 1
        self._confirm_btn.setText("Confirm  ✓")
        self._confirm_btn.setStyleSheet(self._btn_style_primary)
        self._update_step_display()

    def _finish(self):
        self._poll_timer.stop()
        self._progress.setValue(TOTAL_STEPS)
        self.hide()
        valid = {}
        for axis, vals in self._results.items():
            if "min" in vals and "max" in vals and vals["min"] != vals["max"]:
                valid[axis] = vals
        self.calibration_finished.emit(valid)

    def _cancel(self):
        self._poll_timer.stop()
        self.hide()
        self.calibration_cancelled.emit()

    # ---- Painting ----

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(24, 24, 37, 240))
        p.setPen(QPen(QColor(69, 71, 90), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 12, 12)
        p.end()


class _CalibrationGauge(QWidget):
    """Vertical bar gauge showing the current axis value."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0.5
        self._color = QColor(137, 180, 250)

    def set_value(self, normalized: float, color: QColor):
        self._value = max(0.0, min(1.0, normalized))
        self._color = color
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        margin = 10
        bar_w = 40
        bar_h = h - margin * 2
        bar_x = (w - bar_w) // 2
        bar_y = margin

        # Track
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(49, 50, 68))
        p.drawRoundedRect(bar_x, bar_y, bar_w, bar_h, 6, 6)

        # Grid
        p.setPen(QPen(QColor(69, 71, 90), 1, Qt.PenStyle.DashLine))
        for frac in [0.0, 0.25, 0.5, 0.75, 1.0]:
            y = bar_y + bar_h - frac * bar_h
            p.drawLine(bar_x + 2, int(y), bar_x + bar_w - 2, int(y))

        # Fill
        val_y = bar_y + bar_h - self._value * bar_h
        fill_color = QColor(self._color)
        fill_color.setAlpha(60)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill_color)
        fill_h = bar_y + bar_h - val_y
        p.drawRoundedRect(bar_x, int(val_y), bar_w, int(fill_h), 4, 4)

        # Indicator line
        p.setPen(QPen(self._color, 3))
        p.drawLine(bar_x - 4, int(val_y), bar_x + bar_w + 4, int(val_y))

        # Dot
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(self._color))
        p.drawEllipse(int(w / 2 - 8), int(val_y - 8), 16, 16)
        p.setBrush(QColor(255, 255, 255, 200))
        p.drawEllipse(int(w / 2 - 4), int(val_y - 4), 8, 8)

        p.end()
