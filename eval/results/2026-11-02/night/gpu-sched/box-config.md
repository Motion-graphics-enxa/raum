# GPU-level options on the GX10 (NOT tonight: every one needs a restart of Ollama / whisper / raum-tts)

Box facts (20:33): driver 580.173.02, CUDA 13.0, compute mode Default, persistence on, no MIG,
/usr/bin/nvidia-cuda-mps-control installed, no MPS daemon running. Ollama runners: e4b and gpt-oss:20b
both `-np 4 -b 1024 -ub 1024 --flash-attn auto` (e4b with MTP draft). Another team's cocoindex python
holds 4.5 GB on the GPU (not ours; it also time-slices).

## 1. CUDA MPS, user space, our clients only (needs a GPU window + restarts; untested on GB10)
Without MPS, contexts from different processes are time-sliced: the TTS T3 step (tiny kernels) waits
for other contexts' slices -> 8 ms becomes 26 ms. With MPS all clients share one context, kernels run
concurrently, and loads can be capped (SM %) and deprioritised.
```
R=$HOME/Desktop/HackathonRaum/runtime/mps; mkdir -p $R/pipe $R/log
CUDA_MPS_PIPE_DIRECTORY=$R/pipe CUDA_MPS_LOG_DIRECTORY=$R/log nvidia-cuda-mps-control -d
# test first with a throwaway container (no prod service):
docker run --rm --gpus all --ipc=host -v $R/pipe:$R/pipe -e CUDA_MPS_PIPE_DIRECTORY=$R/pipe \
  nvcr.io/nvidia/pytorch:26.07-py3 python -c "import torch;print(torch.ones(1,device='cuda')+1)"
cat $R/log/control.log $R/log/server.log      # 'New client' lines = MPS works on GB10
# clients (each needs its restart):
#  Ollama  (ollama-env.sh): CUDA_MPS_PIPE_DIRECTORY=$R/pipe CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=60 CUDA_MPS_CLIENT_PRIORITY=1
#  whisper (box.sh env):    CUDA_MPS_PIPE_DIRECTORY=$R/pipe CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=40 CUDA_MPS_CLIENT_PRIORITY=1
#  raum-tts start.sh:       --ipc=host -v $R/pipe:$R/pipe -e CUDA_MPS_PIPE_DIRECTORY=$R/pipe   (100 %, priority 0)
# stop: echo quit | CUDA_MPS_PIPE_DIRECTORY=$R/pipe nvidia-cuda-mps-control
```
Risks: one client's GPU fault can take the MPS server and every client down with it; MPS on the
integrated GB10 is unverified; a process not pointed at our pipe dir (other teams) keeps time-slicing
against the MPS server as one context. Re-run bench.py (ALL vs POLICY) before and after.

## 2. Ollama (restart needed -> after the hackathon or in a quiet window)
- `OLLAMA_NUM_PARALLEL=2` (now 4): live + speculative MT is 2; fewer sequences per decode step.
- `num_batch: 256` in spark.yaml translate/vision/verify options: prompt prefill runs in 256-token
  kernels instead of 1024, so the voice gets the GPU between them. Changing num_batch RELOADS the runner
  (Ollama compares it), so set it in all three entries at once, at deploy time, never per request.
- Flash attention is already on (`--flash-attn auto`). Not measured tonight (would reload prod models).

## 3. whisper.cpp
Partials (:8081) cost ~360 ms each under the voice; 2 rooms of partials alone push the voice to rtf 0.83.
The pipeline gate (01-voice-gate.diff) spaces them; server flags (-t, beam) don't change GPU time slices.

## 4. Not useful
- CUDA stream priorities in the TTS process: they only order streams inside one context, and the
  contention is between processes (and tts-speed found extra CUDA streams crash the engine).
- nice / ionice / cgroup cpu.weight: CPU only.
