"""Nightly encrypted backup (tools/backup.py).

Real gpg, real SQLite, real round trip: a throwaway key is generated in a
temporary keyring, so what is asserted is that an archive written by the job can
actually be DECRYPTED and the database inside is intact, not merely that a file
appeared. A backup nobody can restore is worse than none.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import io
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import backup as bk  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg not installed")

SECRET = "TELEGRAM_API_HASH=0123456789abcdef-NOT-A-REAL-SECRET"


@pytest.fixture(scope="module")
def gpg_home(tmp_path_factory):
    home = tmp_path_factory.mktemp("gnupg")
    home.chmod(0o700)
    mp = pytest.MonkeyPatch()
    mp.setenv("GNUPGHOME", str(home))
    r = subprocess.run(
        ["gpg", "--batch", "--passphrase", "", "--quick-generate-key",
         "backup-test <t@example.invalid>", "rsa2048", "encr", "never"],
        capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    yield "backup-test"
    mp.undo()


@pytest.fixture
def files(tmp_path):
    db = tmp_path / "data" / "bot.db"
    db.parent.mkdir()
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT, pnl TEXT)")
    c.executemany("INSERT INTO trades (symbol, pnl) VALUES (?, ?)",
                  [(f"PF_{i}", str(i)) for i in range(500)])
    c.commit()
    c.close()
    session = tmp_path / "secrets" / "telegram.session"
    session.parent.mkdir()
    s = sqlite3.connect(session)
    s.execute("CREATE TABLE sessions (dc_id INTEGER, auth_key BLOB)")
    s.execute("INSERT INTO sessions VALUES (2, ?)", (b"\x01\x02AUTHKEY-MARKER\x03",))
    s.commit()
    s.close()
    env = tmp_path / ".env"
    env.write_text(f"TRADING_MODE=paper\n{SECRET}\n")
    return db, session, env


def do_backup(tmp_path, files, recipient, **kw):
    db, session, env = files
    return bk.run_backup(dest=tmp_path / "out", recipient=recipient, db=db,
                         session=session, env_file=env, **kw)


def test_round_trip_writes_an_archive_that_decrypts_to_the_same_data(tmp_path, files, gpg_home):
    out = do_backup(tmp_path, files, gpg_home)
    assert bk.PATTERN.match(out.name) and out.stat().st_size > 1000
    names = bk.restore(out, tmp_path / "restored")
    assert {"MANIFEST.json", "data/bot.db", "secrets/telegram.session", ".env"} <= set(names)

    restored = sqlite3.connect(tmp_path / "restored" / "data" / "bot.db")
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert restored.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 500
    restored.close()
    sess = sqlite3.connect(tmp_path / "restored" / "secrets" / "telegram.session")
    assert b"AUTHKEY-MARKER" in sess.execute("SELECT auth_key FROM sessions").fetchone()[0]
    sess.close()
    assert SECRET in (tmp_path / "restored" / ".env").read_text()


def test_nothing_in_the_destination_is_readable_without_the_private_key(tmp_path, files, gpg_home):
    do_backup(tmp_path, files, gpg_home)
    for p in (tmp_path / "out").iterdir():
        raw = p.read_bytes()
        assert SECRET.encode() not in raw
        assert b"AUTHKEY-MARKER" not in raw
        assert b"SQLite format 3" not in raw
        assert b"TRADING_MODE" not in raw


def test_no_partial_or_plaintext_file_is_left_behind(tmp_path, files, gpg_home):
    do_backup(tmp_path, files, gpg_home)
    assert all(bk.PATTERN.match(p.name) for p in (tmp_path / "out").iterdir())


def test_snapshot_is_consistent_while_the_database_is_being_written(tmp_path, files, gpg_home):
    """bot.db is WAL and live. A file copy mid-write can tear it."""
    db, _, _ = files
    stop = threading.Event()

    def writer():
        c = sqlite3.connect(db, timeout=30)
        n = 0
        while not stop.is_set():
            c.execute("INSERT INTO trades (symbol, pnl) VALUES (?, ?)", (f"W{n}", "1"))
            c.commit()
            n += 1
        c.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        out = do_backup(tmp_path, files, gpg_home)
    finally:
        stop.set()
        t.join()
    bk.restore(out, tmp_path / "r")
    c = sqlite3.connect(tmp_path / "r" / "data" / "bot.db")
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] >= 500
    c.close()


def test_no_recipient_means_no_backup_never_an_unencrypted_one(tmp_path, files, gpg_home):
    with pytest.raises(bk.BackupError, match="unencrypted"):
        do_backup(tmp_path, files, "")
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


def test_an_unknown_recipient_fails_before_any_work(tmp_path, files, gpg_home):
    with pytest.raises(bk.BackupError, match="no public key"):
        do_backup(tmp_path, files, "nobody-has-this-key")
    assert not (tmp_path / "out").exists()


def test_a_corrupt_database_aborts_instead_of_archiving_garbage(tmp_path, files, gpg_home):
    db, _, _ = files
    db.write_bytes(b"this is not a sqlite database at all" * 100)
    with pytest.raises(Exception):
        do_backup(tmp_path, files, gpg_home)
    assert not list((tmp_path / "out").glob("*")) if (tmp_path / "out").exists() else True


def test_a_missing_source_file_is_an_error_not_a_silent_skip(tmp_path, files, gpg_home):
    files[2].unlink()                                    # no .env
    with pytest.raises(bk.BackupError, match="does not exist"):
        do_backup(tmp_path, files, gpg_home)


def test_an_unwritable_destination_is_reported(tmp_path, files, gpg_home):
    blocker = tmp_path / "out"
    blocker.write_text("a file where the folder should be")
    with pytest.raises(bk.BackupError, match="not writable"):
        do_backup(tmp_path, files, gpg_home)


def test_retention_keeps_only_the_newest(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    for day in range(1, 21):
        (d / f"kraken-bot-202607{day:02d}-033000.tar.gpg").write_bytes(b"x")
    (d / "notes.txt").write_text("not a backup: must survive")
    gone = bk.prune(d, 14)
    assert len(gone) == 6 and gone[0].startswith("kraken-bot-20260701")
    left = sorted(p.name for p in d.iterdir())
    assert len(left) == 15 and "notes.txt" in left
    assert "kraken-bot-20260720-033000.tar.gpg" in left


def test_a_backup_run_applies_retention(tmp_path, files, gpg_home):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    for day in range(1, 11):
        (out_dir / f"kraken-bot-202601{day:02d}-000000.tar.gpg").write_bytes(b"old")
    do_backup(tmp_path, files, gpg_home, keep=3)
    assert len([p for p in out_dir.iterdir() if bk.PATTERN.match(p.name)]) == 3


def test_verify_accepts_a_real_archive_and_rejects_everything_else(tmp_path, files, gpg_home):
    out = do_backup(tmp_path, files, gpg_home)
    bk.verify_encrypted(out, gpg_home)                   # no private key needed
    junk = tmp_path / "junk.tar.gpg"
    junk.write_bytes(b"not gpg " * 100)
    with pytest.raises(bk.BackupError):
        bk.verify_encrypted(junk, gpg_home)
    with pytest.raises(bk.BackupError, match="missing or implausibly small"):
        bk.verify_encrypted(tmp_path / "nope.tar.gpg", gpg_home)


def test_restore_refuses_a_non_empty_directory(tmp_path, files, gpg_home):
    out = do_backup(tmp_path, files, gpg_home)
    target = tmp_path / "live"
    target.mkdir()
    (target / "precious").write_text("do not overwrite")
    with pytest.raises(bk.BackupError, match="not empty"):
        bk.restore(out, target)
    assert (target / "precious").read_text() == "do not overwrite"


def test_restore_refuses_an_archive_that_tries_to_escape_the_directory(tmp_path, gpg_home):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tar:
        payload = b"pwned"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    evil = tmp_path / "evil.tar.gpg"
    subprocess.run(["gpg", "--batch", "--yes", "--trust-model", "always", "--recipient",
                    gpg_home, "--encrypt", "--output", str(evil)],
                   input=buf.getvalue(), check=True, timeout=60)
    with pytest.raises(bk.BackupError, match="unsafe path"):
        bk.restore(evil, tmp_path / "target")
    assert not (tmp_path / "escape.txt").exists()


def test_restore_detects_a_tampered_member_via_the_manifest(tmp_path, gpg_home):
    import json, hashlib
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tar:
        good = b"original"
        man = json.dumps({"files": [{"name": "data/x",
                                     "sha256": hashlib.sha256(good).hexdigest()}]}).encode()
        for name, data in (("MANIFEST.json", man), ("data/x", b"TAMPERED")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    arc = tmp_path / "t.tar.gpg"
    subprocess.run(["gpg", "--batch", "--yes", "--trust-model", "always", "--recipient",
                    gpg_home, "--encrypt", "--output", str(arc)],
                   input=buf.getvalue(), check=True, timeout=60)
    with pytest.raises(bk.BackupError, match="checksum"):
        bk.restore(arc, tmp_path / "t")


def test_the_cli_fails_closed_without_a_recipient(tmp_path, files, monkeypatch, capsys):
    monkeypatch.delenv("BACKUP_GPG_RECIPIENT", raising=False)
    alerts = []
    monkeypatch.setattr(bk, "_alert", alerts.append)
    db, session, env = files
    rc = bk.main(["backup", "--dest", str(tmp_path / "o"), "--db", str(db),
                  "--session", str(session), "--env-file", str(env)])
    assert rc == 1 and "unencrypted" in capsys.readouterr().err
    assert alerts and "unencrypted" in alerts[0], "a failed backup must alert"


def test_the_destination_is_derived_not_hardcoded(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKUP_WINDOWS_USER", "definitely-not-a-real-user-xyz")
    assert bk.default_dest() is None
    text = (ROOT / "tools" / "backup.py").read_text()
    assert "/mnt/c/Users/dchyt" not in text


def test_staging_happens_in_ram_when_available():
    assert bk._ram_dir() in ("/dev/shm", None)
