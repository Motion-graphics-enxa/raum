"""Per-room turn pipeline (plan §6.4). Runs inside raum-agent; testable without LiveKit.

CONTRACT (implemented by the pipeline workstream; the worker in raum.agent calls exactly this):

    p = RoomPipeline(room, mode, engines, emit, config)
    p.set_participants([...ParticipantInfo])          # whenever LiveKit membership/metadata changes
    p.set_state(SessionState)                          # from core (off_record etc.)
    utt = await p.speech_start(speaker_id)             # VAD START_OF_SPEECH  -> utterance.start
    await p.speech_audio(speaker_id, frame_16k)        # while speaking; partial ASR every ~1 s
    await p.speech_end(speaker_id, audio_16k, vad_ms)  # VAD END_OF_SPEECH -> utterance.end, final ASR,
                                                       # streamed translation per target language,
                                                       # fact guard, async second-model check, timings
    await p.handle_control(Control)                    # repeat_back, explain_simply, correct_term, burn
    await p.aclose()

``emit`` is ``async def emit(env: Envelope) -> None``: the worker publishes it on the data
channel (topic raum.events) and forwards it to core over the link.

Rules (§6.4): checks never block display; partials at lower priority than finals; off_record drops audio
entirely. Facts (``raum.pipeline.readback``): green -> a quiet "noted" flag for the professional's screen,
nobody is asked; uncertain (guard mismatch, checker amber/red, low ASR confidence) -> one spoken read-back to
the listener (flag with needs_confirm); their next utterance, if a short yes/no, answers it.

Additions beyond the contract (all optional):

- ``await p.drain()`` waits for background work (second check, voice, timings); tests use it.
- ``p.tts_sink = async fn(lang, pcm_int16, sample_rate)`` receives voice output (the worker
  publishes it as track ``tts-<lang>``).
- ``p.denoise_ms[speaker] = ms`` lets the worker report its cleaning time in ``pipeline.timing``.
- ``speech_end`` detaches the utterance before its first ``await``, so the worker may run it as a
  task and keep reading VAD events; one ``await asyncio.sleep(0)`` after ``create_task`` suffices.
- Semantic end of turn (``raum.pipeline.turn``): with ``p.eot`` set (the worker does, from
  ``models.turn``) the worker calls ``await p.speech_pause(speaker, audio_16k, vad_ms)`` on VAD
  END_OF_SPEECH instead of ``speech_end``. The final ASR runs at once; ``p.eot.verdict`` decides how
  much silence to allow (0 / unsure / max); the turn then ends with that transcript (no second ASR).
  ``speech_start`` for the same speaker before that continues the same utterance (the next pause
  starts a new decision on all of it). ``pipeline.timing`` gains ``eot`` and ``asr_early``.
- Echo control per device (C-14, ``raum.pipeline.echo``): ``p.mic_open(identity)`` says whether the
  worker passes that device's mic audio on (else it feeds silence to the VAD). While the voice for a
  language plays (+300 ms) the devices that would hear it are paused, ``speech_start`` refuses them
  (echo, not speech), and ``voice.state`` announces start (with the paused identities) and end.
- Voice like the speaker's (C-13, ``raum.pipeline.voice``): if the voice engine uses a voice sample
  (``tts.uses_voice_ref``), the first ~8 s of each speaker's clean speech are kept in memory only and
  passed with that speaker's translated words (``synthesize(..., voice_ref=wav, rate=pace)``); dropped
  on burn, session end / a new session (``set_state``) and ``aclose``. Per device
  (``ParticipantInfo.voice``): ``standard`` means no sample is ever taken or sent for that speaker (the
  service's built-in voice); a switch to it drops the sample at once (``forget_speaker``). ``rate`` (the
  speaker's words per second / 2.5) is passed to every engine that accepts it. Never in events, logs, the
  store or the audit log.
- Voice lock (F-10, ``raum.pipeline.voicelock``, ``models.voicelock``, off by default): each device's mic only
  produces its owner's utterances. The speaker embedding runs beside the final ASR; after the ASR, an utterance
  that is not the owner's voice is dropped before ``transcript.final`` (not translated, spoken, stored or
  summarized). ``voice.lock`` carries the verdict and score. The voices are memory only and go with the voice
  samples (burn, session end, ``aclose``); control ``voice_lock_reset`` learns a device's voice again.
- Voice echo check (``raum.pipeline.echo_check``, ``tts.options.echo_check: facts``, off by default): a spoken clause
  with a dose / number / date / time is transcribed back at background priority beside the playback; a figure the
  voice said with another value -> ``voice.echo`` + the clause once more in the standard voice ("I repeat: ...").
- Simultaneous mode (simul.py) and speculative translation (speculate.py) hook in at marked lines.
- A sentence cut at a pause, merged again (merge.py, mode ``pipeline.merge_continuation_ms``, 0 = off): the same
  speaker again within that window of an end of turn whose voice still plays continues it in a new utterance
  (``utterance.continued`` for the old one); ``p.tts_stop = fn(lang)`` (the worker) drops what the voice track
  of that language still holds.

Timing (``pipeline.timing``, one per utterance and target language, after the check): ``vad_ms``
is the endpoint delay; ``asr_ms`` the final transcription; ``mt_first_ms``, ``mt_full_ms`` and
``tts_first_ms`` are measured from the moment the speaker stopped (= endpoint - vad_ms), the
numbers of the §3 table; ``check_ms`` is translation.final -> verify.result. Also: ``asr_from``
(``partial`` = the stable last partial, ``early`` = the pause's early final, ``final`` = a final pass),
``tts_synth_first_ms`` (stop -> first PCM from the voice engine) and ``tts_publish_first_ms`` (stop -> the
first frame the worker's AudioSource accepted; the sink returns that moment); with the echo check, ``echo_ms``
(clause synthesized -> heard back and compared, the slowest clause) and ``echo_check`` (the worst result).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, get_args

import numpy as np

from raum.config import RaumConfig
from raum.events import (
    Control,
    Envelope,
    FlagKind,
    ParticipantInfo,
    SessionState,
    make_event,
    now_ms,
)
from raum.glossary import Glossary, GlossaryError, parse_terms
from raum.modes.schema import ModeConfig
from raum.pipeline import codeswitch, readback, stats
from raum.pipeline import texts as T
from raum.pipeline._util import int16_to_float, lang_code, lang_name, load_prompt, render_prompt
from raum.pipeline.base import AsrResult, Engines, TranslateRequest, TurnContext
from raum.pipeline.echo import EchoControl
from raum.pipeline.echo_check import EchoCheck
from raum.pipeline.incomplete import join_fragments
from raum.pipeline.merge import Continuations
from raum.pipeline.simul import Simultaneous
from raum.pipeline.speculate import Speculator
from raum.pipeline.tts_http import voice_id
from raum.pipeline.voice import SpeakingRate, VoiceArgs, VoiceRef, speech_seconds
from raum.pipeline.voicelock import VoiceLock
from raum.scheduler import Scheduler
from raum.verify import guard
from raum.verify.backtranslate import CheckResult, second_check

Emit = Callable[[Envelope], Awaitable[None]]
TtsSink = Callable[[str, np.ndarray, int], Awaitable[float | None]]  # -> monotonic time the 1st frame was accepted
TtsStop = Callable[[str], None]  # drop what the voice track of a language still holds (merge.py)

log = logging.getLogger("raum.pipeline.room")

SAMPLE_RATE = 16000
ASR_TIMEOUT_S = 15.0
PARTIAL_TIMEOUT_S = 5.0
MT_TIMEOUT_S = 30.0
CHECK_TIMEOUT_S = 20.0
EXPLAIN_TIMEOUT_S = 30.0  # a whole plain-language rewrite (K-02), off the live path
VOICE_IDLE_S = MT_TIMEOUT_S + 15.0  # a voice lane waits this long for the next clause, then frees itself
VOICE_MAX_FAILS = 3  # failed clauses in a row before a language's voice pauses
VOICE_PAUSE_S = 20.0  # then voice for that language retries (text never stops)
_FLAG_KINDS = set(get_args(FlagKind))
_WRAP_QUOTES = ('"', "'", "„", "“", "”", "«", "»")


def _strip_wrapping(text: str) -> str:
    """A model's rewrite without wrapping quotes or a leading label line ("Plain language:")."""
    t = (text or "").strip()
    first, nl, rest = t.partition("\n")
    if nl and first.rstrip().endswith(":") and len(first) < 40:
        t = rest.strip()
    if len(t) >= 2 and t[0] in _WRAP_QUOTES and t[-1] in _WRAP_QUOTES:
        t = t[1:-1].strip()
    return t

_NAME_PATTERNS = [
    re.compile(r"\b(?:Herr|Frau|Dr\.|Prof\.|Mr\.?|Mrs\.?|Ms\.?|Rechtsanwältin|Rechtsanwalt|Bay|Bayan)"
               r"(?:\s+Dr\.)?\s+[A-ZÄÖÜÇĞİŞ][\w'\-]+"),
    re.compile(r"\b[A-ZÇĞİÖŞÜ][\wçğıöşü]+\s+(?:Hanım|Bey)\b"),
]
_DIGITS = re.compile(r"\d+(?:[.,]\d+)*")

_guard_warned = False


def _guard_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, bool]:
    """Call the fact guard; ``(None, False)`` if it is not implemented yet or fails (logged once)."""
    global _guard_warned
    try:
        return fn(*args, **kwargs), True
    except NotImplementedError:
        if not _guard_warned:
            log.warning("fact guard not implemented yet: continuing without facts")
            _guard_warned = True
        return None, False
    except Exception:
        log.exception("fact guard failed: continuing without facts")
        return None, False


def _takes_voice_args(fn: Any) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return any(p.kind is p.VAR_KEYWORD for p in params.values()) or {"voice_ref", "rate"} <= set(params)


def _as_float(audio: np.ndarray) -> np.ndarray:
    a = np.asarray(audio)
    return int16_to_float(a) if a.dtype == np.int16 else a.astype(np.float32, copy=False).reshape(-1)


