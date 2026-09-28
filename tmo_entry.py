#!/usr/bin/env python3
"""PyPI entry point: `tmo` console script.

Routes the CLI args into the package's dispatcher, reusing the pip-installed
`lib` package (installed alongside this module). Voice/AI extras are
detected at runtime with a friendly message if missing.
"""

import os
import subprocess
import sys


def _lib_dir() -> str:
    """Locate the lib/ directory: pip layout (site-packages/lib) or repo."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "lib"),                              # repo layout
        os.path.join(os.path.dirname(here), "lib"),             # py-modules sibling
        # pip installs both tmo_entry.py and lib/ into site-packages root
        os.path.join(os.path.dirname(here), "lib"),
    ]
    import importlib.util
    spec = importlib.util.find_spec("lib")
    if spec and spec.submodule_search_locations:
        return list(spec.submodule_search_locations)[0]
    for c in candidates:
        if os.path.isfile(os.path.join(c, "digits_login.py")):
            return c
    raise SystemExit("[!] cannot locate tmo lib/ package — reinstall with: pip install --force-reinstall tmo-digits-cli")


def main():
    lib = _lib_dir()

    # heavy-extra guards: give actionable errors instead of tracebacks
    args = sys.argv[1:]
    needs_voice = args[:1] == ["call"] or args[:1] == ["ai-call"]
    if needs_voice:
        try:
            import aiortc  # noqa: F401
        except ImportError:
            raise SystemExit("[!] voice calls need the extra: pip install tmo-digits-cli[voice]")
    if args[:1] == ["ai-call"]:
        try:
            import openai  # noqa: F401
        except ImportError:
            raise SystemExit("[!] ai-call needs: pip install tmo-digits-cli[ai]")

    # playwright browser check (needed by login's headless path + ws channel)
    if args[:1] in (["register"], ["call"], ["ai-call"], ["listen"]):
        try:
            import playwright  # noqa: F401
        except ImportError:
            raise SystemExit("[!] missing playwright: pip install tmo-digits-cli && python -m playwright install chromium")

    script = os.path.join(lib, "tmo_run.py")
    r = subprocess.run([sys.executable, script] + args)
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
