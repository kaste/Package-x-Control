from __future__ import annotations
from contextlib import contextmanager
import json
import os
import time
import urllib.request

from typing import TypedDict
from typing_extensions import NotRequired, Required, TypeAlias

from package_control.cache import clear_cache

from .config_management import extract_repo_name, extract_user
from .git_package import InstallablePackage, Version, strip_possible_prefix
from .utils import remove_prefix


REPOSITORY_TEMPLATE = {
    "schema_version": "2.0",
    "packages": [
        # {
        #     "name": "SublimeLinter",
        #     "description": "A full-featured linter framework",
        #     "author": "et.al.",
        #     "homepage": "https://sublimelinter.io",
        #     "donate": "https://sublimelinter.io/about",
        #     "last_modified": "2024-04-23 12:43:50",
        #     "releases": [
        #         {
        #             "sublime_text": "*",
        #             "version": "23.2.2",
        #             "url": "http://localhost:8000/sublime_linter.sublime-package",
        #             "date": "2024-04-23 12:43:50",
        #         }
        #     ],
        # }
    ],
}


class PackageEntry(TypedDict):
    name: str
    description: str
    author: str
    homepage: str
    last_modified: str
    releases: list[Release]
    previous_names: NotRequired[list[str]]


class Release(TypedDict):
    sublime_text: str
    version: str
    url: str
    date: str


def ensure_repository_registry(fpath: str) -> None:
    if not os.path.exists(fpath):
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(REPOSITORY_TEMPLATE, f)


@contextmanager
def mutate_repository(fpath: str):
    ensure_repository_registry(fpath)
    with open(fpath, "r") as f:
        repo_data = json.load(f)

    yield repo_data["packages"]

    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(repo_data, f, indent=4)
    clear_cache()


def recreate_repository(entries: list[PackageEntry], repo_file: str):
    with mutate_repository(repo_file) as packages:
        # Call add_package_entry for each entry as it
        # ought to handle renames for us.
        seen = set()
        for entry in entries:
            seen.add(entry["name"])
            add_package_entry(entry, packages)
        # Cleanup packages with the same name or homepage
        packages[:] = cleanup_packages(packages)
        # Remove packages not in the new set
        packages[:] = [p for p in packages if p["name"] in seen]


def add_package_to_repository(package: PackageEntry, repo_file: str):
    with mutate_repository(repo_file) as packages:
        add_package_entry(package, packages)


def remove_package_from_repository(package: str | PackageEntry, repo_file: str):
    if not isinstance(package, str):
        package = package["name"]

    with mutate_repository(repo_file) as packages:
        packages[:] = [p for p in packages if p["name"] != package]


def create_package_entry(package_info: InstallablePackage) -> PackageEntry:
    """Create package entry for Package Control from package info.

    See REPOSITORY_TEMPLATE for the structure of the package entry.
    """
    timestamp = package_info["timestamp"]
    last_modified = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(timestamp))
    return {
        "name": package_info["name"],
        # We could pull that from the repository for registered apps if
        # we wanted to, but unregistered apps don't have a description
        # per definition (unless we'd use Github API calls to get the
        # description of the repository).
        # Anyway, it's likely not mandatory.
        "description": "",
        "author": extract_user(package_info["url"]),
        "homepage": package_info["url"],
        "last_modified": last_modified,
        "releases": [
            {
                "sublime_text": "*",
                "version": format_as_package_version(
                    package_info["version"], timestamp
                ),
                "url": path_to_file_url(package_info["zip_file"]),
                "date": last_modified,
            }
        ]
    }


def path_to_file_url(path: str) -> str:
    """
    Convert a file path to a file:/// URL using standard library functions.
    """
    url_path = urllib.request.pathname2url(path)
    return f"file:{url_path}"


def format_as_package_version(version: Version, timestamp: float) -> str:
    """
    Return a version for Package Control that fulfills `PEP440Version`.
    For tags, return the "stripped" version, e.g.
        4.0.1 for "st4070-4.0.1", "4070-4.0.1", or "4.0.1".
    For everything else, return a calendar version with a build suffix, e.g.
        2025.12.31+deadbeef
    """
    refname, sha = version
    if refname is not None and refname.startswith("refs/tags/"):
        return strip_possible_prefix(remove_prefix(refname, "refs/tags/"))
    return f"{time.strftime('%Y.%m.%d', time.gmtime(timestamp))}+{sha[:8]}"


def add_package_entry(package_entry: PackageEntry, packages: list[PackageEntry]):
    for i, entry in enumerate(packages):
        if entry["name"] == package_entry["name"]:
            packages[i] = package_entry
            break
        if (
            entry["homepage"] == package_entry["homepage"]
            or (
                extract_repo_name(entry["homepage"])
                == extract_repo_name(package_entry["homepage"])
            )
        ):
            package_entry["previous_names"] = [entry["name"]]
            packages[i] = package_entry
            break
    else:
        packages.append(package_entry)


def cleanup_packages(packages: list[PackageEntry]) -> list[PackageEntry]:
    """
    Remove duplicate entries from the package list.

    Args:
        packages: A list of package entries

    Returns:
        A list of package entries with duplicates removed
    """
    seen: set[str] = set()
    rv = []
    for package in packages:
        names = {
            package["name"],
            package["homepage"],
            extract_repo_name(package["homepage"])
        }
        if seen.intersection(names):
            continue
        seen.update(names)
        rv.append(package)
    return rv
