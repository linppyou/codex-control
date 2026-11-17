"""Compatibility import for codexctl.accounts.refresher."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("codexctl.accounts.refresher")
