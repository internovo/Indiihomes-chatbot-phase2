"""Manual CLI for inspecting/resetting the polling checkpoint without
spinning up the whole app. Handy after a backfill, or when debugging
why a lead didn't get picked up.

Usage:
    python scripts/checkpoint_cli.py show
    python scripts/checkpoint_cli.py set 2026-07-30T14:50:12.000Z
    python scripts/checkpoint_cli.py reset
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import checkpoint  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    action = sys.argv[1]

    if action == "show":
        print(f"Current afterDate that the next cycle will use: {checkpoint.get_after_date()}")

    elif action == "set":
        if len(sys.argv) != 3:
            print("Usage: python scripts/checkpoint_cli.py set <ISO_TIMESTAMP>")
            return
        checkpoint.save_checkpoint(sys.argv[2])
        print(f"Checkpoint set to {sys.argv[2]}")

    elif action == "reset":
        checkpoint.reset_checkpoint()
        print("Checkpoint reset - next cycle will use the lookback-window fallback.")

    else:
        print(f"Unknown action '{action}'.\n")
        print(__doc__)


if __name__ == "__main__":
    main()
