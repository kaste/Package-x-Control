from __future__ import annotations

from collections import deque
from concurrent.futures import as_completed, Future
from datetime import datetime, timezone
import os
import traceback

from typing import (
    Callable, Literal, NamedTuple, TypedDict, Optional
)
from typing_extensions import TypeAlias

import sublime
from package_control.package_manager import PackageManager

from .config import (
    BUILD, PACKAGE_CONTROL_PREFERENCES,
    PACKAGES_PATH, PLATFORM, ROOT_DIR, SUBLIME_PREFERENCES
)
from .config_management import (
    PackageConfiguration, get_configuration, process_config
)
from .git_package import (
    GitCallable, UpdateInfo, Version,
    check_for_updates, ensure_repository, describe_current_commit, get_commit_date,
    repo_is_valid
)
from .runtime import cooperative, gather, on_ui, AWAIT_UI
from .the_registry import fetch_registry, PackageDb
from .utils import remove_prefix
from . import worker


class VersionDescription(NamedTuple):
    kind: str  # 'tag', 'branch', 'commit'
    specifier: str
    date: Optional[float] = None  # Date for this version/commit


class PackageInfo(TypedDict, total=False):
    name: str
    version: VersionDescription | None  # Current version/commit
    update_available: VersionDescription | None  # Info about available update
    checked_out: bool  # If the package is a git checkout


class State(TypedDict, total=False):
    installed_packages: list[PackageInfo]
    package_controlled_packages: list[PackageInfo]
    unmanaged_packages: list[PackageInfo]
    disabled_packages: list[str]  # List of package names that are disabled
    status_messages: deque[str]  # For messages at the bottom
    registered_packages: PackageDb
    initial_fetch_of_package_control_io: Future


UpdateCallback: TypeAlias = Callable[[State], None]
state: State = {
    "installed_packages": [],
    "package_controlled_packages": [],
    "unmanaged_packages": [],
    "disabled_packages": [],
    "status_messages": deque([], 10),
    "registered_packages": {},
    "initial_fetch_of_package_control_io": Future()
}
registered_callbacks: set[UpdateCallback] = set()


@on_ui
def set_state(partial_state: State):
    state.update(partial_state)
    run_on_update(state)


def register(fn: UpdateCallback) -> UpdateCallback:
    registered_callbacks.add(fn)
    return fn


def run_on_update(state: State) -> None:
    for fn in registered_callbacks:
        try:
            fn(state)
        except Exception:
            traceback.print_exc()


# --- State Refresher


def refresh() -> None:
    """Fetches the latest state (if necessary) and renders the view."""
    global state
    fast_state(state, set_state)
    pm = PackageManager()
    worker.replace_or_add_task(
        "fetch_packages:orchestrator", fetch_registered_packages, state, set_state)
    worker.replace_or_add_task(
        "refresh_our_packages:orchestrator", refresh_our_packages, state, set_state, pm)
    worker.replace_or_add_task(
        "refresh_installed_packages:orchestrator", refresh_installed_packages, state, set_state, pm)
    worker.replace_or_add_task(
        "refresh_unmanaged_packages:orchestrator", refresh_unmanaged_packages, state, set_state)


StateSetter: TypeAlias = Callable[[State], None]


@cooperative
def fetch_registered_packages(state: State, set_state: StateSetter):
    @on_ui
    def printer(message: str):
        d = state["status_messages"]
        d.append(message)
        set_state({"status_messages": d})

    printer(f"[{datetime.now():%d.%m.%Y %H:%M}]")
    packages = fetch_registry(BUILD, PLATFORM, printer)
    yield AWAIT_UI   # ensure ordered update: the data *before* the future
    set_state({"registered_packages": packages})
    if not state["initial_fetch_of_package_control_io"].done():
        state["initial_fetch_of_package_control_io"].set_result(None)


