"""First-run setup: create the Navidrome admin (#97).

Run through the setup compose override, the same in bash, PowerShell and the
macOS terminal (Navidrome stays unpublished while it runs):

    docker compose -f docker-compose.yml -f docker-compose.setup.yml run --rm setup
    docker compose down
    docker compose up -d

Needs PV_DB_PATH, PV_NAVIDROME_URL and PV_SECRET_KEY. The password is read
with a hidden prompt, never from arguments or the environment.
"""

from __future__ import annotations

import getpass
import os
import sys

from app.clients.navidrome import NavidromeClient, NavidromeError
from app.services.encryption import SecretBox, SecretKeyError
from app.services.setup import AlreadySetUp, NavidromeNotReady, create_first_admin
from app.stores.database import Database


def main() -> int:
    missing = [n for n in ("PV_DB_PATH", "PV_NAVIDROME_URL") if not os.environ.get(n)]
    if missing:
        print(f"Setup needs {', '.join(missing)} set.", file=sys.stderr)
        return 2
    try:
        box = SecretBox.from_env()
    except SecretKeyError as e:
        print(f"Setup needs a valid PV_SECRET_KEY: {e}", file=sys.stderr)
        return 2

    print("Create the first Navidrome admin (also the first Post-Vinyl admin).")
    username = input("Admin username: ").strip()
    password = getpass.getpass("Admin password: ")
    if password != getpass.getpass("Repeat password: "):
        print("The passwords don't match; nothing was created.", file=sys.stderr)
        return 1
    try:
        user = create_first_admin(
            NavidromeClient(os.environ["PV_NAVIDROME_URL"]),
            Database(os.environ["PV_DB_PATH"]),
            box,
            username,
            password,
        )
    except AlreadySetUp:
        print("Navidrome already has an admin; setup isn't needed.", file=sys.stderr)
        return 1
    except NavidromeNotReady as e:
        print(f"{e}. Is the Navidrome container running?", file=sys.stderr)
        return 1
    except (ValueError, NavidromeError) as e:
        print(f"Setup failed: {e}", file=sys.stderr)
        return 1
    print(f"Created admin {user.username!r}. Now run: docker compose down, then")
    print("docker compose up -d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
