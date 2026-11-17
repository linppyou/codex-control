"""Compatibility import for codexctl.users.sysusers."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("codexctl.users.sysusers")
