"""Room (mode) schema, plan §6.6. A room is one YAML file in ``modes/``."""

from __future__ import annotations

import warnings
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GuardKind = Literal[
    "dose", "frequency", "allergy", "date", "time", "duration", "number", "unit", "medication",
    "name", "negation", "deadline", "amount", "currency", "percent",
]
Button = Literal[
    "off_record", "repeat_back", "human_interpreter", "end", "explain_simply", "offer_tracker",
    "document_camera", "push_to_talk",
]
SummarySectionKey = str  # free-form; templates in prompts/summary-*.md define them


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoleConfig(_Strict):
    label: dict[str, str]  # per language
    default_lang: str  # language code or "auto" (= navigator.language)


class GuardConfig(_Strict):
    kinds: list[GuardKind]
    confirm_on: list[GuardKind] = Field(default_factory=list)

    @model_validator(mode="after")
    def _confirm_subset(self) -> GuardConfig:
        extra = set(self.confirm_on) - set(self.kinds)
        if extra:
            raise ValueError(f"guard.confirm_on has kinds not in guard.kinds: {sorted(extra)}")
        return self


class PromptsConfig(_Strict):
    translate: str
    summary: str
    repeat_back: str | None = None
    explain_simply: str | None = None
    backtranslate: str = "prompts/backtranslate.md"


class SummaryConfig(_Strict):
    sections: list[SummarySectionKey]
    takehome_for: Literal["a", "b"] | None = None
    file_note_for: Literal["a", "b"] | None = None


class RetentionConfig(_Strict):
    auto_burn_minutes: int | None = 30


class PipelineModeConfig(_Strict):
    """Per-room defaults for the live pipeline (raum.pipeline.simul, raum.pipeline.merge). ``raum.yaml``
    ``pipeline.simultaneous: off`` still switches simultaneous mode off everywhere."""

    simultaneous: Literal["auto", "off"] = "auto"
    simultaneous_after_ms: float = 0.0  # simultaneous only once the turn has this much speech (long turns)
    # listeners who play the voice aloud (audio_out speaker / room), raum.pipeline.simul. "consecutive": only
    # headphone listeners go simultaneous. "held": their segments show as text while the speaker talks and their
    # voice is made ahead, played at the end of the turn. "live": voiced while the speaker talks too, unless the
    # speaker's own mic would hear it (echo.paused_for: a room speakerphone), then held. Both: a sentence the
    # final transcript rewrote is never voiced in its stale version (simul.py, the final-revision guard).
    loudspeaker: Literal["consecutive", "held", "live"] = "consecutive"
    # live draft text (raum.pipeline.draft): the stable part of the partials translated while the speaker talks,
    # shown as a draft, never voiced. false: no draft MT at all in this room (raum.yaml pipeline.draft_text too)
    draft_text: bool = True
    # simultaneous commits (simul.py): a clause on ONE partial when whisper's word timings show its boundary word
    # ended >= 250 ms before the audio end (numbers, doses, negation, dates still need two partials that agree)
    commit_once: bool = True
    # en / ru: an SVO phrase before its comma ("I take my medicine | with water"), nothing pending (number, "not")
    svo_phrases: bool = True
    # a sentence cut at a pause: the same speaker again within this many ms of an end of turn whose voice still
    # plays (or has none) continues it; its voice stops and both parts are transcribed, translated and spoken
    # as one (raum.pipeline.merge). 0 = off (everyday rooms: two short sentences stay two)
    merge_continuation_ms: float = Field(default=0.0, ge=0.0)
    # this room's ASR confidence below which a fact is read back (None = raum.yaml asr_confirm_conf, 0.6)
    asr_confirm_conf: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("simultaneous", mode="before")
    @classmethod
    def _yaml_bool(cls, v: object) -> object:
        return "off" if v is False or v is None else ("auto" if v is True else v)


class AsrModeConfig(_Strict):
    """Speech recognition per room. ``glossary_prompt``: the room glossary's terms (session uploads included)
    go to the recogniser with every call as spelling hints (whisper's ``prompt``; ``Glossary.asr_prompt``),
    for rooms whose glossary holds words a recogniser mishears: drug names ("Medformin"), legal terms."""

    glossary_prompt: bool = False


warnings.filterwarnings("ignore", message='Field name "register"')


class ModeConfig(_Strict):
    id: str
    name: dict[str, str]
    icon: str
    prefix: str = Field(pattern=r"^[a-z]{2}$")  # room ids look like "<prefix>-<4 digits>", e.g. sz-4821
    tagline: dict[str, str] | None = None
    roles: dict[Literal["a", "b"], RoleConfig]
    register: Literal["formal", "informal"] = "formal"
    guard: GuardConfig
    glossary: str | None = None
    prompts: PromptsConfig
    summary: SummaryConfig
    buttons: list[Button]
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    pipeline: PipelineModeConfig = Field(default_factory=PipelineModeConfig)
    asr: AsrModeConfig = Field(default_factory=AsrModeConfig)
    # the second model's back-translation check (§6.9). "off": fact guard only, for everyday talk where
    # speed matters more than a second opinion (Gespraech); the app then shows no badge unless red.
    checker: Literal["on", "off"] = "on"
    order: int = 100  # position in room lists (the setup page): Sprechzimmer leads the pitch

    @field_validator("checker", mode="before")
    @classmethod
    def _checker_yaml_bool(cls, v: object) -> object:  # YAML 1.1 reads a bare on / off as a boolean
        return "on" if v is True else ("off" if v is False else v)

    @model_validator(mode="after")
    def _roles(self) -> ModeConfig:
        if set(self.roles) != {"a", "b"}:
            raise ValueError("roles must define exactly 'a' and 'b'")
        if "end" not in self.buttons:
            raise ValueError("buttons must include 'end' (every conversation ends on the summary)")
        return self

    def label(self, role: str, lang: str) -> str:
        labels = self.roles[role].label  # type: ignore[index]
        return labels.get(lang) or labels.get("en") or next(iter(labels.values()))

    def display_name(self, lang: str) -> str:
        return self.name.get(lang) or self.name.get("en") or self.id
