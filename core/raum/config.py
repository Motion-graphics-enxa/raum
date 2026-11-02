"""``raum.yaml`` model registry and hardware profiles (plan §6.7).

Resolution order (later wins):
  1. ``profiles/<profile>.yaml``      full engine set for a hardware class
  2. ``raum.yaml``                    the box's own choices (``profile:`` + overrides)
  3. env ``RAUM_PROFILE``             switch profile without editing files
  4. env ``RAUM_CONFIG``              path to an alternative raum.yaml
  5. env ``RAUM_DATA_DIR``            sessions + audit log directory (install.sh writes it into the
                                      box's env file: /var/lib/raum, or ~/Desktop/HackathonRaum/data)

Switching the box from the mock pipeline to the real models is one line in raum.yaml:
``profile: mock`` -> ``profile: spark``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


def repo_root() -> Path:
    env = os.environ.get("RAUM_HOME")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "raum.yaml").is_file() and (parent / "modes").is_dir():
            return parent
    return here.parents[2]


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class EngineConfig(_Open):
    """One model slot. ``engine`` selects the implementation; other keys go to it."""

    engine: str
    url: str | None = None
    model: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("engine", mode="before")
    @classmethod
    def _yaml_off(cls, v: Any) -> Any:
        # YAML 1.1 reads a bare `off` / `none` as a boolean / null
        if v is False or v is None:
            return "off"
        return v

    @property
    def enabled(self) -> bool:
        return self.engine not in ("off", "none")


class ModelsConfig(_Open):
    vad: EngineConfig = EngineConfig(engine="silero")
    denoise: EngineConfig = EngineConfig(engine="passthrough")
    asr: EngineConfig = EngineConfig(engine="mock")
    translate: EngineConfig = EngineConfig(engine="mock")
    verify: EngineConfig = EngineConfig(engine="mock")
    embed: EngineConfig = EngineConfig(engine="mock")
    vision: EngineConfig = EngineConfig(engine="off")
    tts: EngineConfig = EngineConfig(engine="mock")
    summary: EngineConfig | None = None  # defaults to the verify engine/model


class SchedulerConfig(_Open):
    live_slots: int = 2
    check_slots: int = 1
    background_slots: int = 1


class SecurityConfig(_Open):
    session_cipher: Literal["aes-256-gcm"] = "aes-256-gcm"
    data_dir: str = "data"  # relative to repo root unless absolute; /var/lib/raum on the box
    audit_log: str | None = None  # default: <data_dir>/audit.jsonl


class NetworkConfig(_Open):
    livekit_url: str = "auto"  # "auto" = same origin as the app (Caddy / Vite proxy /rtc)
    livekit_internal_url: str = "ws://127.0.0.1:7880"  # how agent and core reach LiveKit
    livekit_api_key: str = "devkey"
    livekit_api_secret: str = "secret"
    core_url: str = "http://127.0.0.1:8000"  # how the agent reaches raum-core
    internal_key: str = "dev-internal"  # shared secret agent <-> core
    tunnel: str | None = "tailscale"
    # C-12: LiveKit room settings carried in every token RAUM mints (they apply when that join
    # creates the room). An agent does not keep a room open, so departure_timeout is how long the
    # agent (and its conversation memory) survives an empty room: phones that all drop at once.
    departure_timeout_s: int = 120
    empty_timeout_s: int = 300
    # Explicit dispatch: tokens name this agent (worker run with --agent-name / RAUM_AGENT_NAME).
    # Empty = auto-dispatch to every RAUM room. Env RAUM_AGENT_NAME wins.
    agent_name: str = ""

    def dispatch_name(self) -> str:
        import os

        return os.environ.get("RAUM_AGENT_NAME", self.agent_name or "")


class VerifyThresholds(_Open):
    green: float = 0.86
    amber: float = 0.78


class RaumConfig(_Open):
    profile: str = "mock"
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    thresholds: VerifyThresholds = Field(default_factory=VerifyThresholds)
    asr_min_conf: float = 0.55  # below: "please say that again" (V-03)

    def data_dir(self) -> Path:
        p = Path(self.security.data_dir).expanduser()
        return p if p.is_absolute() else repo_root() / p

    def audit_path(self) -> Path:
        if self.security.audit_log:
            p = Path(self.security.audit_log).expanduser()
            return p if p.is_absolute() else repo_root() / p
        return self.data_dir() / "audit.jsonl"

    def engine_names(self) -> dict[str, str]:
        """stage -> human-readable model name for the glass box."""
        out: dict[str, str] = {}
        for stage in ("vad", "denoise", "asr", "translate", "verify", "embed", "tts"):
            ec: EngineConfig = getattr(self.models, stage)
            out[stage] = f"{ec.engine}:{ec.model}" if ec.model else ec.engine
        return out


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping")  # noqa: TRY004  (callers catch ValueError)
    return data


def load_config(path: str | Path | None = None) -> RaumConfig:
    root = repo_root()
    cfg_path = Path(path or os.environ.get("RAUM_CONFIG") or root / "raum.yaml")
    box = _read_yaml(cfg_path)
    profile = os.environ.get("RAUM_PROFILE") or box.get("profile") or "mock"
    prof_path = root / "profiles" / f"{profile}.yaml"
    if not prof_path.is_file():
        raise ValueError(f"unknown profile '{profile}': {prof_path} not found")
    merged = _deep_merge(_read_yaml(prof_path), box)
    merged["profile"] = profile
    if os.environ.get("RAUM_PROFILE") and "models" in box:
        # an env profile switch means "use that profile's engines", not the box overrides
        merged["models"] = _deep_merge(_read_yaml(prof_path).get("models", {}), {})
    if os.environ.get("RAUM_DATA_DIR"):
        merged["security"] = {**(merged.get("security") or {}), "data_dir": os.environ["RAUM_DATA_DIR"]}
    return RaumConfig.model_validate(merged)


@lru_cache(maxsize=1)
def get_config() -> RaumConfig:
    return load_config()
