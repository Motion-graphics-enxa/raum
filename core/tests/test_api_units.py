"""Privacy meter (T-06), sealed mode (T-07), GPU stats (G-02), LiveKit publisher (fail soft)."""

from __future__ import annotations

import json
import os

import pytest

from raum.config import load_config
from raum.events import make_event
from raum.gpu import GpuMonitor, parse_nvidia_smi
from raum.privacy import meter as meter_mod
from raum.privacy.meter import PrivacyMeterReader, parse_nft_counters
from raum.privacy.seal import SealController, find_seal_rule, flip_expr, replace_command
from raum.publisher import LiveKitPublisher, http_url

pytestmark = pytest.mark.filterwarnings("ignore:The HMAC key")

NFT_COUNTERS = {"nftables": [
    {"metainfo": {"version": "1.0.9", "json_schema_version": 1}},
    {"counter": {"family": "inet", "name": "third_party", "table": "raum", "handle": 2, "packets": 3, "bytes": 180}},
    {"counter": {"family": "inet", "name": "third_party_fwd", "table": "raum", "handle": 3, "packets": 0,
                 "bytes": 20}},
    {"counter": {"family": "inet", "name": "tunnel", "table": "raum", "handle": 4, "packets": 9, "bytes": 9000}},
    {"counter": {"family": "inet", "name": "sealed_drop", "table": "raum", "handle": 5, "packets": 1,
                 "bytes": 60}},
]}


def test_parse_nft_counters():
    assert parse_nft_counters(json.dumps(NFT_COUNTERS)) == (200, 60)
    assert parse_nft_counters({"nftables": []}) == (0, 0)


async def test_meter_stub_and_tunnel(tmp_path, monkeypatch):
    stats = tmp_path / "tailscale0" / "statistics"
    stats.mkdir(parents=True)
    (stats / "rx_bytes").write_text("1000\n")
    (stats / "tx_bytes").write_text("500\n")
    monkeypatch.setattr(meter_mod, "nft_available", lambda: False)
    m = PrivacyMeterReader(sysfs=tmp_path)
    first = await m.read(sealed=False)
    assert first.third_party_bytes == 0 and first.tunnel_bytes == 0 and first.source == "stub"
    (stats / "rx_bytes").write_text("3048\n")
    second = await m.read(sealed=True)
    assert second.tunnel_bytes == 2048 and second.sealed is True  # counted from core start


async def test_meter_nft(monkeypatch, tmp_path):
    async def fake_nft(*args, **kw):
        assert args[:5] == ("-j", "list", "counters", "table", "inet")
        return 0, json.dumps(NFT_COUNTERS), ""

    monkeypatch.setattr(meter_mod, "nft_available", lambda: True)
    monkeypatch.setattr(meter_mod, "nft", fake_nft)
    m = await PrivacyMeterReader(sysfs=tmp_path).read(sealed=False)
    assert m.source == "nft" and m.third_party_bytes == 200 and m.model_extra["blocked_bytes"] == 60


RULESET = {"nftables": [
    {"table": {"family": "inet", "name": "raum", "handle": 1}},
    {"rule": {"family": "inet", "table": "raum", "chain": "output", "handle": 7, "comment": "count",
              "expr": [{"counter": {"packets": 0, "bytes": 0}}]}},
    {"rule": {"family": "inet", "table": "raum", "chain": "output", "handle": 9, "comment": "raum-seal",
              "expr": [{"match": {"op": "==", "left": {"meta": {"key": "skuid"}}, "right": "raum"}},
                       {"counter": {"packets": 0, "bytes": 0}}]}},
]}


def test_seal_rule_flip():
    rule = find_seal_rule(json.dumps(RULESET))
    assert rule["handle"] == 9
    sealed = flip_expr(rule["expr"], True)
    assert sealed[-1] == {"drop": None} and len(sealed) == 3
    assert flip_expr(sealed, False) == rule["expr"]
    assert flip_expr(flip_expr(sealed, True), True).count({"drop": None}) == 1
    cmd = replace_command(rule, True)["nftables"][0]["replace"]["rule"]
    assert cmd["handle"] == 9 and cmd["chain"] == "output" and cmd["comment"] == "raum-seal"
    assert find_seal_rule({"nftables": []}) is None


