"""D-Drive OneDrive Backup.

Backs up the folder named in config.json to your work OneDrive through
Microsoft Graph, as a one-way mirror. See README.md for setup and options.

    .venv\\Scripts\\python.exe DDriveOneDriveBackup.py                 run a backup now
    .venv\\Scripts\\python.exe DDriveOneDriveBackup.py --dry-run       show what would happen
    .venv\\Scripts\\python.exe DDriveOneDriveBackup.py --install-schedule
"""

import sys

from ddrive_backup.app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], script_path=__file__))
