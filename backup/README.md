# Backups

Every night at 02:17 UTC, GitHub runs `.github/workflows/backup.yml`:

1. `backup/dump.py` copies the whole database into one SQL file, using a
   read-only Turso token (a backup can never change data).
2. The file is compressed and locked with `BACKUP_PASSPHRASE`. Without the
   passphrase the file is unreadable, even to GitHub.
3. It's saved on the backup run in the Actions tab, and GitHub deletes it
   automatically after 30 days.

If a nightly backup fails, GitHub emails you.

Note: GitHub pauses scheduled jobs in a repo with no pushes for 60 days.
If that happens, the Actions tab shows a button to turn it back on.

## Restoring

1. Actions tab > backup > pick a run > Artifacts > download the zip.
2. In the folder you downloaded it to:

```bash
unzip agenndar-backup-*.zip
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
  -in agenndar-backup.sql.gz.enc | gunzip > agenndar-backup.sql
```

It asks for the passphrase.

3. To look inside safely on your laptop:

```bash
python3 -c "import sqlite3; sqlite3.connect('restored.db')\
.executescript(open('agenndar-backup.sql').read())"
```

4. To put it back live, make a new Turso database from it, then point
   Render's `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` at the new one:

```bash
turso db create priority-planner-restored --from-dump agenndar-backup.sql
```
