"""
Main Window
============
Central application window integrating video player, controller input,
recording engine, timeline, and position display.

Phase 1-7 enhancements:
  - Stabilization presets (Off/Light/Medium/Heavy)
  - Per-axis lock buttons and all-6-axis record toggles
  - Multi-lane timeline overlays with visibility controls
  - Heatmap toggle
  - Beat detection + beat markers + snap tools
  - Bundle export
"""

import os
import sys
import json
import time
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QSlider, QComboBox, QFileDialog, QMessageBox,
    QGroupBox, QSpinBox, QDoubleSpinBox, QCheckBox, QStatusBar,
    QMenuBar, QMenu, QSplitter, QFrame, QToolTip, QApplication,
    QScrollArea
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QSize, QUrl
from PyQt6.QtGui import QAction, QKeySequence, QFont, QIcon, QMouseEvent, QDesktopServices

from video_player import VideoPlayerWidget
from vr_controller import VRControllerInput, MouseFallbackInput, HAS_OPENVR
from recorder import Recorder
from timeline_widget import TimelineWidget
from position_display import PositionDisplay, PositionDisplayOverlay
from funscript_io import FunscriptProject, AXIS_DEFINITIONS, FunscriptAction
from calibration_wizard import CalibrationWizard
from controller_viz import ControllerVizPanel, ControllerVizOverlay

# Optional beat detection
try:
    from beat_detection import detect_beats, BeatData
    HAS_BEAT_DETECTION = True
except ImportError:
    HAS_BEAT_DETECTION = False

# Axis overlay colors for multi-lane timeline
AXIS_OVERLAY_COLORS = {
    "stroke": "#89b4fa",
    "surge":  "#a6e3a1",
    "sway":   "#fab387",
    "twist":  "#cba6f7",
    "roll":   "#f5c2e7",
    "pitch":  "#94e2d5",
}


