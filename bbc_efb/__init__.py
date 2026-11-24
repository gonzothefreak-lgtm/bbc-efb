"""BBC Integrated EFB internal concept.

``VERSION`` is the single version source. ``pyproject.toml`` reads it
(``[tool.setuptools.dynamic] version = {attr = "bbc_efb.VERSION"}``), and the
Windows build checks ``packaging/version_info.txt`` against it. It uses the
PEP 440 form: ``0.1.0a1`` is internal concept A, build 1 (previously written
``0.1.0-concept-a.1``).
"""

VERSION = "0.1.0a1"
__version__ = VERSION
