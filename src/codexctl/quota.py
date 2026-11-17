"""Compatibility import for codexctl.usage.quota."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("codexctl.usage.quota")