def refresh_our_packages(state: State, set_state: StateSetter, pm: PackageManager):
    config_data = get_configuration()
    entries = process_config(config_data)
    _p = {
        p["name"]: p
        for p in state.get("installed_packages", [])
    }
    managed_packages = [entry["name"] for entry in entries]
    packages = [
        _p.get(package_name) or default_entry(package_name)
        for package_name in managed_packages
    ]

    def fetch_package_info(entry: PackageConfiguration, i: int):
        package_name = entry["name"]
        metadata = pm.get_metadata(package_name)
        if metadata:
            version = version_from_metadata(metadata)
            update_available = packages[i].get("update_available")
            packages[i] = {
                "name": package_name,
                "checked_out": False,
                "version": version,
                **(
                    {"update_available": update_available}
                    if update_available and update_available != version
                    else {}
                )
            }
            worker.add_task(package_name, fetch_update_info, entry, i)

        elif (
            os.path.exists(os.path.join(PACKAGES_PATH, package_name, ".git"))
            and not os.path.exists(os.path.join(
                PACKAGES_PATH, package_name, "package-metadata.json")
            )
        ):
            packages[i] = {
                "name": package_name,
                "checked_out": True
            }

        else:
            packages[i] = {
                "name": package_name,
                "checked_out": False,
                **next_version_from_git_repo(entry)
            }

        set_state({"installed_packages": packages})

    def fetch_update_info(entry: PackageConfiguration, i: int) -> None:
        update_info = next_version_from_git_repo(entry)
        update_available = update_info.get("update_available")
        info = packages[i]
        if not update_available:
            info.pop("update_available", None)
        elif update_available != info.get("version"):
            info.update(update_info)  # type: ignore[typeddict-item]
        set_state({"installed_packages": packages})

    gather([
        worker.add_task(entry["name"], fetch_package_info, entry, i)
        for i, entry in enumerate(entries)
    ])



def fast_state(state: State, set_state: StateSetter):
    config_data = get_configuration()
    entries = process_config(config_data)
    _p = {
        p["name"]: p
        for p in state.get("installed_packages", [])
    }
    managed_packages = [entry["name"] for entry in entries]
    installed_packages = [
        _p.get(package_name) or default_entry(package_name)
        for package_name in managed_packages
    ]

    s = sublime.load_settings(SUBLIME_PREFERENCES)
    disabled_packages = s.get("ignored_packages") or []

    _p = {
        p["name"]: p
        for p in state.get("package_controlled_packages", [])
    }
    s = sublime.load_settings(PACKAGE_CONTROL_PREFERENCES)
    package_controlled_packages = [
        _p.get(package_name) or default_entry(package_name)
        for package_name in s.get("installed_packages")
    ]

    _p = {
        p["name"]: p
        for p in state.get("unmanaged_packages", [])
    }
    unmanaged_packages = [
        _p.get(package_name) or default_entry(package_name)
        for package_name in get_unmanaged_package_names()
    ]
    set_state({
        "disabled_packages": disabled_packages,
        "installed_packages": installed_packages,
        "package_controlled_packages": package_controlled_packages,
        "unmanaged_packages": unmanaged_packages,
    })


def default_entry(package_name: str) -> PackageInfo:
    return {"name": package_name, "checked_out": False}


def get_unmanaged_package_names() -> list[str]:
    s = sublime.load_settings(PACKAGE_CONTROL_PREFERENCES)
    installed_packages = s.get("installed_packages")
    return sorted((
        name
        for name in os.listdir(PACKAGES_PATH)
        if name != "."
        if name != ".."
        if name.lower() != "user"
        if name not in installed_packages
        if (fpath := os.path.join(PACKAGES_PATH, name))
        if os.path.isdir(fpath)
        if not os.path.exists(os.path.join(fpath, ".hidden-sublime-package"))
        if not os.path.exists(os.path.join(fpath, ".package-metadata.json"))
    ), key=lambda s: s.lower())


