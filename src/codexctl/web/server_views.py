"""Compatibility import for codexctl.web.routes.servers."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("codexctl.web.routes.servers")
