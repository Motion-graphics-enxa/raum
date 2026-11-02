"""Host<->GPU sync-free hot paths for Chatterbox (S3Gen flow, HiFT vocoder) and a GPU-side T3 sampler.

Why: the GB10 GPU is time-sliced across ~9 processes. Every host sync (``.item()``, ``.any()``, an H2D copy from
pageable memory, the window check inside ``torch.istft``) waits for our queue to drain and then for the next time
slice: 10-18 ms each under load. One S3Gen chunk decode had ~42 of them, and T3 had one per token.

What changes (``apply()``; ``revert()`` restores the library, both idempotent):
- ``mask.add_optional_chunk_mask``: the "row of all False -> all True" fix-up is a tensor op, not ``.item()``.
- ``CausalMaskedDiffWithXvec.inference``: no ``.any()`` range check (it only fed a log line); pad masks are built
  with the known width (``make_pad_mask(len, T)``) instead of ``lengths.max().item()``.
- ``S3Token2Mel.forward``: token lengths are made on the device, not ``LongTensor([...]).to(cuda)``.
- ``SineGen.forward``: harmonics built on the device; the CPU-drawn random phases (same RNG as upstream) go up
  through pinned memory without blocking.
- ``HiFTGenerator._stft/_istft``: the Hann window is cached on the device; iSTFT is the same irfft + overlap-add
  that ``torch.istft`` does, minus its ``min(envelope) > 1e-11`` host check.
- ``T3Sampler``: samples on the GPU (the exponential trick ``torch.multinomial`` itself uses for one sample) and
  copies tokens to the host only every ``RAUM_TTS_EOS_EVERY`` steps (EOS overshoot is dropped by the caller).

Outputs are bit-identical to upstream except the iSTFT (float rounding, ~1e-7). Flags: ``RAUM_TTS_SYNCFREE=0``
turns all of it off (upstream code paths, ``torch.multinomial`` + one ``.item()`` per token);
``RAUM_TTS_EOS_EVERY`` (default 2, 1 = per token; 2 beat 4 on TTFA and stalls under load).
"""
from __future__ import annotations

import os

import torch
import torch.nn.functional as F

ENABLED = os.environ.get("RAUM_TTS_SYNCFREE", "1") == "1"
EOS_EVERY = max(1, int(os.environ.get("RAUM_TTS_EOS_EVERY", "2") or 2))

# T3 sampler RNG: "own" = a CUDA generator per stream seeded from the CPU RNG (tokens do not depend on
# EOS_EVERY); "global" = torch's default CUDA generator (with EOS_EVERY=1 that reproduces upstream exactly).
SAMPLER_RNG = "own"

_ORIG: dict = {}
_active = False


# ---------------------------------------------------------------- S3Gen flow / masks
def add_optional_chunk_mask(xs, masks, use_dynamic_chunk, use_dynamic_left_chunk, decoding_chunk_size,
                            static_chunk_size, num_decoding_left_chunks, enable_full_context=True):
    if use_dynamic_chunk:  # training only (random chunk sizes): keep upstream
        return _ORIG["add_optional_chunk_mask"](xs, masks, use_dynamic_chunk, use_dynamic_left_chunk,
                                                decoding_chunk_size, static_chunk_size, num_decoding_left_chunks,
                                                enable_full_context)
    if static_chunk_size > 0:
        from chatterbox.models.s3gen.utils import mask as _mask
        cm = _mask.subsequent_chunk_mask(xs.size(1), static_chunk_size, num_decoding_left_chunks, xs.device)
        chunk_masks = masks & cm.unsqueeze(0)
    else:
        chunk_masks = masks
    assert chunk_masks.dtype == torch.bool
    # upstream: if any row is all False (host check), set that row all True. Same thing, on the device.
    return chunk_masks | ~chunk_masks.any(dim=-1, keepdim=True)


def make_pad_mask(lengths, max_len):
    """``mask.make_pad_mask`` with the width given (no ``lengths.max().item()``)."""
    seq = torch.arange(0, max_len, dtype=torch.int64, device=lengths.device)
    return seq.unsqueeze(0).expand(lengths.size(0), max_len) >= lengths.long().unsqueeze(-1)


