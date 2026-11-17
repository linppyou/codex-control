"""Compatibility import for codexctl.sites.ssh."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("codexctl.sites.ssh")
