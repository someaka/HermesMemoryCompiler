"""Pytest fixtures and path setup for HermesMemoryCompiler tests."""

import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
