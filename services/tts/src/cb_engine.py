"""Streaming wrapper around Chatterbox Multilingual (Resemble AI, MIT).

- Voices are built in memory from a reference waveform (never written to disk).
- `stream()` runs the T3 token loop itself and vocodes growing token prefixes,
  yielding float32 24 kHz audio chunks with a short crossfade between chunks.
- CFM noise is fixed (CosyVoice-style) so re-vocoded prefixes stay consistent.
"""
import os
os.environ.setdefault("TQDM_DISABLE", "1")
import time
import numpy as np
import torch
import torch.nn.functional as F
import librosa

from chatterbox.mtl_tts import Conditionals, punc_norm, SUPPORTED_LANGUAGES
from chatterbox.models.t3.modules.cond_enc import T3Cond
from chatterbox.models.s3tokenizer import S3_SR, drop_invalid_tokens
from chatterbox.models.s3gen import S3GEN_SR
from chatterbox.models.s3gen import flow_matching as _fm
from chatterbox.models.s3gen import S3Gen
from chatterbox.models.s3gen.s3gen import S3Token2Mel
from safetensors.torch import load_file as _load_st
from huggingface_hub import hf_hub_download
from transformers import StaticCache
from transformers.generation.logits_process import (
    TopPLogitsWarper, MinPLogitsWarper, RepetitionPenaltyLogitsProcessor)

SR = S3GEN_SR            # 24000
SAMPLES_PER_TOKEN = SR // 25   # 960 (25 speech tokens / s)
LOOKAHEAD = 3            # tokens the flow drops when finalize=False
# tokens before the first vocode. 10 (v1.4, was 14): first audio 192 -> 153 ms on a quiet GPU, 395 -> 262 ms under
# load (box, 2 Nov, chunk_tokens 12 unchanged)
FIRST_TOKENS = int(os.environ.get("RAUM_TTS_FIRST_TOKENS", "10"))


@torch.inference_mode()
def _fixed_noise_forward(self, mu, mask, n_timesteps, temperature=1.0, spks=None, cond=None,
                         noised_mels=None, meanflow=False):
    T = mu.size(2)
    buf = getattr(self, "_raum_noise", None)
    if buf is None or buf.size(2) < T or buf.device != mu.device:
        g = torch.Generator(device="cpu").manual_seed(1234)
        buf = torch.randn(1, mu.size(1), max(T, 6000), generator=g).to(mu.device)
        self._raum_noise = buf
    z = buf[:, :, :T].to(mu.dtype).expand(mu.size(0), -1, -1).contiguous()
    if noised_mels is not None:
        prompt_len = mu.size(2) - noised_mels.size(2)
        z[..., prompt_len:] = noised_mels
    t_span = torch.linspace(0, 1, n_timesteps + 1, device=mu.device, dtype=mu.dtype)
    if (not meanflow) and (self.t_scheduler == 'cosine'):
        t_span = 1 - torch.cos(t_span * 0.5 * torch.pi)
    if meanflow:
        return self.basic_euler(z, t_span=t_span, mu=mu, mask=mask, spks=spks, cond=cond), None
    return self.solve_euler(z, t_span=t_span, mu=mu, mask=mask, spks=spks, cond=cond, meanflow=meanflow), None


_fm.CausalConditionalCFM.forward = _fixed_noise_forward