async def test_seal_state_only_off_linux(monkeypatch):
    import raum.privacy.seal as seal_mod

    monkeypatch.setattr(seal_mod, "nft_available", lambda: False)
    s = SealController()
    r = await s.set(True)
    assert r == {"sealed": True, "applied": False, "method": "state-only", "error": None}


async def test_seal_applies_with_nft(monkeypatch):
    import raum.privacy.seal as seal_mod

    calls = []

    async def fake_nft(*args, timeout=1.0, stdin=None):
        calls.append((args, stdin))
        if args[:2] == ("-j", "-a"):
            return 0, json.dumps(RULESET), ""
        return 0, "", ""

    monkeypatch.setattr(seal_mod, "nft_available", lambda: True)
    monkeypatch.setattr(seal_mod, "nft", fake_nft)
    r = await SealController().set(True)
    assert r["applied"] is True and r["method"] == "nft"
    payload = json.loads(calls[-1][1])
    assert payload["nftables"][0]["replace"]["rule"]["expr"][-1] == {"drop": None}


def test_parse_nvidia_smi():
    assert parse_nvidia_smi("41, 12288\n") == (41.0, 12.0)
    assert parse_nvidia_smi("41, [N/A]\n") == (41.0, None)  # GB10 unified memory
    assert parse_nvidia_smi("30, 1024\n50, 1024\n") == (40.0, 2.0)
    assert parse_nvidia_smi("") is None


async def test_gpu_mock(monkeypatch):
    monkeypatch.delenv("RAUM_PROFILE", raising=False)
    g = GpuMonitor(load_config())
    g._smi = False
    stats = await g.read()
    assert stats.source == "mock" and 0 <= stats.util_pct <= 100
    assert "mock-gemma" in stats.models_loaded and "passthrough" not in stats.models_loaded


async def test_gpu_ollama_ps(monkeypatch):
    monkeypatch.setenv("RAUM_PROFILE", "spark")
    g = GpuMonitor(load_config())
    g._smi = False

    async def loaded():
        return ["gemma4:26b", "bge-m3"]

    monkeypatch.setattr(g, "_ollama_loaded", loaded)
    stats = await g.read()
    assert "large-v3-turbo" in stats.models_loaded and "gemma4:26b" in stats.models_loaded


def test_http_url():
    assert http_url("ws://127.0.0.1:7880") == "http://127.0.0.1:7880"
    assert http_url("wss://box:7443") == "https://box:7443"
    assert http_url("http://x") == "http://x"


async def test_publisher_fails_soft(monkeypatch):
    monkeypatch.delenv("RAUM_PROFILE", raising=False)
    cfg = load_config()
    cfg.network.livekit_internal_url = "ws://127.0.0.1:1"  # nothing listens here
    p = LiveKitPublisher(cfg, timeout_s=1.0)
    env = make_event("burn.done", "sz-1", {"key_destroyed_at": 1, "audit_head_hash": "0" * 64})
    assert await p.send(env) is False
    p.publish(env)  # queued; the background sender logs and carries on
    await p.aclose()
    assert p.failed >= 1


@pytest.mark.skipif(not os.environ.get("RAUM_LIVE_LIVEKIT"), reason="set RAUM_LIVE_LIVEKIT=1 with livekit-server --dev")
async def test_publisher_live(monkeypatch):  # pragma: no cover
    import secrets

    from livekit import api

    monkeypatch.delenv("RAUM_PROFILE", raising=False)
    cfg = load_config()
    room = f"sz-t{secrets.token_hex(4)}"
    p = LiveKitPublisher(cfg)
    env = make_event("burn.done", room, {"key_destroyed_at": 1, "audit_head_hash": "0" * 64})
    try:
        assert await p.send(env) is False and p.skipped == 1  # no such room: skipped fast, not raised
        async with api.LiveKitAPI(http_url(cfg.network.livekit_internal_url), cfg.network.livekit_api_key,
                                  cfg.network.livekit_api_secret) as lk:
            await lk.room.create_room(api.CreateRoomRequest(name=room, empty_timeout=5))
            p._exists.clear()
            assert await p.send(env) is True
            await lk.room.delete_room(api.DeleteRoomRequest(room=room))
    finally:
        await p.aclose()
