# D-Drive OneDrive Backup

Backs up `D:\OneDrive Backup` to your work OneDrive (folder `D-Drive-Backup`) through
Microsoft Graph. It doesn't need the OneDrive app or a second copy of your data on the laptop.

- **It starts by itself.** A hidden check runs about 1 minute after you sign in to Windows,
  30 seconds after the PC connects to a network, and every 10 minutes. You never start it
  yourself, and it stays on after restarts.
- **It pauses for 4 hours after each backup.** The finish time of the last backup is saved, so
  the pause survives restarts and power-offs. During the pause the check does nothing at all.
  To back up anyway, double-click **`Back up now.bat`**, which ignores the pause and the timer.
- **It backs up only on the office Wi-Fi ("Horizon 5G").** If something changed, a window opens
  and shows the progress. If nothing changed, you see nothing.
- **The check is quick and quiet.** It compares `D:\OneDrive Backup` with the record of the last
  backup and makes no OneDrive requests. OneDrive itself is checked fully in every real backup,
  and by the check once a day, to catch files changed or deleted directly in OneDrive.
- **The window closes by itself** 30 seconds after a backup that went fine. If something needs
  your attention (files that could not be read, the safety stop, a sign-in), it stays open until
  you press a key.
- **Lasting problems are shown, not hidden.** For example, the backup folder is missing or Windows
  won't reveal the Wi-Fi name. If such a problem is still there after 30 minutes, the window
  opens once to show it, then at most once a day.
- **Only new or changed files are uploaded.** The script keeps a record of what it uploaded. Files
  with no record are compared against OneDrive's fingerprint of each file (QuickXorHash), so
  nothing is uploaded twice.
- **Every upload is checked.** After each upload, OneDrive's fingerprint must match the local
  file, otherwise the file counts as failed and is retried.
- **One bad file never stops the backup.** Files that can't be read (locked, blocked by antivirus)
  are skipped and listed at the end. Such a file doesn't reopen the window again until it changes.
- **Exact mirror after the first complete backup.** Files you delete in `D:\OneDrive Backup` are
  deleted from OneDrive too, into OneDrive's recycle bin.
  - Before that happens, the dry run and the first backup tell you how many files are in OneDrive
    but no longer on D:.
  - Safety stop: if one run would delete more than 10% of the backup, counted in files or in GB,
    it deletes nothing and shows the exact command to approve it.
  - If a moved or renamed file can't be uploaded at its new place, its old copy stays in OneDrive
    until the new one is uploaded.

## Install / update (on the laptop that does the backup)

Everything goes into the existing `C:\DDriveOneDriveBackup` folder. Your `.venv`, `config.json`
and Entra app registration stay as they are, and no new Python packages are needed.

1. Download this repository: on GitHub, choose the branch, then **Code > Download ZIP**.
   Before unzipping, right-click the ZIP > **Properties** > tick **Unblock** > **OK**. Otherwise
   Windows shows a security warning when you double-click the `.bat` files. The program also
   removes that warning from its `.bat` files the first time it runs.