@torch.inference_mode()
def flow_inference(self, token, token_len, prompt_token, prompt_token_len, prompt_feat, prompt_feat_len,
                   embedding, finalize, n_timesteps=10, noised_mels=None, meanflow=False):
    from chatterbox.models.s3gen.flow import _repeat_batch_dim
    B = token.size(0)
    embedding = torch.atleast_2d(embedding)
    embedding = F.normalize(embedding, dim=1)
    embedding = self.spk_embed_affine_layer(embedding)
    prompt_token = _repeat_batch_dim(prompt_token, B, ndim=2)
    prompt_token_len = _repeat_batch_dim(prompt_token_len, B, ndim=1)
    prompt_feat = _repeat_batch_dim(prompt_feat, B, ndim=3)
    embedding = _repeat_batch_dim(embedding, B, ndim=2)

    token, token_len = torch.concat([prompt_token, token], dim=1), prompt_token_len + token_len
    mask = (~make_pad_mask(token_len, token.size(1))).unsqueeze(-1).to(embedding)
    # upstream checks (token >= vocab_size).any() here only to log; T3 tokens are filtered < 6561 by the caller.
    token = self.input_embedding(token.long()) * mask

    h, h_masks = self.encoder(token, token_len)
    if finalize is False:
        h = h[:, :-self.pre_lookahead_len * self.token_mel_ratio]
    h_lengths = h_masks.sum(dim=-1).squeeze(dim=-1)
    mel_len1, mel_len2 = prompt_feat.shape[1], h.shape[1] - prompt_feat.shape[1]
    h = self.encoder_proj(h)

    conds = torch.zeros([B, mel_len1 + mel_len2, self.output_size], device=token.device).to(h.dtype)
    conds[:, :mel_len1] = prompt_feat
    conds = conds.transpose(1, 2)
    mask = (~make_pad_mask(h_lengths, h.size(1))).unsqueeze(1).to(h)
    if mask.shape[0] != B:
        mask = mask.repeat(B, 1, 1)
    feat, _ = self.decoder(mu=h.transpose(1, 2).contiguous(), mask=mask, spks=embedding, cond=conds,
                           n_timesteps=n_timesteps, noised_mels=noised_mels, meanflow=meanflow)
    feat = feat[:, :, mel_len1:]
    assert feat.shape[2] == mel_len2
    return feat, None


def s3token2mel_forward(self, speech_tokens, ref_wav, ref_sr, ref_dict=None, n_cfm_timesteps=None,
                        finalize=False, speech_token_lens=None, noised_mels=None):
    speech_tokens = torch.atleast_2d(speech_tokens)
    if speech_token_lens is None and ref_dict is not None:
        speech_token_lens = torch.full((speech_tokens.size(0),), speech_tokens.size(-1), dtype=torch.long,
                                       device=speech_tokens.device)
    return _ORIG["S3Token2Mel.forward"](self, speech_tokens, ref_wav, ref_sr, ref_dict=ref_dict,
                                        n_cfm_timesteps=n_cfm_timesteps, finalize=finalize,
                                        speech_token_lens=speech_token_lens, noised_mels=noised_mels)


# ---------------------------------------------------------------- HiFT vocoder
def to_device(t, device):
    """CPU tensor -> device without a blocking pageable copy (pinned staging; the caching host allocator keeps the
    staging block alive until the copy is done)."""
    if not torch.cuda.is_available() or torch.device(device).type != "cuda":
        return t.to(device)
    return t.pin_memory().to(device, non_blocking=True)


def tokens_to_device(toks, device):
    """Python token list -> (1, T) long tensor on ``device``. Upstream path (``torch.tensor(..., device=)``)
    is a blocking H2D copy."""
    if not _active:
        return torch.tensor(toks, dtype=torch.long, device=device).unsqueeze(0)
    return to_device(torch.tensor(toks, dtype=torch.long), device).unsqueeze(0)


@torch.no_grad()
def sinegen_forward(self, f0):
    import numpy as np
    from torch.distributions.uniform import Uniform
    H = self.harmonic_num + 1
    harm = torch.arange(1, H + 1, device=f0.device, dtype=torch.float32).view(1, H, 1)
    F_mat = f0 * harm / self.sampling_rate                                   # == upstream's per-harmonic loop
    theta_mat = 2 * np.pi * (torch.cumsum(F_mat, dim=-1) % 1)
    u_dist = Uniform(low=-np.pi, high=np.pi)                                 # CPU RNG, as upstream
    phase_vec = to_device(u_dist.sample(sample_shape=(f0.size(0), H, 1)), F_mat.device)
    phase_vec[:, 0, :] = 0
    sine_waves = self.sine_amp * torch.sin(theta_mat + phase_vec)
    uv = self._f02uv(f0)
    noise_amp = uv * self.noise_std + (1 - uv) * self.sine_amp / 3
    noise = noise_amp * torch.randn_like(sine_waves)
    sine_waves = sine_waves * uv + noise
    return sine_waves, uv, noise


