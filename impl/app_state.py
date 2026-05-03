from __future__ import annotations

from collections import deque
from concurrent.futures import as_completed, Future
from datetime import datetime, timezone
import importlib
import os
import traceback

from typing import (
    Any, Callable, Literal, NamedTuple, TypedDict, Optional
)
from typing_extensions import TypeAlias

import sublime

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
from .utils import isjunction, remove_prefix
from . import worker

PackageManager: Any = \
    importlib.import_module('Package Control.package_control.package_manager').PackageManager


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
StateSetter: TypeAlias = Callable[[State], None]
RepoSignature: TypeAlias = "tuple[Any, ...]"
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
unmanaged_package_cache: dict[str, tuple[RepoSignature, PackageInfo]] = {}
unmanaged_package_futures: dict[str, tuple[RepoSignature, Future[PackageInfo]]] = {}
linked_git_dir_cache: dict[str, str | None] = {}


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
    active_packages = set(unmanaged_packages)
    packages: list[PackageInfo] = []
    fetches: dict[Future[PackageInfo], tuple[str, RepoSignature]] = {}

    def fetch_package_info(package_name: str) -> PackageInfo:
        return {
            "name": package_name,
            # That's a lie, these are all checked out, but we
            # don't want to show that explicitly in the UI.
            "checked_out": False,
            **current_version_of_git_repo(os.path.join(PACKAGES_PATH, package_name))
        }

    prune_unmanaged_package_cache(active_packages)
    for package_name in unmanaged_packages:
        repo_path = os.path.join(PACKAGES_PATH, package_name)
        signature = unmanaged_package_signature(repo_path)
        if cached_package := cached_unmanaged_package(package_name, signature):
            packages.append(cached_package)
            continue

        if not signature_needs_git_probe(signature):
            info = default_entry(package_name)
            unmanaged_package_cache[package_name] = (signature, info)
            packages.append(info)
            continue

        if fetch := matching_unmanaged_package_fetch(package_name, signature):
            packages.append(_p.get(package_name) or default_entry(package_name))
            fetches[fetch] = (package_name, signature)
            continue
        if cached_package := cached_unmanaged_package(package_name, signature):
            packages.append(cached_package)
            continue

        info = _p.get(package_name) or default_entry(package_name)
        packages.append(info)
        fetch = worker.add_task(package_name, fetch_package_info, package_name)
        unmanaged_package_futures[package_name] = (signature, fetch)
        fetches[fetch] = (package_name, signature)

    set_state({"unmanaged_packages": packages})

    for f in as_completed(fetches):
        package_name, signature = fetches[f]
        info = f.result()
        cache_unmanaged_package_info(package_name, signature, f, info)
        for i, p in enumerate(packages):
            if p["name"] == package_name:
                packages[i] = info
                break
        set_state({"unmanaged_packages": packages})


def prune_unmanaged_package_cache(active_packages: set[str]) -> None:
    for package_name in list(unmanaged_package_cache):
        if package_name not in active_packages:
            unmanaged_package_cache.pop(package_name, None)
    for package_name in list(unmanaged_package_futures):
        if package_name not in active_packages:
            unmanaged_package_futures.pop(package_name, None)


def cached_unmanaged_package(
    package_name: str, signature: RepoSignature
) -> PackageInfo | None:
    cached = unmanaged_package_cache.get(package_name)
    if cached and cached[0] == signature:
        return cached[1]
    return None


def signature_needs_git_probe(signature: RepoSignature) -> bool:
    return signature[0] != "plain"


def matching_unmanaged_package_fetch(
    package_name: str, signature: RepoSignature
) -> Future[PackageInfo] | None:
    inflight = unmanaged_package_futures.get(package_name)
    if not inflight or inflight[0] != signature:
        return None

    future = inflight[1]
    if future.done():
        info = future.result()
        unmanaged_package_cache[package_name] = (signature, info)
        unmanaged_package_futures.pop(package_name, None)
        return None
    return future


def cache_unmanaged_package_info(
    package_name: str,
    signature: RepoSignature,
    future: Future[PackageInfo],
    info: PackageInfo,
) -> None:
    inflight = unmanaged_package_futures.get(package_name)
    if inflight and inflight == (signature, future):
        unmanaged_package_futures.pop(package_name, None)
    unmanaged_package_cache[package_name] = (signature, info)


