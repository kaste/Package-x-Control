import os
import sublime

BUILD = int(sublime.version())
PLATFORM = f"{sublime.platform()}-{sublime.arch()}"

INSTALLED_PACKAGES_PATH = sublime.installed_packages_path()
PACKAGES_PATH = sublime.packages_path()
CACHE_PATH = sublime.cache_path()
BACKUP_DIR = os.path.abspath(os.path.join(INSTALLED_PACKAGES_PATH, "..", "Backup"))

PACKAGE = "Package x Control"
PACKAGE_SETTINGS = f"{PACKAGE}.sublime-settings"
PACKAGE_SETTINGS_LISTENER_KEY = "967fb34e-ad73-4cfa-bcc0-e058eb9b9ed6"

PACKAGE_CONTROL_PREFERENCES = "Package Control.sublime-settings"
PACKAGE_CONTROL_PREFERENCES_LISTENER_KEY = "ad520441-7701-4038-b5a4-c360ac87528d"
SUBLIME_PREFERENCES = "Preferences.sublime-settings"

PACKAGE_DIR = os.path.join(PACKAGES_PATH, PACKAGE)
ROOT_DIR = os.path.abspath(os.path.join(CACHE_PATH, "..", "Package Storage", PACKAGE))
PACKAGES_REPOSITORY = os.path.join(ROOT_DIR, "repository.json")
CACHE_DIR = os.path.join(CACHE_PATH, PACKAGE)
PACKAGE_CONTROL_OVERRIDE = os.path.join(CACHE_DIR, "Package Control.sublime-settings")

DEFAULT_CHANNEL = (
    "https://raw.githubusercontent.com/wbond/package_control_channel"
    "/refs/heads/master/channel.json"
)
REGISTRY_URL = (
    "https://github.com/packagecontrol/thecrawl/releases/download/"
    "crawler-status/registry.json"
)
