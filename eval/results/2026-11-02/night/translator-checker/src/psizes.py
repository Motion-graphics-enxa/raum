import json, sys, urllib.request
try:
    ms = json.load(urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=10)).get("models", [])
except Exception as e:  # noqa: BLE001
    sys.exit(f"ps error {e!r}")
print("; ".join("%s %.1f GiB (vram %.1f) ctx %s" % (m["name"], m["size"] / 2**30, m.get("size_vram", 0) / 2**30,
                                                    m.get("context_length")) for m in ms))
