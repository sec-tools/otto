"""
Otto module execution entry point (python3 -m otto).
"""
import sys

# Otto's whole runtime footprint beyond the standard library (see pyproject).
_RUNTIME_DEPS = ("httpx", "aiosqlite", "tomli")


def run() -> int:
    """`otto.cli.main`, with one courtesy: a fresh checkout run before
    ``./otto setup`` falls back to the system python3, which may lack Otto's
    two dependencies — then say what to do instead of showing a traceback."""
    try:
        from otto.cli import main
        return main()
    except ModuleNotFoundError as e:
        if e.name not in _RUNTIME_DEPS:
            raise
        sys.stderr.write(
            f"\notto: the Python package '{e.name}' is not installed for {sys.executable}.\n"
            "      Run ./otto setup once (it creates ./.venv with Otto's dependencies),\n"
            f"      or: {sys.executable} -m pip install -e .\n"
        )
        return 1


if __name__ == "__main__":
    sys.exit(run())