class T3Graph:
    """CUDA-graph capture of one T3 (Llama) decode step on a transformers StaticCache."""

    def __init__(self, t3, B=2, max_len=1024, dtype=torch.bfloat16):
        self.t3, self.llama, self.B, self.L, self.dtype = t3, t3.tfmr, B, max_len, dtype
        cfg = self.llama.config
        dev = next(self.llama.parameters()).device
        self.cache = StaticCache(config=cfg, max_cache_len=max_len)
        kvh = getattr(cfg, "num_key_value_heads", None) or cfg.num_attention_heads
        hd = getattr(cfg, "head_dim", None) or cfg.hidden_size // cfg.num_attention_heads
        dummy = torch.zeros(B, kvh, 1, hd, dtype=dtype, device=dev)
        for layer in self.cache.layers:
            if not layer.is_initialized:
                layer.lazy_initialization(dummy, dummy)
        self.inp = torch.zeros(B, 1, cfg.hidden_size, dtype=dtype, device=dev)
        self.cpos = torch.zeros(1, dtype=torch.long, device=dev)
        self.pids = torch.zeros(B, 1, dtype=torch.long, device=dev)
        ar = torch.arange(max_len, device=dev)
        self.table = torch.where(ar[None, :] <= ar[:, None], 0.0, torch.finfo(dtype).min).to(dtype)
        self.mask = torch.zeros(1, 1, 1, max_len, dtype=dtype, device=dev)
        self.logits = None
        self.graph = None

    def _set_pos(self, pos):
        self.cpos.fill_(pos)
        self.pids.fill_(pos)
        self.mask.copy_(self.table[pos].view(1, 1, 1, -1))

    def _step(self):
        out = self.llama(inputs_embeds=self.inp, attention_mask=self.mask, past_key_values=self.cache,
                         cache_position=self.cpos, position_ids=self.pids, use_cache=True, return_dict=True)
        lg = self.t3.speech_head(out.last_hidden_state.float())
        if self.logits is None:
            self.logits = torch.empty_like(lg)
        self.logits.copy_(lg)

    @torch.inference_mode()
    def capture(self, pos=300):
        self._set_pos(pos)
        for _ in range(3):
            self._step()
        torch.cuda.synchronize()
        self.graph = torch.cuda.CUDAGraph()
        s = torch.cuda.Stream()
        s.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(s):
            self._step()
            torch.cuda.synchronize()
            with torch.cuda.graph(self.graph):
                self._step()
        torch.cuda.current_stream().wait_stream(s)
        torch.cuda.synchronize()
        self.cache.reset()

    @torch.inference_mode()
    def prefill(self, x):
        self.cache.reset()
        P = x.size(1)
        out = self.llama(inputs_embeds=x.to(self.dtype), past_key_values=self.cache,
                         cache_position=torch.arange(P, device=x.device), use_cache=True, return_dict=True)
        return self.t3.speech_head(out.last_hidden_state[:, -1:].float())

    def step(self, x, pos):
        self.inp.copy_(x)
        self._set_pos(pos)
        self.graph.replay()
        return self.logits


