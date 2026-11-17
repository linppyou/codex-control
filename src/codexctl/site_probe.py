"""Compatibility import for codexctl.sites.probe."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("codexctl.sites.probe")
