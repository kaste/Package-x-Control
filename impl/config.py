import os
import sublime


INSTALLED_PACKAGES_PATH = sublime.installed_packages_path()
PACKAGES_PATH = sublime.packages_path()
CACHE_PATH = sublime.cache_path()
BACKUP_DIR = os.path.join(os.path.dirname(INSTALLED_PACKAGES_PATH), "Backup")
BUILD = int(sublime.version())
PLATFORM = f"{sublime.platform()}-{sublime.arch()}"

PACKAGE = "Package x Control"
PACKAGE_SETTINGS = f"{PACKAGE}.sublime-settings"
PACKAGE_SETTINGS_LISTENER_KEY = "967fb34e-ad73-4cfa-bcc0-e058eb9b9ed6"

PACKAGE_CONTROL_PREFERENCES = "Package Control.sublime-settings"
PACKAGE_CONTROL_PREFERENCES_LISTENER_KEY = "ad520441-7701-4038-b5a4-c360ac87528d"
SUBLIME_PREFERENCES = "Preferences.sublime-settings"

DEFAULT_CHANNEL = (
    "https://raw.githubusercontent.com/wbond/package_control_channel"
    "/refs/heads/master/channel.json"
)

ROOT_DIR = os.path.join(CACHE_PATH, PACKAGE)
PACKAGES_REPOSITORY = os.path.join(ROOT_DIR, "repository.json")
PACKAGE_CONTROL_OVERRIDE = os.path.join(PACKAGES_PATH, PACKAGE, "Package Control.sublime-settings")
REGISTRY_URL = (
    "https://github.com/kaste/Package-x-Control/"
    "releases/download/registry-latest/registry.json"
)