class CBEngine:
    def __init__(self, device="cuda", t3_model="v3", t3_dtype=torch.bfloat16, meanflow=True,
                 cuda_graph=True, max_len=768):
        """Lean loader: T3 (bf16) + one S3Gen decoder; no watermarker, no unused decoder."""
        from pathlib import Path
        from chatterbox.models.t3 import T3
        from chatterbox.models.t3.modules.t3_config import T3Config
        from chatterbox.models.tokenizers import MTLTokenizer
        from chatterbox.models.voice_encoder import VoiceEncoder
        import chatterbox.mtl_tts as _mt
        self.device = device
        repo = "ResembleAI/chatterbox"
        t3_file = _mt._resolve_multilingual_t3_model(t3_model)
        get = lambda f, r=repo: hf_hub_download(r, f)
        self.ve = VoiceEncoder()
        self.ve.load_state_dict(torch.load(get("ve.pt"), map_location="cpu", weights_only=True))
        self.ve.to(device).eval()

        def build(ctor, dtype):
            # Construct straight on the GPU so the CPU heap never holds (and fragments around) a model copy.
            prev = torch.get_default_dtype()
            torch.set_default_dtype(dtype)
            try:
                with torch.device(device):
                    return ctor()
            finally:
                torch.set_default_dtype(prev)

        t3 = build(lambda: T3(T3Config.multilingual()), t3_dtype)
        for name, child in t3.named_children():
            if name != "tfmr":
                child.float()             # only the Llama backbone runs in bf16
        st = _load_st(get(t3_file), device=device)
        if "model" in st.keys():
            st = st["model"][0]
        t3.load_state_dict(st)
        del st
        torch.cuda.empty_cache()
        self.t3 = t3.to(device).eval()
        self.meanflow = meanflow
        if meanflow:
            # Distilled 2-step token->mel decoder from Chatterbox-Turbo (MIT); same S3 token space.
            s3 = build(lambda: S3Gen(meanflow=True), torch.float32)
            sd = _load_st(get("s3gen_meanflow.safetensors", "ResembleAI/chatterbox-turbo"), device=device)
            s3.load_state_dict(sd, strict=True)
        else:
            s3 = build(lambda: S3Gen(), torch.float32)
            sd = torch.load(get("s3gen.pt"), map_location=device, weights_only=True)
            s3.load_state_dict(sd)
        del sd
        self.s3 = s3.to(device).eval()
        torch.cuda.empty_cache()
        self.tokenizer = MTLTokenizer(get("grapheme_mtl_merged_expanded_v1.json"))
        self.default_voice = Conditionals.load(get("conds.pt"), map_location="cpu").to(device)
        self.DEC_COND_LEN = _mt.ChatterboxMultilingualTTS.DEC_COND_LEN
        self.ENC_COND_LEN = _mt.ChatterboxMultilingualTTS.ENC_COND_LEN
        self.n_cfm_default = 2 if meanflow else 10
        self.t3_dtype = t3_dtype
        self.max_len = max_len
        self.graph = None
        if cuda_graph:
            self.graph = T3Graph(self.t3, B=2, max_len=max_len, dtype=t3_dtype)
            self.graph.capture()
        import gc, ctypes
        gc.collect()
        torch.cuda.empty_cache()
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass
        xf = 480  # 20 ms crossfade
        self.xf = xf
        self.fade_in = np.linspace(0.0, 1.0, xf, dtype=np.float32)
        self.fade_out = 1.0 - self.fade_in
        self.langs = sorted(SUPPORTED_LANGUAGES.keys())

    # ---------- voices ----------
    @torch.inference_mode()
    def make_voice(self, wav: np.ndarray, sr: int, exaggeration=0.5) -> Conditionals:
        wav = np.asarray(wav, dtype=np.float32).reshape(-1)
        w24 = librosa.resample(wav, orig_sr=sr, target_sr=SR) if sr != SR else wav
        ref16 = librosa.resample(w24, orig_sr=SR, target_sr=S3_SR)
        w24 = w24[: self.DEC_COND_LEN]
        gen = self.s3.embed_ref(w24, SR, device=self.device)
        prompt_tokens = None
        if plen := self.t3.hp.speech_cond_prompt_len:
            prompt_tokens, _ = self.s3.tokenizer.forward([ref16[: self.ENC_COND_LEN]], max_len=plen)
            prompt_tokens = torch.atleast_2d(prompt_tokens).to(self.device)
        ve = torch.from_numpy(self.ve.embeds_from_wavs([ref16], sample_rate=S3_SR))
        ve = ve.mean(axis=0, keepdim=True).to(self.device)
        t3c = T3Cond(speaker_emb=ve, cond_prompt_speech_tokens=prompt_tokens,
                     emotion_adv=exaggeration * torch.ones(1, 1, 1)).to(device=self.device)
        return Conditionals(t3c, gen)

    # ---------- synthesis ----------
    def _llm(self, x, past):
        out = self.t3.tfmr(inputs_embeds=x.to(self.t3_dtype), past_key_values=past,
                           use_cache=True, return_dict=True)
        return self.t3.speech_head(out.last_hidden_state.float()), out.past_key_values

    @torch.inference_mode()
    def _vocode(self, toks, gen, finalize, n_cfm, speed):
        t = torch.tensor(toks, dtype=torch.long, device=self.device).unsqueeze(0)
        mel = S3Token2Mel.forward(self.s3, t, ref_wav=None, ref_sr=None, ref_dict=gen,
                                  n_cfm_timesteps=n_cfm or self.n_cfm_default, finalize=finalize)
        mel = mel.to(dtype=self.s3.dtype)
        if speed and abs(speed - 1.0) > 1e-3:
            n = max(1, int(round(mel.size(2) / speed)))
            mel = F.interpolate(mel, size=n, mode="linear", align_corners=False)
        wav, _ = self.s3.hift_inference(mel, None)
        wav = wav[0]
        n = min(len(self.s3.trim_fade), wav.numel())
        wav[:n] *= self.s3.trim_fade[:n]
        return wav.float().cpu().numpy()

    @torch.inference_mode()
    def stream(self, text, lang, voice=None, *, cfg_weight=0.5, exaggeration=0.5, temperature=0.8,
               repetition_penalty=1.2, min_p=0.05, top_p=1.0, first_tokens=FIRST_TOKENS, chunk_tokens=12,
               grow=1.6, max_chunk_tokens=80, n_cfm=None, speed=1.0, max_new_tokens=600, stats=None,
               should_stop=None):
        """Yield float32 numpy chunks at 24 kHz. ``should_stop()`` is polled every token: True ends the
        stream at once (no final vocode). Hitting ``max_new_tokens`` without EOS sets stats['capped']."""
        lang = lang.lower()
        if lang not in SUPPORTED_LANGUAGES:
            raise ValueError(f"unsupported language {lang}")
        voice = voice or self.default_voice
        c3 = voice.t3
        if float(exaggeration) != float(c3.emotion_adv[0, 0, 0].item()):
            c3 = T3Cond(speaker_emb=c3.speaker_emb, cond_prompt_speech_tokens=c3.cond_prompt_speech_tokens,
                        emotion_adv=exaggeration * torch.ones(1, 1, 1)).to(device=self.device)
        t3, hp = self.t3, self.t3.hp
        tt = self.tokenizer.text_to_tokens(punc_norm(text), language_id=lang).to(self.device)
        use_cfg = cfg_weight > 0
        B = 2 if use_cfg else 1
        tt = tt.expand(B, -1)
        tt = F.pad(tt, (1, 0), value=hp.start_text_token)
        tt = F.pad(tt, (0, 1), value=hp.stop_text_token)
        start = hp.start_speech_token * torch.ones_like(tt[:, :1])
        embeds, _ = t3.prepare_input_embeds(t3_cond=c3, text_tokens=tt, speech_tokens=start,
                                            cfg_weight=cfg_weight)
        bos = torch.tensor([[hp.start_speech_token]], dtype=torch.long, device=self.device)
        bos_e = t3.speech_emb(bos) + t3.speech_pos_emb.get_fixed_embedding(0)
        x = torch.cat([embeds, bos_e.expand(B, -1, -1)], dim=1)

        rep = RepetitionPenaltyLogitsProcessor(penalty=float(repetition_penalty))
        minp = MinPLogitsWarper(min_p=min_p)
        topp = TopPLogitsWarper(top_p=top_p)
        gen_ids = bos.clone()
        toks = []
        if self.graph is not None:
            if use_cfg is False:
                x = x.expand(2, -1, -1)
            P = x.size(1)
            max_new_tokens = min(max_new_tokens, self.max_len - P - 2)
            logits_all, past = self.graph.prefill(x), None
        else:
            logits_all, past = self._llm(x, None)
        stop = hp.stop_speech_token
        emitted, prev_tail, next_emit = 0, None, first_tokens
        cur_chunk = chunk_tokens
        t0 = time.perf_counter()
        eos = False
        for i in range(max_new_tokens):
            if should_stop is not None and should_stop():
                if stats is not None:
                    stats["stopped"] = True
                    stats["tokens"] = len(toks)
                return
            lg = logits_all[:, -1, :]
            logits = lg[0:1] + cfg_weight * (lg[0:1] - lg[1:2]) if use_cfg else lg[0:1]
            logits = rep(gen_ids, logits)
            if temperature != 1.0:
                logits = logits / temperature
            logits = minp(gen_ids, logits)
            logits = topp(gen_ids, logits)
            nxt = torch.multinomial(torch.softmax(logits, dim=-1), num_samples=1)
            tok = int(nxt.item())
            if tok == stop:
                eos = True
                break
            gen_ids = torch.cat([gen_ids, nxt], dim=1)
            if tok < 6561:
                toks.append(tok)
            if len(toks) >= next_emit and len(toks) > LOOKAHEAD + 2:
                # flow's own finalize=False path is broken upstream (mask/len mismatch), so vocode
                # the prefix as final and hold back the last LOOKAHEAD tokens, which will change.
                w = self._vocode(toks, voice.gen, True, n_cfm, speed)
                cut = len(w) - int(LOOKAHEAD * SAMPLES_PER_TOKEN / (speed or 1.0))
                if cut > emitted + 2 * self.xf:
                    seg = w[emitted:cut].copy()
                    if prev_tail is not None:
                        n = min(self.xf, len(prev_tail), len(seg))
                        seg[:n] = prev_tail[:n] * self.fade_out[:n] + seg[:n] * self.fade_in[:n]
                    prev_tail = seg[-self.xf:].copy()
                    out = seg[:-self.xf]
                    emitted = cut - self.xf
                    if stats is not None and "ttfa" not in stats:
                        stats["ttfa"] = time.perf_counter() - t0
                    yield out
                next_emit = len(toks) + cur_chunk
                cur_chunk = min(max_chunk_tokens, int(cur_chunk * grow))
            e = t3.speech_emb(nxt) + t3.speech_pos_emb.get_fixed_embedding(i + 1)
            if self.graph is not None:
                logits_all = self.graph.step(e.expand(2, -1, -1), P + i)
            else:
                logits_all, past = self._llm(e.expand(B, -1, -1), past)
        if stats is not None:
            stats["tokens"] = len(toks)
            stats["t3_time"] = time.perf_counter() - t0
            stats["capped"] = not eos
        if should_stop is not None and should_stop():
            if stats is not None:
                stats["stopped"] = True
            return
        if not toks:
            return
        w = self._vocode(toks, voice.gen, True, n_cfm, speed)
        end = int((len(toks) - 1) * SAMPLES_PER_TOKEN / (speed or 1.0))
        w = w[:max(end, 0)]
        if len(w) > emitted:
            seg = w[emitted:].copy()
            if prev_tail is not None:
                n = min(self.xf, len(prev_tail), len(seg))
                seg[:n] = prev_tail[:n] * self.fade_out[:n] + seg[:n] * self.fade_in[:n]
            if stats is not None and "ttfa" not in stats:
                stats["ttfa"] = time.perf_counter() - t0
            yield seg

    def synth(self, text, lang, voice=None, **kw):
        kw.setdefault("first_tokens", 10**6)
        return np.concatenate(list(self.stream(text, lang, voice, **kw)) or [np.zeros(0, np.float32)])
