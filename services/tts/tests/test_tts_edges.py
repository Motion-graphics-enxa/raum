"""raum-tts edge trimming (src/edges.py, v1.4): fake engine audio with known silence, chunked as cb_engine streams it.

Checks the kept lengths, that sound is never cut or delayed (the first chunk with sound goes out whole, at once), that
a pause inside the clause survives, and that the edits start and end at zero (no clicks). Plain numpy, no model:
``cd core && uv run pytest ../services/tts/tests``."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
SR = 24000


def _load_edges():
    if "edges" in sys.modules:  # server.py imports it under this name
        return sys.modules["edges"]
    spec = importlib.util.spec_from_file_location("edges", SRC / "edges.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["edges"] = mod
    spec.loader.exec_module(mod)
    return mod


edges = _load_edges()


def ms(n: int) -> float:
    return n / SR * 1000.0


def speech(seconds: float, f: float = 220.0) -> np.ndarray:
    """A loud tone that starts and ends on full amplitude: any cut into it would show."""
    t = np.arange(int(seconds * SR)) / SR
    return (0.3 * np.cos(2 * np.pi * f * t)).astype(np.float32)


def quiet(seconds: float) -> np.ndarray:
    """Chatterbox's edge 'silence': low noise far under the -46 dBFS threshold."""
    rng = np.random.default_rng(int(seconds * 1000))
    return (rng.standard_normal(int(seconds * SR)) * 3e-4).astype(np.float32)


def clause(lead=0.12, words=(0.6, 0.5), pause=0.3, tail=0.6) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """lead silence, speech, a pause inside the clause, speech, the T3 tail; and the sample spans of the speech."""
    parts, spans, pos = [quiet(lead)], [], int(lead * SR)
    for i, w in enumerate(words):
        if i:
            parts.append(quiet(pause))
            pos += int(pause * SR)
        s = speech(w, 220.0 + 40 * i)
        parts.append(s)
        spans.append((pos, pos + len(s)))
        pos += len(s)
    parts.append(quiet(tail))
    return np.concatenate(parts), spans


def engine_chunks(w: np.ndarray, first_tokens=10, chunk_tokens=12, grow=1.6, max_chunk=80) -> list[np.ndarray]:
    """Cut ``w`` as cb_engine.stream does: first chunk after first_tokens (minus lookahead and crossfade), then
    growing chunks, then the rest at end of speech."""
    spt, out, pos, n = SR // 25, [], 0, first_tokens - 3
    cur = chunk_tokens
    while pos + n * spt < len(w):
        out.append(w[pos:pos + n * spt])
        pos += n * spt
        n, cur = cur, min(max_chunk, int(cur * grow))
    out.append(w[pos:])
    return out


def run(trim, chunks):
    outs = [trim.push(c) for c in chunks]
    tail = trim.end()
    return outs, tail, np.concatenate(outs + [tail])


@pytest.mark.parametrize("tail_s", [0.08, 0.20])
def test_both_edges_trimmed_to_the_kept_lengths_and_the_inner_pause_kept(tail_s):
    w, spans = clause()
    trim = edges.EdgeTrim(0.03, tail_s)
    _, _, y = run(trim, engine_chunks(w))
    lead_cut = spans[0][0] - int(0.03 * SR)
    assert abs(len(y) - (spans[-1][1] - lead_cut + int(tail_s * SR))) <= 1  # 30 ms + speech + pause + tail_s
    # every sample from 30 ms before the first sound to the last sound is the engine's, unchanged (the inner pause
    # too); only the 5 ms fade-in at the very start differs
    fade = int(edges.FADE_IN_S * SR)
    assert np.array_equal(y[fade:spans[-1][1] - lead_cut], w[lead_cut + fade:spans[-1][1]])
    assert trim.trimmed == len(w) - len(y)
    # what the listener hears between two clauses: about 110 ms after a comma (was ~0.7 s), 230 ms after a sentence
    assert ms(len(y) - (spans[-1][1] - lead_cut)) + 30 == pytest.approx(30 + tail_s * 1000, abs=0.1)


def test_no_click_at_either_edit():
    w, spans = clause()
    w[: spans[0][0]] += 0.004  # silence with an offset just under the threshold: a hard cut would step
    w[spans[-1][1]:] += 0.004
    _, tail, y = run(edges.EdgeTrim(0.03, 0.08), engine_chunks(w))
    lead_cut = spans[0][0] - int(0.03 * SR)
    assert abs(w[lead_cut]) > 0.003 and abs(w[lead_cut + len(y)]) > 0.003  # a hard cut would step there
    assert y[0] == 0.0 and y[-1] == 0.0  # both edits start/end at zero instead
    noise_step = np.abs(np.diff(w[spans[-1][1]:])).max()  # the steps the engine's own silence has
    assert np.abs(np.diff(y[: int(edges.FADE_IN_S * SR) + 1])).max() <= noise_step  # the fade-in: no bigger step
    assert np.abs(np.diff(tail)).max() <= noise_step and len(tail) == int(edges.FADE_OUT_S * SR)
    i = len(y) - len(tail)
    assert y[i - 1] == w[lead_cut + i - 1] and abs(y[i] - w[lead_cut + i]) < 1e-4  # continuous into the fade


def test_first_chunk_goes_out_at_once_and_sound_is_never_held():
    w, spans = clause(lead=0.05)
    chunks = engine_chunks(w)
    trim = edges.EdgeTrim(0.03, 0.08)
    lead_cut = spans[0][0] - int(0.03 * SR)
    first = trim.push(chunks[0])
    assert len(first) == len(chunks[0]) - lead_cut  # the first chunk, all of it after the lead cut: no extra latency
    got, fed = len(first), len(chunks[0])
    loud = np.flatnonzero(np.abs(w) > edges.THR)
    for c in chunks[1:]:
        got += len(trim.push(c))
        fed += len(c)
        heard = loud[loud < fed]
        if heard.size:  # every sound sample received so far has gone out
            assert got >= heard[-1] + 1 - lead_cut


def test_silence_at_a_chunk_end_is_held_then_sent_whole_when_speech_resumes():
    a, gap, b = speech(0.3), quiet(0.5), speech(0.3)
    trim = edges.EdgeTrim(0.03, 0.08)
    out1 = trim.push(np.concatenate([a, gap]))  # ends in silence: only up to 80 ms (minus the fade) goes out
    assert len(out1) == len(a) + int(0.08 * SR) - int(edges.FADE_OUT_S * SR)
    out2 = trim.push(b)  # speech again: the held 0.43 s of pause first, unchanged
    y = np.concatenate([out1, out2])
    assert np.array_equal(y, np.concatenate([a, gap, b])) and len(trim.end()) == 0


def test_off_and_edge_cases():
    w, _ = clause()
    off = edges.EdgeTrim(None, None)
    _, _, y = run(off, engine_chunks(w))
    assert np.array_equal(y, w) and off.trimmed == 0
    lead_only = edges.EdgeTrim(0.03, None)
    _, _, y = run(lead_only, engine_chunks(w))
    assert len(y) == len(w) - (int(0.12 * SR) - int(0.03 * SR))
    loud = speech(0.4)  # no silence at either edge (as the fake engine of test_tts_server.py): untouched
    _, _, y = run(edges.EdgeTrim(0.03, 0.08), engine_chunks(loud))
    assert np.array_equal(y, loud)
    mute = edges.EdgeTrim(0.03, 0.08)  # nothing audible at all: nothing goes out
    _, _, y = run(mute, engine_chunks(quiet(0.8)))
    assert len(y) == 0 and mute.trimmed == int(0.8 * SR)
