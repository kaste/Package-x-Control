from __future__ import annotations
import re

import sublime

from typing import TypedDict, overload, Literal
from typing_extensions import Required, TypeAlias


from .config import PACKAGE_SETTINGS, PLATFORM
from .utils import remove_lr


ConfigEntry: TypeAlias = "str | PackageConfiguration"
ConfigData: TypeAlias = "list[ConfigEntry]"


class PackageConfiguration(TypedDict, total=False):
    name: Required[str]
    url: Required[str]
    refs: Required[str]
    unpacked: Required[bool]


def get_configuration() -> ConfigData:
    s = sublime.load_settings(PACKAGE_SETTINGS)
    return s.get("packages", [])


def persist_configuration(config: ConfigData) -> None:
    s = sublime.load_settings(PACKAGE_SETTINGS)
    s.set("packages", config)
    sublime.save_settings(PACKAGE_SETTINGS)


def add_package_to_configuration(entry: PackageConfiguration) -> None:
    config_data = get_configuration()
    add_entry_to_configuration(entry, config_data)
    persist_configuration(config_data)


def can_add_package_to_configuration(
    entry: PackageConfiguration
) -> tuple[str, PackageConfiguration] | Literal[True]:
    config_data = get_configuration()
    return add_entry_to_configuration(entry, config_data, dry_run=True)


def remove_package_from_configuration(name: str) -> None:
    config_data = get_configuration()
    remove_entry_by_name(name, config_data)
    persist_configuration(config_data)


def process_config(config_data: ConfigData) -> list[PackageConfiguration]:
    """
    Process configuration data, filling in defaults and expanding URLs.

    Args:
        config_data: A list of configuration entries

    Returns:
        A list of normalized configuration dictionaries

    Raises:
        ValueError if the configuration has duplicate or invalid entries
    """
    rv = []
    for entry in config_data:
        try:
            config_entry = normalize_config_entry(entry)
        except ValueError as e:
            print(f"Error processing configuration entry: {e}")
        else:
            if "example/example-plugin" in config_entry["url"]:
                continue
            rv.append(config_entry)

    _check_for_duplicates(rv)
    return rv


def _check_for_duplicates(entries: list[PackageConfiguration]) -> None:
    """Check for duplicate entries in the configuration."""
    seen = set()
    messages = []
    for entry in entries:
        name = entry["name"]
        url = entry["url"]
        repo_name = extract_repo_name(url)
        if name in seen:
            messages.append(f"Duplicate package name: {name}")
        if repo_name in seen:
            messages.append(f"Duplicate package repository base name: {repo_name}")
        if url in seen:
            messages.append(f"Duplicate package url: {url}")
        seen.update({name, repo_name, url})

    if messages:
        messages.append("Duplicate entries found in configuration.")
        messages.append(
            "Please ensure that each package has a unique name, "
            "repository base name, and URL."
        )
        messages.append("Aborting.")
        raise ValueError("\n".join(messages))


def normalize_config_entry(entry: str | PackageConfiguration) -> PackageConfiguration:
    """
    Normalize a configuration entry by filling in defaults and expanding URLs.

    Args:
        entry: A configuration entry (string or dictionary)

    Returns:
        A normalized configuration dictionary
    """
    config = {
        "refs": "tags/*",
        "unpacked": False,
    }
    if isinstance(entry, str):
        # Simple string entry: "username/repository"
        config.update({
            "name": extract_repo_name(entry),
            "url": expand_git_url(entry),
        })
        return config  # type: ignore[return-value]

    elif isinstance(entry, dict):
        # Dictionary entry with potential defaults
        try:
            url = entry["url"]
        except KeyError:
            raise ValueError("Missing required 'url' field in package configuration")
        if not isinstance(url, str):
            raise ValueError(f"Unexpected url type: {type(entry)}")

        config.update(entry)
        config["url"] = expand_git_url(url)
        if "name" not in config:
            config["name"] = extract_repo_name(url)
        return config  # type: ignore[return-value]
    else:
        raise ValueError(f"Unexpected configuration entry type: {type(entry)}")


