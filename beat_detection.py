"""
Beat Detection Module
======================
Extracts audio from video files and detects beats/onsets for timeline
alignment. Uses subprocess ffmpeg for audio extraction and a simple
onset detection algorithm (no heavy ML dependencies).

Beat data is stored as a list of timestamps (ms) in the project.

Dependencies (optional):
  - ffmpeg (system binary) for audio extraction
  - numpy (already required) for signal processing
"""

import os
import struct
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass, field
from typing import Optional

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


@dataclass
class BeatData:
    """Beat analysis results stored in a project."""
    beats: list[int] = field(default_factory=list)        # Beat timestamps in ms
    onsets: list[int] = field(default_factory=list)       # Onset timestamps in ms
    bpm: float = 0.0                                       # Estimated BPM
    confidence: float = 0.0                                # 0-1 confidence
    subdivisions: int = 4                                  # Beat subdivisions (4 = 16ths)

    def get_beat_grid(self) -> list[int]:
        """Get full beat grid including subdivisions."""
        if len(self.beats) < 2:
            return list(self.beats)

        grid = []
        for i in range(len(self.beats) - 1):
            start = self.beats[i]
            end = self.beats[i + 1]
            step = (end - start) / self.subdivisions
            for s in range(self.subdivisions):
                grid.append(int(start + s * step))
        grid.append(self.beats[-1])
        return grid

    def snap_to_beat(self, time_ms: int, tolerance_ms: int = 100) -> int:
        """Snap a timestamp to the nearest beat within tolerance."""
        if not self.beats:
            return time_ms

        best = time_ms
        best_dist = tolerance_ms + 1

        for beat in self.beats:
            dist = abs(time_ms - beat)
            if dist < best_dist:
                best_dist = dist
                best = beat

        return best if best_dist <= tolerance_ms else time_ms

    def snap_to_grid(self, time_ms: int, tolerance_ms: int = 50) -> int:
        """Snap a timestamp to the nearest beat grid point."""
        grid = self.get_beat_grid()
        if not grid:
            return time_ms

        best = time_ms
        best_dist = tolerance_ms + 1

        for point in grid:
            dist = abs(time_ms - point)
            if dist < best_dist:
                best_dist = dist
                best = point

        return best if best_dist <= tolerance_ms else time_ms

    def to_dict(self) -> dict:
        return {
            "beats": self.beats,
            "onsets": self.onsets,
            "bpm": self.bpm,
            "confidence": self.confidence,
            "subdivisions": self.subdivisions,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BeatData":
        return cls(
            beats=d.get("beats", []),
            onsets=d.get("onsets", []),
            bpm=d.get("bpm", 0.0),
            confidence=d.get("confidence", 0.0),
            subdivisions=d.get("subdivisions", 4),
        )


def _subprocess_kwargs() -> dict:
    """Get subprocess kwargs to hide console window on Windows frozen builds."""
    kwargs = {}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = si
    return kwargs


def _find_ffmpeg() -> Optional[str]:
    """
    Find the ffmpeg binary. Checks:
      1. Bundled alongside the executable (PyInstaller builds)
      2. _internal/ subfolder (PyInstaller one-folder)
      3. Same directory as this script
      4. System PATH
    Returns the full path, or None if not found.
    """
    candidates = []

    # PyInstaller bundle root
    if getattr(sys, 'frozen', False):
        bundle_dir = os.path.dirname(sys.executable)
        candidates.append(os.path.join(bundle_dir, "ffmpeg.exe"))
        candidates.append(os.path.join(bundle_dir, "_internal", "ffmpeg.exe"))
        candidates.append(os.path.join(bundle_dir, "ffmpeg", "ffmpeg.exe"))

    # Next to this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(script_dir, "ffmpeg.exe"))
    candidates.append(os.path.join(script_dir, "..", "ffmpeg.exe"))

    for path in candidates:
        if os.path.isfile(path):
            return os.path.abspath(path)

    # Fall back to system PATH
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True,
                        check=True, **_subprocess_kwargs())
        return "ffmpeg"
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    return None


