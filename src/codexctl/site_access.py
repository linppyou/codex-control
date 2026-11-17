"""Compatibility import for codexctl.sites.access."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("codexctl.sites.access")
