"""Compatibility import for codexctl.dispatch.worker."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("codexctl.dispatch.worker")
