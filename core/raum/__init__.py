"""RAUM core."""

import os

# onnxruntime (Silero VAD in the agent's job processes) uploads 1DS telemetry to
# mobile.events.data.microsoft.com unless this is set. RAUM never talks to third parties (T-06).
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
