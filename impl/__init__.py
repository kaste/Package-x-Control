from __future__ import annotations
import json
import os
import shutil
import urllib.parse

import sublime

from .config import (
    CACHE_DIR,
    BUILD,
    PACKAGE_CONTROL_OVERRIDE,
    PACKAGE_CONTROL_PREFERENCES,
    PACKAGE_DIR,
    PACKAGES_REPOSITORY,
    PACKAGE_SETTINGS,
    PACKAGE_SETTINGS_LISTENER_KEY,
    PLATFORM,
    ROOT_DIR,
)
from .glue_code import check_all_managed_packages_for_updates
from .the_registry import fetch_registry
from .repository import ensure_repository_registry
from .runtime import determine_thread_names, run_on_executor
from .utils import rmtree
from .dashboard import *  # noqa: F403  # loads the commands and event listeners
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
    migrate_1()

    # Ensure our working directories exist
    os.makedirs(ROOT_DIR, exist_ok=True)
    os.makedirs(PACKAGE_DIR, exist_ok=True)

    # Ensure our repository file exists
    ensure_repository_registry(PACKAGES_REPOSITORY)

    # Ensure our repository is registered
    s = sublime.load_settings(PACKAGE_CONTROL_PREFERENCES)
    repositories = s.get("repositories", [])
    modified = False
    if PACKAGES_REPOSITORY not in repositories:
        modified = True
        repositories.append(PACKAGES_REPOSITORY)
    if modified:
        s.set("repositories", repositories)
        sublime.save_settings(PACKAGE_CONTROL_PREFERENCES)

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
    #     PACKAGE_SETTINGS_LISTENER_KEY, check_our_integrity
    # )
    run_on_executor(check_all_managed_packages_for_updates)
    # sublime.set_timeout_async(
    #     lambda: run_on_executor(fetch_packages, BUILD, PLATFORM, dprint, force=True), 1000)


def migrate_1():
    # Migrate CACHE_DIR to ROOT_DIR after
    # https://github.com/sublimehq/sublime_text/issues/6713
    if os.path.exists(CACHE_DIR) and not os.path.exists(ROOT_DIR):
        print(f"Migrate working dir to {ROOT_DIR}")
        shutil.copytree(CACHE_DIR, ROOT_DIR)
        if not rmtree(CACHE_DIR):
            print(f"Failed to remove {CACHE_DIR}.  Will retry on next restart.")
    elif os.path.exists(CACHE_DIR) and os.path.exists(os.path.join(CACHE_DIR, "repository.json")):
        if not rmtree(CACHE_DIR):
            print(f"Failed to remove {CACHE_DIR}.  Will retry on next restart.")

    s = sublime.load_settings(PACKAGE_CONTROL_PREFERENCES)
    repositories: list[str] = s.get("repositories", [])
    old_packages_repository = os.path.join(CACHE_DIR, "repository.json")
    if old_packages_repository in repositories:
        repositories.remove(old_packages_repository)
        modified = True
        s.set("repositories", repositories)
        sublime.save_settings(PACKAGE_CONTROL_PREFERENCES)

    old_package_control_override = os.path.join(PACKAGE_DIR, "Package Control.sublime-settings")
    if os.path.exists(old_package_control_override):
        try:
            os.remove(old_package_control_override)
        except Exception:
            print(f"Failed to remove {old_package_control_override}.  Will retry on next restart.")


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
