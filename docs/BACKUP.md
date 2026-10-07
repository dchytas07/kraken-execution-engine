# Nightly encrypted backup

Backs up the three things that cannot be regenerated: `data/bot.db` (trades, orders,
audit log), `secrets/telegram.session` (the Telethon login) and `.env`. One encrypted
file per night goes to a Windows-side folder (default
`/mnt/c/Users/<you>/kraken-signal-bot-backups`); the newest 14 are kept.

`tools/backup.py` does the work; `deploy/kraken-signal-bot-backup.{service,timer}` run it
at 03:30 (`Persistent=true`, so a night the PC slept through is caught up on wake).

## One-time setup

The backup is encrypted to a PUBLIC key, so this machine can write backups but cannot read
them. The Windows folder is readable by OneDrive, other apps and other users; without the
private key what lands there is noise.

1. Make a key pair, **ideally somewhere other than this WSL install**:

       gpg --quick-generate-key "kraken-signal-bot backup" rsa4096 encr never
       gpg --export --armor "kraken-signal-bot backup" > backup-public.asc
       gpg --export-secret-keys --armor "kraken-signal-bot backup" > SECRET-KEEP-OFFLINE.asc

2. Put `SECRET-KEEP-OFFLINE.asc` in a password manager or on another machine. If you made the
   key here, then delete the file and the secret key from this keyring
   (`gpg --delete-secret-keys "kraken-signal-bot backup"`), so the box that writes backups
   cannot read them.
3. On this machine, import only the public key and record its fingerprint:

       gpg --import backup-public.asc
       gpg --fingerprint "kraken-signal-bot backup"
       mkdir -p ~/.config/kraken-signal-bot
       echo 'BACKUP_GPG_RECIPIENT=<the 40-hex fingerprint>' > ~/.config/kraken-signal-bot/backup.env

4. Install the timer (system units, like the bot's own service):

       sudo cp deploy/kraken-signal-bot-backup.service deploy/kraken-signal-bot-backup.timer /etc/systemd/system/
       sudo systemctl daemon-reload
       sudo systemctl enable --now kraken-signal-bot-backup.timer

5. Run it once now and check the result:

       sudo systemctl start kraken-signal-bot-backup.service
       systemctl status kraken-signal-bot-backup.service
       ls -l /mnt/c/Users/$USER/kraken-signal-bot-backups

## Fails closed, and tells you

No recipient, no gpg, an unwritable folder, a corrupt database or an archive that does not
verify is a non-zero exit **and a Telegram alert** ("nightly backup FAILED: ..."). It never
writes an unencrypted fallback. A missing night shows up in `systemctl list-timers` and as
a gap in the folder.

## Restore drill (do this once, before you need it)

    .venv/bin/python tools/backup.py verify  --in <file>     # addressed to your key? (no private key needed)
    .venv/bin/python tools/backup.py restore --in <file> --out /tmp/restore-test

`restore` asks gpg to decrypt (it prompts for the private key's passphrase), checks every
file against the manifest's SHA-256, and refuses to restore into a non-empty directory or
an archive with unsafe paths. To recover onto a fresh install: restore into an empty
directory, stop the service, then copy `data/bot.db`, `secrets/telegram.session` and `.env`
into place (mode 600 for the last two) and start it.

The restore path is covered by `tests/test_backup.py`, which round-trips real gpg
encryption and a live-written WAL database.
