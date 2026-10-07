"""Nightly encrypted backup of the three things that cannot be regenerated.

    .venv/bin/python tools/backup.py backup            # what the timer runs
    .venv/bin/python tools/backup.py restore --in FILE --out DIR
    .venv/bin/python tools/backup.py verify  --in FILE

Backs up:
    data/bot.db               the trade ledger, order history, audit log
    secrets/telegram.session  the Telethon login (re-creating it needs a phone)
    .env                      API credentials and every setting

and writes ONE encrypted file per night to a folder on the Windows side
(default /mnt/c/Users/<you>/kraken-signal-bot-backups), keeping the newest 14.

## Why it is built this way

* PUBLIC-KEY encryption (gpg --encrypt --recipient). A nightly unattended job
  cannot type a passphrase, and a key stored beside the data would encrypt
  nothing in practice. The machine holds only the PUBLIC key: it can write
  backups but cannot read them back, so what lands in a Windows folder (which
  OneDrive, other apps and other users can read) is unreadable without a private
  key that is NOT on this machine. Set it up once:

      gpg --quick-generate-key "kraken-signal-bot backup" rsa4096 encr never
      gpg --export --armor "kraken-signal-bot backup" > backup-public.asc
      gpg --export-secret-keys --armor "kraken-signal-bot backup" > SECRET.asc

  Move SECRET.asc to a password manager or another machine, then delete it and
  remove the secret key from this keyring (gpg --delete-secret-keys). Set
  BACKUP_GPG_RECIPIENT to the key's fingerprint. A backup nobody can decrypt is
  worse than none, so `verify` and `restore` exist and the restore path is tested.

* SQLite snapshots use the backup API, not a file copy: bot.db is in WAL mode and
  the service is writing to it, and copying the file mid-write can capture a
  torn database. The snapshot is integrity-checked before it is encrypted; a
  corrupt database aborts the backup loudly rather than archiving garbage.

* Plaintext never touches disk. Snapshots are staged in /dev/shm (RAM) and the
  archive is streamed straight into gpg.

* Fails CLOSED and LOUD: no recipient configured, no gpg, an unwritable
  destination, a corrupt database, an archive that does not verify: each is a
  non-zero exit and a Telegram alert. It never writes an UNencrypted fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

PATTERN = re.compile(r"^kraken-bot-\d{8}-\d{6}\.tar\.gpg$")
KEEP = 14
GPG_TIMEOUT = 120


class BackupError(Exception):
    """A backup step failed. Always fatal: there is no degraded mode."""


def default_dest() -> Path | None:
    """The Windows-side folder, derived rather than hardcoded."""
    user = os.environ.get("BACKUP_WINDOWS_USER") or os.environ.get("USER") or ""
    home = Path("/mnt/c/Users") / user
    if user and home.is_dir():
        return home / "kraken-signal-bot-backups"
    return None


def _ram_dir() -> str | None:
    return "/dev/shm" if Path("/dev/shm").is_dir() else None


def snapshot_sqlite(src: Path, dst: Path) -> None:
    """A consistent copy of a possibly-live SQLite file, integrity-checked."""
    if not src.exists():
        raise BackupError(f"{src} does not exist")
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
        check = target.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise BackupError(f"{src.name} failed its integrity check: {check}")
    finally:
        target.close()
        source.close()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_members(workdir: Path, db: Path, session: Path, env_file: Path) -> list[tuple[str, Path]]:
    """Stage the three files in `workdir`. Returns (archive name, staged path)."""
    members: list[tuple[str, Path]] = []
    for name, src, sqlite in (
        ("data/bot.db", db, True),
        ("secrets/telegram.session", session, True),
        (".env", env_file, False),
    ):
        staged = workdir / name.replace("/", "__")
        if sqlite:
            snapshot_sqlite(src, staged)
        else:
            if not src.exists():
                raise BackupError(f"{src} does not exist")
            shutil.copy2(src, staged)
        staged.chmod(0o600)
        members.append((name, staged))
    return members


def manifest(members: list[tuple[str, Path]], now: datetime) -> bytes:
    return json.dumps({
        "created_at": now.isoformat(),
        "host": os.uname().nodename,
        "files": [{"name": n, "bytes": p.stat().st_size, "sha256": _sha(p)}
                  for n, p in members],
    }, indent=1).encode()


def encrypt_archive(members, manifest_bytes: bytes, out: Path, recipient: str) -> None:
    """tar -> gpg, streamed: the plaintext archive is never a file."""
    partial = out.with_name(out.name + ".partial")
    proc = subprocess.Popen(
        ["gpg", "--batch", "--yes", "--quiet", "--trust-model", "always",
         "--recipient", recipient, "--encrypt", "--output", str(partial)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|") as tar:
            info = tarfile.TarInfo("MANIFEST.json")
            info.size = len(manifest_bytes)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(manifest_bytes))
            for name, path in members:
                tar.add(path, arcname=name,
                        filter=lambda ti: _scrub(ti))
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace").strip()
        if proc.wait(timeout=GPG_TIMEOUT) != 0:
            raise BackupError(f"gpg failed: {err[:300]}")
    except BackupError:
        partial.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        proc.kill()
        partial.unlink(missing_ok=True)
        raise BackupError(f"archiving failed: {exc}") from exc
    partial.replace(out)           # atomic: a half-written file never wears the final name


def _scrub(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.mode = 0o600
    return ti


def recipient_key_ids(recipient: str) -> set[str]:
    """Every key id (primary + subkeys) the recipient resolves to."""
    r = subprocess.run(["gpg", "--batch", "--list-keys", "--with-colons", recipient],
                       capture_output=True, text=True, timeout=GPG_TIMEOUT)
    if r.returncode != 0:
        raise BackupError(f"gpg has no public key for {recipient!r}: {r.stderr.strip()[:200]}")
    ids = {line.split(":")[4].upper() for line in r.stdout.splitlines()
           if line.startswith(("pub:", "sub:")) and len(line.split(":")) > 4}
    if not ids:
        raise BackupError(f"no usable key for {recipient!r}")
    return ids


def verify_encrypted(path: Path, recipient: str) -> None:
    """Prove the file is a gpg message addressed to the recipient, WITHOUT the
    private key (which is deliberately not here)."""
    if not path.exists() or path.stat().st_size < 200:
        raise BackupError(f"{path.name} is missing or implausibly small")
    r = subprocess.run(["gpg", "--batch", "--list-packets", str(path)],
                       capture_output=True, text=True, timeout=GPG_TIMEOUT)
    to = {m.upper() for m in re.findall(r":pubkey enc packet:.*?keyid ([0-9A-Fa-f]+)", r.stdout)}
    if not to:
        raise BackupError(f"{path.name} is not addressed to any public key")
    wanted = recipient_key_ids(recipient)
    if not any(k.endswith(w[-16:]) or w.endswith(k[-16:]) for k in to for w in wanted):
        raise BackupError(f"{path.name} is addressed to {sorted(to)}, "
                          f"not to {recipient!r}")
    if ":encrypted data packet:" not in r.stdout:
        raise BackupError(f"{path.name} carries no encrypted data")


def prune(dest: Path, keep: int) -> list[str]:
    files = sorted(p for p in dest.iterdir() if PATTERN.match(p.name))
    doomed = files[:-keep] if keep > 0 else []
    for p in doomed:
        p.unlink()
    return [p.name for p in doomed]


def run_backup(*, dest: Path, recipient: str, db: Path, session: Path,
               env_file: Path, keep: int = KEEP, now: datetime | None = None) -> Path:
    if not recipient:
        raise BackupError("BACKUP_GPG_RECIPIENT is not set: refusing to write an "
                          "unencrypted backup")
    if shutil.which("gpg") is None:
        raise BackupError("gpg is not installed")
    recipient_key_ids(recipient)                     # fail early, before any work
    now = now or datetime.now(UTC)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        probe = dest / ".write-test"
        probe.write_text("x")
        probe.unlink()
    except OSError as exc:
        raise BackupError(f"destination {dest} is not writable: {exc}") from exc

    out = dest / f"kraken-bot-{now:%Y%m%d-%H%M%S}.tar.gpg"
    with tempfile.TemporaryDirectory(dir=_ram_dir(), prefix="krakenbk-") as tmp:
        work = Path(tmp)
        work.chmod(0o700)
        members = build_members(work, db, session, env_file)
        encrypt_archive(members, manifest(members, now), out, recipient)
    verify_encrypted(out, recipient)
    prune(dest, keep)
    return out


def restore(src: Path, out_dir: Path) -> list[str]:
    """Decrypt (gpg asks for the private key's passphrase) and extract.
    Never overwrites: restoring into a non-empty directory is refused."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if any(out_dir.iterdir()):
        raise BackupError(f"{out_dir} is not empty; refusing to restore over it")
    dec = subprocess.run(["gpg", "--batch", "--yes", "--decrypt", str(src)],
                         capture_output=True, timeout=GPG_TIMEOUT)
    if dec.returncode != 0:
        raise BackupError(f"gpg could not decrypt: {dec.stderr.decode(errors='replace')[:300]}")
    names = []
    with tarfile.open(fileobj=io.BytesIO(dec.stdout), mode="r:") as tar:
        for member in tar.getmembers():
            if member.name.startswith(("/", "..")) or ".." in Path(member.name).parts:
                raise BackupError(f"unsafe path in archive: {member.name}")
        tar.extractall(out_dir, filter="data")
        names = [m.name for m in tar.getmembers()]
    man = json.loads((out_dir / "MANIFEST.json").read_text())
    for f in man["files"]:
        if _sha(out_dir / f["name"]) != f["sha256"]:
            raise BackupError(f"{f['name']} does not match its recorded checksum")
    return names


def _alert(message: str) -> None:
    """Tell the operator. Best effort: the exit code is the primary signal."""
    try:
        from bot.alerting import send_alert
        from bot.config import load_config
        c = load_config()
        send_alert(c.alert_telegram_bot_token, c.alert_telegram_chat_id,
                   f"⚠️ nightly backup FAILED: {message}")
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--dest", type=Path, default=None)
    b.add_argument("--keep", type=int, default=KEEP)
    b.add_argument("--db", type=Path, default=ROOT / "data" / "bot.db")
    b.add_argument("--session", type=Path, default=ROOT / "secrets" / "telegram.session")
    b.add_argument("--env-file", type=Path, default=ROOT / ".env")
    r = sub.add_parser("restore")
    r.add_argument("--in", dest="src", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True)
    v = sub.add_parser("verify")
    v.add_argument("--in", dest="src", type=Path, required=True)
    args = ap.parse_args(argv)

    try:
        if args.cmd == "backup":
            dest = args.dest or (Path(os.environ["BACKUP_DIR"])
                                 if os.environ.get("BACKUP_DIR") else default_dest())
            if dest is None:
                raise BackupError("no destination: pass --dest or set BACKUP_DIR "
                                  "(no /mnt/c/Users/<you> folder was found)")
            out = run_backup(
                dest=dest, recipient=os.environ.get("BACKUP_GPG_RECIPIENT", ""),
                db=args.db, session=args.session, env_file=args.env_file,
                keep=args.keep,
            )
            print(json.dumps({"ok": True, "file": out.name, "bytes": out.stat().st_size,
                              "dest": str(dest)}))
        elif args.cmd == "verify":
            verify_encrypted(args.src, os.environ.get("BACKUP_GPG_RECIPIENT", ""))
            print("ok: addressed to the recipient and carries encrypted data")
        else:
            names = restore(args.src, args.out)
            print(f"restored {len(names)} entries to {args.out}; checksums match")
        return 0
    except BackupError as exc:
        print(f"backup: {exc}", file=sys.stderr)
        if args.cmd == "backup":
            _alert(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
