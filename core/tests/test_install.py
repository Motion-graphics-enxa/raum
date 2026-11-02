"""F-03: install.sh dry runs for every layout (no root, nothing changed) and RAUM_DATA_DIR."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from raum.config import load_config, repo_root

ROOT = repo_root()
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def dry(*args: str, **env: str) -> str:
    e = {**os.environ, **env}
    p = subprocess.run(["bash", str(ROOT / "install.sh"), "--dry-run", *args], check=False, env=e,
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


SPARK = {"RAUM_FAKE_GPU": "NVIDIA GB10, [N/A]", "RAUM_FAKE_OS": "Linux", "RAUM_FAKE_ARCH": "aarch64",
         "RAUM_FAKE_MEM_GB": "121"}


def test_user_space_spark_needs_no_root(tmp_path):
    box = tmp_path / "HackathonRaum"
    out = dry("--user-space", RAUM_BOX_DIR=str(box), **SPARK)
    assert "profile : spark" in out and "layout  : user space" in out
    assert "sudo " not in out.replace("sudo apt-get install", "")  # only as advice to the box admin
    assert "systemctl" not in out and "/opt/raum" not in out
    assert f"+ write {box}/runtime/raum.env (0600)" in out
    assert "RAUM_PROFILE=spark" in out and f"RAUM_DATA_DIR={box}/data" in out
    assert "deploy/box.sh restart" in out
    assert "raum.yaml: profile" not in out  # the profile lives in runtime/raum.env, not the repo
    assert not box.exists()  # dry run: nothing created


def test_user_space_keeps_an_existing_env_file(tmp_path):
    env = tmp_path / "runtime" / "raum.env"
    env.parent.mkdir(parents=True)
    env.write_text("RAUM_PROFILE=spark\n")
    out = dry("--user-space", "--no-services", RAUM_BOX_DIR=str(tmp_path), **SPARK)
    assert f"keep existing {env}" in out and "box.sh restart" not in out
    assert env.read_text() == "RAUM_PROFILE=spark\n"


def test_dedicated_box_and_detection():
    out = dry(**SPARK)
    assert "profile : spark" in out and "RAUM_DATA_DIR=/var/lib/raum" in out and "systemctl enable --now" in out
    out = dry(RAUM_FAKE_GPU="NVIDIA GeForce RTX 4090, 24564", RAUM_FAKE_OS="Linux", RAUM_FAKE_ARCH="x86_64",
              RAUM_FAKE_MEM_GB="64")
    assert "profile : gpu-24" in out
    out = dry(RAUM_FAKE_GPU="", RAUM_FAKE_OS="Linux", RAUM_FAKE_ARCH="aarch64", RAUM_FAKE_MEM_GB="8")
    assert "profile : lite" in out
    out = dry(RAUM_FAKE_GPU="", RAUM_FAKE_OS="Darwin", RAUM_FAKE_ARCH="arm64", RAUM_FAKE_MEM_GB="8")
    assert "profile : lite" in out and "scripts/dev.sh" in out and "5180" in out


def test_raum_data_dir_overrides_the_profile(monkeypatch, tmp_path):
    monkeypatch.delenv("RAUM_CONFIG", raising=False)
    monkeypatch.setenv("RAUM_PROFILE", "spark")
    assert load_config().security.data_dir == "~/Desktop/HackathonRaum/data"
    monkeypatch.setenv("RAUM_DATA_DIR", str(tmp_path / "d"))
    cfg = load_config()
    assert cfg.data_dir() == Path(tmp_path / "d") and cfg.audit_path() == Path(tmp_path / "d" / "audit.jsonl")
