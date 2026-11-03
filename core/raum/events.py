"""RAUM event protocol (plan §6.5). THE contract between agent, core and app.

Transport: LiveKit data channel, topic ``raum.events``, reliable, JSON.
Mirrored to ``/api/events/{room}`` (WebSocket) for the table screen and the event API.

Every message is an :class:`Envelope`::

    { "v": 1, "type": "translation.delta", "room": "sz-4821", "utt": "u-0007",
      "ts": 1793616884123, "data": { ... } }

The TypeScript twin lives in ``app/src/lib/events.ts``. Keep both in sync: a change here
needs the same change there (``tests/test_events.py`` checks the type list matches).

Fields beyond the plan's table are optional extensions, so a client written against the
plan's table keeps working.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1
EVENTS_TOPIC = "raum.events"
# device -> agent, LiveKit data channel, reliable, JSON: push-to-talk {"type": "ptt.press" | "ptt.release", "at": ms,
# "tail_ms": n} (raum.agent.worker: a release ends that device's turn at once, RoomPipeline.end_turn)
CONTROL_TOPIC = "raum.control"

Role = Literal["a", "b", "observer"]
Path = Literal["direct", "tunnel", "relayed"]
Badge = Literal["green", "amber", "red"]
FlagKind = Literal["dose", "allergy", "deadline", "amount", "uncertain", "date", "number", "mismatch", "noted"]
# A checked fact (raum.pipeline.readback): noted = guard and checker green, nobody was asked; unconfirmed = read
# back to the listener (uncertain), no answer yet; confirmed / rejected = the listener said yes / no.
FactStatus = Literal["noted", "unconfirmed", "confirmed", "rejected"]


class _Data(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------- participants


class ParticipantInfo(_Data):
    id: str  # LiveKit identity
    role: Role
    lang: str  # BCP-47 primary subtag, e.g. "de", "ar", "tr"
    device: str = "phone"  # phone | laptop | tablet | speakerphone | table
    path: Path = "direct"
    rtt_ms: int | None = None  # connection latency for the glass-box badge (G-03)
    # extension (C-14): how this device plays the voice. "headphones" = nobody else hears it (no echo,
    # its mic is never paused); "speaker" = the device's own mic pauses while its voice plays;
    # "room" = a shared speaker the whole room hears (all mics pause). Default: speaker.
    audio_out: Literal["headphones", "speaker", "room"] = "speaker"
    # extension (C-13): whose voice speaks this device's words when the voice engine clones voices.
    # "own" = an AI copy of the speaker's voice (a voice sample kept in memory only); "standard" = the
    # engine's built-in voice, no sample is ever taken. Default: own.
    voice: Literal["own", "standard"] = "own"


class ParticipantJoined(ParticipantInfo):
    pass


class ParticipantLeft(_Data):
    id: str


# ---------------------------------------------------------------- utterances


class UtteranceStart(_Data):
    speaker: str
    lang: str


class UtteranceEnd(_Data):
    speaker: str
    lang: str


class UtteranceContinued(_Data):  # extension (raum.pipeline.merge): a sentence cut at a pause, merged again
    """Sent for the cut utterance (``utt``) when its speaker went on right after its end of turn: its voice was
    stopped, nothing more of it comes, and it is no statement of its own (the summary skips it)."""

    speaker: str
    into: str  # the utterance that carries the whole sentence now (this one's audio + the new speech)


class TranscriptPartial(_Data):
    speaker: str
    lang: str
    text: str
    seq: int


class TranscriptFinal(_Data):
    speaker: str
    lang: str
    text: str
    asr_conf: float  # 0..1


class TranslationDelta(_Data):
    target_lang: str
    delta: str
    seq: int | None = None
    seg: int | None = None  # extension (simultaneous mode): segment index within the utterance, 0-based


class TranslationFinal(_Data):
    target_lang: str
    text: str  # with `seg`: this segment's text only; the utterance's translation = segments in order
    seg: int | None = None
    segs: int | None = None  # extension: total segments, sent on the last segment's final
    # extension: the utterance was already in target_lang (the speaker switched language): its own text, voiced
    # as said, no MT (room.py _pass_through)
    passthrough: bool | None = None


class TranslationDraft(_Data):  # extension (raum.pipeline.draft): live draft while the speaker talks, never voiced
    target_lang: str
    text: str  # the whole draft: it replaces the previous one of this utterance and language in place
    seq: int  # grows per (utt, target_lang); an older seq never replaces a newer draft
    # simultaneous mode: the draft is the uncommitted tail, i.e. the segment of this index to come (it follows the
    # committed segments 0 .. seg-1); a segment >= seg arriving covers it. None: the whole utterance so far
    seg: int | None = None


# ---------------------------------------------------------------- verification


class FactCheck(_Data):
    kind: str  # dose | frequency | allergy | date | time | number | unit | medication | name | negation | amount | currency | percent | deadline
    src: str | None  # value as found in the source (None = missing in source)
    tgt: str | None  # value as found in the translation (None = missing in translation)
    match: bool


class VerifyFacts(_Data):
    target_lang: str
    seg: int | None = None  # extension: facts of one segment (app merges segments)
    facts: list[FactCheck] = Field(default_factory=list)
    red: bool = False  # extension: any mismatch
    ms: float | None = None  # extension: guard runtime


class VerifyResult(_Data):
    target_lang: str
    seg: int | None = None  # extension
    similarity: float | None
    badge: Badge
    reasons: list[str] = Field(default_factory=list)
    back_translation: str | None = None  # extension: shown in the glass box


class FlagFact(_Data):  # extension: one fact (a dose with its drug and frequency is one) of a noted / asked flag
    kind: str  # the main fact's kind: dose | allergy | deadline | amount | date | ...
    texts: dict[str, str]  # the chip per language, e.g. {"de": "Metformin 500 mg, zweimal täglich"}
    src: str | None = None  # the value as said
    tgt: str | None = None  # the value as translated
    match: bool = True


class Flag(_Data):
    kind: FlagKind
    # human-readable, in the language of the device that must act: "Nur zur Sicherheit: 500 mg, zweimal täglich?"
    # (kind "noted": the professional's chip line, "Allergie: Penicillin · Metformin 500 mg, zweimal täglich")
    text: str
    needs_confirm: bool  # a yes/no is asked (small buttons, never modal; a spoken yes/no answers too)
    id: str | None = None  # extension: referenced by `confirm`
    confirm_by: Role | None = None  # extension: which role must confirm (default: the listener)
    target_lang: str | None = None  # extension
    texts: dict[str, str] | None = None  # extension: text per language
    # extensions (raum.pipeline.readback): the checked facts, and how they were handled
    status: Literal["noted", "unconfirmed"] | None = None  # noted: nobody asked · unconfirmed: asked, no answer yet
    facts: list[FlagFact] | None = None  # the facts, one chip each
    show_to: Role | None = None  # a flag nobody answers shows only on this role's screen (noted: "a")
    spoken: bool | None = None  # the question was read back aloud in target_lang
    why: list[str] | None = None  # what made it uncertain: "guard", "check:amber", "check:red", "asr" ("debug": forced)
    seg: int | None = None  # simultaneous mode: the segment the guard flagged


class Confirm(_Data):
    by: str  # role or identity
    accepted: bool
    flag_id: str | None = None  # extension
    via: Literal["button", "voice"] | None = None  # extension: a tap, or a spoken yes/no (raum.pipeline.readback)
    answer_utt: str | None = None  # extension (via voice): the utterance that carried the answer


class RepeatBackResult(_Data):  # extension (S-02)
    understood: Literal["yes", "partly", "no"]
    missing: list[str] = Field(default_factory=list)
    wrong: list[str] = Field(default_factory=list)
    instruction_utt: str | None = None


class OfferUpdate(_Data):
    amount: float
    currency: str
    by: str  # role
    ts: int
    text: str | None = None  # extension: the phrase it came from


class ExplainDraft(_Data):  # extension (K-02): only shown to the professional until approved
    source_utt: str
    target_lang: str
    text: str


class ExplainSent(_Data):  # extension (K-02)
    source_utt: str
    target_lang: str
    text: str


class DocumentFact(_Data):  # extension (F-07)
    kind: str  # date | amount | dose | deadline (also time, duration, percent, number)
    value: str  # in the reader's language
    source: str | None = None  # extension: the figure exactly as printed on the paper
    uncertain: bool = False  # extension: the model was unsure, or the fact guard found other figures than printed


class DocumentReady(_Data):  # extension (F-07): a photographed document read by the vision model
    by: str  # role that photographed it
    lang: str  # the reader's language: title, text and fact values are in it
    title: str
    text: str  # plain-language account; parts the model could not read are marked "(?)"
    facts: list[DocumentFact] = Field(default_factory=list)
    ms: float | None = None  # reading time
    source_lang: str | None = None  # extension: language the document is written in
    model: str | None = None  # extension: e.g. "ollama:gemma4:26b"


# ---------------------------------------------------------------- glass box, session


class PipelineTiming(_Data):
    vad_ms: float | None = None
    asr_ms: float | None = None
    mt_first_ms: float | None = None
    mt_full_ms: float | None = None
    tts_first_ms: float | None = None
    check_ms: float | None = None
    target_lang: str | None = None  # extension
    denoise_ms: float | None = None  # extension
    models: dict[str, str] | None = None  # extension: stage -> model name
    # latency breakdown (extensions; ms from the moment the speaker stopped unless noted)
    tts_synth_first_ms: float | None = None  # first PCM from the voice engine
    tts_publish_first_ms: float | None = None  # first frame accepted by the tts-<lang> AudioSource
    asr_from: Literal["partial", "early", "final"] | None = None  # which transcript became final
    asr_early: bool | None = None  # the final ASR ran during the end-of-turn wait
    eot: dict[str, Any] | None = None  # semantic end of turn: verdict, probabilities, wait (turn.py)
    # voice echo check (raum.pipeline.echo_check), when a clause with figures was heard back: the slowest
    # clause from synthesized to heard and compared, and the worst result of the utterance's clauses
    echo_ms: float | None = None
    # speculate.py: where each played clause's audio came from: made ahead of the end of turn (spec), while the
    # clause before played (ahead), or when its turn came (live)
    voice_clauses: dict[str, int] | None = None
    echo_check: Literal["match", "unclear", "mismatch", "skipped"] | None = None
    # pipeline/stats.py: per stage (asr, spec.mt, slot.live, check.*, voice.spec, echo.asr, ...) numbers only:
    # wait_ms (queued), held_ms / wall_ms / *_ms (worked), *_ms marks in ms after the speaker's stop, n (calls)
    stages: dict[str, dict[str, float]] | None = None
    # also sent as extras in simultaneous mode only: simultaneous (true), segs (number of segments)


class VoiceState(_Data):  # extension (C-14): the box's voice is playing in a language
    target_lang: str
    playing: bool
    paused: list[str] = Field(default_factory=list)  # identities whose mic is paused meanwhile


class VoiceEcho(_Data):  # extension (raum.pipeline.echo_check): the voice said another figure than the text
    # The box heard its own voice back (ASR of the synthesized clause) and the fact guard found a figure of the
    # clause said with another value. The listener's card notes "spoken: 50 mg · written: 500 mg"; the clause
    # is said once more in the standard voice ("I repeat: ..."); core writes the audit line
    # ``voice.echo_mismatch`` (the kind only). A figure the box did not hear clearly sends nothing.
    target_lang: str
    kind: str  # dose | number | date | time
    spoken: str  # the figure as the voice was heard to say it
    written: str  # the figure as written in the translation
    clause: int | None = None  # the clause of this utterance's voice in that language, 0-based
    text: str | None = None  # that clause as written (the words said again)


VoiceLockStatus = Literal["enrolling", "match", "mismatch", "short", "unchecked"]


class VoiceLockResult(_Data):  # extension (F-10, raum.pipeline.voicelock): one per utterance while the lock is on
    speaker: str  # the device (identity) whose mic heard the utterance
    # False: not the device owner's voice. The utterance is dropped: no transcript.final, not translated, not
    # spoken, not stored, not summarized. The speaker's own phone shows a quiet "not your voice: ignored".
    accepted: bool
    # enrolling: still learning the owner's voice (passes) · match / mismatch: compared with the owner's voice ·
    # short: too short to verify (passes) · unchecked: the check failed or was late (passes)
    status: VoiceLockStatus
    score: float | None = None  # cosine similarity with the owner's voice (glass box); never the voice itself
    threshold: float | None = None
    enrolled_s: float | None = None  # seconds of the owner's speech learned so far
    enroll_s: float | None = None  # seconds needed before the lock closes
    ms: float | None = None  # embedding time (runs beside the transcription)
    waited_ms: float | None = None  # how long the turn waited for the verdict after transcription (0 when on time)


class SessionState(_Data):
    off_record: bool
    sealed: bool
    participants: list[ParticipantInfo]
    mode: str
    status: Literal["live", "ended", "burned"] = "live"  # extension
    session_id: str | None = None  # extension
    human_interpreter_requested: bool = False  # extension (S-05)
    started_at: int | None = None  # extension


class PrivacyMeter(_Data):
    third_party_bytes: int
    tunnel_bytes: int
    sealed: bool
    source: str | None = None  # extension: "nft" | "stub"


class GpuStats(_Data):
    util_pct: float
    models_loaded: list[str]
    mem_used_gb: float | None = None  # extension
    source: str | None = None  # extension: "nvidia-smi" | "mock"


class SummaryItem(_Data):
    """One bilingual line. Keys are language codes: {"de": "...", "ar": "..."}."""

    text: dict[str, str]
    uncertain: bool = False


class SummarySection(_Data):
    key: str  # e.g. "medications"
    title: dict[str, str]  # per language
    items: list[SummaryItem] = Field(default_factory=list)


class SummaryFact(_Data):  # extension: a checked fact of the conversation and what became of it
    kind: str
    text: dict[str, str]  # per language
    status: FactStatus
    utt: str | None = None


class SummaryReady(_Data):
    template: str  # mode id
    lang_a: str
    lang_b: str
    sections: list[SummarySection]
    file_note: dict[str, str] | None = None  # extension: S-04 / K-03 note, usually {"de": "..."}
    facts: list[SummaryFact] = Field(default_factory=list)  # extension: noted / unconfirmed / confirmed / rejected
    approved_by: list[str] = Field(default_factory=list)  # extension
    generated_ms: float | None = None  # extension


class BurnDone(_Data):
    key_destroyed_at: int  # epoch ms
    audit_head_hash: str


# ---------------------------------------------------------------- registry

EVENT_TYPES: dict[str, type[BaseModel]] = {
    "participant.joined": ParticipantJoined,
    "participant.left": ParticipantLeft,
    "utterance.start": UtteranceStart,
    "utterance.end": UtteranceEnd,
    "utterance.continued": UtteranceContinued,
    "transcript.partial": TranscriptPartial,
    "transcript.final": TranscriptFinal,
    "translation.delta": TranslationDelta,
    "translation.final": TranslationFinal,
    "translation.draft": TranslationDraft,
    "verify.facts": VerifyFacts,
    "verify.result": VerifyResult,
    "flag": Flag,
    "confirm": Confirm,
    "repeat_back.result": RepeatBackResult,
    "offer.update": OfferUpdate,
    "explain.draft": ExplainDraft,
    "explain.sent": ExplainSent,
    "document.ready": DocumentReady,
    "pipeline.timing": PipelineTiming,
    "voice.state": VoiceState,
    "voice.echo": VoiceEcho,
    "voice.lock": VoiceLockResult,
    "session.state": SessionState,
    "privacy.meter": PrivacyMeter,
    "gpu.stats": GpuStats,
    "summary.ready": SummaryReady,
    "burn.done": BurnDone,
}

EventType = Literal[
    "participant.joined",
    "participant.left",
    "utterance.start",
    "utterance.end",
    "utterance.continued",
    "transcript.partial",
    "transcript.final",
    "translation.delta",
    "translation.final",
    "translation.draft",
    "verify.facts",
    "verify.result",
    "flag",
    "confirm",
    "repeat_back.result",
    "offer.update",
    "explain.draft",
    "explain.sent",
    "document.ready",
    "pipeline.timing",
    "voice.state",
    "voice.echo",
    "voice.lock",
    "session.state",
    "privacy.meter",
    "gpu.stats",
    "summary.ready",
    "burn.done",
]

# Events that carry conversation content. Only these are written (encrypted) to the session store.
CONTENT_EVENTS = frozenset(
    {
        "utterance.continued",  # the summary must know which utterances were merged into a later one
        "transcript.final",
        "translation.final",
        "verify.facts",
        "verify.result",
        "flag",
        "confirm",
        "repeat_back.result",
        "offer.update",
        "explain.sent",
        "document.ready",
        "summary.ready",
        "voice.echo",
    }
)


def now_ms() -> int:
    return int(time.time() * 1000)


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    v: Literal[1] = PROTOCOL_VERSION
    type: EventType
    room: str
    utt: str | None = None
    ts: int = Field(default_factory=now_ms)
    data: dict[str, Any]

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=False)

    def payload(self) -> BaseModel:
        """Parse ``data`` into its typed model."""
        return EVENT_TYPES[self.type].model_validate(self.data)


def make_event(type: str, room: str, data: BaseModel | dict[str, Any], utt: str | None = None,
               ts: int | None = None) -> Envelope:
    """Build and validate an envelope. ``data`` may be a model or a dict (validated against the type)."""
    if type not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {type}")
    model = data if isinstance(data, BaseModel) else EVENT_TYPES[type].model_validate(data)
    if not isinstance(model, EVENT_TYPES[type]):
        model = EVENT_TYPES[type].model_validate(model.model_dump())
    return Envelope(type=type, room=room, utt=utt, ts=ts or now_ms(), data=model.model_dump(mode="json"))


def parse_event(raw: str | bytes | dict[str, Any]) -> Envelope:
    """Parse and validate an incoming envelope, including its typed data."""
    env = Envelope.model_validate_json(raw) if isinstance(raw, (str, bytes)) else Envelope.model_validate(raw)
    env.payload()
    return env


# ---------------------------------------------------------------- control (device -> box)

ControlAction = Literal[
    "off_record",  # value: bool
    "confirm",  # flag_id, accepted
    "repeat_back",  # start a repeat-back check (S-02): the next utterance of role b is compared
    "human_interpreter",  # S-05
    "explain_simply",  # utt: the professional's utterance to rewrite (K-02)
    "explain_approve",  # utt + text: send the approved draft (K-02)
    "correct_term",  # V-05: value: {"src": "...", "tgt": "...", "lang": "ar"}
    "glossary",  # N-02 (core -> agent): value: {"terms": [<glossaries/*.yaml term>, ...]} merged for the session
    "end",  # end conversation -> summary
    "approve_summary",  # by role
    "burn",
    "mute",  # value: bool. device-local mic pause, mirrored so the glass box knows
    "voice_lock_reset",  # F-10: forget the owner's voice and learn it again (by: identity, role, or none = all)
]


class Control(BaseModel):
    """Body of ``POST /api/rooms/{room}/control`` and of core -> agent control messages."""

    model_config = ConfigDict(extra="allow")

    action: ControlAction
    by: str | None = None  # role or identity of the sender
    value: Any = None
    flag_id: str | None = None
    accepted: bool | None = None
    utt: str | None = None
    text: str | None = None
