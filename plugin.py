from __future__ import annotations

import sys

# kiss-reloader:
prefix = __package__ + "."  # don't clear the base package
for module_name in [
    module_name
    for module_name in sys.modules
    if module_name.startswith(prefix) and module_name != __name__
]:
    del sys.modules[module_name]


from .impl import *


def plugin_loaded():
    boot()


def plugin_unloaded():
    unboot()