class VideoAreaWidget(QWidget):
    """Wrapper that captures mouse events over the video for fallback mode."""
    mouse_moved = pyqtSignal(float, float)  # normalized x, y

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._tracking_enabled = False

    def set_tracking_enabled(self, enabled: bool):
        self._tracking_enabled = enabled

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._tracking_enabled and self.width() > 0 and self.height() > 0:
            nx = event.position().x() / self.width()
            ny = event.position().y() / self.height()
            self.mouse_moved.emit(nx, ny)
        super().mouseMoveEvent(event)


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("vFAPS")
        self.setMinimumSize(1000, 700)
        self.resize(1280, 800)

        # Core state
        self.project = FunscriptProject()
        self.recorder = Recorder(self.project)
        self.controller = None
        self._use_mouse_fallback = False
        self._active_axis = "stroke"
        self._recording_active = False
        self._autosave_path = None
        self._calibrating = False       # True while calibration wizard is open
        self._viz_overlay = None         # Floating overlay widget (created lazily)
        self._pos_overlay = None         # Floating position display overlay

        # Phase 5: beat detection state
        self._beat_data = None           # BeatData from beat_detection module

        # Phase 3: overlay visibility state
        self._overlay_visible: dict[str, bool] = {}
        for ax_name in AXIS_DEFINITIONS:
            self._overlay_visible[ax_name] = False

        # Initialize controller
        self._init_controller()

        # Build UI
        self._build_menu_bar()
        self._build_central_widget()
        self._build_status_bar()
        self._setup_shortcuts()
        self._setup_timers()

        # Connect signals
        self._connect_signals()

        self._update_status("Ready. Open a video file to begin (Ctrl+O).")

    def _init_controller(self):
        """Initialize VR controller or fall back to mouse."""
        if HAS_OPENVR:
            self.controller = VRControllerInput()
            success, msg = self.controller.initialize()
            if success:
                self.controller.start_polling()
                self._use_mouse_fallback = False
                self._wire_controller_buttons()
                return
            else:
                print(f"VR init: {msg}")

        # Fall back to mouse
        self.controller = MouseFallbackInput()
        self.controller.initialize()
        self.controller.start_polling()
        self._use_mouse_fallback = True

    def _wire_controller_buttons(self):
        """
        Map physical controller buttons to app actions.
        Button callbacks fire from the polling thread, so we use
        thread-safe flags + QTimer to dispatch into the Qt main thread.
        """
        self._btn_trigger_fired = False
        self._btn_a_fired = False
        self._btn_b_fired = False

        def _on_trigger():
            self._btn_trigger_fired = True

        def _on_a_button():
            self._btn_a_fired = True

        def _on_b_button():
            self._btn_b_fired = True

        self.controller.on_button_press("trigger", _on_trigger)
        self.controller.on_button_press("a_button", _on_a_button)
        self.controller.on_button_press("b_button", _on_b_button)

        self._btn_timer = QTimer(self)
        self._btn_timer.setInterval(33)
        self._btn_timer.timeout.connect(self._check_controller_buttons)
        self._btn_timer.start()

    def _check_controller_buttons(self):
        """Called on the Qt thread to process controller button presses and joystick."""
        if self._btn_trigger_fired:
            self._btn_trigger_fired = False
            if self._calibrating:
                # Trigger also confirms during calibration (more reliable than A)
                self._cal_wizard.confirm_via_controller()
            else:
                self._toggle_recording()

        if self._btn_a_fired:
            self._btn_a_fired = False
            if self._calibrating:
                self._cal_wizard.confirm_via_controller()
            else:
                self._auto_calibrate()

        if self._btn_b_fired:
            self._btn_b_fired = False
            if self._calibrating:
                self._cal_wizard.go_back_via_controller()
            else:
                self._recenter_controller()

        # Thumbstick scrubbing
        state = self.controller.get_current_state()
        stick_x = state.thumbstick_x
        deadzone = 0.25
        if abs(stick_x) < deadzone:
            return
        sign = 1 if stick_x > 0 else -1
        magnitude = (abs(stick_x) - deadzone) / (1.0 - deadzone)
        speed = magnitude * magnitude
        seek_delta_ms = int(sign * (30 + speed * 300))
        self.video_player.seek_relative(seek_delta_ms)

    # ================================================================
    #  MENU BAR
    # ================================================================

    def _build_menu_bar(self):
        """Build application menu bar."""
        menubar = self.menuBar()

        # ---- File menu ----
        file_menu = menubar.addMenu("&File")

        open_video = QAction("&Open Video...", self)
        open_video.setShortcut(QKeySequence("Ctrl+O"))
        open_video.triggered.connect(self._open_video)
        file_menu.addAction(open_video)

        file_menu.addSeparator()

        open_project = QAction("Open &Project...", self)
        open_project.setShortcut(QKeySequence("Ctrl+Shift+O"))
        open_project.triggered.connect(self._open_project)
        file_menu.addAction(open_project)

        save_project = QAction("&Save Project", self)
        save_project.setShortcut(QKeySequence("Ctrl+S"))
        save_project.triggered.connect(self._save_project)
        file_menu.addAction(save_project)

        save_project_as = QAction("Save Project &As...", self)
        save_project_as.setShortcut(QKeySequence("Ctrl+Shift+S"))
        save_project_as.triggered.connect(self._save_project_as)
        file_menu.addAction(save_project_as)

        file_menu.addSeparator()

        import_fs = QAction("&Import Funscript...", self)
        import_fs.triggered.connect(self._import_funscript)
        file_menu.addAction(import_fs)

        export_fs = QAction("&Export Funscript...", self)
        export_fs.setShortcut(QKeySequence("Ctrl+E"))
        export_fs.triggered.connect(self._export_funscript)
        file_menu.addAction(export_fs)

        # Phase 7: Bundle export
        export_bundle = QAction("Export All Axes as &ZIP...", self)
        export_bundle.triggered.connect(self._export_bundle)
        file_menu.addAction(export_bundle)

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # ---- Edit menu ----
        edit_menu = menubar.addMenu("&Edit")

        undo = QAction("&Undo", self)
        undo.setShortcut(QKeySequence("Ctrl+Z"))
        undo.triggered.connect(self._undo)
        edit_menu.addAction(undo)

        redo = QAction("&Redo", self)
        redo.setShortcut(QKeySequence("Ctrl+Y"))
        redo.triggered.connect(self._redo)
        edit_menu.addAction(redo)

        edit_menu.addSeparator()

        smooth = QAction("&Smooth Selection...", self)
        smooth.triggered.connect(self._smooth_selection)
        edit_menu.addAction(smooth)

        clear_sel = QAction("&Clear Selection Data", self)
        clear_sel.setShortcut(QKeySequence("Delete"))
        clear_sel.triggered.connect(self._clear_selection)
        edit_menu.addAction(clear_sel)

        # ---- Recording menu ----
        rec_menu = menubar.addMenu("&Recording")

        toggle_rec = QAction("&Toggle Recording", self)
        toggle_rec.setShortcut(QKeySequence("R"))
        toggle_rec.triggered.connect(self._toggle_recording)
        rec_menu.addAction(toggle_rec)

        cancel_rec = QAction("&Cancel Recording", self)
        cancel_rec.setShortcut(QKeySequence("Escape"))
        cancel_rec.triggered.connect(self._cancel_recording)
        rec_menu.addAction(cancel_rec)

        rec_menu.addSeparator()

        cal_action = QAction("Auto-&Calibrate Controller...", self)
        cal_action.triggered.connect(self._auto_calibrate)
        rec_menu.addAction(cal_action)

        recenter_action = QAction("&Re-center Controller", self)
        recenter_action.setShortcut(QKeySequence("C"))
        recenter_action.triggered.connect(self._recenter_controller)
        rec_menu.addAction(recenter_action)

        # ---- View menu ----
        view_menu = menubar.addMenu("&View")

        zoom_fit = QAction("Zoom to &Fit", self)
        zoom_fit.setShortcut(QKeySequence("Ctrl+0"))
        zoom_fit.triggered.connect(lambda: self.timeline.zoom_to_fit())
        view_menu.addAction(zoom_fit)

        view_menu.addSeparator()

        self._viz_overlay_action = QAction("Show 3D Controller &Overlay", self)
        self._viz_overlay_action.setShortcut(QKeySequence("Ctrl+3"))
        self._viz_overlay_action.setCheckable(True)
        self._viz_overlay_action.toggled.connect(self._toggle_viz_overlay)
        view_menu.addAction(self._viz_overlay_action)

        self._pos_overlay_action = QAction("Show &Position Bar Overlay", self)
        self._pos_overlay_action.setShortcut(QKeySequence("Ctrl+4"))
        self._pos_overlay_action.setCheckable(True)
        self._pos_overlay_action.toggled.connect(self._toggle_pos_overlay)
        view_menu.addAction(self._pos_overlay_action)

        view_menu.addSeparator()

        # Phase 4: Heatmap toggle
        self._heatmap_action = QAction("Show &Heatmap", self)
        self._heatmap_action.setShortcut(QKeySequence("Ctrl+H"))
        self._heatmap_action.setCheckable(True)
        self._heatmap_action.toggled.connect(self._toggle_heatmap)
        view_menu.addAction(self._heatmap_action)

        view_menu.addSeparator()

        # Phase 3: Timeline overlay toggles
        overlay_menu = view_menu.addMenu("Timeline &Overlays")

        show_all_overlays = QAction("Show &All", self)
        show_all_overlays.triggered.connect(self._show_all_overlays)
        overlay_menu.addAction(show_all_overlays)

        hide_all_overlays = QAction("Hide All", self)
        hide_all_overlays.triggered.connect(self._hide_all_overlays)
        overlay_menu.addAction(hide_all_overlays)

        overlay_menu.addSeparator()

        self._overlay_actions: dict[str, QAction] = {}
        for ax_name, ax_info in AXIS_DEFINITIONS.items():
            action = QAction(ax_info["label"], self)
            action.setCheckable(True)
            action.setChecked(False)
            action.toggled.connect(lambda checked, name=ax_name: self._on_overlay_toggled(name, checked))
            overlay_menu.addAction(action)
            self._overlay_actions[ax_name] = action

        # ---- Tools menu (Phase 5: beat detection) ----
        tools_menu = menubar.addMenu("&Tools")

        if HAS_BEAT_DETECTION:
            detect_beats_action = QAction("Detect &Beats from Audio...", self)
            detect_beats_action.triggered.connect(self._detect_beats)
            tools_menu.addAction(detect_beats_action)

            self._beats_visible_action = QAction("Show Beat &Markers", self)
            self._beats_visible_action.setShortcut(QKeySequence("Ctrl+B"))
            self._beats_visible_action.setCheckable(True)
            self._beats_visible_action.setEnabled(False)  # Enabled after detection
            self._beats_visible_action.toggled.connect(self._toggle_beats_visible)
            tools_menu.addAction(self._beats_visible_action)

            tools_menu.addSeparator()

            snap_beats_action = QAction("&Snap to Beats (use slider strength)", self)
            snap_beats_action.triggered.connect(self._snap_to_beats)
            tools_menu.addAction(snap_beats_action)
        else:
            no_beats = QAction("Beat Detection (requires numpy + ffmpeg)", self)
            no_beats.setEnabled(False)
            tools_menu.addAction(no_beats)

        # ---- Help menu ----
        help_menu = menubar.addMenu("&Help")

        shortcuts = QAction("&Keyboard Shortcuts", self)
        shortcuts.setShortcut(QKeySequence("F1"))
        shortcuts.triggered.connect(self._show_shortcuts)
        help_menu.addAction(shortcuts)

        about = QAction("&About", self)
        about.triggered.connect(self._show_about)
        help_menu.addAction(about)

        help_menu.addSeparator()

        coffee = QAction("Buy Me a Coffee...", self)
        coffee.triggered.connect(self._open_coffee_link)
        help_menu.addAction(coffee)

    # ================================================================
    #  CENTRAL WIDGET
    # ================================================================

    def _build_central_widget(self):
        """Build the main UI layout."""
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(4)
        main_layout.setContentsMargins(4, 4, 4, 4)

        # Top section: Video + Position Display + Controls
        top_splitter = QSplitter(Qt.Orientation.Horizontal)

        # Video area (with mouse tracking wrapper)
        self._video_area = VideoAreaWidget()
        video_layout = QVBoxLayout(self._video_area)
        video_layout.setContentsMargins(0, 0, 0, 0)

        self.video_player = VideoPlayerWidget()
        video_layout.addWidget(self.video_player)

        if self._use_mouse_fallback:
            self._video_area.set_tracking_enabled(True)
            self._video_area.mouse_moved.connect(self._on_mouse_moved)

        top_splitter.addWidget(self._video_area)

        # Right panel: Position display + axis controls + settings
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 0, 4, 0)
        right_layout.setSpacing(4)

        # Position display
        self.position_display = PositionDisplay()
        self.position_display.overlay_requested.connect(self._show_pos_overlay)
        right_layout.addWidget(self.position_display, 1)

        # 3D Controller visualization panel
        self.viz_panel = ControllerVizPanel()
        self.viz_panel.overlay_requested.connect(self._show_viz_overlay)
        right_layout.addWidget(self.viz_panel, 1)

        # --- Phase 2: Axis Selector with Lock buttons ---
        axis_group = QGroupBox("Axes")
        axis_layout = QVBoxLayout(axis_group)
        axis_layout.setSpacing(3)

        # Primary axis combo (what you're editing in timeline)
        combo_row = QHBoxLayout()
        combo_row.addWidget(QLabel("Edit:"))
        self.axis_combo = QComboBox()
        for name, info in AXIS_DEFINITIONS.items():
            self.axis_combo.addItem(info["label"], name)
        self.axis_combo.currentIndexChanged.connect(self._on_axis_changed)
        combo_row.addWidget(self.axis_combo, 1)
        axis_layout.addLayout(combo_row)

        # Per-axis: record checkbox + lock button
        self._axis_checks: dict[str, QCheckBox] = {}
        self._axis_lock_btns: dict[str, QPushButton] = {}

        rec_header = QHBoxLayout()
        axis_label = QLabel("Record / Lock:")
        axis_label.setStyleSheet("color: #a6adc8; font-size: 11px;")
        rec_header.addWidget(axis_label)
        rec_header.addStretch()

        self._select_all_axes_btn = QPushButton("All")
        self._select_all_axes_btn.setFixedSize(36, 20)
        self._select_all_axes_btn.setToolTip("Select all / none for recording")
        self._select_all_axes_btn.setStyleSheet("""
            QPushButton { background: #313244; color: #a6adc8; border: none;
                border-radius: 3px; font-size: 10px; }
            QPushButton:hover { background: #45475a; color: #cdd6f4; }
        """)
        self._select_all_axes_btn.clicked.connect(self._toggle_all_record_axes)
        rec_header.addWidget(self._select_all_axes_btn)

        axis_layout.addLayout(rec_header)

        lock_btn_style = """
            QPushButton { background: #313244; color: #a6adc8; border: none;
                border-radius: 3px; font-size: 12px; min-width: 22px; max-width: 22px;
                min-height: 22px; max-height: 22px; }
            QPushButton:hover { background: #45475a; }
            QPushButton:checked { background: #f38ba8; color: #1e1e2e; }
        """

        for name, info in AXIS_DEFINITIONS.items():
            row = QHBoxLayout()
            row.setSpacing(4)

            cb = QCheckBox(info["label"].split(" ")[0])
            cb.setChecked(name == "stroke")
            cb.setToolTip(f"Record {info['desc']} axis")
            cb.toggled.connect(self._on_multi_axis_changed)
            self._axis_checks[name] = cb
            row.addWidget(cb, 1)

            lock_btn = QPushButton("\U0001F513")  # unlocked icon
            lock_btn.setCheckable(True)
            lock_btn.setToolTip(f"Lock {info['label'].split(' ')[0]} axis")
            lock_btn.setStyleSheet(lock_btn_style)
            lock_btn.toggled.connect(lambda checked, ax=name: self._on_axis_lock_toggled(ax, checked))
            self._axis_lock_btns[name] = lock_btn
            row.addWidget(lock_btn)

            axis_layout.addLayout(row)

        right_layout.addWidget(axis_group)

        # --- Settings group ---
        settings_group = QGroupBox("Settings")
        settings_layout = QVBoxLayout(settings_group)
        settings_layout.setSpacing(4)

        # Phase 1: Stabilization preset
        stab_layout = QHBoxLayout()
        stab_layout.addWidget(QLabel("Stab:"))
        self.stab_combo = QComboBox()
        self.stab_combo.addItems(["Off", "Light", "Medium", "Heavy"])
        self.stab_combo.setCurrentIndex(2)  # Default: Medium
        self.stab_combo.setToolTip("Realtime stabilization preset")
        self.stab_combo.currentTextChanged.connect(self._on_stabilization_changed)
        stab_layout.addWidget(self.stab_combo, 1)
        settings_layout.addLayout(stab_layout)

        # Point reduction
        self.rdp_check = QCheckBox("Point Reduction")
        self.rdp_check.setChecked(True)
        self.rdp_check.setToolTip("Reduce recorded points while preserving movement shape")
        self.rdp_check.toggled.connect(
            lambda v: setattr(self.recorder, 'point_reduction_enabled', v))
        settings_layout.addWidget(self.rdp_check)

        # Smoothing strength
        smooth_layout = QHBoxLayout()
        smooth_layout.addWidget(QLabel("Smooth:"))
        self.smooth_spin = QSpinBox()
        self.smooth_spin.setRange(3, 15)
        self.smooth_spin.setValue(5)
        self.smooth_spin.setSingleStep(2)
        smooth_layout.addWidget(self.smooth_spin)
        settings_layout.addLayout(smooth_layout)

        # Playback speed
        speed_layout = QHBoxLayout()
        speed_layout.addWidget(QLabel("Speed:"))
        self.speed_combo = QComboBox()
        for spd in ["0.25x", "0.5x", "0.75x", "1.0x", "1.5x", "2.0x"]:
            self.speed_combo.addItem(spd)
        self.speed_combo.setCurrentIndex(3)  # 1.0x
        self.speed_combo.currentTextChanged.connect(self._on_speed_changed)
        speed_layout.addWidget(self.speed_combo)
        settings_layout.addLayout(speed_layout)

        # Sensitivity Slider
        sens_layout = QHBoxLayout()
        sens_layout.addWidget(QLabel("Sens:"))
        self.sens_slider = QSlider(Qt.Orientation.Horizontal)
        self.sens_slider.setRange(50, 300)
        self.sens_slider.setValue(100)
        self.sens_slider.setToolTip("Adjust controller sensitivity (0.5x to 3.0x)")
        self.sens_slider.valueChanged.connect(self._on_sensitivity_changed)
        sens_layout.addWidget(self.sens_slider)

        self.sens_label = QLabel("1.0x")
        self.sens_label.setFixedWidth(35)
        self.sens_label.setStyleSheet("color: #a6adc8; font-size: 11px;")
        sens_layout.addWidget(self.sens_label)
        settings_layout.addLayout(sens_layout)

        # Beat Snap strength
        beat_snap_layout = QHBoxLayout()
        beat_snap_layout.addWidget(QLabel("Beat:"))
        self.beat_snap_slider = QSlider(Qt.Orientation.Horizontal)
        self.beat_snap_slider.setRange(0, 100)
        self.beat_snap_slider.setValue(0)
        self.beat_snap_slider.setToolTip(
            "Beat snap strength (0% = freehand, 100% = fully quantized)\n"
            "Blends each point's timing between its original position\n"
            "and the nearest beat. Use Apply to apply to data.")
        self.beat_snap_slider.valueChanged.connect(self._on_beat_snap_changed)
        beat_snap_layout.addWidget(self.beat_snap_slider)

        self.beat_snap_label = QLabel("0%")
        self.beat_snap_label.setFixedWidth(35)
        self.beat_snap_label.setStyleSheet("color: #a6adc8; font-size: 11px;")
        beat_snap_layout.addWidget(self.beat_snap_label)
        settings_layout.addLayout(beat_snap_layout)

        # Apply button
        self.beat_snap_apply_btn = QPushButton("♩ Apply Snap")
        self.beat_snap_apply_btn.setToolTip(
            "Apply beat snap to selected range (or all points).\n"
            "Slider controls how tightly points follow the beat grid.")
        self.beat_snap_apply_btn.setStyleSheet("""
            QPushButton { background: #313244; color: #f9e2af; border: 1px solid #45475a;
                border-radius: 4px; padding: 3px 8px; font-size: 11px; }
            QPushButton:hover { background: #45475a; }
        """)
        self.beat_snap_apply_btn.clicked.connect(self._apply_beat_snap)
        settings_layout.addWidget(self.beat_snap_apply_btn)

        right_layout.addWidget(settings_group)

        # Input mode indicator
        if not self._use_mouse_fallback and hasattr(self.controller, 'get_controller_name'):
            ctrl_name = self.controller.get_controller_name()
            mode_label = QLabel(f"\U0001F3AE {ctrl_name}")
        elif not self._use_mouse_fallback:
            mode_label = QLabel("\U0001F3AE VR Controller")
        else:
            mode_label = QLabel("\U0001F5B1 Mouse Fallback")
        mode_label.setStyleSheet("color: #a6adc8; font-size: 11px; padding: 4px;")
        mode_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(mode_label)

        right_panel.setMaximumWidth(220)
        top_splitter.addWidget(right_panel)
        top_splitter.setStretchFactor(0, 4)
        top_splitter.setStretchFactor(1, 1)

        main_layout.addWidget(top_splitter, 3)

        # Transport controls
        transport = self._build_transport_controls()
        main_layout.addWidget(transport)

        # Timeline
        self.timeline = TimelineWidget()
        main_layout.addWidget(self.timeline, 1)

        # Calibration wizard overlay (initially hidden)
        self._cal_wizard = CalibrationWizard(self.controller, central)
        self._cal_wizard.hide()
        self._cal_wizard.calibration_finished.connect(self._apply_calibration)
        self._cal_wizard.calibration_cancelled.connect(self._on_cal_cancelled)

    # ================================================================
    #  TRANSPORT CONTROLS
    # ================================================================

    def _build_transport_controls(self) -> QWidget:
        """Build the playback and recording transport bar."""
        frame = QFrame()
        frame.setFrameStyle(QFrame.Shape.StyledPanel)
        frame.setStyleSheet("QFrame { background-color: #181825; border-radius: 6px; }")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 6, 10, 6)

        # Time display
        self.time_label = QLabel("00:00.0 / 00:00.0")
        self.time_label.setFont(QFont("Consolas", 11))
        self.time_label.setStyleSheet("color: #cdd6f4; min-width: 160px;")
        layout.addWidget(self.time_label)

        layout.addSpacing(10)

        btn_style = "QPushButton { min-width: 36px; }"

        skip_back_10 = QPushButton("\u23EA 10s")
        skip_back_10.setStyleSheet(btn_style)
        skip_back_10.clicked.connect(lambda: self.video_player.seek_relative(-10000))
        skip_back_10.setToolTip("Skip back 10 seconds (Ctrl+Left)")
        layout.addWidget(skip_back_10)

        skip_back = QPushButton("\u25C4 5s")
        skip_back.setStyleSheet(btn_style)
        skip_back.clicked.connect(lambda: self.video_player.seek_relative(-5000))
        skip_back.setToolTip("Skip back 5 seconds (Left)")
        layout.addWidget(skip_back)

        frame_back = QPushButton("\u25C0")
        frame_back.setStyleSheet(btn_style)
        frame_back.clicked.connect(lambda: self.video_player.seek_frames(-1))
        frame_back.setToolTip("Previous frame (,)")
        layout.addWidget(frame_back)

        self.play_btn = QPushButton("\u25B6 Play")
        self.play_btn.setCheckable(True)
        self.play_btn.setStyleSheet(btn_style + " QPushButton { min-width: 70px; }")
        self.play_btn.clicked.connect(self._on_play_clicked)
        self.play_btn.setToolTip("Play / Pause (Space)")
        layout.addWidget(self.play_btn)

        frame_fwd = QPushButton("\u25B7")
        frame_fwd.setStyleSheet(btn_style)
        frame_fwd.clicked.connect(lambda: self.video_player.seek_frames(1))
        frame_fwd.setToolTip("Next frame (.)")
        layout.addWidget(frame_fwd)

        skip_fwd = QPushButton("5s \u25BA")
        skip_fwd.setStyleSheet(btn_style)
        skip_fwd.clicked.connect(lambda: self.video_player.seek_relative(5000))
        skip_fwd.setToolTip("Skip forward 5 seconds (Right)")
        layout.addWidget(skip_fwd)

        skip_fwd_10 = QPushButton("10s \u23E9")
        skip_fwd_10.setStyleSheet(btn_style)
        skip_fwd_10.clicked.connect(lambda: self.video_player.seek_relative(10000))
        skip_fwd_10.setToolTip("Skip forward 10 seconds (Ctrl+Right)")
        layout.addWidget(skip_fwd_10)

        layout.addSpacing(20)

        # Seek slider
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.sliderPressed.connect(self._on_seek_start)
        self.seek_slider.sliderReleased.connect(self._on_seek_end)
        self.seek_slider.sliderMoved.connect(self._on_seek_moved)
        self._seeking = False
        layout.addWidget(self.seek_slider, 1)

        layout.addSpacing(20)

        # Record button
        self.record_btn = QPushButton("\u23FA REC")
        self.record_btn.setObjectName("recordBtn")
        self.record_btn.setCheckable(True)
        self.record_btn.clicked.connect(self._toggle_recording)
        self.record_btn.setToolTip("Start/Stop Recording (R)")
        layout.addWidget(self.record_btn)

        return frame

    # ================================================================
    #  STATUS BAR
    # ================================================================

    def _build_status_bar(self):
        """Build the status bar."""
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        self._status_label = QLabel("Ready")
        self._status_label.setObjectName("statusLabel")
        self.status_bar.addWidget(self._status_label, 1)

        self._actions_label = QLabel("Actions: 0")
        self._actions_label.setObjectName("statusLabel")
        self.status_bar.addPermanentWidget(self._actions_label)

        self._backend_label = QLabel("")
        self._backend_label.setObjectName("statusLabel")
        self.status_bar.addPermanentWidget(self._backend_label)

    # ================================================================
    #  SHORTCUTS & TIMERS & SIGNALS
    # ================================================================

    def _setup_shortcuts(self):
        pass  # Menu actions cover all shortcuts

    def keyPressEvent(self, event):
        """Handle key presses for transport and recording."""
        key = event.key()
        mods = event.modifiers()

        if key == Qt.Key.Key_Space:
            self._on_play_clicked()
        elif key == Qt.Key.Key_R and not mods:
            self._toggle_recording()
        elif key == Qt.Key.Key_Escape:
            self._cancel_recording()
        elif key == Qt.Key.Key_Left:
            if mods & Qt.KeyboardModifier.ControlModifier:
                self.video_player.seek_relative(-10000)
            else:
                self.video_player.seek_relative(-5000)
        elif key == Qt.Key.Key_Right:
            if mods & Qt.KeyboardModifier.ControlModifier:
                self.video_player.seek_relative(10000)
            else:
                self.video_player.seek_relative(5000)
        elif key == Qt.Key.Key_Comma:
            self.video_player.seek_frames(-1)
        elif key == Qt.Key.Key_Period:
            self.video_player.seek_frames(1)
        elif key == Qt.Key.Key_Home:
            self.video_player.seek(0)
        elif key == Qt.Key.Key_End:
            self.video_player.seek(self.video_player.get_duration_ms())
        else:
            super().keyPressEvent(event)

    def _setup_timers(self):
        """Set up periodic update timers."""
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(33)
        self._ui_timer.timeout.connect(self._update_ui)
        self._ui_timer.start()

        self._record_timer = QTimer(self)
        self._record_timer.setInterval(11)
        self._record_timer.timeout.connect(self._record_sample)

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(60000)
        self._autosave_timer.timeout.connect(self._autosave)

    def _connect_signals(self):
        """Connect inter-component signals."""
        self.video_player.time_changed.connect(self._on_time_changed)
        self.video_player.duration_changed.connect(self._on_duration_changed)
        self.video_player.state_changed.connect(self._on_state_changed)

        self.timeline.seek_requested.connect(self._on_timeline_seek)
        self.timeline.clear_range_requested.connect(self._on_clear_range)

        self.timeline.point_moved.connect(self._on_point_moved)
        self.timeline.point_deleted.connect(self._on_point_deleted)
        self.timeline.point_added.connect(self._on_point_added)
        self.timeline.points_modified.connect(self._on_points_modified)

    # ================================================================
    #  SIGNAL HANDLERS
    # ================================================================

    def _on_time_changed(self, time_ms: int):
        self.timeline.set_playhead(time_ms)
        if not self._seeking:
            dur = self.video_player.get_duration_ms()
            if dur > 0:
                self.seek_slider.blockSignals(True)
                self.seek_slider.setValue(int(time_ms / dur * 1000))
                self.seek_slider.blockSignals(False)

    def _on_duration_changed(self, duration_ms: int):
        self.timeline.set_duration(duration_ms)

    def _on_state_changed(self, state: str):
        if state == "playing":
            self.play_btn.setText("\u23F8 Pause")
            self.play_btn.setChecked(True)
        else:
            self.play_btn.setText("\u25B6 Play")
            self.play_btn.setChecked(False)

    def _on_play_clicked(self):
        self.video_player.toggle_play_pause()

    def _on_seek_start(self):
        self._seeking = True

    def _on_seek_moved(self, value):
        dur = self.video_player.get_duration_ms()
        if dur > 0:
            time_ms = int(value / 1000 * dur)
            self.time_label.setText(
                f"{self._format_time(time_ms)} / {self._format_time(dur)}")
            self.timeline.set_playhead(time_ms)

    def _on_seek_end(self):
        dur = self.video_player.get_duration_ms()
        if dur > 0:
            time_ms = int(self.seek_slider.value() / 1000 * dur)
            self.video_player.seek(time_ms)
            self.timeline.set_playhead(time_ms)
        self._seeking = False

    def _on_timeline_seek(self, time_ms: int):
        self.video_player.seek(time_ms)
        self.timeline.set_playhead(time_ms)

    def _on_clear_range(self, start_ms: int, end_ms: int):
        self.recorder.clear_range(self._active_axis, start_ms, end_ms)
        self._refresh_timeline()

    # ---- Point editing handlers ----

    def _on_point_moved(self, idx: int, new_at: int, new_pos: int):
        axis = self.project.get_axis(self._active_axis)
        if 0 <= idx < len(axis.actions):
            axis.actions[idx].at = new_at
            axis.actions[idx].pos = new_pos
            axis.sort_actions()
        self._refresh_timeline()
        self._update_status(f"Point moved to {self._format_time(new_at)}, pos {new_pos}")

    def _on_point_deleted(self, idx: int):
        axis = self.project.get_axis(self._active_axis)
        if 0 <= idx < len(axis.actions):
            removed = axis.actions.pop(idx)
            self._refresh_timeline()
            self._update_status(f"Point deleted at {self._format_time(removed.at)}")

    def _on_point_added(self, at_ms: int, pos: int):
        axis = self.project.get_axis(self._active_axis)
        new_action = FunscriptAction(at=at_ms, pos=pos)
        axis.actions.append(new_action)
        axis.sort_actions()
        self._refresh_timeline()
        self._update_status(f"Point added at {self._format_time(at_ms)}, pos {pos}")

    def _on_points_modified(self):
        axis = self.project.get_axis(self._active_axis)
        axis.sort_actions()
        self._refresh_timeline()

    # ---- Axis / settings handlers ----

    def _on_axis_changed(self, index):
        self._active_axis = self.axis_combo.currentData()
        axis_def = AXIS_DEFINITIONS.get(self._active_axis, {})
        label = axis_def.get("label", self._active_axis)
        self.position_display.set_axis_label(label)
        if self._pos_overlay and self._pos_overlay.isVisible():
            self._pos_overlay.set_axis_label(label)
        self._refresh_timeline()

    def _on_multi_axis_changed(self):
        active = set()
        for name, cb in self._axis_checks.items():
            if cb.isChecked():
                active.add(name)
        if not active:
            active.add("stroke")
        self.recorder.active_axes = active

    def _toggle_all_record_axes(self):
        """Toggle all recording axis checkboxes on/off."""
        all_checked = all(cb.isChecked() for cb in self._axis_checks.values())
        for cb in self._axis_checks.values():
            cb.blockSignals(True)
            cb.setChecked(not all_checked)
            cb.blockSignals(False)
        # Ensure at least stroke if we just unchecked everything
        if all_checked:
            self._axis_checks["stroke"].blockSignals(True)
            self._axis_checks["stroke"].setChecked(True)
            self._axis_checks["stroke"].blockSignals(False)
        self._on_multi_axis_changed()
        self._select_all_axes_btn.setText("None" if not all_checked else "All")

    def _on_speed_changed(self, text: str):
        speed = float(text.replace("x", ""))
        self.video_player.set_speed(speed)

    def _on_sensitivity_changed(self, value: int):
        scale = value / 100.0
        self.sens_label.setText(f"{scale:.1f}x")
        if self.controller:
            self.controller.set_sensitivity(scale)

    def _on_mouse_moved(self, nx: float, ny: float):
        if isinstance(self.controller, MouseFallbackInput):
            self.controller.update_from_mouse(nx, ny)

    # ---- Phase 1: Stabilization handler ----

    def _on_stabilization_changed(self, text: str):
        preset = text.lower()
        if self.controller:
            self.controller.set_stabilization_preset(preset)
        self._update_status(f"Stabilization: {text}")

    # ---- Phase 2: Axis lock handler ----

    def _on_axis_lock_toggled(self, axis_name: str, locked: bool):
        """Toggle axis lock on the controller."""
        btn = self._axis_lock_btns.get(axis_name)
        if locked:
            self.controller.lock_axis(axis_name)
            if btn:
                btn.setText("\U0001F512")  # locked icon
                btn.setToolTip(f"Unlock {axis_name}")
        else:
            self.controller.unlock_axis(axis_name)
            if btn:
                btn.setText("\U0001F513")  # unlocked icon
                btn.setToolTip(f"Lock {axis_name}")

    # ---- Phase 3: Overlay toggles ----

    def _on_overlay_toggled(self, axis_name: str, visible: bool):
        """Toggle visibility of an axis overlay lane on the timeline."""
        self._overlay_visible[axis_name] = visible
        self._refresh_timeline()

    def _show_all_overlays(self):
        """Show all axis overlays on the timeline."""
        for ax_name, action in self._overlay_actions.items():
            action.blockSignals(True)
            action.setChecked(True)
            action.blockSignals(False)
            self._overlay_visible[ax_name] = True
        self._refresh_timeline()

    def _hide_all_overlays(self):
        """Hide all axis overlays on the timeline."""
        for ax_name, action in self._overlay_actions.items():
            action.blockSignals(True)
            action.setChecked(False)
            action.blockSignals(False)
            self._overlay_visible[ax_name] = False
        self._refresh_timeline()

    # ---- Phase 4: Heatmap toggle ----

    def _toggle_heatmap(self, show: bool):
        self.timeline.set_heatmap_visible(show)

    # ================================================================
    #  RECORDING
    # ================================================================

    def _toggle_recording(self):
        if self._recording_active:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        if not self.video_player.get_video_path():
            self._update_status("No video loaded. Open a video first.")
            return

        current_time = self.video_player.get_time_ms()
        self.recorder.start_recording(current_time)
        self._recording_active = True
        self.record_btn.setChecked(True)
        self.record_btn.setText("\u23F9 STOP")
        self.timeline.set_recording(True)
        self.position_display.set_recording(True)
        if self._pos_overlay and self._pos_overlay.isVisible():
            self._pos_overlay.set_recording(True)

        self._record_timer.start()

        if not self.video_player.is_playing():
            self.video_player.play()

        self._update_status("\U0001F534 Recording...")

    def _stop_recording(self):
        self._record_timer.stop()
        segments = self.recorder.stop_recording()
        self._recording_active = False
        self.record_btn.setChecked(False)
        self.record_btn.setText("\u23FA REC")
        self.timeline.set_recording(False)
        self.timeline.set_buffer_actions([])
        self.position_display.set_recording(False)
        if self._pos_overlay and self._pos_overlay.isVisible():
            self._pos_overlay.set_recording(False)

        if self.video_player.is_playing():
            self.video_player.pause()

        total_points = sum(len(s.actions) for s in segments.values())
        axes = ", ".join(segments.keys())
        self._update_status(f"Recording saved: {total_points} points on {axes}")
        self._refresh_timeline()

    def _cancel_recording(self):
        if not self._recording_active:
            return
        self._record_timer.stop()
        self.recorder.cancel_recording()
        self._recording_active = False
        self.record_btn.setChecked(False)
        self.record_btn.setText("\u23FA REC")
        self.timeline.set_recording(False)
        self.timeline.set_buffer_actions([])
        self.position_display.set_recording(False)
        if self._pos_overlay and self._pos_overlay.isVisible():
            self._pos_overlay.set_recording(False)
        self._update_status("Recording cancelled.")

    def _record_sample(self):
        if not self._recording_active:
            return
        state = self.controller.get_current_state()
        if not state.is_tracked:
            return
        video_time = self.video_player.get_time_ms()
        self.recorder.add_sample(video_time, state.mapped)

    # ================================================================
    #  UI UPDATES
    # ================================================================

    def _update_ui(self):
        """Periodic UI update (called at ~30fps)."""
        current = self.video_player.get_time_ms()
        duration = self.video_player.get_duration_ms()
        self.time_label.setText(
            f"{self._format_time(current)} / {self._format_time(duration)}")

        if not self._seeking:
            self.timeline.set_playhead(current)

        if not self._seeking and duration > 0:
            slider_val = int(current / duration * 1000)
            if abs(self.seek_slider.value() - slider_val) > 1:
                self.seek_slider.blockSignals(True)
                self.seek_slider.setValue(slider_val)
                self.seek_slider.blockSignals(False)

        # Update position display from controller
        state = self.controller.get_current_state()
        self.position_display.set_tracked(state.is_tracked)

        _pos_ol = self._pos_overlay
        _pos_visible = _pos_ol is not None and _pos_ol.isVisible()
        if _pos_visible:
            _pos_ol.set_tracked(state.is_tracked)

        if state.is_tracked and state.mapped:
            axis_def = AXIS_DEFINITIONS.get(self._active_axis, {})
            ctrl_axis = axis_def.get("controller_axis", "y")
            primary_pos = state.mapped.get(ctrl_axis, 50)
            self.position_display.set_position(primary_pos)
            if _pos_visible:
                _pos_ol.set_position(primary_pos)

            secondary = {}
            for name, info in AXIS_DEFINITIONS.items():
                if name != self._active_axis:
                    ca = info.get("controller_axis", "y")
                    secondary[name] = state.mapped.get(ca, 50)
            self.position_display.set_secondary_positions(secondary)
            if _pos_visible:
                _pos_ol.set_secondary_positions(secondary)

        # Update 3D controller visualization
        self.viz_panel.set_controller_state(
            state.pitch, state.yaw, state.roll,
            state.x, state.y, state.z, state.is_tracked
        )
        if self._viz_overlay and self._viz_overlay.isVisible():
            self._viz_overlay.set_controller_state(
                state.pitch, state.yaw, state.roll,
                state.x, state.y, state.z, state.is_tracked
            )

        # Update recording buffer preview
        if self._recording_active:
            buf = self.recorder.get_buffer_preview()
            axis_buf = buf.get(self._active_axis, [])
            self.timeline.set_buffer_actions(axis_buf)

        # Update action count
        total = self.project.get_total_actions()
        axis = self.project.get_axis(self._active_axis)
        self._actions_label.setText(
            f"Actions: {len(axis.actions)} ({total} total)")

        # Backend label
        backend = self.video_player.get_backend_name()
        if not self._use_mouse_fallback and hasattr(self.controller, 'get_controller_name'):
            input_mode = self.controller.get_controller_name()
        elif not self._use_mouse_fallback:
            input_mode = "VR"
        else:
            input_mode = "Mouse"
        self._backend_label.setText(f"Video: {backend} | Input: {input_mode}")

    def _refresh_timeline(self):
        """
        Refresh timeline with current axis data + overlay lanes.
        Phase 3: populates primary lane + visible overlay lanes.
        """
        # Primary lane (editable)
        axis = self.project.get_axis(self._active_axis)
        self.timeline.set_actions(axis.actions)

        # Overlay lanes for other axes
        overlay_lanes = {}
        overlay_vis = {}
        for ax_name in AXIS_DEFINITIONS:
            if ax_name == self._active_axis:
                continue
            other_axis = self.project.get_axis(ax_name)
            if other_axis.actions:
                overlay_lanes[ax_name] = other_axis.actions
                overlay_vis[ax_name] = self._overlay_visible.get(ax_name, False)
        self.timeline.set_overlay_lanes(overlay_lanes, overlay_vis)

        # Beat analysis lane (Phase 5)
        if self._beat_data is not None:
            self.timeline.set_analysis_lane("beats", self._beat_data, True)

    # ================================================================
    #  FILE OPERATIONS
    # ================================================================

    def _open_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Video",
            os.path.expanduser("~"),
            "Video Files (*.mp4 *.mkv *.avi *.wmv *.webm *.mov *.m4v *.flv);;All Files (*)"
        )
        if path:
            self.video_player.load_video(path)
            self.project.video_path = path
            self.setWindowTitle(f"vFAPS - {os.path.basename(path)}")
            self._update_status(f"Loaded: {os.path.basename(path)}")
            self._autosave_timer.start()

    def _open_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project",
            os.path.expanduser("~"),
            "Project Files (*.fsproj);;All Files (*)"
        )
        if path:
            try:
                self.project, extra = FunscriptProject.load_project(path)
                self.recorder = Recorder(self.project)
                self._autosave_path = path
                # Restore beat data if present
                self._restore_extra_data(extra)
                if self.project.video_path and os.path.exists(self.project.video_path):
                    self.video_player.load_video(self.project.video_path)
                self._refresh_timeline()
                self._update_status(f"Project loaded: {os.path.basename(path)}")
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to load project:\n{e}")

    def _save_project(self):
        if self._autosave_path:
            self.project.save_project(self._autosave_path, self._get_extra_data())
            self._update_status(f"Project saved: {os.path.basename(self._autosave_path)}")
        else:
            self._save_project_as()

    def _save_project_as(self):
        default_name = "untitled.fsproj"
        if self.project.video_path:
            default_name = os.path.splitext(
                os.path.basename(self.project.video_path))[0] + ".fsproj"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project",
            os.path.join(os.path.expanduser("~"), default_name),
            "Project Files (*.fsproj)"
        )
        if path:
            self.project.save_project(path, self._get_extra_data())
            self._autosave_path = path
            self._update_status(f"Project saved: {os.path.basename(path)}")

    def _import_funscript(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Funscript",
            os.path.expanduser("~"),
            "Funscript Files (*.funscript);;JSON Files (*.json)"
        )
        if path:
            try:
                self.project.import_funscript(path, self._active_axis)
                self._refresh_timeline()
                self._update_status(f"Imported funscript to {self._active_axis} axis")
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to import:\n{e}")

    def _export_funscript(self):
        if self.project.get_total_actions() == 0:
            QMessageBox.information(self, "Export", "No actions to export. Record some data first.")
            return

        dir_path = QFileDialog.getExistingDirectory(
            self, "Export Funscript - Select Output Directory",
            os.path.expanduser("~")
        )
        if dir_path:
            try:
                exported = self.project.export_funscript(dir_path)
                files = ", ".join(os.path.basename(f) for f in exported)
                self._update_status(f"Exported: {files}")
                QMessageBox.information(
                    self, "Export Complete",
                    f"Exported {len(exported)} funscript file(s):\n\n" +
                    "\n".join(os.path.basename(f) for f in exported)
                )
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Export failed:\n{e}")

    def _export_bundle(self):
        """Export all axes as standard .funscript files inside a single .zip."""
        if self.project.get_total_actions() == 0:
            QMessageBox.information(self, "Export Bundle", "No actions to export.")
            return

        default_name = "untitled_funscripts.zip"
        if self.project.video_path:
            default_name = os.path.splitext(
                os.path.basename(self.project.video_path))[0] + "_funscripts.zip"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Funscript Bundle (ZIP)",
            os.path.join(os.path.expanduser("~"), default_name),
            "ZIP Archive (*.zip)"
        )
        if path:
            try:
                import zipfile
                import tempfile
                with tempfile.TemporaryDirectory() as tmp_dir:
                    exported = self.project.export_funscript(tmp_dir)
                    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
                        for fpath in exported:
                            zf.write(fpath, os.path.basename(fpath))
                names = ", ".join(os.path.basename(f) for f in exported)
                self._update_status(f"Bundle exported: {os.path.basename(path)} ({len(exported)} files)")
                QMessageBox.information(
                    self, "Bundle Export Complete",
                    f"Exported {len(exported)} funscript file(s) to:\n"
                    f"{os.path.basename(path)}\n\nContents:\n{names}"
                )
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Bundle export failed:\n{e}")

    def _autosave(self):
        if self._autosave_path and self.project.get_total_actions() > 0:
            try:
                self.project.save_project(self._autosave_path, self._get_extra_data())
            except Exception:
                pass

    # ---- Extra data helpers (Phase 7) ----

    def _get_extra_data(self) -> dict:
        """Collect extra data to persist with the project."""
        extra = {}
        if self._beat_data is not None:
            # Serialize beat data
            bd = self._beat_data
            beat_dict = {}
            if hasattr(bd, 'beats'):
                beat_dict["beats"] = list(bd.beats) if bd.beats is not None else []
            if hasattr(bd, 'bpm'):
                beat_dict["bpm"] = bd.bpm
            if hasattr(bd, 'subdivisions'):
                beat_dict["subdivisions"] = bd.subdivisions
            extra["beat_data"] = beat_dict
        return extra

    def _restore_extra_data(self, extra: dict):
        """Restore extra data from a loaded project."""
        if not extra:
            return
        beat_dict = extra.get("beat_data")
        if beat_dict and HAS_BEAT_DETECTION:
            try:
                bd = BeatData(
                    beats=beat_dict.get("beats", []),
                    bpm=beat_dict.get("bpm", beat_dict.get("tempo", 0.0)),
                    subdivisions=beat_dict.get("subdivisions", 4),
                )
                self._beat_data = bd
                self._beats_visible_action.setEnabled(True)
                self._beats_visible_action.setChecked(True)
            except Exception:
                pass

    # ================================================================
    #  EDIT OPERATIONS
    # ================================================================

    def _undo(self):
        if self.recorder.undo():
            self._refresh_timeline()
            self._update_status("Undo")
        else:
            self._update_status("Nothing to undo")

    def _redo(self):
        if self.recorder.redo():
            self._refresh_timeline()
            self._update_status("Redo")
        else:
            self._update_status("Nothing to redo")

    def _smooth_selection(self):
        s1, s2 = self.timeline.get_selection()
        window = self.smooth_spin.value()
        if s1 >= 0:
            self.recorder.apply_smoothing(self._active_axis, window, s1, s2)
        else:
            self.recorder.apply_smoothing(self._active_axis, window)
        self._refresh_timeline()
        self._update_status(f"Applied smoothing (window={window})")

    def _clear_selection(self):
        s1, s2 = self.timeline.get_selection()
        if s1 >= 0:
            self.recorder.clear_range(self._active_axis, s1, s2)
            self.timeline.clear_selection()
            self._refresh_timeline()
            self._update_status("Cleared data in selection")

    # ================================================================
    #  CALIBRATION
    # ================================================================

    def _auto_calibrate(self):
        if self._calibrating:
            return
        self._calibrating = True
        self._cal_wizard.start()
        self._update_status("Calibration wizard open -- follow the on-screen steps.")

    def _apply_calibration(self, results: dict):
        self._calibrating = False
        for axis, vals in results.items():
            lo = vals.get("min", 0)
            hi = vals.get("max", 0)
            if lo != hi:
                margin = abs(hi - lo) * 0.05
                self.controller.calibrate_axis(axis, lo - margin, hi + margin)
        self.controller.reset_filters()
        n = len(results)
        self._update_status(f"Calibration applied to {n} axes!")

    def _on_cal_cancelled(self):
        self._calibrating = False
        self._update_status("Calibration cancelled.")

    def _recenter_controller(self):
        if self._calibrating:
            return
        self.controller.recenter()
        self._update_status("Controller re-centered to current position.")

    # ================================================================
    #  PHASE 5: BEAT DETECTION
    # ================================================================

    def _detect_beats(self):
        """Run beat detection on the current video's audio track."""
        if not HAS_BEAT_DETECTION:
            QMessageBox.warning(self, "Beat Detection",
                "Beat detection requires numpy.\nInstall with: pip install numpy")
            return

        video_path = self.video_player.get_video_path()
        if not video_path:
            QMessageBox.information(self, "Beat Detection",
                "Load a video first.")
            return

        self._update_status("Detecting beats... (this may take a moment)")
        QApplication.processEvents()

        try:
            bd = detect_beats(video_path)
            if bd is None:
                QMessageBox.warning(self, "Beat Detection",
                    "Beat detection returned no results.\n"
                    "Make sure ffmpeg is installed and the video has an audio track.")
                self._update_status("Beat detection: no results.")
                return
            self._beat_data = bd
            self.timeline.set_analysis_lane("beats", bd, True)
            self._beats_visible_action.setEnabled(True)
            self._beats_visible_action.setChecked(True)
            n_beats = len(bd.beats) if bd.beats else 0
            n_onsets = len(bd.onsets) if bd.onsets else 0
            bpm_str = f"{bd.bpm:.1f} BPM" if bd.bpm else "unknown"
            conf_str = f"{bd.confidence * 100:.0f}%" if bd.confidence else "?"
            msg = f"Beat detection: {bpm_str} (confidence {conf_str}), {n_beats} beats, {n_onsets} onsets"
            if bd.confidence < 0.2:
                msg += " ⚠ Low confidence — audio may lack clear rhythm"
            self._update_status(msg)
        except Exception as e:
            QMessageBox.warning(self, "Beat Detection Error",
                f"Failed to detect beats:\n{e}")
            self._update_status("Beat detection failed.")

    def _toggle_beats_visible(self, show: bool):
        if self._beat_data is not None:
            self.timeline.set_analysis_visibility("beats", show)
            self.timeline.update()

    def _snap_to_beats(self):
        """Menu action: snap using the current slider strength (or 100% if slider is at 0)."""
        strength = self.beat_snap_slider.value()
        if strength == 0:
            strength = 100  # Menu action with slider at 0 means "full snap"
        self._apply_beat_snap_with_strength(strength)

    def _on_beat_snap_changed(self, value: int):
        """Update the beat snap label when slider moves."""
        self.beat_snap_label.setText(f"{value}%")

    def _apply_beat_snap(self):
        """Apply beat snap at the current slider strength."""
        strength = self.beat_snap_slider.value()
        if strength == 0:
            self._update_status("Beat snap is at 0% — move the slider to set snap strength.")
            return
        self._apply_beat_snap_with_strength(strength)

    def _apply_beat_snap_with_strength(self, strength: int):
        """
        Snap points toward the nearest beat with variable strength.
        strength: 0-100, where 0 = no change, 100 = fully on the beat.
        Each point's timestamp is interpolated:
          new_at = original_at + (nearest_beat - original_at) * strength/100
        """
        if not HAS_BEAT_DETECTION or self._beat_data is None:
            self._update_status("No beat data. Run beat detection first (Tools → Detect Beats).")
            return

        axis = self.project.get_axis(self._active_axis)
        if not axis.actions:
            self._update_status("No actions to snap.")
            return

        beats = self._beat_data.beats
        if not beats:
            self._update_status("No beats detected.")
            return

        # Use beat grid (including subdivisions) for finer snapping
        grid = self._beat_data.get_beat_grid()
        if not grid:
            grid = beats

        # Determine target range
        s1, s2 = self.timeline.get_selection()
        if s1 >= 0:
            target = axis.get_actions_in_range(s1, s2)
        else:
            target = axis.actions

        # Save for undo
        import copy
        previous = copy.deepcopy(target)

        blend = strength / 100.0
        tolerance_ms = 60000.0 / max(1, self._beat_data.bpm) * 0.6  # ~60% of beat interval
        snapped = 0

        for action in target:
            # Find nearest grid point
            nearest = min(grid, key=lambda b: abs(b - action.at))
            dist = abs(nearest - action.at)

            if dist <= tolerance_ms:
                # Lerp: blend between original and beat position
                original_at = action.at
                new_at = int(original_at + (nearest - original_at) * blend)
                if new_at != original_at:
                    action.at = new_at
                    snapped += 1

        axis.sort_actions()
        axis.remove_duplicates()

        # Push to undo stack
        from recorder import RecordingSegment
        if target and snapped > 0:
            seg = RecordingSegment(
                axis_name=self._active_axis,
                actions=copy.deepcopy(target),
                start_ms=target[0].at if target else 0,
                end_ms=target[-1].at if target else 0,
            )
            self.recorder.undo_manager.push("beat_snap", seg, previous)

        self._refresh_timeline()
        range_str = f" in selection" if s1 >= 0 else ""
        self._update_status(
            f"Beat snap {strength}%: {snapped} points adjusted{range_str}"
            f" ({self._beat_data.bpm:.0f} BPM grid)"
        )

    # ================================================================
    #  3D VIZ OVERLAY
    # ================================================================

    def _show_viz_overlay(self):
        """Toggle the floating 3D viz overlay."""
        if self._viz_overlay and self._viz_overlay.isVisible():
            self._viz_overlay.hide()
            self._viz_overlay_action.blockSignals(True)
            self._viz_overlay_action.setChecked(False)
            self._viz_overlay_action.blockSignals(False)
            return
        if self._viz_overlay is None:
            self._viz_overlay = ControllerVizOverlay(self._video_area)
            self._viz_overlay.closed.connect(self._on_viz_overlay_closed)
        video_global = self._video_area.mapToGlobal(
            self._video_area.rect().topLeft()
        )
        self._viz_overlay.move(video_global.x() + 10, video_global.y() + 10)
        self._viz_overlay.show()
        self._viz_overlay.raise_()
        self._viz_overlay_action.blockSignals(True)
        self._viz_overlay_action.setChecked(True)
        self._viz_overlay_action.blockSignals(False)

    def _toggle_viz_overlay(self, show: bool):
        if show:
            self._show_viz_overlay()
        elif self._viz_overlay:
            self._viz_overlay.hide()

    def _on_viz_overlay_closed(self):
        self._viz_overlay_action.blockSignals(True)
        self._viz_overlay_action.setChecked(False)
        self._viz_overlay_action.blockSignals(False)

    # ================================================================
    #  POSITION DISPLAY OVERLAY
    # ================================================================

    def _show_pos_overlay(self):
        """Toggle the floating position bar overlay."""
        if self._pos_overlay and self._pos_overlay.isVisible():
            self._pos_overlay.hide()
            self._pos_overlay_action.blockSignals(True)
            self._pos_overlay_action.setChecked(False)
            self._pos_overlay_action.blockSignals(False)
            return
        if self._pos_overlay is None:
            self._pos_overlay = PositionDisplayOverlay(self._video_area)
            self._pos_overlay.closed.connect(self._on_pos_overlay_closed)
        video_global = self._video_area.mapToGlobal(
            self._video_area.rect().topRight()
        )
        self._pos_overlay.move(video_global.x() - 130, video_global.y() + 10)
        self._pos_overlay.show()
        self._pos_overlay.raise_()
        self._pos_overlay_action.blockSignals(True)
        self._pos_overlay_action.setChecked(True)
        self._pos_overlay_action.blockSignals(False)

    def _toggle_pos_overlay(self, show: bool):
        if show:
            self._show_pos_overlay()
        elif self._pos_overlay:
            self._pos_overlay.hide()

    def _on_pos_overlay_closed(self):
        self._pos_overlay_action.blockSignals(True)
        self._pos_overlay_action.setChecked(False)
        self._pos_overlay_action.blockSignals(False)

    # ================================================================
    #  HELP
    # ================================================================

    def _show_shortcuts(self):
        QMessageBox.information(self, "Keyboard Shortcuts", """
Keyboard Shortcuts:

PLAYBACK:
  Space          Play / Pause
  Left Arrow     Skip back 5s
  Right Arrow    Skip forward 5s
  Ctrl+Left      Skip back 10s
  Ctrl+Right     Skip forward 10s
  , (comma)      Previous frame
  . (period)     Next frame
  Home           Go to start
  End            Go to end

RECORDING:
  R              Start / Stop recording
  Escape         Cancel recording
  C              Re-center controller

EDITING:
  Ctrl+Z         Undo
  Ctrl+Y         Redo
  Delete         Clear selected data / delete point

FILE:
  Ctrl+O         Open video
  Ctrl+S         Save project
  Ctrl+E         Export funscript

VIEW:
  Ctrl+3         Toggle 3D controller overlay
  Ctrl+4         Toggle position bar overlay
  Ctrl+H         Toggle heatmap
  Ctrl+B         Toggle beat markers

TIMELINE:
  Click          Seek to position
  Click point    Select & drag to move
  Double-click   Add new point / select point
  Ctrl+Click     Add new point
  Shift+Drag     Select range
  Middle-Drag    Pan
  Scroll wheel   Zoom in/out
  Ctrl+0         Zoom to fit
  Right-click    Context menu (add/delete point)

VALVE INDEX CONTROLLER:
  Trigger        Start/Stop recording + play/pause
  A Button       Start auto-calibration (confirm during calibration)
  B Button       Re-center controller (go back during calibration)
  Thumbstick L/R Scrub backward/forward (speed varies with deflection)

META QUEST TOUCH (via Quest Link / Air Link):
  Trigger        Start/Stop recording + play/pause
  A/X Button     Start auto-calibration (confirm during calibration)
  B/Y Button     Re-center controller (go back during calibration)
  Thumbstick L/R Scrub backward/forward (speed varies with deflection)
""")

    def _show_about(self):
        QMessageBox.about(self, "About vFAPS",
            "vFAPS v2.0\n"
            "VR Funscript Authoring & Playback Studio\n\n"
            "Create multi-axis funscripts using\n"
            "VR controller motion tracking.\n\n"
            "Supported Controllers:\n"
            "  * Valve Index (Knuckles)\n"
            "  * Meta Quest Touch (via Link)\n"
            "  * HTC Vive Wand\n"
            "  * Other SteamVR controllers\n\n"
            "Features:\n"
            "  * Realtime stabilization pipeline\n"
            "  * Per-axis lock/unlock controls\n"
            "  * Multi-lane timeline overlays\n"
            "  * Heatmap visualization\n"
            "  * Beat detection & snap tools\n"
            "  * Bundle export\n\n"
            "Supports mouse fallback mode for\n"
            "development and testing.\n\n"
            "\u2615 Support: https://buymeacoffee.com/vfaps")

    def _open_coffee_link(self):
        QDesktopServices.openUrl(QUrl("https://buymeacoffee.com/vfaps"))

    # ================================================================
    #  UTILITIES
    # ================================================================

    @staticmethod
    def _format_time(ms: int) -> str:
        if ms < 0:
            ms = 0
        total_s = ms / 1000.0
        minutes = int(total_s // 60)
        seconds = total_s % 60
        return f"{minutes:02d}:{seconds:04.1f}"

    def _update_status(self, msg: str):
        self._status_label.setText(msg)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._calibrating and self._cal_wizard.isVisible():
            cw = self.centralWidget()
            if cw:
                global_pos = cw.mapToGlobal(cw.rect().topLeft())
                self._cal_wizard.setGeometry(
                    global_pos.x(), global_pos.y(),
                    cw.width(), cw.height()
                )

    def moveEvent(self, event):
        super().moveEvent(event)
        if self._calibrating and self._cal_wizard.isVisible():
            cw = self.centralWidget()
            if cw:
                global_pos = cw.mapToGlobal(cw.rect().topLeft())
                self._cal_wizard.setGeometry(
                    global_pos.x(), global_pos.y(),
                    cw.width(), cw.height()
                )

    def closeEvent(self, event):
        if self._recording_active:
            self._cancel_recording()

        if self._viz_overlay:
            self._viz_overlay.close()
        if self._pos_overlay:
            self._pos_overlay.close()
        if self._cal_wizard.isVisible():
            self._cal_wizard.close()

        if self._autosave_path and self.project.get_total_actions() > 0:
            self.project.save_project(self._autosave_path, self._get_extra_data())

        self.controller.shutdown()
        self.video_player.shutdown()
        event.accept()
