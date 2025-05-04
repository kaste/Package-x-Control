from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
import json
import os
import re
import time
import urllib.request

from typing import Callable, TypedDict, TypeVar
from typing_extensions import NotRequired, ParamSpec, Required, TypeAlias

import sublime
import sublime_plugin

from package_control.activity_indicator import ActivityIndicator
from package_control.package_manager import PackageManager
from package_control.download_manager import http_get, resolve_urls
from package_control.providers import ChannelProvider
from .config import BUILD, DEFAULT_CHANNEL, PACKAGES_CACHE, PLATFORM
from .utils import DedupQueue, format_items


P = ParamSpec("P")
T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)
LogWriter: TypeAlias = Callable[[str], None]


class ProprietaryPackage(TypedDict, total=False):
    name: Required[str]


class GitInstallablePackage(TypedDict, total=False):
    name: Required[str]
    git_url: Required[str]
    refs: Required[str]


PackageControlEntry: TypeAlias = "ProprietaryPackage | GitInstallablePackage"
PackageDb: TypeAlias = "dict[str, PackageControlEntry]"

MAX_WORKERS = 16

packages: PackageDb
try:
    packages
except NameError:
    packages = {}


def fetch_packages(
    build: int, platform: str, log: LogWriter, force: bool = False
) -> PackageDb:
    global packages

    # Try to load from cache first
    if not force and (cached := load_cached_packages(PACKAGES_CACHE, log)):
        packages, timestamp = cached
        formatted_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
        log(f"Using cached packages from {formatted_time}")
        return packages

    log("Fetching registered packages from packagecontrol.io")
    manager = PackageManager()
    now = time.monotonic()
    provider = ChannelProvider(DEFAULT_CHANNEL, manager.settings)
    repos: list[str] = provider.get_repositories()
    urls_to_fetch = DedupQueue(repos, thread_safe=True)
    results: dict[str, dict] = {}

    num_threads = min(MAX_WORKERS, len(repos))
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [
            executor.submit(drain_queue, manager.settings, urls_to_fetch, results, log)
            for _ in range(num_threads)
        ]
        wait(futures, timeout=60)

    # Collect the results
    # The main repo is the first one.  Run reversed so that others
    # can't override it.
    urls_ordered = DedupQueue(reversed(repos))
    packages_ = []
    while True:
        try:
            url = urls_ordered.pop()
        except IndexError:
            break
        result = results[url]
        packages_.extend(result["packages"])
        for include in reversed(result["includes"]):
            urls_ordered.append(include)

    log(f"{len(packages_)} packages in total.")
    packages = prepare_packages_data(packages_, build, platform, log)
    elapsed = time.monotonic() - now
    log(f"Prepared packages in {elapsed:.2f} seconds.")
    save_packages_cache(PACKAGES_CACHE, packages, log)
    return packages


def drain_queue(
    manager_settings,
    urls_to_fetch: DedupQueue[str],
    results: dict[str, dict],
    log: LogWriter,
) -> None:
    while True:
        try:
            location = urls_to_fetch.popleft()
        except IndexError:
            break

        try:
            result = fetch_repo(location, manager_settings, log)
            urls_to_fetch.extend(result["includes"])
        except Exception as e:
            result = {"packages": [], "includes": []}
            log(f"Error fetching {location}: {e}")
        finally:
            results[location] = result


def fetch_repo(location: str, manager_settings, log: LogWriter) -> dict:
    now = time.monotonic()
    if re.match(r"https?://", location, re.I):
        json_string = http_get(location, manager_settings)
    else:
        with open(location, "rb") as f:
            json_string = f.read()

    repo_info = json.loads(json_string.decode("utf-8"))
    repo_info["includes"] = list(resolve_urls(
        location, repo_info.get("includes", [])
    ))
    elapsed = time.monotonic() - now
    # log(f"Fetched {location} in {elapsed:.2f} seconds.")
    return repo_info


def prepare_packages_data(
    packages: list[dict], build: int, platform: str, log: LogWriter
) -> PackageDb:
    rv = {}
    proprietary = []
    for p in packages:
        name = p.get("name")
        website = p.get("details") or p.get("homepage") or ""
        if not name:
            if name := extract_name_from_url(website):
                p["name"] = name
            else:
                log(f"skip {website or p}. can't extract a name from it.")
                continue
        if git_url := website_to_https_git(website):
            p["git_url"] = git_url
            p["refs"] = compute_refs_from_releases(p.get("releases", []), build, platform)
        else:
            proprietary.append(name)

        rv[name] = p

    if proprietary:
        if len(proprietary) == 1:
            msg = f"{format_items(proprietary)} is proprietary"
        elif len(proprietary) > 5:
            msg = f"{format_items(proprietary)} ({len(proprietary)}) are proprietary"
        else:
            msg = f"{format_items(proprietary)} are proprietary"
        log(msg)
    return rv  # type: ignore[return-value]


