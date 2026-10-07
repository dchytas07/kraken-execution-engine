"""The helper scripts must not hardcode one operating system's paths."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
RUN_BOT = ROOT / "tools" / "run_bot.sh"


def sh(snippet, env_extra):
    env = {"PATH": os.environ["PATH"], "HOME": "/home/tester", **env_extra,
           "BOT_PYTHON": sys.executable}
    return subprocess.run(
        ["bash", "-c", f'source "{RUN_BOT}"; {snippet}'],
        capture_output=True, text=True, env=env, timeout=30,
    )


def test_run_bot_is_sourceable_without_starting_anything():
    r = sh("echo sourced", {})
    assert r.returncode == 0 and r.stdout.strip() == "sourced"


def test_run_bot_resolves_the_lock_where_kraken_cli_keeps_it_on_linux():
    r = sh("paper_lock_path", {})
    # Python's sys.platform decides, exactly as bot.status does: this test runs on
    # whichever OS CI uses, and the expectation follows it.
    expected = ("/home/tester/Library/Application Support/kraken/paper/futures_state.json.lock"
                if sys.platform == "darwin"
                else "/home/tester/.config/kraken/paper/futures_state.json.lock")
    assert r.stdout.strip() == expected, r.stderr


@pytest.mark.skipif(sys.platform == "darwin", reason="XDG applies to Linux")
def test_run_bot_honours_xdg_config_home():
    r = sh("paper_lock_path", {"XDG_CONFIG_HOME": "/custom/cfg"})
    assert r.stdout.strip() == "/custom/cfg/kraken/paper/futures_state.json.lock"


def test_run_bot_clears_only_a_lock_whose_holder_is_dead(tmp_path):
    cfg = tmp_path / "xdg"
    lock_dir = cfg / "kraken" / "paper"
    lock_dir.mkdir(parents=True)
    lock = lock_dir / "futures_state.json.lock"
    env = {"XDG_CONFIG_HOME": str(cfg), "HOME": str(tmp_path)}
    if sys.platform == "darwin":
        pytest.skip("macOS layout differs")

    lock.write_text("999999:orphan")                 # no such process
    assert sh("clear_orphaned_lock", env).returncode == 0
    assert not lock.exists(), "a lock held by a dead pid must be cleared"

    lock.write_text(f"{os.getpid()}:alive")          # this very process
    r = sh("clear_orphaned_lock", env)
    assert lock.exists(), "a lock held by a LIVE pid must be left alone"
    assert "live PID" in r.stderr


def test_no_tool_hardcodes_a_macos_path():
    for path in (RUN_BOT, ROOT / "tools" / "soak_run.py"):
        text = path.read_text()
        assert not re.search(r"Library/Application Support|/Users/", text), path.name


def test_soak_run_resolves_its_lock_through_the_shared_helper():
    from bot.status import paper_lock_path
    text = (ROOT / "tools" / "soak_run.py").read_text()
    assert "paper_lock_path()" in text and "Library" not in text
    assert paper_lock_path({"HOME": "/h"}, "linux") == Path(
        "/h/.config/kraken/paper/futures_state.json.lock")
    assert paper_lock_path({"HOME": "/h"}, "darwin") == Path(
        "/h/Library/Application Support/kraken/paper/futures_state.json.lock")