def _window(self, device):
    w = getattr(self, "_raum_win", None)
    if w is None or w.device != device:
        w = self.stft_window.to(device)
        self._raum_win = w
    return w


def hift_stft(self, x):
    n_fft = self.istft_params["n_fft"]
    spec = torch.stft(x, n_fft, self.istft_params["hop_len"], n_fft, window=_window(self, x.device),
                      return_complex=True)
    spec = torch.view_as_real(spec)
    return spec[..., 0], spec[..., 1]


def istft(spec, n_fft, hop, window):
    """``torch.istft(spec, n_fft, hop, n_fft, window)`` (center=True, onesided, length=None) without its host-side
    envelope check. spec: (B, n_fft//2+1, T) complex -> (B, hop*(T-1))."""
    B, _, T = spec.shape
    frames = torch.fft.irfft(spec.transpose(1, 2), n_fft, dim=-1)           # (B, T, n_fft)
    frames = frames * window.view(1, 1, n_fft)
    L = n_fft + hop * (T - 1)
    y = torch.ops.aten.unfold_backward(frames, [B, L], 1, n_fft, hop)
    env = torch.ops.aten.unfold_backward(window.pow(2).expand(1, T, n_fft), [1, L], 1, n_fft, hop)
    a, b = n_fft // 2, L - n_fft // 2
    return y[:, a:b] / env[:, a:b]


def hift_istft(self, magnitude, phase):
    magnitude = torch.clip(magnitude, max=1e2)
    real = magnitude * torch.cos(phase)
    img = magnitude * torch.sin(phase)
    return istft(torch.complex(real, img), self.istft_params["n_fft"], self.istft_params["hop_len"],
                 _window(self, magnitude.device))


def s3_hift_inference(self, speech_feat, cache_source=None):
    if cache_source is None:
        cache_source = torch.zeros(1, 1, 0, device=self.device, dtype=self.dtype)
    return self.mel2wav.inference(speech_feat=speech_feat, cache_source=cache_source)


# ---------------------------------------------------------------- apply / revert
def _targets():
    from chatterbox.models.s3gen import decoder, flow, hifigan, s3gen
    from chatterbox.models.s3gen.transformer import upsample_encoder
    from chatterbox.models.s3gen.utils import mask
    return [
        (mask, "add_optional_chunk_mask", add_optional_chunk_mask, "add_optional_chunk_mask"),
        (decoder, "add_optional_chunk_mask", add_optional_chunk_mask, None),
        (upsample_encoder, "add_optional_chunk_mask", add_optional_chunk_mask, None),
        (flow.CausalMaskedDiffWithXvec, "inference", flow_inference, None),
        (s3gen.S3Token2Mel, "forward", s3token2mel_forward, "S3Token2Mel.forward"),
        (s3gen.S3Token2Wav, "hift_inference", s3_hift_inference, None),
        (hifigan.SineGen, "forward", sinegen_forward, None),
        (hifigan.HiFTGenerator, "_stft", hift_stft, None),
        (hifigan.HiFTGenerator, "_istft", hift_istft, None),
    ]


def apply(enable: bool | None = None) -> bool:
    """Install the sync-free versions (``enable`` defaults to RAUM_TTS_SYNCFREE). Returns whether active."""
    global _active
    enable = ENABLED if enable is None else enable
    if not enable:
        revert()
        return False
    if _active:
        return True
    for obj, name, new, key in _targets():
        _ORIG.setdefault((id(obj), name), getattr(obj, name))
        if key:
            _ORIG.setdefault(key, getattr(obj, name))
        setattr(obj, name, new)
    _active = True
    return True


def revert():
    global _active
    if not _active:
        return
    for obj, name, _new, _key in _targets():
        setattr(obj, name, _ORIG[(id(obj), name)])
    _active = False


def active() -> bool:
    return _active


# ---------------------------------------------------------------- T3 sampling
def sample_exponential(probs, generator=None):
    """One sample per row, exactly as ``torch.multinomial(probs, 1)`` draws it (argmax of p / Exp(1)), minus its
    host-side validity check. Same generator state -> same token."""
    q = torch.empty_like(probs).exponential_(1, generator=generator)
    return torch.argmax(probs / q, dim=-1, keepdim=True)


