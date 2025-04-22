from __future__ import annotations
import json
import os
import urllib.parse

import sublime

from .config import (
    BUILD,
    PACKAGE_CONTROL_OVERRIDE,
    PACKAGE_CONTROL_PREFERENCES,
    PACKAGES_REPOSITORY,
    PACKAGE_SETTINGS,
    PACKAGE_SETTINGS_LISTENER_KEY,
    PLATFORM,
    ROOT_DIR,
)
from .glue_code import sync_managed_packages_with_package_control
from .pc_repository import fetch_packages
from .repository import ensure_repository_registry
from .runtime import determine_thread_names, run_on_executor
from .dashboard import *
from . import dashboard


__all__ = dashboard.__all__ + ("boot", "unboot",)


def dprint(*args):
    ...
    print(*args)


PACKAGE_CONTROL_OVERRIDE_TEMPLATE: dict[str, object] = {
    "installed_packages": [],
}


def boot():
    determine_thread_names()

    # Ensure our working directory exists
    os.makedirs(ROOT_DIR, exist_ok=True)

    # Ensure our repository file exists
    ensure_repository_registry(PACKAGES_REPOSITORY)

    # Ensure our repository is registered
    s = sublime.load_settings(PACKAGE_CONTROL_PREFERENCES)
    repositories = s.get("repositories", [])
    if PACKAGES_REPOSITORY not in repositories:
        repositories.append(PACKAGES_REPOSITORY)
    s.set("repositories", repositories)
    # Do we actually need to save the settings?
    # sublime.save_settings(PACKAGE_CONTROL_PREFERENCES)

    # Delay monkey-patching or it won't work.
    # Must be something with PC doing its own boot delayed.
    sublime.set_timeout_async(
        lambda: ensure_package_managers_http_get_is_patched(), 2000)
    # ensure_package_managers_http_get_is_patched()

    # Ensure local Package Control Preferences exist
    pc_settings_override = os.path.join(PACKAGE_CONTROL_OVERRIDE)
    if not os.path.exists(pc_settings_override):
        with open(pc_settings_override, "w", encoding="utf-8") as f:
            json.dump(PACKAGE_CONTROL_OVERRIDE_TEMPLATE, f)

    # Ensure managed packages are in sync with Package Control
    # sublime.load_settings(PACKAGE_SETTINGS).add_on_change(
    #     PACKAGE_SETTINGS_LISTENER_KEY, sync_managed_packages_with_package_control
    # )
    run_on_executor(sync_managed_packages_with_package_control)
    # sublime.set_timeout_async(
    #     lambda: run_on_executor(fetch_packages, BUILD, PLATFORM, dprint, force=True), 1000)


def unboot():
    # sublime.load_settings(PACKAGE_SETTINGS).clear_on_change(PACKAGE_SETTINGS_LISTENER_KEY)
    ...


def ensure_package_managers_http_get_is_patched():
    from package_control import package_manager

    try:
        original_http_get = package_manager.http_get.__wrapped__
    except AttributeError:
        original_http_get = package_manager.http_get

    def patched_http_get(url, settings, error_message="", prefer_cached=False):
        if url.startswith("file:///"):
            with open(urllib.parse.unquote(url[8:]), "rb") as f:
                return f.read()

        return original_http_get(url, settings, error_message, prefer_cached)

    patched_http_get.__wrapped__ = original_http_get  # type: ignore[attr-defined]
    print(
        "Package Control X: patch package_control.package_manager.http_get "
        "to support file URL's."
    )
    package_manager.http_get = patched_http_get
