"""Sites domain and compatibility access to its original module API."""
from importlib import import_module


def __getattr__(name):
    return getattr(import_module("codexctl.sites.inventory"), name)
