"""Load and validate room configs from ``modes/*.yaml``."""

from __future__ import annotations

import random
import re
from functools import lru_cache
from pathlib import Path

import yaml

from raum.config import repo_root
from raum.modes.schema import ModeConfig

ROOM_ID_RE = re.compile(r"^(?P<prefix>[a-z]{2})-(?P<num>[0-9a-z]{2,12})$")


class ModeError(ValueError):
    pass


def modes_dir() -> Path:
    return repo_root() / "modes"


def load_mode(path: str | Path, *, check_files: bool = True) -> ModeConfig:
    path = Path(path)
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ModeError(f"{path}: not a mapping")
    try:
        mode = ModeConfig.model_validate(raw)
    except Exception as e:  # pydantic.ValidationError
        raise ModeError(f"{path}: {e}") from e
    if mode.id != path.stem:
        raise ModeError(f"{path}: id '{mode.id}' must match the file name '{path.stem}'")
    if check_files:
        root = repo_root()
        refs = [mode.glossary, *mode.prompts.model_dump().values()]
        missing = [r for r in refs if r and not (root / r).is_file()]
        if missing:
            raise ModeError(f"{path}: referenced files not found: {missing}")
    return mode


@lru_cache(maxsize=1)
def load_modes(directory: str | None = None) -> dict[str, ModeConfig]:
    d = Path(directory) if directory else modes_dir()
    modes: dict[str, ModeConfig] = {}
    prefixes: dict[str, str] = {}
    for p in sorted(d.glob("*.yaml")):
        m = load_mode(p)
        if m.prefix in prefixes:
            raise ModeError(f"{p}: prefix '{m.prefix}' already used by {prefixes[m.prefix]}")
        prefixes[m.prefix] = m.id
        modes[m.id] = m
    if not modes:
        raise ModeError(f"no modes found in {d}")
    return dict(sorted(modes.items(), key=lambda kv: (kv[1].order, kv[0])))


def get_mode(mode_id: str) -> ModeConfig:
    try:
        return load_modes()[mode_id]
    except KeyError:
        raise ModeError(f"unknown mode: {mode_id}") from None


def mode_for_room(room_id: str) -> ModeConfig:
    """Room ids encode their mode: ``sz-4821`` -> sprechzimmer."""
    m = ROOM_ID_RE.match(room_id)
    if not m:
        raise ModeError(f"invalid room id: {room_id!r} (expected e.g. 'sz-4821')")
    for mode in load_modes().values():
        if mode.prefix == m.group("prefix"):
            return mode
    raise ModeError(f"no mode with prefix '{m.group('prefix')}' for room {room_id}")


def new_room_id(mode_id: str) -> str:
    return f"{get_mode(mode_id).prefix}-{random.randint(1000, 9999)}"
