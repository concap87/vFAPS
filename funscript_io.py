"""
Funscript I/O Module
====================
Handles reading, writing, and manipulating funscript format data.

Funscript is JSON with:
{
  "version": "1.0",
  "inverted": false,
  "range": 100,
  "actions": [
    {"at": <timestamp_ms>, "pos": <0-100>},
    ...
  ]
}

Multi-axis files use naming convention:
  video.funscript        -> L0 (stroke/up-down)
  video.surge.funscript  -> L1 (forward-back)
  video.sway.funscript   -> L2 (left-right)
  video.twist.funscript  -> R0 (twist)
  video.roll.funscript   -> R1 (roll)
  video.pitch.funscript  -> R2 (pitch)

Project files (.fsproj) v2.0 now support an extra_data section for
beat detection results, vision tracking, and other metadata.

Bundle export (.funscript_bundle) combines all axes + metadata into
a single JSON file for easy sharing.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Optional


# Axis definitions matching funscript multi-axis spec
AXIS_DEFINITIONS = {
    "stroke":  {"suffix": "",       "label": "Stroke (L0)",  "desc": "Up/Down movement",    "controller_axis": "y"},
    "surge":   {"suffix": ".surge", "label": "Surge (L1)",   "desc": "Forward/Back",         "controller_axis": "z"},
    "sway":    {"suffix": ".sway",  "label": "Sway (L2)",    "desc": "Left/Right",           "controller_axis": "x"},
    "twist":   {"suffix": ".twist", "label": "Twist (R0)",   "desc": "Twist rotation",       "controller_axis": "yaw"},
    "roll":    {"suffix": ".roll",  "label": "Roll (R1)",    "desc": "Roll rotation",        "controller_axis": "roll"},
    "pitch":   {"suffix": ".pitch", "label": "Pitch (R2)",   "desc": "Pitch rotation",       "controller_axis": "pitch"},
}

PROJECT_FORMAT = "funscript-motion-creator-project"
PROJECT_VERSION = "2.0"
BUNDLE_FORMAT = "funscript-motion-creator-bundle"
BUNDLE_VERSION = "1.0"


@dataclass
class FunscriptAction:
    """A single funscript action point."""
    at: int    # Timestamp in milliseconds
    pos: int   # Position 0-100

    def to_dict(self) -> dict:
        return {"at": self.at, "pos": self.pos}

    @classmethod
    def from_dict(cls, d: dict) -> "FunscriptAction":
        return cls(at=int(d["at"]), pos=int(d["pos"]))


@dataclass
class FunscriptAxis:
    """Data for a single funscript axis."""
    axis_name: str
    actions: list[FunscriptAction] = field(default_factory=list)
    inverted: bool = False
    range_val: int = 100

    def sort_actions(self):
        """Sort actions by timestamp."""
        self.actions.sort(key=lambda a: a.at)

    def remove_duplicates(self):
        """Remove actions at the same timestamp, keeping the last one."""
        self.sort_actions()
        seen = {}
        for action in self.actions:
            seen[action.at] = action
        self.actions = list(seen.values())
        self.sort_actions()

    def get_actions_in_range(self, start_ms: int, end_ms: int) -> list[FunscriptAction]:
        """Get actions within a time range."""
        return [a for a in self.actions if start_ms <= a.at <= end_ms]

    def remove_actions_in_range(self, start_ms: int, end_ms: int):
        """Remove all actions within a time range."""
        self.actions = [a for a in self.actions if not (start_ms <= a.at <= end_ms)]

    def add_actions(self, new_actions: list[FunscriptAction]):
        """Add actions, replacing any existing ones in the same time range."""
        if not new_actions:
            return
        start_ms = min(a.at for a in new_actions)
        end_ms = max(a.at for a in new_actions)
        self.remove_actions_in_range(start_ms, end_ms)
        self.actions.extend(new_actions)
        self.sort_actions()

    def to_dict(self) -> dict:
        """Export as funscript JSON dict."""
        return {
            "version": "1.0",
            "inverted": self.inverted,
            "range": self.range_val,
            "actions": [a.to_dict() for a in self.actions]
        }

    @classmethod
    def from_dict(cls, axis_name: str, data: dict) -> "FunscriptAxis":
        """Load from funscript JSON dict."""
        actions = [FunscriptAction.from_dict(a) for a in data.get("actions", [])]
        return cls(
            axis_name=axis_name,
            actions=actions,
            inverted=data.get("inverted", False),
            range_val=data.get("range", 100),
        )


@dataclass
class FunscriptProject:
    """Complete funscript project with multi-axis support."""
    video_path: Optional[str] = None
    axes: dict[str, FunscriptAxis] = field(default_factory=dict)

    def __post_init__(self):
        # Ensure at least the stroke axis exists
        if "stroke" not in self.axes:
            self.axes["stroke"] = FunscriptAxis(axis_name="stroke")

    def get_axis(self, axis_name: str) -> FunscriptAxis:
        """Get or create an axis."""
        if axis_name not in self.axes:
            self.axes[axis_name] = FunscriptAxis(axis_name=axis_name)
        return self.axes[axis_name]

    def export_funscript(self, output_dir: str, base_name: Optional[str] = None):
        """
        Export all axes as separate funscript files.
        Returns list of exported file paths.
        """
        if base_name is None:
            if self.video_path:
                base_name = os.path.splitext(os.path.basename(self.video_path))[0]
            else:
                base_name = "output"

        exported = []
        for axis_name, axis_data in self.axes.items():
            if not axis_data.actions:
                continue

            axis_def = AXIS_DEFINITIONS.get(axis_name, {"suffix": f".{axis_name}"})
            suffix = axis_def["suffix"]
            filename = f"{base_name}{suffix}.funscript"
            filepath = os.path.join(output_dir, filename)

            with open(filepath, 'w') as f:
                json.dump(axis_data.to_dict(), f, indent=2)

            exported.append(filepath)

        return exported

    def import_funscript(self, filepath: str, axis_name: str = "stroke"):
        """Import a funscript file into the given axis."""
        with open(filepath, 'r') as f:
            data = json.load(f)
        self.axes[axis_name] = FunscriptAxis.from_dict(axis_name, data)

    def save_project(self, filepath: str, extra_data: Optional[dict] = None):
        """
        Save complete project state as JSON.
        extra_data: optional dict of additional data to persist
                    (beat detection results, settings, etc.)
        """
        project_data = {
            "format": PROJECT_FORMAT,
            "version": PROJECT_VERSION,
            "video_path": self.video_path,
            "axes": {name: axis.to_dict() for name, axis in self.axes.items()}
        }
        if extra_data:
            project_data["extra_data"] = extra_data
        with open(filepath, 'w') as f:
            json.dump(project_data, f, indent=2)

    @classmethod
    def load_project(cls, filepath: str) -> tuple["FunscriptProject", dict]:
        """
        Load project from file.
        Returns (project, extra_data) tuple.
        extra_data is an empty dict if none was saved.
        """
        with open(filepath, 'r') as f:
            data = json.load(f)
        project = cls(video_path=data.get("video_path"))
        for axis_name, axis_data in data.get("axes", {}).items():
            project.axes[axis_name] = FunscriptAxis.from_dict(axis_name, axis_data)
        extra_data = data.get("extra_data", {})
        return project, extra_data

    def export_bundle(self, output_path: str, extra_data: Optional[dict] = None):
        """
        Export a single combined bundle file containing all axes + metadata.
        This is a non-standard format for easy project sharing.
        """
        bundle = {
            "format": BUNDLE_FORMAT,
            "version": BUNDLE_VERSION,
            "video_path": self.video_path,
            "axes": {}
        }
        for axis_name, axis_data in self.axes.items():
            if axis_data.actions:
                bundle["axes"][axis_name] = axis_data.to_dict()
        if extra_data:
            bundle["extra_data"] = extra_data

        with open(output_path, 'w') as f:
            json.dump(bundle, f, indent=2)

    @classmethod
    def import_bundle(cls, filepath: str) -> tuple["FunscriptProject", dict]:
        """
        Import a bundle file. Returns (project, extra_data).
        """
        with open(filepath, 'r') as f:
            data = json.load(f)
        project = cls(video_path=data.get("video_path"))
        for axis_name, axis_data in data.get("axes", {}).items():
            project.axes[axis_name] = FunscriptAxis.from_dict(axis_name, axis_data)
        extra_data = data.get("extra_data", {})
        return project, extra_data

    def get_total_actions(self) -> int:
        """Get total number of actions across all axes."""
        return sum(len(ax.actions) for ax in self.axes.values())

    def get_duration_ms(self) -> int:
        """Get the duration covered by actions."""
        max_time = 0
        for axis in self.axes.values():
            if axis.actions:
                max_time = max(max_time, max(a.at for a in axis.actions))
        return max_time
