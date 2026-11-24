"""Offline test suite for BBC EFB and Black Box Core.

The test modules import shared fakes from sibling modules by their plain names
(for example ``from test_simconnect_provider_offline import FakeSdk``). The
documented command, ``python -m unittest discover -s tests``, puts this folder on
``sys.path`` itself. Running from the repository root as a package (``unittest
discover -s tests -t .`` or pytest) imports this file first, so the folder is
added here too and the sibling imports resolve the same way in every runner.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
