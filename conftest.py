"""Pytest root configuration.

This file exists (rather than living in ``tests/``) so pytest inserts the project
root into ``sys.path`` during collection, which lets tests import ``config`` and
``data_sources.base`` the same way the application does.
"""

from __future__ import annotations
