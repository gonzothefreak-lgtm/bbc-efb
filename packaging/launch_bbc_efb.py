"""Frozen-build launcher for the BBC EFB.

PyInstaller runs its entry script as a top-level ``__main__`` module, which would
break relative imports inside ``bbc_efb/__main__.py``. This launcher imports the
real entry point as ``bbc_efb.__main__`` and calls ``main()``, which is the same
target as the ``bbc-efb`` launcher declared in ``pyproject.toml``.
"""

import multiprocessing
import sys


def run() -> int:
    # Needed before anything else if a later build ever starts worker processes.
    multiprocessing.freeze_support()
    from bbc_efb.__main__ import main

    result = main()
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(run())