def unmanaged_package_signature(repo_path: str) -> RepoSignature:
    git_dir = resolve_git_dir(repo_path)
    if git_dir:
        return git_dir_signature(git_dir)

    is_linked = os.path.islink(repo_path) or isjunction(repo_path)
    if is_linked and (git_dir := discover_linked_git_dir(repo_path)):
        return git_dir_signature(git_dir)

    return (
        "linked" if is_linked else "plain",
        os.path.normcase(os.path.realpath(repo_path)),
        file_signature(repo_path),
        file_signature(os.path.join(repo_path, ".git")),
    )


def git_dir_signature(git_dir: str) -> RepoSignature:
    common_dir = resolve_common_git_dir(git_dir)
    head_path = os.path.join(git_dir, "HEAD")
    head = read_text_file(head_path)
    parts: list[Any] = [
        os.path.normcase(os.path.realpath(git_dir)),
        os.path.normcase(os.path.realpath(common_dir)),
        file_signature(head_path),
        head,
        file_signature(os.path.join(common_dir, "packed-refs")),
        directory_signature(os.path.join(common_dir, "refs", "heads")),
        directory_signature(os.path.join(common_dir, "refs", "tags")),
    ]
    if head.startswith("ref: "):
        ref_path = os.path.join(common_dir, head[5:].strip().replace("/", os.sep))
        parts.extend((file_signature(ref_path), read_text_file(ref_path)))
    return ("git", tuple(parts))


def discover_linked_git_dir(repo_path: str) -> str | None:
    cache_key = os.path.normcase(os.path.realpath(repo_path))
    if cache_key in linked_git_dir_cache:
        return linked_git_dir_cache[cache_key]

    git = GitCallable(repo_path)
    try:
        git_dir = git("rev-parse", "--git-dir")
    except Exception:
        git_dir = None
    else:
        if not os.path.isabs(git_dir):
            git_dir = os.path.join(repo_path, git_dir)
        git_dir = os.path.normpath(git_dir)

    linked_git_dir_cache[cache_key] = git_dir
    return git_dir


def resolve_git_dir(repo_path: str) -> str | None:
    dot_git = os.path.join(repo_path, ".git")
    if os.path.isdir(dot_git):
        return dot_git
    if not os.path.isfile(dot_git):
        return None

    gitdir_prefix = "gitdir:"
    content = read_text_file(dot_git)
    if not content.lower().startswith(gitdir_prefix):
        return None

    git_dir = content[len(gitdir_prefix):].strip()
    if not os.path.isabs(git_dir):
        git_dir = os.path.join(repo_path, git_dir)
    return os.path.normpath(git_dir)


def resolve_common_git_dir(git_dir: str) -> str:
    common_dir_path = os.path.join(git_dir, "commondir")
    common_dir = read_text_file(common_dir_path)
    if not common_dir:
        return git_dir
    if not os.path.isabs(common_dir):
        common_dir = os.path.join(git_dir, common_dir)
    return os.path.normpath(common_dir)


def file_signature(path: str) -> tuple[str, int, int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (os.path.normcase(os.path.realpath(path)), stat.st_mtime_ns, stat.st_size)


def directory_signature(path: str) -> tuple[tuple[str, int, int], ...] | None:
    try:
        os.stat(path)
    except OSError:
        return None

    signatures: list[tuple[str, int, int]] = []
    collect_directory_signatures(path, "", signatures)
    return tuple(sorted(signatures))


def collect_directory_signatures(
    path: str, relpath: str, signatures: list[tuple[str, int, int]]
) -> None:
    try:
        stat = os.stat(path)
        entries = os.scandir(path)
    except OSError:
        return

    signatures.append((relpath, stat.st_mtime_ns, stat.st_size))
    with entries:
        for entry in entries:
            child_relpath = os.path.join(relpath, entry.name)
            if entry.is_dir(follow_symlinks=False):
                collect_directory_signatures(entry.path, child_relpath, signatures)
                continue
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            signatures.append((child_relpath, entry_stat.st_mtime_ns, entry_stat.st_size))


def read_text_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def current_version_of_git_repo(repo_path: str) -> dict:
    git = GitCallable(repo_path)
    if (
        os.path.exists(git.git_dir)
        or os.path.islink(repo_path)
        or isjunction(repo_path)
    ) and repo_is_valid(git):
        version = describe_current_commit(git)
        return {"version": git_version_to_description(version, git)}
    return {}


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
            and os.path.exists(os.path.join(PACKAGES_PATH, package_name, ".git"))
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