@overload
def add_entry_to_configuration(  # noqa: E704
    entry: PackageConfiguration,
    config: ConfigData,
    dry_run: Literal[True]
) -> tuple[str, PackageConfiguration] | Literal[True]: ...

@overload                        # noqa: E302
def add_entry_to_configuration(  # noqa: E704
    entry: PackageConfiguration,
    config: ConfigData,
    dry_run: Literal[False] = False
) -> None: ...

def add_entry_to_configuration(  # noqa: E302
    entry: PackageConfiguration,
    config: ConfigData,
    dry_run: bool = False
) -> tuple[str, PackageConfiguration] | Literal[True] | None:
    """Add or update an entry in the configuration."""
    for i, item in enumerate(process_config(config)):
        if conflict := _match_items(entry, item):
            if dry_run:
                return (conflict, item)
            config[i] = simplify_entry(entry)
            return None

    if dry_run:
        return True
    config.append(simplify_entry(entry))
    return None


def _match_items(a: PackageConfiguration, b: PackageConfiguration) -> str | Literal[False]:
    """Check if an entry conflicts with an existing item."""
    if a["name"] == b["name"]:
        return "entry with the same name"
    if a["url"] == b["url"]:
        return "entry with the same url"
    if extract_repo_name(a["url"]) == extract_repo_name(b["url"]):
        return "entry with the same repo name"
    return False


def simplify_entry(entry: PackageConfiguration) -> str | PackageConfiguration:
    if (
        extract_repo_name(entry["url"]) == entry["name"]
        and entry["refs"] == "tags/*"
        and not entry["unpacked"]
    ):
        if entry["url"].startswith("https://github.com/"):
            return remove_lr(entry["url"], "https://github.com/", ".git")
        return entry["url"]
    return entry


def remove_entry_by_url(url: str, config: ConfigData):
    for i, item in enumerate(process_config(config)):
        if item["url"] == url:
            del config[i]
            break
    else:
        return ("url not found")


def remove_entry_by_name(name: str, config: ConfigData):
    for i, item in enumerate(process_config(config)):
        if item["name"] == name:
            del config[i]
            break
    else:
        return ("name not found")


def expand_git_url(url: str) -> str:
    """
    Expand a URL to a full Git URL.

    If the URL is in the format "username/repository", it is expanded to
    "https://github.com/username/repository.git".
    Otherwise, the URL is returned as is assumed to be a complete git URL
    that can be used as a remote.
    """
    # Check if the URL is a GitHub shortname (username/repository)
    if re.match(r'^[\w-]+/[\w-]+$', url):
        return f"https://github.com/{url}.git"

    # Check for invalid characters in the URL.  This does not need to
    # be comprehensive, but should catch common typos.
    if re.search(r'[\s<>"\'\\^{}|`]', url):
        raise ValueError(f"Invalid characters in URL: '{url}'")

    if "/" not in url:
        message = f"Doesn't look like a URL or path: '{url}'"
        if PLATFORM.startswith("windows"):
            raise ValueError(message + '. For local paths, use forward slashes ("/").')
        raise ValueError(message)

    if url.startswith("."):
        raise ValueError(f"no relative paths allowed: '{url}'")

    # Otherwise, return the URL as is
    return url


def extract_repo_name(url: str) -> str:
    """
    Extract the repository name from a URL or GitHub shortname.

    For shortnames like "username/repository", the repository name is "repository".
    For full URLs, the repository name is extracted from the path.
    """
    url = url.rsplit('/', 1)[-1]
    if url.endswith('.git'):
        url = url[:-4]
    return url


def extract_user(url: str) -> str:
    """
    https://github.com/alexkuz/SublimeLinter-inline-errors.git
    git@github.com:alexkuz/SublimeLinter-inline-errors.git
    => alexkuz
    """
    if url.startswith("git@"):
        return url.split(":")[1].split("/")[0]
    return url.split("/")[-2]