def _role_from_identity(identity: str) -> str:
    head = identity.split("-", 1)[0]
    return head if head in ("a", "b") else "b"


@dataclass
class _Utt:
    id: str
    speaker: str
    role: str
    lang: str  # for events
    hint: str | None  # for ASR
    turn: TurnContext
    frames: list[np.ndarray] = field(default_factory=list)
    samples: int = 0
    seq: int = 0
    next_partial_ms: float = 0.0
    partial_task: asyncio.Task[None] | None = None
    closed: bool = False
    echoed: bool = False  # a voice this device hears played during it: not a clean voice sample
    parts: list[np.ndarray] = field(default_factory=list)  # END_OF_SPEECH audio of each pause (held turns)
    hold: asyncio.Task[None] | None = None  # end-of-turn wait after a pause (speech_pause)
    prefix: np.ndarray | None = None  # merge.py: the audio of the cut utterance this one continues
    ref_upto: int = 0  # samples of this utterance's audio already in the speaker's voice sample (_grow_ref)
    partial_texts: list[str] = field(default_factory=list)  # codeswitch.py: a phrase the final drops comes back
    stages: stats.Stages = field(default_factory=stats.Stages)  # stats.py: wait vs work, pipeline.timing stages

    def _with_prefix(self, chunks: list[np.ndarray]) -> np.ndarray:
        chunks = [self.prefix, *chunks] if self.prefix is not None and len(self.prefix) else chunks
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    def audio(self) -> np.ndarray:
        return self._with_prefix(self.frames)

    def final_audio(self) -> np.ndarray:
        return self._with_prefix(self.parts) if self.parts else self.audio()


@dataclass
class _Turn:
    utt: str
    speaker: str
    role: str
    lang: str
    text: str
    conf: float
    turn: TurnContext
    ts: int = field(default_factory=now_ms)
    translations: dict[str, str] = field(default_factory=dict)


@dataclass
class _Ask:
    """A read-back waiting for the listener's spoken yes/no (``readback.yes_no``)."""

    flag_id: str
    utt: str  # the utterance whose facts were read back
    role: str  # who answers (the listener)
    lang: str  # in their language
    after: int  # only an utterance that starts after this one (``u-NNNN`` number) answers
    ready: bool = True  # False until the read-back has started playing: nothing earlier answers it


_CLAUSE_END = re.compile(r"[.!?;:…。！？،؛؟](?=\s)|,(?=\s)")
_ABBREV = re.compile(r"(?:\b(?:Dr|Fr|Hr|Prof|Nr|ca|bzw|z\.B|u\.a|St|Mr|Mrs|Ms|No|vs)\.|\d\.)$", re.IGNORECASE)
MIN_CLAUSE_CHARS = 16


_QUESTION_END = re.compile(r"[?？؟]\s*$")


def _is_question(text: str) -> bool:
    return bool(_QUESTION_END.search(text or ""))


def _words(s: str) -> int:
    return len(s.split())


def _tail_ok(head: str, min_words: int, expect_words: int | None) -> bool:
    """With a word floor and the expected length of the whole translation: cut only if what is left
    can still make a clause of ``min_words`` (else the voice waits and says the rest in one piece)."""
    return not min_words or expect_words is None or expect_words - _words(head) >= min_words


def take_clause(buf: str, min_words: int = 0, expect_words: int | None = None) -> tuple[str, str]:
    """Split off the longest speakable prefix that ends at a clause boundary (sentence end, or a comma
    once the clause is long enough). Ordinals and abbreviations ("12.", "Dr.") never end a clause.
    ``min_words``: no cut before that many words (a cloned voice is clean only from ~5 words); the
    short clause waits and goes with the next one."""
    cut = -1
    for m in _CLAUSE_END.finditer(buf):
        end = m.end()
        head = buf[:end]
        if len(head.strip()) < MIN_CLAUSE_CHARS or _words(head) < min_words:
            continue
        if not _tail_ok(head, min_words, expect_words):
            continue
        if m.group(0) == "," and len(head.strip()) < 2 * MIN_CLAUSE_CHARS:
            continue
        if m.group(0) == "." and _ABBREV.search(head):
            continue
        cut = end
    if cut < 0:
        return "", buf
    return buf[:cut].strip(), buf[cut:]


# The voice starts on the first clause boundary (a sentence end, or a comma from the 2nd word); a phrase cut by
# word count only after this many words of a long unpunctuated run. Each clause is its own TTS request, and
# Chatterbox ends every request with 0.36-0.6 s of silence: a 5-word cut ("Take 500 mg Metformin twice | a day,
# always after meals.") put a 0.5-0.6 s gap mid-sentence. A longer first text costs no first-audio time
# (232-246 ms for the whole sentence vs 349-356 ms for the 5 words) and, replayed by speculate.py, ~60 ms of MT.
PHRASE_WORDS = 12
_WORD = re.compile(r"\S+(?=\s)")  # a complete word: the next one has begun
_CLOSE = "\"'»«”’)"


def take_phrase(buf: str, min_words: int = 0, expect_words: int | None = None) -> tuple[str, str]:
    """The voice's FIRST phrase, so it starts sooner: up to the first clause boundary (from the 2nd word; a
    sentence end at once) or after ``PHRASE_WORDS`` complete words, whichever comes first; never right after
    a number ("500 | mg"). Later clauses use ``take_clause``. Only where the voice starts changes: the fact
    guard still checks the whole translation. ``min_words`` (a cloned voice): the phrase has at least that
    many words; a shorter sentence is spoken whole once the translation ends."""
    words = max(PHRASE_WORDS, min_words)
    for i, m in enumerate(_WORD.finditer(buf)):
        head = buf[:m.end()].rstrip(_CLOSE)
        c = head[-1:]
        boundary = bool(c) and c in ".!?;:…。！？،؛؟," and not (c == "." and _ABBREV.search(head))
        if not _tail_ok(head, min_words, expect_words):
            continue
        if boundary and (i >= 1 or c in ".!?…。！？؟") and i + 1 >= min_words:
            return buf[:m.end()].strip(), buf[m.end():]
        if i + 1 >= words and (not any(ch.isdigit() for ch in m.group(0)) or i + 1 >= words + 2):
            return buf[:m.end()].strip(), buf[m.end():]
    return "", buf


_NO_SECOND_OPINION = ("checker warming up", "second check failed", "second check off", "embedder off")


def ask_badge(result: Any) -> str:
    """The checker's badge as far as asking goes: an amber that only says the second opinion is missing (the
    checker still loading, failed or off) is no doubt about the facts: nobody is read back for it (the badge
    on the card stays amber). On the box, every fact would otherwise be read back until gpt-oss is resident."""
    reasons = list(getattr(result, "reasons", None) or [])
    if result.badge == "amber" and result.similarity is None and reasons and \
            all(str(r).startswith(_NO_SECOND_OPINION) for r in reasons):
        return "green"
    return result.badge


