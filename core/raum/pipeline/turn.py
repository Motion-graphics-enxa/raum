"""Semantic end of turn: is a VAD pause the end of the speaker's turn, or a pause inside a sentence?

The Silero VAD reports a pause after a short silence (``models.vad`` ``min_silence_duration``, 0.25 s
on spark). ``RoomPipeline.speech_pause`` then runs the final ASR at once (speculatively) and asks
``TurnDetector.verdict`` how much silence to allow in total before the turn ends:

    a deterministic rule (incomplete.py)      -> max_s     "Sie dürfen das Metformin ab morgen" (no verb yet)
    P_text >= fast and P_audio >= audio_min   -> 0         end now; the speculative ASR is the final one
    P_text >= the language's calibrated floor  -> unsure_s
    P_text below it (no verb yet, "und", "äh") -> max_s
    speech again before that                  -> the same utterance goes on (RoomPipeline.speech_start)

Models (CPU), run in the agent server's shared inference process (one copy for all rooms; the room's
event loop only awaits an IPC reply):

* text: LiveKit turn detector, multilingual ``v0.4.1-intl`` (``livekit-plugins-turn-detector``,
  Qwen2.5-0.5B int8 ONNX, ~400 MB RAM, 30-40 ms per call). de en ru tr es fr it nl pt zh ja ko id hi;
  not ar. Weights under the LiveKit Model License: use with LiveKit Agents only (raum-agent is one).
* audio veto: pipecat ``smart-turn-v3.2-cpu`` (8 MB int8 ONNX, BSD-2-Clause, 23 languages), last 8 s of
  the utterance, 20-120 ms. It can only hold back the fast path, never end a turn by itself.
* a language the text model lacks (ar): the resident translate model answers yes/no (one token), or,
  without it, ``unsure_s``.

Before any model, ``incomplete.unfinished`` (German verb-final / negation scope, English trailing auxiliary;
~0 ms) can only make the wait longer: ``verb-final`` and ``neg-scope`` wait ``max_s``, ``aux-open`` (haben /
sein / werden without a closing verb: "Haben Sie Schmerzen?") waits ``aux_s`` (default ``max_s``). A transcript
ending in "?" with ASR confidence below ``q_conf`` (0.85) never ends the turn at once: the "?" is dropped
for the text model and the verdict is at least ``unsure_s`` (whisper's "?" on a cut, misheard sentence is not
evidence that it is finished).

Every failure (model files missing, IPC error, timeout) degrades to ``unsure_s``: never a cut.
Model files are fetched at install time (``python -m raum.pipeline.turn download``), never at runtime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any

import numpy as np
from livekit.agents.inference_runner import _InferenceRunner

from raum.pipeline._util import float_to_int16, lang_code, lang_name
from raum.pipeline.incomplete import unfinished

log = logging.getLogger("raum.pipeline.turn")

SAMPLE_RATE = 16000
LK_REPO, LK_REVISION, LK_METHOD = "livekit/turn-detector", "v0.4.1-intl", "lk_end_of_utterance_multilingual"
ST_REPO, ST_FILE, ST_METHOD = "pipecat-ai/smart-turn-v3", "smart-turn-v3.2-cpu.onnx", "raum_smart_turn_v3"
ST_SECONDS = 8
# livekit/turn-detector v0.4.1-intl languages.json: P(end) below this is "unlikely" (99.3 % of real ends
# lie above it). Pinned with the revision; RAUM uses it as the "clearly unfinished" floor.
LK_FLOOR = {"de": 0.0062, "en": 0.011, "ru": 0.0032, "tr": 0.0045, "es": 0.0058, "fr": 0.0078, "it": 0.0037,
            "nl": 0.0077, "pt": 0.0069, "zh": 0.0066, "ja": 0.0096, "ko": 0.0156, "id": 0.0132, "hi": 0.0398}
IPC_TIMEOUT_S = 1.0
LLM_PROMPT = (
    "You decide turn-taking for a live interpreter. Below is what a person has said so far ({lang}), "
    "transcribed by speech recognition; the speaker has just paused. Has the speaker finished a complete "
    "utterance (a full sentence, question or answer), or are they probably about to continue (sentence "
    "grammatically unfinished, missing verb or object, trailing 'and', a list, or a filler like 'uh')?\n"
    "Utterance: “{text}”\n"
    "Answer with exactly one word: yes (finished) or no (will continue)."
)


@dataclass
class Verdict:
    wait_s: float  # total silence (from the moment the speaker stopped) before the turn ends
    why: str
    p_text: float | None = None
    p_audio: float | None = None

    def timing(self) -> dict[str, Any]:
        out: dict[str, Any] = {"wait_ms": round(self.wait_s * 1000.0), "why": self.why}
        if self.p_text is not None:
            out["p_text"] = round(self.p_text, 4)
        if self.p_audio is not None:
            out["p_audio"] = round(self.p_audio, 3)
        return out


# ---------------------------------------------------------------- model files (install time)


def files_present() -> bool:
    """Both models in the local Hugging Face cache (``HF_HOME``); nothing is downloaded here."""
    try:
        from huggingface_hub import hf_hub_download

        hf_hub_download(LK_REPO, "model_q8.onnx", subfolder="onnx", revision=LK_REVISION, local_files_only=True)
        hf_hub_download(LK_REPO, "tokenizer.json", revision=LK_REVISION, local_files_only=True)
        hf_hub_download(ST_REPO, ST_FILE, local_files_only=True)
        return True
    except Exception as e:  # noqa: BLE001  (not installed, or files missing)
        log.warning("end-of-turn models not available (%s): VAD pauses end turns", e.__class__.__name__)
        return False


def download() -> None:
    from huggingface_hub import hf_hub_download
    from livekit.plugins.turn_detector.multilingual import _EUORunnerMultilingual

    _EUORunnerMultilingual._download_files()
    print(hf_hub_download(ST_REPO, ST_FILE))


# ---------------------------------------------------------------- inference-process side


class SmartTurnRunner(_InferenceRunner):
    """pipecat smart-turn v3.2: P(turn complete) from the last 8 s of 16 kHz int16 PCM."""

    INFERENCE_METHOD = ST_METHOD

    def initialize(self) -> None:
        self._session: Any = None
        try:
            import onnxruntime as ort
            from huggingface_hub import hf_hub_download
            from transformers import WhisperFeatureExtractor

            so = ort.SessionOptions()
            so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            so.inter_op_num_threads = 1
            so.intra_op_num_threads = 1
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            path = hf_hub_download(ST_REPO, ST_FILE, local_files_only=True)
            self._session = ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])
            self._features = WhisperFeatureExtractor(chunk_length=ST_SECONDS)
        except Exception:  # never take the inference process (and the text model) down
            log.exception("smart-turn failed to load")

    def run(self, data: bytes) -> bytes | None:
        if self._session is None:
            return None
        n = ST_SECONDS * SAMPLE_RATE
        x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        x = x[-n:] if len(x) > n else np.pad(x, (n - len(x), 0))
        f = self._features(x, sampling_rate=SAMPLE_RATE, return_tensors="np", padding="max_length",
                           max_length=n, truncation=True, do_normalize=True).input_features
        p = self._session.run(None, {"input_features": f.astype(np.float32)})[0].reshape(-1)[0]
        return json.dumps({"p": float(p)}).encode()


def register_runners() -> bool:
    """Main thread, before the AgentServer runs: its inference process then hosts both models.
    False (and nothing registered) when the extra or the model files are missing."""
    if not files_present():
        return False
    import livekit.plugins.turn_detector.multilingual  # noqa: F401  registers LK_METHOD (and the English model)

    _InferenceRunner.registered_runners.pop("lk_end_of_utterance_en", None)  # English-only model: not needed
    if ST_METHOD not in _InferenceRunner.registered_runners:
        _InferenceRunner.register_runner(SmartTurnRunner)
    return True


# ---------------------------------------------------------------- job side


class LlmJudge:
    """Yes/no from the resident translate model (Ollama ``/api/chat``, one output token)."""

    def __init__(self, url: str, model: str, options: dict[str, Any], *, timeout_s: float = 2.0) -> None:
        import httpx

        self.url, self.model = url.rstrip("/"), model
        # same num_ctx as the translator: another value would make Ollama reload the model (~25 s)
        self.options = {"temperature": 0.0, "num_predict": 1,
                        **{k: options[k] for k in ("num_ctx",) if k in options}}
        self.think = options.get("think")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=1.0))

    async def finished(self, text: str, lang: str) -> bool:
        body: dict[str, Any] = {"model": self.model, "stream": False, "keep_alive": -1, "options": self.options,
                                "messages": [{"role": "user", "content": LLM_PROMPT.format(
                                    lang=lang_name(lang), text=text)}]}
        if self.think is not None:
            body["think"] = self.think
        r = await self._client.post(f"{self.url}/api/chat", json=body)
        r.raise_for_status()
        return str(r.json()["message"]["content"]).strip().lower().startswith("yes")

    async def aclose(self) -> None:
        await self._client.aclose()


class TurnDetector:
    """Job-side client: asks the inference process (or the LLM) and turns the answers into a wait."""

    def __init__(self, executor: Any, options: dict[str, Any] | None = None, llm: LlmJudge | None = None) -> None:
        o = options or {}
        self.executor = executor
        self.llm = llm
        self.fast = float(o.get("fast", 0.1))
        self.unsure_s = float(o.get("unsure_s", 1.0))
        self.max_s = float(o.get("max_s", 1.6))
        self.audio_min = float(o.get("audio_min", 0.5))
        self.audio = bool(o.get("audio", True))
        self.rules = bool(o.get("rules", True))  # incomplete.py
        self.aux_s = float(o.get("aux_s", self.max_s))
        self.q_conf = float(o.get("q_conf", 0.85))
        self._warned: set[str] = set()

    def _warn(self, what: str, e: BaseException) -> None:
        if what not in self._warned:
            self._warned.add(what)
            log.warning("end of turn: %s failed (%s: %s); waiting %.1f s instead", what, e.__class__.__name__, e,
                        self.unsure_s)

    async def audio_p(self, audio: np.ndarray) -> float | None:
        """P(complete) from the audio; start it at the pause, await it after the ASR."""
        if not self.audio:
            return None
        pcm = float_to_int16(np.asarray(audio, dtype=np.float32)[-ST_SECONDS * SAMPLE_RATE:])
        try:
            out = await asyncio.wait_for(self.executor.do_inference(ST_METHOD, pcm.tobytes()), IPC_TIMEOUT_S)
            return float(json.loads(out)["p"]) if out else None
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._warn("smart-turn", e)
            return None

    async def text_p(self, text: str) -> float:
        data = json.dumps({"chat_ctx": [{"role": "user", "content": text}]}).encode()
        out = await asyncio.wait_for(self.executor.do_inference(LK_METHOD, data), IPC_TIMEOUT_S)
        return float(json.loads(out)["eou_probability"])

    def rule(self, text: str, lang: str | None) -> Verdict | None:
        """The deterministic rules (incomplete.py): a wait, or None (no opinion)."""
        why = unfinished(text, lang_code(lang)) if self.rules else None
        if why is None:
            return None
        return Verdict(self.aux_s if why == "aux-open" else self.max_s, why)

    async def verdict(self, text: str, lang: str | None, audio: asyncio.Future[float | None] | None = None, *,
                      conf: float | None = None) -> Verdict:
        text = (text or "").strip()
        code = lang_code(lang) or ""
        if not text:
            return Verdict(0.0, "no-text")  # nothing to wait for; the pipeline drops an empty turn
        unsure_q = conf is not None and conf < self.q_conf and text.endswith("?")
        if unsure_q:
            text = text.rstrip("?？؟ ").strip() or text
        ruled = self.rule(text, code)
        if ruled is not None:
            return ruled
        v = await self._model_verdict(text, code, audio)
        if unsure_q and v.wait_s < self.unsure_s:
            return Verdict(self.unsure_s, "low-conf-question", v.p_text, v.p_audio)
        return v

    async def _model_verdict(self, text: str, code: str, audio: asyncio.Future[float | None] | None) -> Verdict:
        floor = LK_FLOOR.get(code)
        if floor is None:
            if self.llm is None:
                return Verdict(self.unsure_s, f"lang-{code or 'und'}")
            try:
                done = await self.llm.finished(text, code)
            except Exception as e:  # noqa: BLE001
                self._warn("llm judge", e)
                return Verdict(self.unsure_s, "llm-failed")
            return Verdict(0.0 if done else self.max_s, "llm-yes" if done else "llm-no")
        try:
            p = await self.text_p(text)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._warn("text model", e)
            return Verdict(self.unsure_s, "text-failed")
        if p < floor:
            return Verdict(self.max_s, "unfinished", p)
        if p < self.fast:
            return Verdict(self.unsure_s, "unsure", p)
        pa = await audio if audio is not None else None
        if pa is not None and pa < self.audio_min:
            return Verdict(self.unsure_s, "audio-veto", p, pa)
        return Verdict(0.0, "complete", p, pa)

    async def aclose(self) -> None:
        if self.llm is not None:
            await self.llm.aclose()


def build_turn_detector(cfg: Any, executor: Any) -> TurnDetector | None:
    """``models.turn`` (engine ``livekit``) -> detector for one room; None = every VAD pause ends the turn."""
    m = getattr(cfg.models, "turn", None)
    if m is None or not m.enabled or executor is None:
        return None
    if m.engine != "livekit":
        log.warning("unknown end-of-turn engine %r: VAD pauses end turns", m.engine)
        return None
    if not files_present():
        return None
    llm = None
    t = cfg.models.translate
    if m.options.get("llm_fallback", True) and t.engine == "ollama" and t.url and t.model:
        llm = LlmJudge(t.url, t.model, t.options or {})
    return TurnDetector(executor, m.options, llm)


if __name__ == "__main__":  # python -m raum.pipeline.turn download
    if sys.argv[1:] == ["download"]:
        download()
    else:
        print("usage: python -m raum.pipeline.turn download   (fetches both models into HF_HOME)")
