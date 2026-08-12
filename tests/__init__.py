"""Test package.

This file exists so that ``tests`` is an importable package rather than a bare
directory. ``conftest.py`` imports the shared doubles as ``tests.fakes``, and
without ``__init__.py`` pytest puts ``tests/`` — not the repository root — on
``sys.path``, so that import only resolves when pytest happens to be invoked as
``python -m pytest`` (which adds the working directory). Running the bare
``pytest`` entry point, as CI does, would fail at conftest import time.
"""
