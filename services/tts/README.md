# raum-tts: the natural voice (Chatterbox Multilingual V3, MIT)

Box-local streaming TTS service on `127.0.0.1:8090` that RAUM's `tts: {engine: http}` talks to
(`core/raum/pipeline/tts_http.py`; Piper is the fallback). See `API.md` for the routes, the
concurrency model (v1.2: GPU released per job, 503 admission, caps; v1.3: `live` / `background`
request priorities with preemption, `src/admission.py`) and memory notes.

On the GX10 these files live in `~/Desktop/HackathonRaum/runtime/raum-tts/` next to the parts that
are not in git: `venvs/` (built in the NGC pytorch container), `hf-cache/` (model weights),
`voices/<lang>.wav` (default voice per language; only consented or licensed recordings), logs.

- `start.sh` / `stop.sh` / `status.sh`: container `raum-tts-svc` (refuses to start below 14 GB free).
- `guard.sh`: stops only this container if free memory stays under 8 GB (protects Ollama).
- `watchdog.sh`: restarts it if `/health` fails or a probe gets no audio while it reports busy.
- `src/regress.py`: regression run through RAUM's own `HttpTTS` client (needs `core/.venv`). Modes
  `seq` (default), `conc`, `edge`, `prio` (background never delays live), `all`.
- `tests/`: admission logic and `server.py` with a fake engine (no GPU, no model), run by core's
  `uv run pytest` (`testpaths` includes `../services/tts/tests`).
- Deploy note: `src/admission.py` is new in v1.3 and must be copied next to `server.py`.
