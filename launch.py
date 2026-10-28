#!/usr/bin/python3
"""Desktop and terminal entry point; GTK is not imported by the terminal helper."""
import sys

from omabatch import __version__
from omabatch.backend import SafetyError


def main():
    if sys.argv[1:] == ["--version"]:
        print(f"OmaBatch {__version__}")
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--execute":
        from omabatch.backend import execute_plan
        try:
            return execute_plan(sys.argv[2])
        except (SafetyError, OSError, ValueError) as error:
            print(f"\nOmaBatch stopped safely: {error}", file=sys.stderr)
            if sys.stdin.isatty():
                input("\nPress Enter to close… ")
            return 1
    if sys.argv[1:]:
        print("Usage: omabatch [--version]", file=sys.stderr)
        return 2
    from omabatch.gui import main as gui_main
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
