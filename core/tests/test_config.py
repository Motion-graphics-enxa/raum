"""raum.yaml + profiles (§6.7)."""

import pytest

from raum.config import load_config, repo_root


def test_default_box_is_mock():
    cfg = load_config()
    assert cfg.profile == "mock"
    for stage in ("asr", "translate", "verify", "tts"):
        assert getattr(cfg.models, stage).engine == "mock", stage
    assert cfg.models.vad.engine == "silero"


def test_spark_profile_is_real(monkeypatch):
    monkeypatch.setenv("RAUM_PROFILE", "spark")
    cfg = load_config()
    assert cfg.models.asr.engine == "whisper.cpp"
    assert cfg.models.translate.engine == "ollama" and cfg.models.translate.model == "gemma4:26b"
    assert cfg.models.verify.model == "gpt-oss:120b"
    assert cfg.models.embed.model == "bge-m3"


def test_every_profile_validates(monkeypatch):
    for p in sorted((repo_root() / "profiles").glob("*.yaml")):
        monkeypatch.setenv("RAUM_PROFILE", p.stem)
        cfg = load_config()
        assert cfg.profile == p.stem


def test_unknown_profile(monkeypatch):
    monkeypatch.setenv("RAUM_PROFILE", "toaster")
    with pytest.raises(ValueError, match="unknown profile"):
        load_config()


def test_box_overrides_win(tmp_path, monkeypatch):
    monkeypatch.delenv("RAUM_PROFILE", raising=False)
    p = tmp_path / "raum.yaml"
    p.write_text("profile: spark\nmodels:\n  translate: { model: gemma4:e4b }\n")
    cfg = load_config(p)
    assert cfg.models.translate.engine == "ollama"  # from profile
    assert cfg.models.translate.model == "gemma4:e4b"  # override