def load_cached_packages(cache_file: str, log: LogWriter) -> tuple[PackageDb, float] | None:
    cache_file_meta = f"{cache_file}.meta"
    if not all(map(os.path.exists, (cache_file_meta, cache_file))):
        return None
    try:
        with open(cache_file_meta, 'r', encoding='utf-8') as f:
            timestamp = int(f.read())
        if time.time() - timestamp > 600:  # 10 minutes
            return None
        with open(cache_file, 'r', encoding='utf-8') as f:
            return json.load(f), timestamp
    except Exception as e:
        log(f"Failed to load cache: {e}")
    return None


def save_packages_cache(cache_file: str, packages_data: PackageDb, log: LogWriter) -> None:
    cache_file_meta = f"{cache_file}.meta"
    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
    try:
        with open(cache_file_meta, 'w', encoding='utf-8') as f:
            f.write(str(int(time.time())))

        if os.path.exists(cache_file):
            with open(cache_file, 'r', encoding='utf-8') as f:
                if json.load(f) == packages_data:
                    return
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(packages_data, f)
    except Exception as e:
        log(f"Failed to save cache: {e}")


def supported_domain(url: str) -> bool:
    return any(
        domain in url
        for domain in ["github.com", "gitlab.com", "bitbucket.org"]
    )


def extract_name_from_url(url: str) -> str | None:
    # Extract the name from the URL
    if supported_domain(url) and (
        match := re.search(r"/([^/]+?)(?:\.git)?$", url.rstrip("/"))
    ):
        return match.group(1)
    return None


def website_to_https_git(website: str) -> str | None:
    """
    https://github.com/alexkuz/SublimeLinter-inline-errors
    => https://github.com/alexkuz/SublimeLinter-inline-errors.git
    """
    if supported_domain(website):
        return website + ".git"
    return None


def https_git_to_ssh_git(website: str) -> str | None:
    """
    https://github.com/alexkuz/SublimeLinter-inline-errors.git
    => git@github.com:alexkuz/SublimeLinter-inline-errors.git
    """
    if supported_domain(website):
        return website.replace("https://", "git@").replace("/", ":", 1)
    return None


def compute_refs_from_releases(
    releases: list[dict], build: int, platform: str, default: str = "tags/*"
) -> str:
    """Convert package release info to Git ref pattern.

    Args:
        releases: List of release configurations
        build: Sublime Text build number

    Returns:
        Git refs pattern like "tags/*" or "heads/master"
    """
    for release in reversed(releases):
        requirement = release.get("sublime_text", "*")
        if not _fulfills_build_requirement(requirement, build):
            continue
        requirement = release.get("platforms", "*")
        if not _fulfills_platform_requirement(requirement, platform):
            continue

        if "tags" in release:
            if isinstance(release["tags"], bool):
                return "tags/*"
            elif re.match(r'^(st\d+-|\d+-)', release["tags"]):
                # We support "st3-", "4070-", or "st4651" prefixes
                # out-of-the-box, t.i. we match the build without
                # needing any metadata.
                return "tags/*"
            elif isinstance(release["tags"], str):
                return f"tags/{release['tags']}*"
        elif "branch" in release:
            return f"heads/{release['branch']}"

    # Default if no matching release found?
    # The user knows the package and overrules the requirements?
    return default


def _fulfills_build_requirement(requirement: str, build: int) -> bool:
    """Check if build meets the requirement string."""
    try:
        requirement = requirement.replace(" ", "")
        if requirement == "*":
            return True

        # Handle range format (e.g. "3000-3909")
        if "-" in requirement:
            min_build, max_build = map(int, requirement.split("-"))
            return min_build <= build <= max_build

        # Handle comparison operators
        if requirement.startswith(">="):
            return build >= int(requirement[2:])
        if requirement.startswith("<="):
            return build <= int(requirement[2:])
        if requirement.startswith(">"):
            return build > int(requirement[1:])
        if requirement.startswith("<"):
            return build < int(requirement[1:])

        # Exact match
        return build == int(requirement)
    except Exception:
        return False


def _fulfills_platform_requirement(requirement: str | list[str], platform: str) -> bool:
    if isinstance(requirement, str):
        requirement = [requirement]

    if requirement == ["*"]:
        return True

    # Check if any requirement matches the platform
    return any(
        # Either exact match or platform-specific match
        platform == req or req.startswith(platform)
        for req in requirement
    )
