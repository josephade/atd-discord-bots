import json
import os
from datetime import datetime, timezone

from config import STATE_DIR

_ROTATIONS_FILE = os.path.join(STATE_DIR, "rotations.json")


def _load_all() -> dict:
    try:
        with open(_ROTATIONS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_all(data: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(_ROTATIONS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_rotation(team_name: str) -> dict | None:
    return _load_all().get(team_name)


def set_rotation(team_name: str, nicknames: dict, segments: list, set_by: int):
    data = _load_all()
    data[team_name] = {
        "nicknames": nicknames,
        "segments": segments,
        "set_by": set_by,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_all(data)


def clear_rotation(team_name: str) -> bool:
    data = _load_all()
    if team_name not in data:
        return False
    del data[team_name]
    _save_all(data)
    return True


def count_rotations() -> int:
    return len(_load_all())


def clear_all_rotations() -> int:
    """Wipe every saved rotation (e.g. for a fresh ATD). Returns how many were removed."""
    count = count_rotations()
    _save_all({})
    return count
