"""Compatibility import for codexctl.users.assignments."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("codexctl.users.assignments")