# Cached ffmpeg path (resolved once)
_FFMPEG_PATH: Optional[str] = None


def _get_ffmpeg() -> Optional[str]:
    """Get the ffmpeg path, caching the result."""
    global _FFMPEG_PATH
    if _FFMPEG_PATH is None:
        _FFMPEG_PATH = _find_ffmpeg() or ""
    return _FFMPEG_PATH if _FFMPEG_PATH else None


def _check_ffmpeg() -> bool:
    """Check if ffmpeg is available."""
    return _get_ffmpeg() is not None


def extract_audio_wav(video_path: str, output_path: Optional[str] = None,
                      sample_rate: int = 22050) -> Optional[str]:
    """
    Extract audio from video to a mono WAV file using ffmpeg.
    Returns the path to the WAV file, or None on failure.
    """
    if not _check_ffmpeg():
        return None

    ffmpeg_bin = _get_ffmpeg()

    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

    try:
        subprocess.run([
            ffmpeg_bin, "-y", "-i", video_path,
            "-vn",                    # no video
            "-acodec", "pcm_s16le",   # 16-bit PCM
            "-ar", str(sample_rate),  # sample rate
            "-ac", "1",               # mono
            output_path
        ], capture_output=True, check=True, timeout=120, **_subprocess_kwargs())
        return output_path
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _load_wav(path: str) -> tuple[Optional[list], int]:
    """Load a WAV file and return (samples_as_float_list, sample_rate)."""
    try:
        with wave.open(path, 'r') as wf:
            sr = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
            # Assume 16-bit signed PCM mono
            samples = struct.unpack(f'<{n_frames}h', raw)
            # Normalize to -1.0 to 1.0
            max_val = 32768.0
            return [s / max_val for s in samples], sr
    except Exception:
        return None, 0


def detect_beats(video_path: str, progress_callback=None) -> Optional[BeatData]:
    """
    Full beat detection pipeline:
    1. Extract audio from video
    2. Compute onset envelope
    3. Find peaks (onsets)
    4. Estimate tempo and build beat grid

    progress_callback(float) is called with 0.0-1.0 progress if provided.

    Returns BeatData or None on failure.
    """
    if not HAS_NUMPY:
        return None

    if progress_callback:
        progress_callback(0.0)

    # Step 1: Extract audio
    wav_path = extract_audio_wav(video_path)
    if wav_path is None:
        return None

    if progress_callback:
        progress_callback(0.2)

    try:
        # Step 2: Load audio
        samples, sr = _load_wav(wav_path)
        if samples is None or sr == 0:
            return None

        audio = np.array(samples, dtype=np.float32)
        duration_ms = int(len(audio) / sr * 1000)

        if progress_callback:
            progress_callback(0.3)

        # Step 3: Compute onset envelope using spectral flux
        hop_size = 512
        window_size = 1024

        onset_env = _spectral_flux(audio, window_size, hop_size)

        if progress_callback:
            progress_callback(0.5)

        # Step 4: Peak picking on onset envelope
        onset_frames = _pick_peaks(onset_env, threshold_ratio=0.3, min_distance=4)
        onsets_ms = [int(f * hop_size / sr * 1000) for f in onset_frames]

        if progress_callback:
            progress_callback(0.7)

        # Step 5: Tempo estimation
        bpm, confidence = _estimate_tempo(onset_env, sr, hop_size)

        # Step 5b: Validate BPM against onset intervals as a sanity check
        if onsets_ms and len(onsets_ms) >= 4:
            ioi_bpm = _estimate_tempo_from_ioi(onsets_ms)
            if ioi_bpm > 0:
                # If autocorrelation and IOI disagree strongly, prefer IOI
                ratio = bpm / ioi_bpm if ioi_bpm > 0 else 999
                if ratio > 1.8 or ratio < 0.55:
                    bpm = ioi_bpm
                    confidence = max(0.1, confidence * 0.5)

        if progress_callback:
            progress_callback(0.8)

        # Step 6: Build beat grid from tempo
        if bpm > 0 and onsets_ms:
            beats = _build_beat_grid(bpm, onsets_ms, duration_ms)
        else:
            beats = onsets_ms  # Fall back to onsets as beats

        if progress_callback:
            progress_callback(1.0)

        return BeatData(
            beats=beats,
            onsets=onsets_ms,
            bpm=round(bpm, 1),
            confidence=round(confidence, 2),
            subdivisions=4,
        )

    finally:
        # Clean up temp file
        try:
            os.unlink(wav_path)
        except OSError:
            pass