2. Copy into `C:\DDriveOneDriveBackup`, replacing what's there:
   - `DDriveOneDriveBackup.py`
   - the whole `ddrive_backup` folder (the program's code)
   - the three `.bat` files: `Install autostart.bat`, `Remove autostart.bat` and `Back up now.bat`
3. Recommended, because the old sign-in file was not encrypted and was shared in a zip: open
   <https://mysignins.microsoft.com/security-info> and choose **Sign out everywhere**. The new
   script deletes the old `auth_cache.bin` anyway. You'll sign in once more, and the new sign-in
   file (`auth_cache_dpapi.bin`) is encrypted for your Windows user.
4. Open **cmd** and try a dry run, which changes nothing:

   ```
   cd /d C:\DDriveOneDriveBackup
   .venv\Scripts\python.exe DDriveOneDriveBackup.py --dry-run
   ```

   Sign in with the code it shows. It then lists what it would compare, upload and delete.
5. Run the real backup once by hand:

   ```
   .venv\Scripts\python.exe DDriveOneDriveBackup.py
   ```

   The first run compares the files already in OneDrive with the local ones. This reads them
   from D: once and uploads only what is missing or different.
6. Turn on autostart: double-click **`Install autostart.bat`**. This is needed only once; it
   stays on after restarts.
   - Double-click it normally, while signed in as yourself. Don't use "Run as administrator" with
     another account, because the task would then belong to that account.
   - To turn autostart off again, double-click **`Remove autostart.bat`**.
   - To run a backup right away by hand, double-click **`Back up now.bat`**.

## Commands

Run these in cmd from the program folder (`cd /d C:\DDriveOneDriveBackup`):

| Command | What it does |
|---|---|
| `.venv\Scripts\python.exe DDriveOneDriveBackup.py` | Back up now (only on the office Wi-Fi) |
| `... --dry-run` | Show what would happen; change nothing |
| `... --approve-deletions` | Allow this run to delete more than the safety limit from OneDrive |
| `... --any-network` | Run even when not on the office Wi-Fi |
| `... --install-schedule` / `--remove-schedule` | Turn autostart on or off (same as the `.bat` files) |

Exit codes: 0 = OK, 1 = finished but some files had problems, 2 = config error,
3 = not on the office Wi-Fi, 4 = another backup is already running, 5 = stopped, 6 = failed.

## config.json

See `config.example.json`. Your existing file works as it is. New settings are optional:

| Setting | Default | Meaning |
|---|---|---|
| `office_wifi_ssid` | | Wi-Fi name, or a list of names. `""` = run on any network |
| `scan_interval_minutes` | 10 | How often the background check runs (used by `--install-schedule`) |
| `mirror_deletions` | `"auto"` | `"auto"`: on after the first complete backup. `"on"` or `"off"` |
| `mirror_safety_limit_percent` | 10 | Never delete more than this share of the backup in one run without approval |
| `parallel_uploads` | 4 | Files uploaded at the same time (1–8) |
| `window_close_seconds` | 30 | How long the pop-up window stays open after a run |
| `cooldown_hours` | 4 | After a backup finishes, the automatic check waits this long (0 = no pause) |
| `dry_run` | false | Same as `--dry-run`. While it is `true`, the background check does nothing |

`delete_remote_files` from the old version is no longer used.

## Files the script creates

- `backup.log`: what happened, file by file. It rotates at 5 MB and keeps 5 old logs.
- `backup_state.json`: the record of what was uploaded. If it is deleted, the next run rebuilds
  it by comparing fingerprints, which is slower once but safe.
- `auth_cache_dpapi.bin`: your saved Microsoft sign-in, encrypted for your Windows user. Never
  share it.
- `backup.lock`, `window.lock`: make sure only one backup and one window run at a time.
- `check_problem.json`: remembers a lasting problem the background check found, so it can show it.

## Troubleshooting

- **"Could not find out which Wi-Fi this PC is using … location"**: newer Windows versions only
  reveal the Wi-Fi name when location access is on. Go to Settings > Privacy & security >
  Location and turn on "Let desktop apps access your location".
- **"Blocked by antivirus (Windows Security)"**: Windows Security stopped the script from reading
  that file because it thinks the file is malware. See Windows Security > Virus & threat
  protection > Protection history.
- **"NOT deleting N files from OneDrive"**: more than 10% of the backup would be deleted. If you
  really removed those files, run with `--approve-deletions` once.
- **On battery the laptop went to sleep mid-backup**: many laptops still sleep a few minutes after
  the screen turns off on battery, whatever a program asks. The backup continues on the next run.
  Large uploads resume where they stopped. Plug in for big first uploads.
- **"OneDrive asked us to slow down"**: this is normal. Microsoft limits how many requests an app
  may make per minute, and the script waits as asked and carries on. Details are in `backup.log`.
- **The ETA says "estimating…"**: the script only shows a finish time once its speed
  measurements support it. For example, after many small files it can't yet know how long a
  2 GB file will take.

## Development

The tests run against a mock OneDrive server (`tests/mock_graph.py`) that follows Microsoft's
Graph documentation:

```
python -m pip install msal msal-extensions requests pytest
python -m pytest tests -q
```