class T3Sampler:
    """The T3 decode loop: sample, feed the token back, repeat. Iterating yields ``(i, token)`` in order.

    Sync-free mode (``every`` >= 1, patches active): tokens are sampled on the GPU and copied to the host in batches
    of ``every`` steps (one sync per batch), so up to ``every - 1`` steps past EOS are computed and dropped when the
    caller breaks. ``until(k)`` ends the current batch after ``k`` more steps (so a chunk boundary is seen at once).
    A dedicated CUDA generator (seeded from the CPU RNG) keeps the token sequence independent of ``every`` and of
    vocoder RNG use. Legacy mode (``legacy=True``): upstream ``torch.multinomial`` + ``.item()`` every step.
    """

    def __init__(self, eng, logits, gen_ids, *, P, B, past, max_new, cfg_weight, use_cfg, procs, temperature,
                 every=None, legacy=None, generator=None):
        self.eng, self.logits, self.gen_ids = eng, logits, gen_ids
        self.P, self.B, self.past, self.max_new = P, B, past, max_new
        self.cfg_weight, self.use_cfg, self.procs, self.temperature = cfg_weight, use_cfg, procs, temperature
        self.legacy = (not _active) if legacy is None else legacy
        self.every = 1 if self.legacy else (EOS_EVERY if every is None else max(1, int(every)))
        self._until = None
        self.seen = 0                                                 # tokens handed to the caller so far
        t3 = eng.t3
        self.pos = t3.speech_pos_emb.emb.weight                       # get_fixed_embedding(i) == pos[i]
        self.gen = None
        generator = SAMPLER_RNG if generator is None else generator
        if not self.legacy and generator == "own":
            self.gen = torch.Generator(device=logits.device)
            self.gen.manual_seed(int(torch.randint(0, 2 ** 62, (1,)).item()))   # CPU RNG: no device sync
        elif not self.legacy and generator is not None and generator != "global":
            self.gen = generator
        self.issued = 0

    def until(self, k):
        """The caller acts after ``k`` more tokens: end the batch that contains that token right there."""
        self._until = self.seen + max(1, int(k))

    def _probs(self):
        lg = self.logits[:, -1, :]
        logits = lg[0:1] + self.cfg_weight * (lg[0:1] - lg[1:2]) if self.use_cfg else lg[0:1]
        rep, minp, topp = self.procs
        logits = rep(self.gen_ids, logits)
        if self.temperature != 1.0:
            logits = logits / self.temperature
        logits = minp(self.gen_ids, logits)
        logits = topp(self.gen_ids, logits)
        return torch.softmax(logits, dim=-1)

    def _feed(self, nxt, i):
        eng, t3 = self.eng, self.eng.t3
        if self.legacy:
            e = t3.speech_emb(nxt) + t3.speech_pos_emb.get_fixed_embedding(i + 1)
        else:
            e = t3.speech_emb(nxt) + self.pos[i + 1].view(1, 1, -1)
        if eng.graph is not None:
            self.logits = eng.graph.step(e.expand(2, -1, -1), self.P + i)
        else:
            self.logits, self.past = eng._llm(e.expand(self.B, -1, -1), self.past)

    def __iter__(self):
        while self.issued < self.max_new:
            if self.legacy:
                nxt = torch.multinomial(self._probs(), num_samples=1)
                tok = int(nxt.item())
                self.seen = self.issued + 1
                yield self.issued, tok
                self.gen_ids = torch.cat([self.gen_ids, nxt], dim=1)
                self._feed(nxt, self.issued)
                self.issued += 1
                continue
            n = min(self.every, self.max_new - self.issued)
            if self._until is not None and self._until > self.issued:
                n = min(n, self._until - self.issued)
            i0 = self.issued
            outs = []
            for k in range(n):
                nxt = sample_exponential(self._probs(), self.gen)
                outs.append(nxt)
                self.gen_ids = torch.cat([self.gen_ids, nxt], dim=1)
                self._feed(nxt, i0 + k)
            self.issued = i0 + n
            host = torch.cat(outs, dim=1)[0].tolist() if n > 1 else [int(outs[0].item())]   # the one sync
            for k, tok in enumerate(host):
                self.seen = i0 + k + 1
                yield i0 + k, tok
