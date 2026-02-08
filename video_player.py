"""
Video Player Widget
====================
Embeds mpv video player into a PyQt6 widget with precise playback control.
Falls back to QMediaPlayer if mpv is not available.
"""

import os
import sys
import time
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QUrl
from PyQt6.QtGui import QColor

try:
    import mpv
    HAS_MPV = True
except (ImportError, OSError, FileNotFoundError):
    HAS_MPV = False

try:
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PyQt6.QtMultimediaWidgets import QVideoWidget
    HAS_QTMEDIA = True
except (ImportError, OSError, ModuleNotFoundError, RuntimeError):
    HAS_QTMEDIA = False


class VideoPlayerWidget(QWidget):
    """
    Video player widget that embeds mpv (preferred) or falls back to Qt multimedia.
    Provides precise time synchronization for funscript recording.
    """

    # Signals
    time_changed = pyqtSignal(int)       # Current time in ms
    duration_changed = pyqtSignal(int)    # Total duration in ms
    state_changed = pyqtSignal(str)       # "playing", "paused", "stopped"
    video_loaded = pyqtSignal(str)        # Video file path

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(480, 270)

        self._backend = None     # "mpv" or "qt"
        self._mpv_player = None
        self._qt_player = None
        self._qt_audio = None
        self._video_widget = None

        self._duration_ms: int = 0
        self._current_time_ms: int = 0
        self._is_playing: bool = False
        self._video_path: str = ""
        self._playback_speed: float = 1.0

        # Seek grace period - prevents poll from snapping playhead back
        # while mpv is still processing a seek command
        self._seek_grace_until: float = 0.0

        # Layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Video container
        self._container = QFrame(self)
        self._container.setStyleSheet("background-color: #11111b;")
        self._container.setFrameStyle(QFrame.Shape.NoFrame)
        layout.addWidget(self._container)

        # Timer for polling playback position
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(16)  # ~60fps polling
        self._poll_timer.timeout.connect(self._poll_time)

        # Initialize backend
        self._init_backend()

    def _init_backend(self):
        """Initialize the best available video backend."""
        if HAS_MPV:
            try:
                self._init_mpv()
                self._backend = "mpv"
                return
            except Exception as e:
                print(f"mpv init failed: {e}, trying Qt backend...")

        if HAS_QTMEDIA:
            try:
                self._init_qt_media()
                self._backend = "qt"
                return
            except Exception as e:
                print(f"Qt media init failed: {e}")

        # No backend - show message in the video container
        print("WARNING: No video backend available!")
        print("Install python-mpv: pip install python-mpv")
        print("And mpv: download from https://mpv.io/installation/")
        self._show_no_backend_message()

    def _init_mpv(self):
        """Initialize mpv player embedded in the widget."""
        container_layout = QVBoxLayout(self._container)
        container_layout.setContentsMargins(0, 0, 0, 0)

        self._video_widget = QWidget(self._container)
        self._video_widget.setStyleSheet("background-color: black;")
        container_layout.addWidget(self._video_widget)

        # Create mpv player
        self._mpv_player = mpv.MPV(
            wid=str(int(self._video_widget.winId())),
            vo='gpu-next,gpu,d3d11',
            hwdec='auto',
            keep_open='yes',
            idle='yes',
            osc='no',
            input_default_bindings='no',
            input_vo_keyboard='no',
            log_handler=lambda *args: None,
        )

        # Observe properties
        @self._mpv_player.property_observer('duration')
        def on_duration(name, value):
            if value:
                self._duration_ms = int(value * 1000)
                self.duration_changed.emit(self._duration_ms)

        @self._mpv_player.property_observer('pause')
        def on_pause(name, value):
            self._is_playing = not value
            self.state_changed.emit("paused" if value else "playing")

    def _init_qt_media(self):
        """Initialize Qt multimedia backend."""
        container_layout = QVBoxLayout(self._container)
        container_layout.setContentsMargins(0, 0, 0, 0)

        self._video_widget = QVideoWidget(self._container)
        container_layout.addWidget(self._video_widget)

        self._qt_player = QMediaPlayer()
        self._qt_audio = QAudioOutput()
        self._qt_player.setAudioOutput(self._qt_audio)
        self._qt_player.setVideoOutput(self._video_widget)

        self._qt_player.durationChanged.connect(
            lambda d: self.duration_changed.emit(d))
        self._qt_player.playbackStateChanged.connect(self._on_qt_state_changed)

    def _on_qt_state_changed(self, state):
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._is_playing = True
            self.state_changed.emit("playing")
        else:
            self._is_playing = False
            self.state_changed.emit("paused")

    def _show_no_backend_message(self):
        """Show a helpful message when no video backend is available."""
        container_layout = QVBoxLayout(self._container)
        container_layout.setContentsMargins(20, 20, 20, 20)
        msg = QLabel(
            "<div style='text-align:center; color:#f38ba8;'>"
            "<h3>No Video Backend Found</h3>"
            "<p style='color:#cdd6f4;'>Install <b>mpv</b> to enable video playback:</p>"
            "<p style='color:#a6adc8;'>"
            "1. <code>pip install python-mpv</code><br>"
            "2. Install mpv player: <code>winget install mpv</code><br>"
            "&nbsp;&nbsp;&nbsp;or download from "
            "<span style='color:#89b4fa;'>https://mpv.io/installation/</span><br>"
            "3. Ensure <code>mpv-2.dll</code> is in your system PATH"
            "</p></div>"
        )
        msg.setWordWrap(True)
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        container_layout.addWidget(msg)

    # ---- Public API ----

    def load_video(self, path: str):
        """Load a video file."""
        if not os.path.exists(path):
            return

        self._video_path = path

        if self._backend == "mpv" and self._mpv_player:
            self._mpv_player.play(path)
            self._mpv_player.pause = True
            # Wait briefly for duration
            QTimer.singleShot(500, lambda: self.duration_changed.emit(self._duration_ms))
        elif self._backend == "qt" and self._qt_player:
            self._qt_player.setSource(QUrl.fromLocalFile(path))

        self._poll_timer.start()
        self.video_loaded.emit(path)

    def play(self):
        """Start or resume playback."""
        if self._backend == "mpv" and self._mpv_player:
            self._mpv_player.pause = False
        elif self._backend == "qt" and self._qt_player:
            self._qt_player.play()
        self._is_playing = True

    def pause(self):
        """Pause playback."""
        if self._backend == "mpv" and self._mpv_player:
            self._mpv_player.pause = True
        elif self._backend == "qt" and self._qt_player:
            self._qt_player.pause()
        self._is_playing = False

    def toggle_play_pause(self):
        """Toggle between play and pause."""
        if self._is_playing:
            self.pause()
        else:
            self.play()

    def seek(self, time_ms: int):
        """Seek to a specific time in milliseconds."""
        time_ms = max(0, min(self._duration_ms, time_ms))
        self._current_time_ms = time_ms

        # Set a grace period - the poll should not override our position
        # until mpv has actually caught up to the seek target
        self._seek_grace_until = time.time() + 0.15  # 150ms grace

        if self._backend == "mpv" and self._mpv_player:
            self._mpv_player.seek(time_ms / 1000.0, reference='absolute')
        elif self._backend == "qt" and self._qt_player:
            self._qt_player.setPosition(time_ms)

        # Immediately emit so timeline/slider update right now
        self.time_changed.emit(time_ms)

    def seek_relative(self, delta_ms: int):
        """Seek relative to current position."""
        self.seek(self._current_time_ms + delta_ms)

    def seek_frames(self, frames: int):
        """Seek by frame count (approximate at 30fps if unknown)."""
        if self._backend == "mpv" and self._mpv_player:
            if frames > 0:
                for _ in range(frames):
                    self._mpv_player.frame_step()
            elif frames < 0:
                for _ in range(-frames):
                    self._mpv_player.frame_back_step()
            # Read back the new position after frame step
            try:
                pos = self._mpv_player.time_pos
                if pos is not None:
                    self._current_time_ms = int(pos * 1000)
                    self.time_changed.emit(self._current_time_ms)
            except Exception:
                pass
        else:
            # Approximate at 33ms per frame
            self.seek_relative(frames * 33)

    def set_speed(self, speed: float):
        """Set playback speed (0.25 to 4.0)."""
        speed = max(0.25, min(4.0, speed))
        self._playback_speed = speed
        if self._backend == "mpv" and self._mpv_player:
            self._mpv_player.speed = speed
        elif self._backend == "qt" and self._qt_player:
            self._qt_player.setPlaybackRate(speed)

    def get_time_ms(self) -> int:
        """Get current playback time in milliseconds."""
        return self._current_time_ms

    def get_duration_ms(self) -> int:
        """Get total video duration in milliseconds."""
        return self._duration_ms

    def is_playing(self) -> bool:
        return self._is_playing

    def get_backend_name(self) -> str:
        return self._backend or "none"

    def get_video_path(self) -> str:
        return self._video_path

    # ---- Internal ----

    def _poll_time(self):
        """Poll current playback time from the backend."""
        # During seek grace period, don't override _current_time_ms
        # with stale data from the backend
        in_grace = time.time() < self._seek_grace_until

        try:
            if self._backend == "mpv" and self._mpv_player:
                pos = self._mpv_player.time_pos
                if pos is not None:
                    new_time = int(pos * 1000)
                    if in_grace:
                        # Backend hasn't caught up yet - check if it's close
                        # to our target before accepting
                        if abs(new_time - self._current_time_ms) > 500:
                            # Still stale, skip this poll
                            pass
                        else:
                            # Backend caught up, end grace early
                            self._seek_grace_until = 0
                            if new_time != self._current_time_ms:
                                self._current_time_ms = new_time
                                self.time_changed.emit(new_time)
                    else:
                        if new_time != self._current_time_ms:
                            self._current_time_ms = new_time
                            self.time_changed.emit(new_time)

                # Also check duration
                dur = self._mpv_player.duration
                if dur and int(dur * 1000) != self._duration_ms:
                    self._duration_ms = int(dur * 1000)
                    self.duration_changed.emit(self._duration_ms)

            elif self._backend == "qt" and self._qt_player:
                new_time = self._qt_player.position()
                if not in_grace and new_time != self._current_time_ms:
                    self._current_time_ms = new_time
                    self.time_changed.emit(new_time)
                dur = self._qt_player.duration()
                if dur != self._duration_ms:
                    self._duration_ms = dur
                    self.duration_changed.emit(dur)
        except Exception:
            pass

    def shutdown(self):
        """Clean up resources."""
        self._poll_timer.stop()
        if self._mpv_player:
            try:
                self._mpv_player.terminate()
            except Exception:
                pass
        if self._qt_player:
            self._qt_player.stop()

    def handle_mouse_for_fallback(self, normalized_x: float, normalized_y: float):
        """For mouse fallback mode - pass mouse position from video area."""
        pass  # Handled by main window
