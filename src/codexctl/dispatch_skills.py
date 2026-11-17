"""Compatibility import for codexctl.dispatch.skills."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("codexctl.dispatch.skills")