def refresh_unmanaged_packages(state: State, set_state: StateSetter):
    _p = {
        p["name"]: p
        for p in state.get("unmanaged_packages", [])
    }
    unmanaged_packages = get_unmanaged_package_names()
    packages = [
        _p.get(package_name) or default_entry(package_name)
        for package_name in unmanaged_packages
    ]
    set_state({"unmanaged_packages": packages})

    def fetch_package_info(package_name: str) -> PackageInfo:
        return {
            "name": package_name,
            # That's a lie, these are all checked out, but we
            # don't want to show that explicitly in the UI.
            "checked_out": False,
            **current_version_of_git_repo(os.path.join(PACKAGES_PATH, package_name))
        }

    for f in as_completed([
        worker.add_task(package_name, fetch_package_info, package_name)
        for package_name in unmanaged_packages
    ]):
        info = f.result()
        package_name = info["name"]
        for i, p in enumerate(packages):
            if p["name"] == package_name:
                packages[i] = info
                break
        set_state({"unmanaged_packages": packages})


def current_version_of_git_repo(repo_path: str) -> dict:
    git = GitCallable(repo_path)
    if not os.path.exists(git.git_dir) or not repo_is_valid(git):
        return {}
    version = describe_current_commit(git)
    return {"version": git_version_to_description(version, git)}


def next_version_from_git_repo(entry: PackageConfiguration) -> dict:
    git = ensure_repository(entry, ROOT_DIR, GitCallable)
    info = check_for_updates(entry["refs"], BUILD, git)
    if info["status"] == "no-suitable-version-found":
        return {}
    else:
        return {"update_available": git_version_to_description(info["version"], git)}


def git_version_to_description(
    version: Version | None, git: GitCallable
) -> VersionDescription | None:
    if version is None:
        return None
    if version.refname and version.refname.startswith("refs/tags/"):
        return VersionDescription(
            "tag",
            remove_prefix(version.refname, "refs/tags/").lstrip("v"),
            get_commit_date(version.sha, git)
        )
    else:
        return VersionDescription(
            "commit",
            version.sha[:8],
            get_commit_date(version.sha, git)
        )


def refresh_installed_packages(state: State, set_state: StateSetter, pm: PackageManager):
    s = sublime.load_settings(PACKAGE_CONTROL_PREFERENCES)
    info: PackageInfo
    packages = []
    for package_name in s.get("installed_packages"):
        metadata = pm.get_metadata(package_name)
        if (
            not metadata
            or (
                os.path.exists(os.path.join(PACKAGES_PATH, package_name, ".git"))
                and not os.path.exists(os.path.join(
                    PACKAGES_PATH, package_name, "package-metadata.json")
                )
            )
        ):
            info = {
                "name": package_name,
                "checked_out": True
            }
            packages.append(info)
            continue

        info = {
            "name": package_name,
            "version": version_from_metadata(metadata),
            "checked_out": False
        }
        packages.append(info)

    set_state({"package_controlled_packages": packages})


def is_calendar_version(version_str: str) -> Literal[False] | float:
    parts = version_str.split('.')
    return len(parts) == 6 and all(part.isdigit() for part in parts)


def calendar_version_to_timestamp(version_str: str) -> float:
    dt = datetime.strptime(version_str, "%Y.%m.%d.%H.%M.%S").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def datetime_to_ts(string) -> float:
    dt = datetime.strptime(string, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def timestamp_to_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%b %d %Y")


def version_from_metadata(metadata: dict) -> VersionDescription:
    version = metadata.get("version")
    calendar_version = is_calendar_version(version) if version else False
    release_time = metadata.get("release_time")
    return VersionDescription(
        "tag" if version and not calendar_version else "",
        version if version and not calendar_version else "",
        (
            datetime_to_ts(release_time)
            if release_time else
            calendar_version_to_timestamp(version)
            if version and calendar_version else
            None
        )
    )