class RoomPipeline:
    def __init__(self, room: str, mode: ModeConfig, engines: Engines, emit: Emit, config: RaumConfig) -> None:
        self.room = room
        self.mode = mode
        self.engines = engines
        self._emit = emit
        self.cfg = config
        self.glossary = self._load_glossary()
        self.participants: dict[str, ParticipantInfo] = {}
        self.state: SessionState | None = None
        self.off_record = False
        self.partial_every_ms = float(config.models.asr.options.get("partial_every_ms", 1000))
        self.tts_sink: TtsSink | None = None
        self.tts_stop: TtsStop | None = None  # merge.py: the worker drops a language's queued voice
        self.eot: Any = None  # raum.pipeline.turn.TurnDetector (set by the worker); None: a VAD pause ends the turn
        self.denoise_ms: dict[str, float] = {}
        self.echo = EchoControl()  # C-14: whose mic pauses while which voice plays
        tts_opts = config.models.tts.options or {}
        self.voice_ref_s = float(tts_opts.get("voice_ref_s", 8.0))  # voice sample per speaker (memory only)
        self.voice_ref_min_s = float(tts_opts.get("voice_ref_min_s", 3.0))  # used from this length on
        self.voice_ref_on = bool(tts_opts.get("voice_ref", True))
        # speculate.py, voice made before the end of turn: "first" (default) = the whole utterance from the pause's
        # early final, the first phrase of a partial; "pause" = the early final only; "all" = partials too; "off"
        self.spec_voice = str(tts_opts.get("spec_voice", "first"))
        self.base_wps = float(tts_opts.get("base_wps", 2.5))
        self.vad_pad_s = float((config.models.vad.options or {}).get("prefix_padding_duration", 0.5))
        self._voice_refs: dict[str, VoiceRef] = {}
        self._rates: dict[str, SpeakingRate] = {}
        self.voicelock = VoiceLock.for_room(engines, config, emit=self.emit, spawn=self._spawn)  # F-10
        self.scheduler: Scheduler | None = None
        self._utt_n = 0
        self._turn_index = 0
        self._active: dict[str, _Utt] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._turns: deque[_Turn] = deque(maxlen=64)  # conversation memory (V-04), cleared on burn
        self._names: list[str] = []
        self._last_instruction: _Turn | None = None
        self._repeat_back = False
        self._drafts: dict[str, tuple[str, str]] = {}  # utt -> (target_lang, text)
        # Voice (C-13): one lane per target language, so utterances speak one after another and each
        # language's audio track has a single writer. A failing clause is dropped; only repeated
        # failures pause that language's voice, and only for VOICE_PAUSE_S (never the whole session).
        self._service_forgets: set[asyncio.Task[Any]] = set()  # DELETEs of cloned voices in flight
        self._voice_lanes: dict[str, asyncio.Lock] = {}
        self._voice_fails: dict[str, int] = {}
        self._voice_paused_until: dict[str, float] = {}
        self._closed = False
        self._asks: dict[tuple[str, str], _Ask] = {}  # (role, lang) -> the read-back their next utterance answers
        self._asked: dict[tuple[Any, ...], str] = {}  # facts read back this session -> flag id: never asked twice
        # testing the read-back path on the box: every fact that would be noted is read back (why "debug")
        self.force_readback = os.environ.get("RAUM_DEBUG_FORCE_READBACK", "").strip() in ("1", "true", "yes")
        if self.force_readback:
            log.warning("RAUM_DEBUG_FORCE_READBACK: every confirm_on fact is read back (testing only)")
        self.simul = Simultaneous(self)  # simultaneous mode + stable partial as final (simul.py)
        self.spec = Speculator(self)  # speculative translation (speculate.py)
        self.merge = Continuations(self)  # a sentence cut at a pause, merged again (merge.py)
        self.echo_check = EchoCheck(self)  # the voice heard back for its figures (echo_check.py)

    # ---------------------------------------------------------------- setup

    def _load_glossary(self) -> Glossary:
        try:
            return Glossary.load(self.mode.glossary)
        except Exception:
            log.exception("glossary %s failed to load", self.mode.glossary)
            return Glossary()

    def _asr_prompt(self, lang: str | None) -> str | None:
        """Spelling hints for every ASR call of an utterance (rooms with ``asr.glossary_prompt``): the current
        glossary, session uploads and corrections included, in the speaker's language (``Glossary.asr_prompt``;
        "und": the language-neutral German forms). Rebuilt only when the glossary changed."""
        if not self.mode.asr.glossary_prompt:
            return None
        return self.glossary.asr_prompt(lang) or None

    def fix_terms(self, text: str, lang: str | None) -> str:
        """A misheard glossary medication in the glossary's spelling ("Medformin" -> "Metformin", guard.py
        ``correct_medications``), and a sentence end whisper put at a pause inside a sentence removed ("You
        should not. Take ..." -> "You should not take ...", incomplete.py ``join_fragments``): what the card
        shows, the translator reads and the fact guard checks."""
        text = join_fragments(text, lang)
        fixed, _ok = _guard_call(guard.correct_medications, text, lang or "", self.glossary)
        return fixed if isinstance(fixed, str) else text

    @property
    def sched(self) -> Scheduler:
        if self.scheduler is None:
            self.scheduler = Scheduler.shared(self.cfg)
        return self.scheduler

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("pipeline task failed", exc_info=task.exception())

    async def emit(self, type: str, data: Any, utt: str | None = None) -> Envelope | None:
        if self.merge.silenced(utt) and type != "utterance.continued":
            return None  # merge.py: continued in a later utterance, nothing more of it goes out
        try:
            env = make_event(type, self.room, data, utt=utt)
        except Exception:
            log.exception("invalid %s event", type)
            return None
        try:
            await self._emit(env)
        except Exception:
            log.exception("emit %s failed", type)
        return env

    # ---------------------------------------------------------------- membership and state

    def set_participants(self, participants: list[ParticipantInfo]) -> None:
        """Membership or a device's language / voice output changed: used from the next utterance (ASR hint,
        target languages) and at once by echo control. A device that switched to the standard voice has its
        voice sample dropped at once, here and in the voice service (C-13)."""
        self.participants = {p.id: p for p in participants}
        for speaker in [i for i, p in self.participants.items() if p.voice == "standard" and i in self._voice_refs]:
            self.forget_speaker(speaker)
        changed = self.echo.refresh(self.participants.values())
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        for pb in changed:  # mid-playback: the phones learn the new paused set
            self._spawn(self.emit("voice.state", {"target_lang": pb.lang, "playing": True, "paused": pb.paused}))

    def set_state(self, state: SessionState) -> None:
        prev, self.state = self.state, state
        new_session = prev is not None and prev.session_id is not None and state.session_id != prev.session_id
        if state.status != "live" or new_session:  # session over: its voice samples go with it
            self._forget_voices()
            self._forget_asks()
        self._set_off_record(bool(state.off_record))

    def _set_off_record(self, on: bool) -> None:
        if on and not self.off_record:
            self.merge.clear()  # merge.py: no audio held across the off-the-record part
            try:
                asyncio.get_running_loop()
                running = True
            except RuntimeError:
                running = False
            for u in list(self._active.values()):
                self._drop(u)
                if running:  # close the bubble on the devices; no content
                    self._spawn(self.emit("utterance.end", {"speaker": u.speaker, "lang": u.lang}, utt=u.id))
            self._active.clear()
        self.off_record = on

    def _drop(self, u: _Utt) -> None:
        u.closed = True
        for ext in (self.simul, self.spec):  # simul.py, speculate.py: forget it
            ext.drop(u.id)
        u.frames.clear()
        u.parts.clear()
        u.prefix = None
        if u.partial_task and not u.partial_task.done():
            u.partial_task.cancel()
        self._cancel_hold(u)

    @staticmethod
    def _cancel_hold(u: _Utt) -> None:
        h, u.hold = u.hold, None
        if h is not None and not h.done() and h is not asyncio.current_task():
            h.cancel()

    def _participant_lang(self, p: ParticipantInfo | None) -> str | None:
        code = lang_code(p.lang) if p else None
        return None if code in (None, "auto", "und") else code

    def _role_lang(self, role: str) -> str | None:
        for p in self.participants.values():
            if p.role == role:
                code = self._participant_lang(p)
                if code:
                    return code
        return None

    def _default_lang(self, role: str) -> str:
        rc = self.mode.roles.get(role) if role in ("a", "b") else None  # type: ignore[call-overload]
        code = lang_code(rc.default_lang) if rc else None
        if code and code != "auto":
            return code
        return "de" if role == "a" else "en"

    # ---------------------------------------------------------------- speech

    async def speech_start(self, speaker: str) -> str | None:
        if self.off_record or self._closed:
            return None
        u = self._active.get(speaker)
        if u is not None:
            self._cancel_hold(u)  # speech again within the end-of-turn wait: the same utterance goes on
            self.spec.drop(u.id)  # speculate.py: what was translated / voiced ahead for that pause is discarded
            u.stages = stats.Stages()  # stats.py: the next pause's timings, not this one's
            return u.id
        if self.echo.is_paused(speaker):  # C-14: the box's own voice from this device's speaker, not speech
            return None
        p = self.participants.get(speaker)
        role = p.role if p else _role_from_identity(speaker)
        hint = self._participant_lang(p)
        lang = hint or (self._default_lang(role) if role == "a" else "und")
        self._utt_n += 1
        uid = f"u-{self._utt_n:04d}"
        turn = TurnContext(room=self.room, mode=self.mode.id, speaker=speaker, role=role, utt=uid,
                           speech_ms=0.0, turn_index=self._turn_index, asr_prompt=self._asr_prompt(lang))
        self._turn_index += 1
        u = _Utt(id=uid, speaker=speaker, role=role, lang=lang, hint=hint, turn=turn,
                 next_partial_ms=self.partial_every_ms)
        cut = self.merge.take(u)  # merge.py: right after a cut turn whose voice still plays: u carries both parts
        self._active[speaker] = u
        if cut is not None:
            await self.emit("utterance.continued", {"speaker": speaker, "into": uid}, utt=cut)
        await self.emit("utterance.start", {"speaker": speaker, "lang": lang}, utt=uid)
        return uid

    async def speech_audio(self, speaker: str, frame: np.ndarray) -> None:
        if self.off_record:
            return
        u = self._active.get(speaker)
        if u is None or u.closed:
            return
        f = np.asarray(frame)
        f = int16_to_float(f) if f.dtype == np.int16 else f.astype(np.float32, copy=False).reshape(-1)
        if not u.echoed and self.echo.is_paused(speaker):
            u.echoed = True
        u.frames.append(f)
        u.samples += len(f)
        speech_ms = u.samples * 1000.0 / SAMPLE_RATE
        if speech_ms >= u.next_partial_ms and (u.partial_task is None or u.partial_task.done()):
            u.next_partial_ms = speech_ms + self.partial_every_ms
            u.partial_task = self._spawn(self._partial(u))

    async def _partial(self, u: _Utt) -> None:
        stats.use(u.stages)
        audio = u.audio()
        turn = replace(u.turn, speech_ms=len(audio) * 1000.0 / SAMPLE_RATE)
        try:
            async with self.sched.slot("background"):
                if u.closed:
                    return
                res = await asyncio.wait_for(
                    self.engines.asr.transcribe(audio, lang_hint=u.hint, partial=True, turn=turn), PARTIAL_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("partial asr failed: %s", e)
            return
        if u.closed or self.off_record or not res.text.strip():
            return
        u.seq += 1
        u.partial_texts = [*u.partial_texts[-11:], res.text.strip()]
        await self.emit("transcript.partial", {"speaker": u.speaker, "lang": lang_code(res.lang) or u.lang,
                                               "text": res.text.strip(), "seq": u.seq}, utt=u.id)
        await self.simul.on_partial(u, res, audio)  # simul.py: speculate, commit stable complete clauses

    async def speech_end(self, speaker: str, audio: np.ndarray | None, vad_ms: float, *,
                         asr: AsrResult | None = None, eot: Any = None) -> None:
        # detach synchronously (before any await): a new speech_start for this speaker starts a new utterance
        t_detect = time.monotonic()
        u = self._active.pop(speaker, None)
        if u is not None:
            self._cancel_hold(u)
        if self.off_record or self._closed:
            if u:
                self._drop(u)
            return
        if u is None:
            if audio is None or len(audio) == 0:
                return
            if await self.speech_start(speaker) is None:
                return
            u = self._active.pop(speaker, None)
            if u is None:
                return
        u.closed = True
        if u.partial_task and not u.partial_task.done():
            u.partial_task.cancel()
        if audio is not None and len(audio):
            u.parts.append(_as_float(audio))
        final_audio = u.final_audio()
        u.frames.clear()
        u.parts.clear()
        u.prefix = None
        t0 = t_detect - max(0.0, float(vad_ms)) / 1000.0  # the moment the speaker stopped
        u.stages.t0 = t0
        self.merge.ended(u, final_audio, t_detect)  # merge.py: the speaker may still go on
        tok = stats.use(u.stages)  # the turn's tasks inherit it (asyncio copies the context)
        try:  # merge.py: if they do, the turn stops and speech_end just returns
            stats.mark("eot", "end_ms")
            await self.merge.run(u.id, self._turn(u, final_audio, float(vad_ms), t0, asr=asr, eot=eot))
        finally:
            stats.reset(tok)
            self.simul.release(u.id)  # simul.py: close segments of a turn that ended without a final

    async def speech_pause(self, speaker: str, audio: np.ndarray | None, vad_ms: float) -> None:
        """VAD END_OF_SPEECH with semantic end of turn: start the decision and return (see module doc)."""
        u = self._active.get(speaker)
        if self.eot is None or u is None or u.closed or self.off_record or self._closed:
            await self.speech_end(speaker, audio, vad_ms)
            return
        if audio is not None and len(audio):
            u.parts.append(_as_float(audio))
        self._cancel_hold(u)
        t_stop = time.monotonic() - max(0.0, float(vad_ms)) / 1000.0  # the moment the speaker stopped
        u.hold = self._spawn(self._hold(u, t_stop))

    async def _hold(self, u: _Utt, t_stop: float) -> None:
        """Speculative final ASR + verdict, then wait out the remaining silence; cancelled by speech_start."""
        u.stages.t0 = t_stop
        stats.use(u.stages)
        pause_ms = (time.monotonic() - t_stop) * 1000.0  # the silence at the end of this audio (VAD's pause)
        audio = u.final_audio()
        n = len(audio)
        audio_p = self._spawn(self.eot.audio_p(audio))  # CPU, in parallel with the ASR
        self.voicelock.start(u.speaker, u.id, audio, (time.monotonic() - t_stop) * 1000.0,  # F-10: beside it too
                             getattr(self.participants.get(u.speaker), "device", None))
        res: AsrResult | None = None
        t_asr = time.monotonic()
        try:
            with stats.timed("asr.settle"):
                await self.simul.settle(u, n, PARTIAL_TIMEOUT_S)  # simul.py: a running partial may be the final
            res = self.simul.reuse_final(u, n)  # simul.py: a stable last partial is the final
            if res is None:
                async with self.sched.slot("live"):
                    res = await asyncio.wait_for(self.engines.asr.transcribe(
                        audio, lang_hint=u.hint, partial=False, turn=replace(u.turn, speech_ms=n * 1000.0 / SAMPLE_RATE)),
                        ASR_TIMEOUT_S)
            res.ms = (time.monotonic() - t_asr) * 1000.0
            res.text = codeswitch.restore(res.text, u.partial_texts, lang_code(res.lang) or u.hint)
            stats.mark("asr", "early_ms")
            if res.text.strip():  # speculate.py: MT + the utterance's voice now, in parallel with the verdict
                if res.conf >= self.cfg.asr_min_conf:  # the voice sample as this turn's voice will use it
                    self._grow_ref(u, audio, pause_ms, early=True)
                self.spec.on_text(u, res.text, lang_code(res.lang) or u.hint or self._default_lang(u.role),
                                  slot="live", seconds=speech_seconds(n / SAMPLE_RATE, (time.monotonic() - t_stop)
                                                                      * 1000.0, res.words, self.vad_pad_s))
            verdict = await self.eot.verdict(res.text, lang_code(res.lang) or u.hint, audio_p,
                                             conf=float(res.conf) if res.has_conf else None)
        except asyncio.CancelledError:
            audio_p.cancel()
            raise
        except Exception as e:  # noqa: BLE001  the final ASR in _turn tries again
            log.warning("early asr / end of turn failed for %s: %s: %s", u.id, e.__class__.__name__, e)
            res, verdict = None, None
        wait_s = verdict.wait_s if verdict is not None else float(getattr(self.eot, "unsure_s", 1.0))
        rest = t_stop + wait_s - time.monotonic()
        if rest > 0:
            await asyncio.sleep(rest)
        if self._active.get(u.speaker) is not u or u.closed or u.hold is not asyncio.current_task():
            return
        if len(u.final_audio()) != n:
            res = None  # more audio arrived meanwhile: transcribe again
        await self.speech_end(u.speaker, None, (time.monotonic() - t_stop) * 1000.0, asr=res,
                              eot=verdict.timing() if verdict is not None else {"why": "failed"})

    # ---------------------------------------------------------------- the live path

    async def _turn(self, u: _Utt, audio: np.ndarray, vad_ms: float, t0: float, *,
                    asr: AsrResult | None = None, eot: dict[str, Any] | None = None) -> None:
        # F-10: the speaker embedding runs on a worker thread beside the final ASR below; with the early ASR
        # (asr given) it already started beside that one in _hold, on the same audio
        device = getattr(self.participants.get(u.speaker), "device", None)
        lock = self.voicelock.begin(u.speaker, u.id, audio, vad_ms, device, reuse=asr is not None)
        await self.emit("utterance.end", {"speaker": u.speaker, "lang": u.lang}, utt=u.id)
        turn = replace(u.turn, speech_ms=len(audio) * 1000.0 / SAMPLE_RATE)
        timing: dict[str, Any] = {"vad_ms": round(vad_ms, 1), "models": dict(self.engines.names)}
        if u.speaker in self.denoise_ms:
            timing["denoise_ms"] = round(self.denoise_ms[u.speaker], 2)
        if eot is not None:
            timing["eot"] = eot

        if asr is not None:  # ran during the end-of-turn wait (speech_pause)
            res = asr
            timing["asr_ms"] = round(asr.ms, 1)
            timing["asr_early"] = True
        else:
            t_asr = time.monotonic()
            try:
                res = self.simul.reuse_final(u, len(audio))  # simul.py: a stable last partial stands in for it
                if res is None:
                    async with self.sched.slot("live"):
                        res = await asyncio.wait_for(
                            self.engines.asr.transcribe(audio, lang_hint=u.hint, partial=False, turn=turn),
                            ASR_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("final asr failed for %s: %s: %s", u.id, e.__class__.__name__, e)
                if await self.voicelock.admit(lock, clean=False):  # F-10: no prompt for someone else's voice
                    await self._say_again(u, u.lang if u.lang != "und" else None)
                return
            timing["asr_ms"] = round((time.monotonic() - t_asr) * 1000.0, 1)
            res.text = codeswitch.restore(res.text, u.partial_texts, lang_code(res.lang) or u.hint)
        # which transcript this is: the stable partial (simul.py), the early final of the pause, or a final pass
        timing["asr_from"] = "partial" if res.from_partial else ("early" if asr is not None else "final")
        src_lang = lang_code(res.lang) or u.hint or self._default_lang(u.role)
        text = self.fix_terms(res.text.strip(), src_lang)  # the glossary's spelling, before anyone sees it
        # F-10 voice lock: not the device owner's voice -> dropped here, before anything of it is published
        clean = bool(text) and res.conf >= self.cfg.asr_min_conf and not u.echoed  # may teach the owner's voice
        if not await self.voicelock.admit(lock, clean=clean):
            return
        await self.emit("transcript.final", {"speaker": u.speaker, "lang": src_lang, "text": text,
                                             "asr_conf": round(float(res.conf), 3)}, utt=u.id)
        if not text:
            return
        if res.conf < self.cfg.asr_min_conf:  # V-03: not translated
            await self._say_again(u, src_lang)
            await self.emit("pipeline.timing", timing, utt=u.id)
            return

        rec = _Turn(utt=u.id, speaker=u.speaker, role=u.role, lang=src_lang, text=text, conf=res.conf, turn=turn)
        context = self._context()  # names from earlier turns, before this one is remembered
        self._remember(rec)
        self._take_answer(rec)  # readback.py: a short yes/no to the read-back this speaker heard
        if self._repeat_back and rec.role == "b" and self._last_instruction is not None:
            self._repeat_back = False
            self._spawn(self._repeat_back_check(self._last_instruction, rec))
        if rec.role == "a":
            self._last_instruction = rec

        voice = self._voice_args(u, audio, vad_ms, res.words, text)
        targets = self._targets(rec)
        simul, targets = self.simul.finish_turn(u, rec, targets, t0, timing)  # simul.py: segmented langs: tail
        await asyncio.gather(*simul, *(self._translate(u, rec, tgt, t0, dict(timing), context, voice)
                                       for tgt in targets))
        if "offer_tracker" in self.mode.buttons:
            await self._offers(rec)

    def _targets(self, rec: _Turn) -> list[str]:
        """Every other participant's language (deduped, minus the source language). No listener yet:
        the other role's default language."""
        langs: list[str] = []
        listeners = [p for p in self.participants.values() if p.id != rec.speaker and p.role != "observer"]
        for p in listeners:
            code = self._participant_lang(p)
            if code and code != rec.lang and code not in langs:
                langs.append(code)
        if not listeners:
            other = "b" if rec.role == "a" else "a"
            code = self._default_lang(other)
            if code != rec.lang:
                langs.append(code)
        return langs

    def _listener_role(self, tgt: str, speaker_role: str) -> str:
        roles = [p.role for p in self.participants.values()
                 if p.role in ("a", "b") and self._participant_lang(p) == tgt]
        other = "b" if speaker_role == "a" else "a"
        if other in roles or not roles:
            return other
        return roles[0]

    def _request(self, rec: _Turn, tgt: str, context: str) -> TranslateRequest:
        lang_a = self._role_lang("a") or (rec.lang if rec.role == "a" else tgt)
        lang_b = self._role_lang("b") or (rec.lang if rec.role == "b" else tgt)
        return TranslateRequest(
            text=rec.text, src_lang=rec.lang, tgt_lang=tgt,
            room_name=self.mode.display_name("en"), role_a=self.mode.label("a", "en"),
            role_b=self.mode.label("b", "en"), lang_a=lang_a, lang_b=lang_b, register=self.mode.register,
            glossary=self.glossary.prompt_lines(rec.lang, tgt), context=context, turn=rec.turn,
        )

    async def _translate(self, u: _Utt, rec: _Turn, tgt: str, t0: float, timing: dict[str, Any],
                         context: str, voice: VoiceArgs | None = None) -> None:
        req = self._request(rec, tgt, context)
        parts: list[str] = []
        first_ms: float | None = None
        # C-13: voice starts on the first phrase while the rest of the sentence still streams
        voice_q: asyncio.Queue[str | None] | None = asyncio.Queue() if self._tts_enabled() else None
        marks: dict[str, float] = {}  # tts_synth_first_ms / tts_publish_first_ms
        self.echo_check.track(marks, u.id, tgt)  # echo_check.py: this voice's clauses are heard back
        tts_task: asyncio.Task[float | None] | None = None
        if voice_q is not None:  # merge.py: the utterance's voice, stopped if the speaker goes on
            tts_task = self.merge.track(u.id, self._spawn(self._speak_stream(tgt, voice_q, t0, voice, marks, utt=u.id)))
        pending = ""
        min_words = self.min_voice_words(u.speaker)  # a cloned voice: no clause under ~5 words
        expect = _words(rec.text) if min_words else None  # the translation's length, roughly the source's
        spoke = False

        async def run() -> None:
            nonlocal first_ms, pending, spoke
            async for delta in self.spec.stream(u.id, tgt, req):  # speculate.py: replayed if it matched
                if not delta:
                    continue
                if first_ms is None:
                    first_ms = (time.monotonic() - t0) * 1000.0
                parts.append(delta)
                await self.emit("translation.delta", {"target_lang": tgt, "delta": delta, "seq": len(parts)},
                                utt=u.id)
                if voice_q is not None:
                    pending += delta
                    clause, pending = (take_clause(pending, min_words, expect) if spoke
                                       else take_phrase(pending, min_words, expect))
                    if clause:
                        spoke = True
                        voice_q.put_nowait(clause)

        try:
            async with self.sched.slot("live"):
                await asyncio.wait_for(run(), MT_TIMEOUT_S)
        except asyncio.CancelledError:
            if voice_q is not None:
                voice_q.put_nowait(None)  # free this language's voice lane
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("translation %s -> %s failed for %s: %s: %s", rec.lang, tgt, u.id, e.__class__.__name__, e)
            if not parts:
                if voice_q is not None:
                    voice_q.put_nowait(None)
                await self.emit("pipeline.timing", {**timing, "target_lang": tgt}, utt=u.id)
                return
        if voice_q is not None:
            if pending.strip():
                voice_q.put_nowait(pending.strip())
            voice_q.put_nowait(None)
        text = "".join(parts).strip()
        timing["mt_first_ms"] = round(first_ms, 1) if first_ms is not None else None
        timing["mt_full_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
        await self.emit("translation.final", {"target_lang": tgt, "text": text}, utt=u.id)
        rec.translations[tgt] = text
        t_final = time.monotonic()

        # fact guard: code, < 5 ms, on the live path
        res, ok = _guard_call(guard.check, rec.text, rec.lang, text, tgt, kinds=self.mode.guard.kinds,
                              confirm_on=self.mode.guard.confirm_on, glossary=self.glossary)
        facts: dict[str, Any] = {"target_lang": tgt, "facts": [], "red": False}
        if res is not None:
            facts.update(facts=[f.model_dump() for f in res.facts], red=bool(res.red), ms=round(res.ms, 2))
        else:
            facts["error"] = "fact guard unavailable"
        await self.emit("verify.facts", facts, utt=u.id)
        flagged = await self._fact_flags(rec, tgt, res)

        # never blocks the live path: second check + voice, then the timing line
        self.merge.track(u.id, self._spawn(self._after_final(rec, tgt, text, res, ok, flagged, t0, t_final, timing,
                                                             tts_task, marks)))

    async def _after_final(self, rec: _Turn, tgt: str, text: str, gres: guard.GuardResult | None, guard_ok: bool,
                           flagged: bool, t0: float, t_final: float, timing: dict[str, Any],
                           tts_task: asyncio.Task[float | None] | None = None,
                           marks: dict[str, float] | None = None) -> None:
        check_ms: float | None = None
        try:
            check_ms = await self._check(rec, tgt, text, gres, guard_ok, flagged, t_final)
        finally:
            tts_first = await tts_task if tts_task else None
            timing.update(target_lang=tgt, check_ms=check_ms, tts_first_ms=tts_first)
            if marks is not None:  # stop -> first PCM from the engine / first frame the worker's AudioSource took
                timing.update(tts_synth_first_ms=marks.get("synth"), tts_publish_first_ms=marks.get("publish"))
                if any(k in marks for k in ("voice_spec", "voice_ahead", "voice_live")):  # speculate.py: where each
                    timing["voice_clauses"] = {k: int(marks.get(f"voice_{k}", 0))  # clause's audio came from
                                               for k in ("spec", "ahead", "live")}
                timing.update(await self.echo_check.settle(marks))  # echo_check.py: echo_ms, echo_check
            st = stats.current()  # stats.py: per stage, what queued (wait_ms) and what worked; ms after the stop
            if st is not None:
                timing["stages"] = st.snapshot()
            await self.emit("pipeline.timing", timing, utt=rec.utt)

    def checker_for(self, role: str | None, src_lang: str, tgt: str) -> tuple[Any, str | None]:
        """The second model for this target, or (None, why) when this target gets the fact guard only:
        the room has ``checker: off``, or a room with 3+ languages checks one target per utterance
        (``pipeline.check_targets: one``): the professional's language, or the main listener's when the
        professional speaks."""
        if self.engines.verify is None:
            return None, None
        if getattr(self.mode, "checker", "on") == "off":
            return None, "second check off in this room: fact guard only"
        if getattr(self.cfg.pipeline, "check_targets", "one") == "one":
            primary = self._check_target(role, src_lang)
            if primary is not None and tgt != primary:
                return None, f"second check on {primary} only (one per utterance): fact guard here"
        return self.engines.verify, None

    def _check_target(self, role: str | None, src_lang: str) -> str | None:
        """The one target language that gets the second model's check (None: no preference)."""
        langs = [(p.role, self._participant_lang(p)) for p in self.participants.values() if p.role in ("a", "b")]
        want = "b" if role == "a" else "a"
        for r, code in langs:  # join order: the first listener of the other role
            if r == want and code and code != src_lang:
                return code
        for _, code in langs:
            if code and code != src_lang:
                return code
        return None

    async def _check(self, rec: _Turn, tgt: str, text: str, gres: guard.GuardResult | None, guard_ok: bool,
                     flagged: bool, t_final: float) -> float:
        verifier, why = self.checker_for(rec.role, rec.lang, tgt)

        async def run() -> CheckResult:
            res = await second_check(rec.text, rec.lang, text, tgt, verifier=verifier, embedder=self.engines.embed,
                                     thresholds=self.cfg.thresholds, guard=gres, guard_available=guard_ok,
                                     turn=rec.turn, timeout_s=CHECK_TIMEOUT_S)
            if why:
                res.reasons = [why if r.startswith("second check off") else r for r in res.reasons]
            return res

        if verifier is None:  # guard only: no model, no slot
            result = await run()
        else:
            async with self.sched.slot("check"):
                result = await run()
        check_ms = round((time.monotonic() - t_final) * 1000.0, 1)
        await self.emit("verify.result", {"target_lang": tgt, "similarity": result.similarity, "badge": result.badge,
                                          "reasons": result.reasons, "back_translation": result.back_translation},
                        utt=rec.utt)
        if not flagged:  # readback.py: the room's facts noted (all green) or read back once (uncertain)
            flagged = await self._settle_facts(rec, tgt, gres, ask_badge(result))
        if result.badge == "red" and not flagged:  # a red meaning check with no fact to read back
            texts = T.all_langs(T.UNSURE, tgt)
            await self.emit("flag", {"kind": "mismatch", "text": T.pick(texts, tgt), "needs_confirm": True,
                                     "id": f"{rec.utt}-{tgt}-check",
                                     "confirm_by": self._listener_role(tgt, rec.role), "target_lang": tgt,
                                     "texts": texts}, utt=rec.utt)
        return check_ms

    def _tts_enabled(self) -> bool:
        return self.engines.tts is not None

    def min_voice_words(self, speaker: str | None) -> int:
        """Shortest clause the voice may speak alone: cross-lingual clones are clean only from ~5 words
        ("Danke. Danke.", dropped words), so with the speaker's own cloned voice short clauses go with the
        next one (``tts.options.min_clause_words``, default 5). 0 for stock voices and the standard voice."""
        tts = self.engines.tts
        if speaker is None or tts is None or not self._own_voice(speaker):
            return 0
        ref = self._voice_refs.get(speaker)
        if ref is None or ref.seconds < self.voice_ref_min_s:
            return 0  # no sample yet: the service speaks its stock voice
        return int((getattr(tts, "options", None) or {}).get("min_clause_words", 5))

    def _voice_paused(self, tgt: str) -> bool:
        return time.monotonic() < self._voice_paused_until.get(tgt, 0.0)

    async def _speak_stream(self, tgt: str, q: asyncio.Queue[str | None], t0: float,
                            voice: VoiceArgs | None = None, marks: dict[str, float] | None = None,
                            utt: str | None = None) -> float | None:
        """Speak clauses in order as they arrive; returns ms from the speaker's stop to the first audio.

        Holds ``tgt``'s voice lane for the whole utterance: a second utterance into the same language
        (two people, or one sentence cut at a pause) waits its turn instead of writing into the same
        audio track at the same time (the GX10 phone test: "InvalidState - failed to capture frame")."""
        lane = self._voice_lanes.setdefault(tgt, asyncio.Lock())
        first: float | None = None
        ahead: list[str | None] = []  # clauses already queued behind the one playing
        async with lane:
            while True:
                if ahead:
                    clause = ahead.pop(0)
                else:
                    try:
                        clause = await asyncio.wait_for(q.get(), VOICE_IDLE_S)
                    except TimeoutError:
                        log.warning("voice %s: no clause for %.0f s, releasing the lane", tgt, VOICE_IDLE_S)
                        return first
                if clause is None:
                    await self.echo_check.flush(tgt, marks)  # echo_check.py: a repeat already due, before the lane frees
                    return first
                if self._voice_paused(tgt) or self._closed:
                    continue
                while not q.empty():  # speculate.py: the next clause is made while this one plays (no gap)
                    ahead.append(q.get_nowait())
                tts = self.engines.tts
                if tts is not None:
                    nxt = ahead[0] if ahead else None
                    self.spec.prepare(tts, tgt, clause, nxt, self._voice_kwargs(tts, voice), utt=utt)
                f = await self._speak_watching(tgt, clause, t0, voice, marks, utt, q, ahead)
                if first is None:
                    first = f

    async def _speak_watching(self, tgt: str, clause: str, t0: float, voice: VoiceArgs | None,
                              marks: dict[str, float] | None, utt: str | None, q: asyncio.Queue[str | None],
                              ahead: list[str | None]) -> float | None:
        """``_speak`` one clause; a clause that arrives meanwhile is queued in ``ahead`` and, if it is the next one,
        made now (speculate.py ``prepare_next``), not when its turn comes (before: a gap of one first-audio time)."""
        speak = asyncio.ensure_future(self._speak(tgt, clause, t0, voice, marks, utt=utt))
        getter: asyncio.Future[str | None] | None = None
        try:
            while not speak.done():
                getter = asyncio.ensure_future(q.get())
                await asyncio.wait({speak, getter}, return_when=asyncio.FIRST_COMPLETED)
                if not getter.done():
                    getter.cancel()
                    await asyncio.wait({getter})  # it may have taken an item just before the cancel
                if getter.cancelled():
                    getter = None
                    continue
                item = getter.result()
                getter = None
                ahead.append(item)
                tts = self.engines.tts
                if item is not None and len(ahead) == 1 and tts is not None:
                    self.spec.prepare_next(tts, tgt, item, self._voice_kwargs(tts, voice), utt=utt)
            return speak.result()
        finally:
            if not speak.done():
                speak.cancel()
            if getter is not None and not getter.done():
                getter.cancel()

    async def _speak(self, tgt: str, text: str, t0: float, voice: VoiceArgs | None = None,
                     marks: dict[str, float] | None = None, utt: str | None = None) -> float | None:
        tts = self.engines.tts
        if tts is None or not text:
            return None
        await self.echo_check.flush(tgt, marks)  # echo_check.py: an earlier clause said a figure wrong: repeat first
        probe = self.echo_check.probe(tgt, text, marks)  # echo_check.py: a clause with figures is heard back
        first: float | None = None
        try:
            # speculate.py: a pre-synthesised clause if it was made with these voice args
            async for chunk in self.spec.synthesize(tts, text, tgt, marks=marks, **self._voice_kwargs(tts, voice)):
                if first is None:
                    first = round((time.monotonic() - t0) * 1000.0, 1)
                    if marks is not None:
                        marks.setdefault("synth", first)
                if self.tts_sink is not None and not self.off_record and len(chunk):
                    self._voice_out(tgt, len(chunk), tts.sample_rate)
                    self.merge.played(utt, tgt, len(chunk) / tts.sample_rate)  # merge.py: until when it plays
                    if probe is not None:
                        probe.add(chunk)
                    took = await self.tts_sink(tgt, chunk, tts.sample_rate)
                    if marks is not None and "publish" not in marks:  # the sink says when its 1st frame went in
                        at = took if isinstance(took, float) else time.monotonic()
                        marks["publish"] = round((at - t0) * 1000.0, 1)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  §6.15: text continues; drop this clause, keep the voice
            n = self._voice_fails[tgt] = self._voice_fails.get(tgt, 0) + 1
            if n >= VOICE_MAX_FAILS:
                self._voice_paused_until[tgt] = time.monotonic() + VOICE_PAUSE_S
                self._voice_fails[tgt] = 0
                log.warning("voice %s failed %d times in a row (%s: %s): paused for %.0f s, text continues",
                            tgt, n, e.__class__.__name__, e, VOICE_PAUSE_S)
            else:
                log.warning("voice %s: clause dropped (%s: %s)", tgt, e.__class__.__name__, e)
            return None
        self._voice_fails[tgt] = 0
        if probe is not None:
            probe.done(tts.sample_rate)  # echo_check.py: transcribed in the background, beside the playback
        if first is None and getattr(tts, "simulated", False):
            first = round((time.monotonic() - t0) * 1000.0, 1)
            if marks is not None:
                marks.setdefault("synth", first)
        return first

    # ---------------------------------------------------------------- the speaker's voice (C-13)

    def _uses_voice_ref(self) -> bool:
        return self.voice_ref_on and self._tts_enabled() and bool(getattr(self.engines.tts, "uses_voice_ref", False))

    def _own_voice(self, speaker: str | None) -> bool:
        """Are this speaker's words spoken in a copy of their own voice? Only with an engine that clones and
        only while their device keeps ``voice: own`` (the default); with ``standard`` no voice sample is ever
        taken and none is sent: the service speaks its built-in voice."""
        if speaker is None or not self._uses_voice_ref():
            return False
        return getattr(self.participants.get(speaker), "voice", "own") != "standard"

    def _voice_args(self, u: _Utt, audio: np.ndarray, vad_ms: float, words: list[dict[str, Any]],
                    text: str) -> VoiceArgs:
        """The speaker's pace, and their voice sample (grown from clean utterances up to ``voice_ref_s``)."""
        seconds = speech_seconds(len(audio) / SAMPLE_RATE, vad_ms, words, self.vad_pad_s)
        rate = self._rates.setdefault(u.speaker, SpeakingRate(self.base_wps)).update(text, seconds)
        if not self._own_voice(u.speaker):
            return VoiceArgs(rate=rate)
        return VoiceArgs(ref=self._grow_ref(u, audio, vad_ms), rate=rate)

    def _grow_ref(self, u: _Utt, audio: np.ndarray, vad_ms: float, *, early: bool = False) -> VoiceRef | None:
        """The speaker's voice sample with this utterance's clean speech in it (up to ``voice_ref_s``). Done at
        the pause already (``_hold``), so the voice made ahead there (speculate.py) and the turn's own voice use
        the same sample: the cache matches. Only what is new since then is added (``u.ref_upto``). F-10 voice
        lock on: only at the turn (it decides first whose voice this is)."""
        if not self._own_voice(u.speaker):
            return None
        if early and self.voicelock.enabled:
            return self._voice_refs.get(u.speaker)
        ref = self._voice_refs.get(u.speaker)
        if ref is None:
            ref = self._voice_refs[u.speaker] = VoiceRef(max_s=self.voice_ref_s)
        if not u.echoed and not ref.full:
            tail = int(max(0.0, vad_ms) / 1000.0 * SAMPLE_RATE)  # the endpoint silence
            end = max(0, len(audio) - tail)
            if end > u.ref_upto:
                ref.add(audio[u.ref_upto:end])
                u.ref_upto = end
        return ref

    def voice_ref(self, speaker: str) -> bytes | None:
        """The speaker's voice sample as WAV bytes once long enough (memory only), else None."""
        ref = self._voice_refs.get(speaker)
        return ref.wav() if ref is not None and ref.seconds >= self.voice_ref_min_s else None

    def _forget_voices(self) -> None:
        ids = [i for ref in self._voice_refs.values() for i in ref.sent_ids]
        for ref in self._voice_refs.values():
            ref.sent_ids.clear()
            ref.clear()
        self._voice_refs.clear()
        self._rates.clear()
        self.voicelock.clear()  # F-10: the owners' voices go with the session
        self.merge.clear()  # merge.py: the audio held for a continuation, too
        self.spec.clear()  # speculate.py: audio made ahead (the cloned voice), too
        self._forget_in_service(ids)

    def forget_speaker(self, speaker: str) -> None:
        """A device left for good (after the rejoin grace) or switched to the standard voice: its voice sample
        goes, here and in the service. Back to its own voice, a fresh sample starts."""
        ref = self._voice_refs.pop(speaker, None)
        self._rates.pop(speaker, None)
        if ref is not None:
            ids = list(ref.sent_ids)
            ref.sent_ids.clear()
            ref.clear()
            self._forget_in_service(ids)

    def _forget_in_service(self, ids: list[str]) -> None:
        """A voice-cloning service keeps each sample it got until told: tell it (DELETE per id). Not in
        ``self._tasks``: ``aclose`` must not cancel it (it waits for it briefly instead)."""
        forget = getattr(self.engines.tts, "forget", None)
        if not ids or not callable(forget):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        t = loop.create_task(forget(ids))
        self._service_forgets.add(t)
        t.add_done_callback(self._service_forgets.discard)

    def _voice_kwargs(self, tts: Any, voice: VoiceArgs | None) -> dict[str, Any]:
        """``voice_ref`` / ``rate`` for engines that take them (older ones get the plain call)."""
        if voice is None or not _takes_voice_args(tts.synthesize):
            return {}
        ref = voice.ref
        wav = ref.wav() if ref is not None and ref.seconds >= self.voice_ref_min_s else None  # cleared on burn
        if wav is not None and getattr(tts, "uses_voice_ref", False):
            ref.sent_ids.add(voice_id(wav))  # the service will hold it under this id until we delete it
        return {"voice_ref": wav, "rate": voice.rate}

    # ---------------------------------------------------------------- echo control (C-14)

    def mic_open(self, identity: str) -> bool:
        """Pass this device's mic audio on? Never off the record. While a voice it would hear plays, only
        to finish an utterance that began before the pause; anything new from it is echo."""
        if self.off_record or self._closed:
            return False
        return identity in self._active or not self.echo.is_paused(identity)

    def _voice_out(self, lang: str, samples: int, rate: int) -> None:
        pb = self.echo.play(lang, samples / max(1, rate), self.participants.values())
        if pb is not None:  # the cue goes out alongside the first audio, without delaying it
            self._spawn(self.emit("voice.state", {"target_lang": lang, "playing": True, "paused": list(pb.paused)}))
            self._spawn(self._voice_watch(lang))

    async def _voice_watch(self, lang: str) -> None:
        """Lift the pause when the voice has played out plus the tail; then announce the end."""
        while True:
            left = self.echo.remaining(lang)
            if left is None:
                return
            if left > 0:
                await asyncio.sleep(left)
                continue
            self.echo.stop(lang)
            await self.emit("voice.state", {"target_lang": lang, "playing": False, "paused": []})
            return

    # ---------------------------------------------------------------- flags

    async def _say_again(self, u: _Utt, lang: str | None) -> None:
        texts = T.all_langs(T.SAY_AGAIN, lang)
        await self.emit("flag", {"kind": "uncertain", "text": T.pick(texts, lang), "needs_confirm": False,
                                 "id": f"{u.id}-uncertain", "target_lang": lang, "texts": texts}, utt=u.id)

    # ---------------------------------------------------------------- facts: noted or read back (readback.py)

    async def _fact_flags(self, rec: Any, tgt: str, res: guard.GuardResult | None) -> bool:
        """At once, on a guard mismatch: one read-back to the listener of what was SAID (the wrong facts and
        the room's ``confirm_on`` facts), spoken after the utterance's voice. True: handled (no prompt on top)."""
        if res is None or not res.red:
            return False
        bad = [f for f in res.facts if not f.match and readback.worth_noting(f)]
        chosen = bad + [f for f in res.confirm if f not in bad and readback.worth_noting(f)]
        await self._ask(rec, tgt, chosen, res.facts, kind="mismatch", why=["guard"], suffix="mismatch")
        return True

    async def _settle_facts(self, rec: Any, tgt: str, res: guard.GuardResult | None, badge: str) -> bool:
        """After the second check: the room's ``confirm_on`` facts of a statement (a question states nothing;
        allergy "present" names nothing). Guard and checker green and the ASR sure: a quiet "noted" flag for
        the professional's screen, nobody is asked. Otherwise one read-back. True: a question was raised."""
        if res is None or res.red or _is_question(rec.text):
            return False
        chosen = [f for f in res.confirm if readback.worth_noting(f)]
        if not chosen:
            return False
        why = [] if badge == "green" else [f"check:{badge}"]
        conf = getattr(rec, "conf", None)
        floor = self.mode.pipeline.asr_confirm_conf
        if conf is not None and float(conf) < (self.cfg.asr_confirm_conf if floor is None else floor):
            why.append("asr")
        if self.force_readback and not why:
            why.append("debug")
        if not why:
            await self._noted(rec, tgt, chosen, res.facts)
            return False
        kind = next((f.kind for f in chosen if f.kind in _FLAG_KINDS), "number")
        return await self._ask(rec, tgt, chosen, res.facts, kind=kind, why=why, suffix="confirm")

    def _wording(self, rec: Any, tgt: str) -> readback.Wording:
        found, _ok = _guard_call(guard.extract, rec.text, rec.lang, kinds=self.mode.guard.kinds, glossary=self.glossary)
        values = {(f.kind, f.surface.strip().casefold()): f.value for f in found or []}
        return readback.Wording(rec.lang, tgt, self.glossary, values)

    def _flag_langs(self, rec: Any, tgt: str) -> list[str]:
        return list(dict.fromkeys([*T.LANGS, tgt, rec.lang]))

    async def _noted(self, rec: Any, tgt: str, chosen: list[Any], context: list[Any]) -> None:
        """Green: chips on the professional's screen ("✓ Allergie: Penicillin"), "noted" in the summary."""
        gs = readback.groups(chosen, context)
        if not gs:
            return
        w = self._wording(rec, tgt)
        langs = self._flag_langs(rec, tgt)
        facts = w.chips(gs, langs)
        texts = {lang: " · ".join(str(f["texts"][lang]) for f in facts) for lang in langs}  # type: ignore[index]
        pro = self._role_lang("a") or (rec.lang if rec.role == "a" else tgt)
        await self.emit("flag", {"kind": "noted", "text": texts.get(pro) or texts[tgt], "needs_confirm": False,
                                 "id": f"{rec.utt}-{tgt}-noted", "target_lang": tgt, "texts": texts,
                                 "status": "noted", "facts": facts, "show_to": "a"}, utt=rec.utt)

    async def _ask(self, rec: Any, tgt: str, chosen: list[Any], context: list[Any], *, kind: str, why: list[str],
                   suffix: str) -> bool:
        """Uncertain: one short read-back in the listener's language ("Nur zur Sicherheit: 400 mg?"), shown with
        small yes/no buttons and spoken through the voice lane; their next utterance may answer it aloud. A fact
        read back before is never asked again. Nothing to read back (a translation that added a figure): the
        "may be wrong" note, not spoken. True: handled (asked now, or all of it asked before)."""
        w = self._wording(rec, tgt)
        gs = readback.groups(chosen, context)
        fresh = [g for g in gs if self._fact_key(g, w, tgt) not in self._asked]
        if gs and not fresh:
            return True  # all of it read back before: never twice, and nothing else on top
        langs = self._flag_langs(rec, tgt)
        role = self._listener_role(tgt, rec.role)
        fid = f"{rec.utt}-{tgt}-{suffix}"
        texts = {lang: w.question(fresh, lang) for lang in langs} if fresh else T.all_langs(T.UNSURE, tgt)
        voice = bool(fresh) and self._tts_enabled() and not self.off_record
        await self.emit("flag", {"kind": kind, "text": texts.get(tgt) or T.pick(texts, tgt), "needs_confirm": True,
                                 "id": fid, "confirm_by": role, "target_lang": tgt, "texts": texts,
                                 "status": "unconfirmed", "facts": w.chips(fresh, langs), "spoken": voice,
                                 "why": why}, utt=rec.utt)
        for g in fresh:
            self._asked[self._fact_key(g, w, tgt)] = fid
        ask = _Ask(flag_id=fid, utt=rec.utt, role=role, lang=tgt, after=self._utt_n, ready=not voice)
        self._asks[(role, tgt)] = ask  # their next utterance answers the latest; an older one stays unconfirmed
        if voice:  # merge.py: cancelled with the utterance if it was a sentence cut at a pause
            self.merge.track(rec.utt, self._spawn(self._read_back(ask, readback.spoken(texts.get(tgt) or "", tgt))))
        return True

    def forget_asks(self, utt: str) -> None:
        """merge.py: ``utt`` was cut at a pause and merged into the next utterance: its read-backs were never
        heard, so its facts may be asked again for the merged sentence, and no answer is taken for it."""
        self._asked = {k: v for k, v in self._asked.items() if not v.startswith(f"{utt}-")}
        self._asks = {k: v for k, v in self._asks.items() if v.utt != utt}

    @staticmethod
    def _fact_key(group: list[Any], w: readback.Wording, tgt: str) -> tuple[Any, ...]:
        vals = tuple(sorted((f.kind, w.values.get((f.kind, (f.src or "").strip().casefold())) or
                             (f.src or "").strip().casefold()) for f in group))
        return (tgt, vals)

    async def _read_back(self, ask: _Ask, text: str) -> None:
        def started() -> None:  # an utterance begun before the question was heard does not answer it
            ask.after = self._utt_n
            ask.ready = True

        try:
            await self.speak_line(ask.lang, text, on_start=started)
        finally:
            ask.ready = True

    async def speak_line(self, lang: str, text: str, on_start: Callable[[], None] | None = None) -> float | None:
        """A system line (a read-back) spoken in ``lang`` through that language's voice lane: after the utterance
        holding it, never inside one. The standard voice (no speaker's sample); echo control as for any voice
        (``_voice_out`` pauses the mics that hear it). Not while that language's voice is paused or off the record."""
        if not text or not self._tts_enabled():
            return None
        async with self._voice_lanes.setdefault(lang, asyncio.Lock()):
            if self._voice_paused(lang) or self._closed or self.off_record:
                return None
            if on_start is not None:
                on_start()
            return await self._speak(lang, text, time.monotonic())

    def _take_answer(self, rec: _Turn) -> None:
        """The asked person's next utterance after a read-back: a short yes/no (``readback.yes_no``) answers it,
        a ``confirm`` (via voice) on the asked flag. Anything else closes the question: the fact stays
        unconfirmed, nobody asks again. The utterance is translated either way."""
        if not self._asks:
            return
        lang = self._participant_lang(self.participants.get(rec.speaker)) or rec.lang
        ask = self._asks.get((rec.role, lang)) or self._asks.get((rec.role, rec.lang))
        if ask is None or not ask.ready or readback.utt_number(rec.utt) <= ask.after:
            return
        self._asks = {k: v for k, v in self._asks.items() if v is not ask}
        verdict = readback.yes_no(rec.text, rec.lang)
        if verdict is None:
            return
        self._spawn(self.emit("confirm", {"by": rec.role, "accepted": verdict, "flag_id": ask.flag_id, "via": "voice",
                                          "answer_utt": rec.utt}, utt=ask.utt))

    def _answered(self, flag_id: str | None) -> None:
        """A tap answered a read-back: no spoken answer is taken for it any more."""
        if flag_id:
            self._asks = {k: v for k, v in self._asks.items() if v.flag_id != flag_id}

    def _forget_asks(self) -> None:
        self._asks.clear()
        self._asked.clear()

    # ---------------------------------------------------------------- memory (V-04)

    def _remember(self, rec: _Turn) -> None:
        self._turns.append(rec)
        found: list[str] = []
        for pat in _NAME_PATTERNS:
            found += [m.group(0).strip() for m in pat.finditer(rec.text)]
        facts, _ = _guard_call(guard.extract, rec.text, rec.lang, kinds=["name"], glossary=self.glossary)
        found += [f.surface for f in facts or [] if getattr(f, "kind", "") == "name"]
        for name in found:
            if name and name not in self._names:
                self._names.append(name)
        del self._names[:-12]

    def _context(self) -> str:
        return ("Names: " + ", ".join(self._names)) if self._names else "(none)"

    def _find_turn(self, utt: str | None) -> _Turn | None:
        for rec in reversed(self._turns):
            if utt is None or rec.utt == utt:
                return rec
        return None

    # ---------------------------------------------------------------- offers (N-01)

    async def _offers(self, rec: _Turn) -> None:
        facts, ok = _guard_call(guard.extract, rec.text, rec.lang, kinds=["amount", "currency"], glossary=self.glossary)
        if not ok or not facts:
            return
        currency = next((f.value.strip().upper() for f in facts if f.kind == "currency"), None)
        for f in facts:
            if f.kind != "amount":
                continue
            m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z€$£]{1,3})?\s*$", f.value)
            if not m:
                continue
            cur = (m.group(2) or currency or "EUR").upper().replace("€", "EUR").replace("$", "USD").replace("£", "GBP")
            await self.emit("offer.update", {"amount": float(m.group(1)), "currency": cur, "by": rec.role,
                                             "ts": rec.ts, "text": f.surface or rec.text}, utt=rec.utt)

    # ---------------------------------------------------------------- repeat-back (S-02)

    async def _repeat_back_check(self, inst: _Turn, rest: _Turn) -> None:
        verifier = self.engines.verify
        result: dict[str, Any] | None = None
        if verifier is not None and not getattr(verifier, "is_mock", False):
            prompt = render_prompt(load_prompt(self.mode.prompts.repeat_back or "prompts/repeat-back.md"),
                                   lang_a=lang_name(inst.lang), lang_b=lang_name(rest.lang),
                                   instruction=inst.text, restatement=rest.text)
            try:
                async with self.sched.slot("check"):
                    j = await asyncio.wait_for(verifier.complete_json(prompt, purpose="repeat_back"), CHECK_TIMEOUT_S)
                understood = str(j.get("understood", "partly")).lower()
                result = {"understood": understood if understood in ("yes", "partly", "no") else "partly",
                          "missing": [str(x) for x in j.get("missing") or []],
                          "wrong": [str(x) for x in j.get("wrong") or []]}
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("repeat-back model failed (%s): comparing facts", e.__class__.__name__)
        if result is None:
            if verifier is not None and getattr(verifier, "is_mock", False):
                await verifier.complete_text("", purpose="repeat_back")  # mock latency only
            result = self._compare_facts(inst, rest)
        result["instruction_utt"] = inst.utt
        await self.emit("repeat_back.result", result, utt=rest.utt)

    def _compare_facts(self, inst: _Turn, rest: _Turn) -> dict[str, Any]:
        kinds = self.mode.guard.kinds
        a, ok_a = _guard_call(guard.extract, inst.text, inst.lang, kinds=kinds, glossary=self.glossary)
        b, ok_b = _guard_call(guard.extract, rest.text, rest.lang, kinds=kinds, glossary=self.glossary)
        if ok_a and ok_b:
            want = Counter((f.kind, f.value) for f in a)
            got = Counter((f.kind, f.value) for f in b)
            surface = {(f.kind, f.value): f.surface for f in a}
            missing = [surface.get(k) or k[1] for k in (want - got).elements()]
            want_kinds = {k for k, _ in want}
            wrong = [f.surface or f.value for f in b if f.kind in want_kinds and (f.kind, f.value) not in want]
            total = sum(want.values())
        else:  # guard not available yet: compare the numbers only
            want_n = Counter(_DIGITS.findall(inst.text))
            got_n = Counter(_DIGITS.findall(rest.text))
            missing = list((want_n - got_n).elements())
            wrong = []
            total = sum(want_n.values())
        if not missing and not wrong:
            understood = "yes"
        elif total and len(missing) >= total:
            understood = "no"
        else:
            understood = "partly"
        return {"understood": understood, "missing": missing, "wrong": wrong}

    # ---------------------------------------------------------------- controls

    async def handle_control(self, control: Control) -> None:
        action = control.action
        try:
            if action == "off_record":
                self._set_off_record(bool(control.value))
            elif action == "confirm":
                self._answered(control.flag_id)  # a tap: no spoken answer taken for that read-back any more
            elif action == "repeat_back":
                self._repeat_back = True
            elif action == "explain_simply":
                # a model call of seconds: never hold up the link (state, off_record) while it runs
                self._spawn(self._explain(control))
            elif action == "explain_approve":
                await self._explain_approve(control)
            elif action == "correct_term":
                self._correct_term(control.value)
            elif action == "glossary":
                self._merge_glossary(control.value)
            elif action == "burn":
                self._burn()
            elif action == "voice_lock_reset":  # F-10
                self.voicelock.reset(control.by, self.participants.values())
            # human_interpreter, end, approve_summary, mute: handled by core / the devices
        except Exception:
            log.exception("control %s failed", action)

    def _explain_target(self, control: Control) -> str:
        v = control.value
        if isinstance(v, dict) and v.get("lang"):
            return str(lang_code(str(v["lang"])))
        if isinstance(v, str) and v:
            return str(lang_code(v))
        return self._role_lang("b") or "en"

    async def _explain(self, control: Control) -> None:
        """K-02: a plain-language draft for the professional (``explain.draft``); sent only on approve.
        The second model rewrites with ``prompts/explain-simply.md``; if it is off, still loading or
        fails, the draft is the faithful translation, so the professional is never left waiting."""
        rec = self._find_turn(control.utt) if control.utt else next(
            (r for r in reversed(self._turns) if r.role == "a"), None)
        if rec is None:
            log.info("explain_simply: utterance %s not found", control.utt)
            return
        tgt = self._explain_target(control)
        text = ""
        try:
            text = await self._simple_text(rec, tgt)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - WarmingUp, timeout, model server down
            log.warning("explain simply via the second model failed (%s): drafting the translation", e.__class__.__name__)
        if not text.strip():
            text = await self._faithful(rec, tgt)
        if self._closed:
            return
        self._drafts[rec.utt] = (tgt, text)
        await self.emit("explain.draft", {"source_utt": rec.utt, "target_lang": tgt, "text": text}, utt=rec.utt)

    async def _simple_text(self, rec: _Turn, tgt: str) -> str:
        verifier = self.engines.verify
        if verifier is None:
            return ""
        simple = getattr(verifier, "simple_version", None)
        if callable(simple):  # mock: canned plain-language lines
            return str(await simple(rec.text, rec.lang, tgt, self.mode.id))
        prompt = render_prompt(load_prompt(self.mode.prompts.explain_simply or "prompts/explain-simply.md"),
                               lang_a=lang_name(rec.lang), lang_b=lang_name(tgt), text=rec.text)
        async with self.sched.slot("background"):
            text = await asyncio.wait_for(verifier.complete_text(prompt, purpose="explain_simply"), EXPLAIN_TIMEOUT_S)
        return _strip_wrapping(text)

    async def _faithful(self, rec: _Turn, tgt: str) -> str:
        """The turn's translation into ``tgt``; translated now if that language was not a target."""
        if rec.translations.get(tgt):
            return rec.translations[tgt]
        if tgt == rec.lang:
            return rec.text
        parts: list[str] = []

        async def run() -> None:
            async for delta in self.engines.translate.stream(self._request(rec, tgt, self._context())):
                parts.append(delta)

        try:
            async with self.sched.slot("background"):
                await asyncio.wait_for(run(), MT_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("explain simply: translation into %s failed (%s)", tgt, e.__class__.__name__)
        return "".join(parts).strip()

    async def _explain_approve(self, control: Control) -> None:
        utt = control.utt
        draft = self._drafts.pop(utt, None) if utt else None
        if draft is None and not control.text:
            log.info("explain_approve: no draft for %s", utt)
            return
        tgt = draft[0] if draft else self._explain_target(control)
        text = control.text or (draft[1] if draft else "")
        await self.emit("explain.sent", {"source_utt": utt or "", "target_lang": tgt, "text": text}, utt=utt)

    def _correct_term(self, value: Any) -> None:
        if not isinstance(value, dict) or not value.get("src") or not value.get("tgt") or not value.get("lang"):
            log.info("correct_term: expected {src, tgt, lang}, got %r", value)
            return
        src_lang = str(value.get("src_lang") or self._default_lang("a"))
        self.glossary.add_correction(str(value["src"]), str(value["tgt"]), str(value["lang"]), src_lang=src_lang)

    def _merge_glossary(self, value: Any) -> None:
        """N-02: an uploaded glossary (``{"terms": [...]}`` in the glossaries/*.yaml shape) applies to the
        translation prompt and the fact guard for the rest of the session; a burn forgets it."""
        raw = value.get("terms") if isinstance(value, dict) else value
        try:
            terms = parse_terms(raw)
        except GlossaryError as e:
            log.info("glossary control: %s", e)
            return
        added, updated = self.glossary.merge(terms)
        log.info("session glossary: %d terms added, %d updated", added, updated)

    def _burn(self) -> None:
        """Forget the conversation: memory, names, drafts, voice samples, session glossary corrections."""
        for u in list(self._active.values()):
            self._drop(u)
        self._active.clear()
        self._turns.clear()
        self._names.clear()
        self._last_instruction = None
        self._repeat_back = False
        self._drafts.clear()
        self._forget_asks()
        self._forget_voices()
        self.echo_check.clear()  # echo_check.py: no repeat of burned words
        self.glossary = self._load_glossary()
        reset = getattr(self.engines.asr, "reset", None)
        if callable(reset):
            reset(self.room)

    # ---------------------------------------------------------------- lifecycle

    async def drain(self, timeout: float | None = None) -> None:
        """Wait until all background work (checks, voice, timings) has finished."""
        async def _wait() -> None:
            while True:
                pending = [t for t in self._tasks if not t.done()]
                if not pending:
                    break
                await asyncio.wait(pending)
            await asyncio.sleep(0)  # let done-callbacks run

        await asyncio.wait_for(_wait(), timeout)

    async def aclose(self) -> None:
        self._closed = True
        for u in list(self._active.values()):
            self._drop(u)
        self._active.clear()
        self.echo.clear()
        self._forget_voices()
        if self._service_forgets:  # the voice service deletes the samples before the room goes
            await asyncio.wait(list(self._service_forgets), timeout=3.0)
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---------------------------------------------------------------- introspection

    @property
    def names(self) -> list[str]:
        return list(self._names)

    def memory(self) -> list[dict[str, Any]]:
        return [{"utt": r.utt, "role": r.role, "lang": r.lang, "text": r.text} for r in self._turns]