def _spectral_flux(audio: "np.ndarray", window_size: int, hop_size: int) -> "np.ndarray":
    """Compute spectral flux onset envelope."""
    # Pad audio
    audio = np.pad(audio, (window_size // 2, window_size // 2))

    n_frames = (len(audio) - window_size) // hop_size + 1
    window = np.hanning(window_size)
    flux = np.zeros(n_frames)
    prev_spectrum = None

    for i in range(n_frames):
        start = i * hop_size
        frame = audio[start:start + window_size] * window
        spectrum = np.abs(np.fft.rfft(frame))

        if prev_spectrum is not None:
            # Half-wave rectified spectral flux (only increases)
            diff = spectrum - prev_spectrum
            flux[i] = np.sum(np.maximum(0, diff))

        prev_spectrum = spectrum

    # Normalize
    if flux.max() > 0:
        flux = flux / flux.max()

    return flux


def _pick_peaks(envelope: "np.ndarray", threshold_ratio: float = 0.3,
                min_distance: int = 4) -> list[int]:
    """Pick peaks from onset envelope with adaptive threshold."""
    # Adaptive threshold: local mean + ratio * local std
    # This avoids picking noise in quiet sections
    local_window = max(16, len(envelope) // 50)
    peaks = []

    for i in range(1, len(envelope) - 1):
        # Local statistics for adaptive threshold
        start = max(0, i - local_window)
        end = min(len(envelope), i + local_window)
        local_mean = np.mean(envelope[start:end])
        local_std = np.std(envelope[start:end])
        threshold = local_mean + threshold_ratio * max(local_std, 0.05)

        if envelope[i] > threshold:
            if envelope[i] >= envelope[i - 1] and envelope[i] >= envelope[i + 1]:
                if not peaks or (i - peaks[-1]) >= min_distance:
                    peaks.append(i)

    return peaks


def _estimate_tempo(onset_env: "np.ndarray", sr: int, hop_size: int) -> tuple[float, float]:
    """Estimate tempo using autocorrelation of onset envelope with octave correction."""
    # Autocorrelation
    n = len(onset_env)
    if n < 100:
        return 0.0, 0.0

    autocorr = np.correlate(onset_env, onset_env, mode='full')
    autocorr = autocorr[n:]  # Take positive lags only

    # Convert lag range to BPM range (40-200 BPM)
    frames_per_sec = sr / hop_size
    min_lag = max(1, int(frames_per_sec * 60 / 200))  # 200 BPM
    max_lag = min(len(autocorr) - 1, int(frames_per_sec * 60 / 40))  # 40 BPM

    if min_lag >= max_lag:
        return 0.0, 0.0

    # Find peak in autocorrelation within BPM range
    search = autocorr[min_lag:max_lag + 1]
    if len(search) == 0:
        return 0.0, 0.0

    peak_idx = np.argmax(search) + min_lag
    peak_val = autocorr[peak_idx]

    # Convert lag to BPM
    bpm = 60.0 * frames_per_sec / peak_idx

    # ---- Octave correction ----
    # Check if double the period (half BPM) also has a strong peak.
    # Prefer the lower BPM (longer period) if its peak is at least 80%
    # as strong, since humans tend to feel the slower pulse.
    double_lag = peak_idx * 2
    if double_lag < len(autocorr):
        # Check a small window around 2x lag for the real peak
        search_start = max(0, double_lag - 3)
        search_end = min(len(autocorr), double_lag + 4)
        local_peak = np.max(autocorr[search_start:search_end])
        if local_peak > peak_val * 0.8:
            half_bpm = bpm / 2
            if 40 <= half_bpm <= 200:
                bpm = half_bpm
                peak_val = local_peak

    # Also check half period (double BPM) — sometimes the real beat
    # is faster and we found a subharmonic
    half_lag = peak_idx // 2
    if half_lag >= min_lag:
        search_start = max(0, half_lag - 2)
        search_end = min(len(autocorr), half_lag + 3)
        local_peak = np.max(autocorr[search_start:search_end])
        if local_peak > peak_val * 0.85:  # Even slightly weaker peaks are likely the true beat
            double_bpm = bpm * 2
            if 40 <= double_bpm <= 200:
                bpm = double_bpm

    # Confidence based on peak prominence
    confidence = float(peak_val / (autocorr[0] + 1e-10))
    confidence = min(1.0, max(0.0, confidence))

    return bpm, confidence


def _estimate_tempo_from_ioi(onsets_ms: list[int]) -> float:
    """
    Estimate tempo from inter-onset intervals (IOI) as a sanity check.
    Uses the median IOI to estimate BPM, which is more robust against
    the octave errors that autocorrelation is prone to.
    """
    if len(onsets_ms) < 4:
        return 0.0

    # Compute inter-onset intervals
    intervals = []
    for i in range(1, len(onsets_ms)):
        ioi = onsets_ms[i] - onsets_ms[i - 1]
        if 150 < ioi < 2000:  # Only consider reasonable intervals (30-400 BPM range)
            intervals.append(ioi)

    if len(intervals) < 3:
        return 0.0

    # Use median interval (robust to outliers)
    median_ioi = float(np.median(intervals))
    if median_ioi <= 0:
        return 0.0

    bpm = 60000.0 / median_ioi

    # Prefer BPM in comfortable range (60-160)
    # Halve or double if out of range
    while bpm > 200 and bpm / 2 >= 40:
        bpm /= 2
    while bpm < 50 and bpm * 2 <= 200:
        bpm *= 2

    return bpm


def _build_beat_grid(bpm: float, onsets_ms: list[int],
                     duration_ms: int) -> list[int]:
    """Build a regular beat grid aligned to detected onsets."""
    beat_interval = 60000.0 / bpm
    if beat_interval <= 0:
        return onsets_ms

    # Phase search: try many candidate offsets and find the best alignment
    # with detected onsets.  We try:
    #  - every onset (mod beat_interval) as a candidate
    #  - plus 50 evenly spaced candidates for robustness
    best_phase = 0.0
    best_score = -1
    tolerance = beat_interval * 0.20  # 20% of beat interval

    # Build candidate phases
    candidates = set()
    for onset in onsets_ms[:min(40, len(onsets_ms))]:
        candidates.add(onset % beat_interval)
    # Also add evenly spaced candidates
    for i in range(50):
        candidates.add(i / 50.0 * beat_interval)

    for phase in candidates:
        score = 0
        beat_t = phase
        while beat_t < duration_ms:
            # Count onsets near this beat position
            for onset in onsets_ms:
                if abs(onset - beat_t) <= tolerance:
                    score += 1
                    break
            beat_t += beat_interval

        if score > best_score:
            best_score = score
            best_phase = phase

    # Generate beat grid
    beats = []
    t = best_phase
    while t < duration_ms:
        beats.append(int(t))
        t += beat_interval

    # If first beat is very close to 0, start from 0
    if beats and beats[0] < beat_interval * 0.1:
        beats[0] = 0

    return beats


def snap_actions_to_beats(actions, beat_data: BeatData,
                          tolerance_ms: int = 100, use_grid: bool = False):
    """
    Snap a list of FunscriptAction timestamps to nearest beats.
    Returns a new list with adjusted timestamps (non-destructive).
    """
    from funscript_io import FunscriptAction

    if not beat_data.beats:
        return list(actions)

    result = []
    snap_fn = beat_data.snap_to_grid if use_grid else beat_data.snap_to_beat

    for action in actions:
        snapped_at = snap_fn(action.at, tolerance_ms)
        result.append(FunscriptAction(at=snapped_at, pos=action.pos))

    return result
