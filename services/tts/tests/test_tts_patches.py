"""cb_patches.py without a GPU or Chatterbox: the sync-free replacements against upstream-equivalent references
(copied from chatterbox / torch semantics), and T3Sampler's batched EOS handling on a fake engine.

Needs torch (CPU is enough); skipped where it is missing (core's venv): ``uv run --with torch --with numpy
--with pytest pytest services/tts/tests/test_tts_patches.py``."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import cb_patches as CP  # noqa: E402


# ---- upstream references (chatterbox/models/s3gen/utils/mask.py) ----
def ref_make_pad_mask(lengths, max_len=0):
    lengths = lengths.long()
    max_len = max_len if max_len > 0 else int(lengths.max().item())
    seq = torch.arange(0, max_len, dtype=torch.int64, device=lengths.device)
    return seq.unsqueeze(0).expand(lengths.size(0), max_len) >= lengths.unsqueeze(-1)


def ref_fixup(chunk_masks):
    chunk_masks = chunk_masks.clone()
    if (chunk_masks.sum(dim=-1) == 0).sum().item() != 0:
        chunk_masks[chunk_masks.sum(dim=-1) == 0] = True
    return chunk_masks


def test_make_pad_mask_matches_upstream_at_full_width():
    lens = torch.tensor([7, 5, 7])
    assert torch.equal(CP.make_pad_mask(lens, 7), ref_make_pad_mask(lens))


@pytest.mark.parametrize("shape", [(1, 1, 9), (2, 9, 9), (3, 1, 4)])
def test_chunk_mask_fixup_matches_upstream(shape):
    g = torch.Generator().manual_seed(0)
    for _ in range(20):
        m = torch.rand(shape, generator=g) > 0.7          # some rows all False
        out = CP.add_optional_chunk_mask(torch.zeros(shape[0], shape[-1], 4), m, False, False, 0, 0, -1)
        assert torch.equal(out, ref_fixup(m))


def test_istft_matches_torch_istft():
    g = torch.Generator().manual_seed(1)
    n_fft, hop, T = 16, 4, 37
    win = torch.hann_window(n_fft, periodic=True)
    spec = torch.complex(torch.randn(2, n_fft // 2 + 1, T, generator=g), torch.randn(2, n_fft // 2 + 1, T, generator=g))
    ref = torch.istft(spec, n_fft, hop, n_fft, window=win)
    out = CP.istft(spec, n_fft, hop, win)
    assert out.shape == ref.shape
    assert torch.allclose(out, ref, atol=1e-6, rtol=1e-5)


def test_exponential_sample_is_multinomial():
    probs = torch.softmax(torch.randn(1, 8194, generator=torch.Generator().manual_seed(2)) * 3, dim=-1)
    for seed in range(10):
        a = torch.multinomial(probs, 1, generator=torch.Generator().manual_seed(seed))
        b = CP.sample_exponential(probs, torch.Generator().manual_seed(seed))
        assert torch.equal(a, b)


def test_tokens_to_device_on_cpu():
    t = CP.tokens_to_device([3, 1, 4], "cpu")
    assert t.dtype == torch.long and t.shape == (1, 3) and t.tolist() == [[3, 1, 4]]


# ---- T3Sampler on a fake engine ----
V, STOP = 16, 15


class FakeT3:
    """Logits that make a deterministic token stream: token i+1 = SEQ[i] (one-hot logits)."""

    def __init__(self, seq):
        self.seq = seq
        self.speech_emb = lambda nxt: torch.zeros(1, 1, 4)
        self.speech_pos_emb = types.SimpleNamespace(emb=types.SimpleNamespace(weight=torch.zeros(1024, 4)),
                                                    get_fixed_embedding=lambda i: torch.zeros(1, 1, 4))


class FakeEngine:
    graph = None

    def __init__(self, seq):
        self.t3 = FakeT3(seq)
        self.steps = 0

    def _llm(self, x, past):
        self.steps += 1
        k = past + 1
        return onehot(self.t3.seq[min(k, len(self.t3.seq) - 1)]), k


def onehot(tok):
    lg = torch.full((2, 1, V), -1e9)
    lg[:, :, tok] = 10.0
    return lg


def make(seq, every, legacy=False, max_new=50):
    eng = FakeEngine(seq)
    ident = lambda ids, s: s  # noqa: E731
    s = CP.T3Sampler(eng, onehot(seq[0]), torch.zeros(1, 1, dtype=torch.long), P=None, B=2, past=0, max_new=max_new,
                     cfg_weight=0.0, use_cfg=False, procs=(ident, ident, ident), temperature=1.0, every=every,
                     legacy=legacy, generator=torch.Generator().manual_seed(0) if not legacy else None)
    return eng, s


def consume(s, stop=STOP):
    got = []
    for i, tok in s:
        if tok == stop:
            break
        got.append((i, tok))
    return got


@pytest.mark.parametrize("every", [1, 2, 3, 4])
def test_sampler_yields_same_tokens_for_any_batch(every):
    seq = [1, 2, 3, 4, 5, 6, 7, STOP, 9, 9, 9, 9, 9]
    eng, s = make(seq, every)
    got = consume(s)
    assert got == [(i, t) for i, t in enumerate(seq[:7])]
    assert 8 <= eng.steps <= 8 + every - 1          # EOS overshoot is at most every-1 steps
    eng_l, s_l = make(seq, 1, legacy=True)
    assert consume(s_l) == got


def test_sampler_until_ends_the_batch_at_the_chunk_boundary():
    seq = list(range(1, 14)) + [STOP]
    eng, s = make(seq, every=4)
    s.until(6)
    it = iter(s)
    for _ in range(6):
        next(it)
    assert s.issued == 6                               # batches [0-3], [4-5]: token 6 seen without overshoot
    s.until(5)
    for _ in range(5):
        next(it)
    assert s.issued == 11


def test_sampler_respects_max_new():
    seq = [1] * 40
    eng, s = make(seq, every=4, max_new=10)
    assert len(consume(s)) == 10 and eng.steps == 10


def test_apply_revert_without_chatterbox_is_harmless(monkeypatch):
    """apply() patches chatterbox modules; with them stubbed it swaps and restores every target."""
    mods = {}
    for name in ("chatterbox", "chatterbox.models", "chatterbox.models.s3gen", "chatterbox.models.s3gen.decoder",
                 "chatterbox.models.s3gen.flow", "chatterbox.models.s3gen.hifigan", "chatterbox.models.s3gen.s3gen",
                 "chatterbox.models.s3gen.transformer", "chatterbox.models.s3gen.transformer.upsample_encoder",
                 "chatterbox.models.s3gen.utils", "chatterbox.models.s3gen.utils.mask"):
        mods[name] = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, mods[name])
    for parent, child in [(n.rsplit(".", 1)) for n in mods if "." in n]:
        setattr(mods[parent], child, mods[f"{parent}.{child}"])
    orig = object()
    for m in ("decoder", "transformer.upsample_encoder", "utils.mask"):
        setattr(mods[f"chatterbox.models.s3gen.{m}"], "add_optional_chunk_mask", orig)
    classes = {"flow": ["CausalMaskedDiffWithXvec"], "s3gen": ["S3Token2Mel", "S3Token2Wav"],
               "hifigan": ["SineGen", "HiFTGenerator"]}
    for mod, names in classes.items():
        for n in names:
            setattr(mods[f"chatterbox.models.s3gen.{mod}"], n,
                    type(n, (), {k: orig for k in ("inference", "forward", "hift_inference", "_stft", "_istft")}))
    monkeypatch.setattr(CP, "_ORIG", {})
    monkeypatch.setattr(CP, "_active", False)
    assert CP.apply(True) and CP.active()
    assert mods["chatterbox.models.s3gen.decoder"].add_optional_chunk_mask is CP.add_optional_chunk_mask
    assert mods["chatterbox.models.s3gen.hifigan"].HiFTGenerator._istft is CP.hift_istft
    CP.revert()
    assert not CP.active()
    assert mods["chatterbox.models.s3gen.decoder"].add_optional_chunk_mask is orig
    assert mods["chatterbox.models.s3gen.hifigan"].HiFTGenerator._istft is orig
    assert CP.apply(False) is False
