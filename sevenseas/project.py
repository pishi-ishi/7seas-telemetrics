"""Save/load project files (.7seas.json): file paths, sync, trim, layout."""

import json

from . import gauges


def save_project(path, video_path, data_path, mapping, speed_units,
                 user_offset, overlay_fps, gauge_list,
                 trim=None, maneuvers=None):
    doc = {
        "app": "7seas-telemetrics",
        "version": 2,
        "video": video_path or "",
        "data": data_path or "",
        "mapping": mapping or None,
        "speed_units": speed_units or {},
        "user_offset": float(user_offset),
        "overlay_fps": int(overlay_fps),
        "trim": list(trim) if trim else [None, None],
        "maneuvers": maneuvers or [],
        "gauges": [g.to_dict() for g in gauge_list],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)


def load_project(path):
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("app") != "7seas-telemetrics":
        raise ValueError("Not a 7seas-telemetrics project file")
    doc.setdefault("gauges", [])
    doc.setdefault("trim", [None, None])
    doc.setdefault("maneuvers", [])
    doc["gauge_objects"] = [g for g in
                            (gauges.Gauge.from_dict(d) for d in doc["gauges"])
                            if g is not None]
    return doc
