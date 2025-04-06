from __future__ import annotations
from collections import defaultdict
from functools import lru_cache
import os
import re
import shutil
import subprocess
import sys
import threading

import sublime
import sublime_plugin

from .config_management import PackageConfiguration
from .utils import drop_falsy, human_date, remove_lr, remove_prefix
from package_control.pep440 import PEP440Version


from typing import Literal, NamedTuple, TypedDict, TypeVar
from typing_extensions import Required, TypeAlias


PACKAGE_NAME = "Package Control X"
T = TypeVar('T')
Ref: TypeAlias = str
Sha: TypeAlias = str


class GitCallable:
    def __init__(self, repo_path: str):
        self.repo_path = repo_path

    @property
    def git_dir(self) -> str:
        return os.path.join(self.repo_path, ".git")

    def __call__(self, *args: str, check: bool = True) -> str:
        return git(*args, cwd=self.repo_path, check=check)


if sys.platform == "win32":
    STARTUPINFO = subprocess.STARTUPINFO()
    STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
else:
    STARTUPINFO = None


def git(*args, cwd: str, check: bool = True) -> str:
    return subprocess.run(
        (git_binary(),) + drop_falsy(args),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=check,
        startupinfo=STARTUPINFO
    ).stdout.strip()


@lru_cache(1)
def git_binary() -> str:
    return shutil.which("git") or "git"


class NotInstallablePackage(TypedDict):
    name: Required[str]
    url: Required[str]
    package_dir: Required[str]
    version: Literal[None]
    status: Literal["not-installed", "no-suitable-version-found"]


class InstallablePackage(TypedDict):
    name: Required[str]
    url: Required[str]
    package_dir: Required[str]
    version: Required[Version]
    status: Literal["installed", "up-to-date", "needs-update"]
    timestamp: Required[int]
    zip_file: Required[str]


PackageInfo: TypeAlias = "InstallablePackage | NotInstallablePackage"




def check_package(
    config: PackageConfiguration,
    root_dir: str,
    st_build: int,
    Git: type[GitCallable],
) -> PackageInfo:
    git = ensure_repository(config, root_dir, Git)
    version = describe_current_commit(git)
    if not version:
        return {
            "name": config["name"],
            "url": config["url"],
            "package_dir": git.repo_path,
            "version": None,
            "status": "not-installed",
        }

    zip_file = os.path.join(root_dir, f"{config['name']}.zip")
    if not os.path.exists(zip_file):
        create_archive(zip_file, git)

    return {
        "name": config["name"],
        "url": config["url"],
        "package_dir": git.repo_path,
        "version": version,
        "status": "installed",
        "timestamp": get_commit_date(version.sha, git),
        "zip_file": zip_file,
    }


def update_package(
    config: PackageConfiguration,
    root_dir: str,
    st_build: int,
    Git: type[GitCallable],
) -> PackageInfo:
    """
    Configure a local git repository for a package and fetch the appropriate ref.

    Args:
        config: Package configuration with name, url, and refs
        root_dir: Root directory for all package directories
        st_build: Current build number of the host
        Git: Factory for creating GitCallable instances

    """
    git = ensure_repository(config, root_dir, Git)
    update_info = check_for_updates(config["refs"], st_build, git)
    if update_info["status"] == "no-suitable-version-found":
        return {
            "name": config["name"],
            "url": config["url"],
            "package_dir": git.repo_path,
            "version": None,
            "status": update_info["status"],
        }

    if update_info["status"] == "needs-update":
        if refname := update_info["version"].refname:
            # make sure to populate the local refs
            ref = f'{refname}:{refname}'
        else:
            ref = update_info["version"].sha
        git("fetch", "--depth=1", "origin", ref)
        git("checkout", "FETCH_HEAD")

    zip_file = os.path.join(root_dir, f"{config['name']}.zip")
    if (
        update_info["status"] == "needs-update"
        or not os.path.exists(zip_file)
    ):
        create_archive(zip_file, git)

    return {
        "name": config["name"],
        "url": config["url"],
        "package_dir": git.repo_path,
        "version": update_info["version"],
        "status": update_info["status"],
        "timestamp": get_commit_date(update_info["version"].sha, git),
        "zip_file": zip_file,
    }


class NoUpdateInfo(TypedDict):
    status: Literal["no-suitable-version-found"]
    version: None


class UpdateInfo_(TypedDict):
    status: Literal["up-to-date", "needs-update"]
    version: Version


UpdateInfo: TypeAlias = "UpdateInfo_ | NoUpdateInfo"


class Version(NamedTuple):
    refname: Ref | None
    sha: Sha


def check_for_updates(refs: str, st_build: int, git: GitCallable) -> UpdateInfo:
    next_version = best_version_for(refs, st_build, git)
    if next_version:
        return {
            "version": next_version,
            "status": (
                "up-to-date"
                if package_is_up_to_date(next_version.sha, git)
                else "needs-update"
            )
        }
    else:
        return {
            "version": None,
            "status": "no-suitable-version-found"
        }


def best_version_for(refs: str, st_build: int, git: GitCallable) -> Version | None:
    if refs.startswith("tags/") and "*" in refs:
        # Typically: "tags/*" or "tags/4070-*"
        tags = fetch_remote_tags(f"refs/{refs}", git)
        if refs == "tags/*":
            tags = filter_tags(tags, st_build)
        tag = max(tags.items(), key=lambda it: parse_version(it[0]), default=None)
        if tag:
            return Version(f"refs/tags/{tag[0]}", tag[1])
    elif "/" in refs:
        # E.g. "heads/master", "pull/1909", "tags/2.1.9"
        # In case of tags, we want the dereferences sha (if there is one)
        # so we add "*".  These names should be unique otherwise.
        for refname, sha in fetch_remote_refs(f"refs/{refs}*", git).items():
            if remove_prefix(refname, "refs/") == refs:
                return Version(refname, sha)
    else:
        # Everything else is treated as if a commit hash is wanted.
        # Basically: freeze the checked out version
        return Version(None, refs)
    return None


def create_archive(target: str, git: GitCallable) -> None:
    git("archive", "--format=zip", "-o", target, "HEAD")


def package_is_up_to_date(commit_hash: str, git: GitCallable) -> bool:
    """
    Check if the package is up-to-date with the specified commit hash.
    """
    return commit_hash == current_commit(git)


def current_commit(git: GitCallable) -> str | None:
    try:
        return git("rev-parse", "HEAD").strip()
    except Exception:
        return None


def describe_current_commit(git: GitCallable) -> Version | None:
    commit_hash = current_commit(git)
    if commit_hash is None:
        return None

    map = fetch_local_refs(git)
    # try to find a tag first
    for name, sha in map.items():
        if not name.startswith("refs/tags/"):
            continue
        if sha == commit_hash:
            return Version(name, sha)
    for name, sha in map.items():
        if not name.startswith("refs/"):  # filter out "HEAD" etc.
            continue
        if sha == commit_hash:
            return Version(name, sha)
    return Version(None, commit_hash)


def status_for_package(config: PackageConfiguration, root: str, Git: type[GitCallable]):
    git = ensure_repository(config, root, Git)
    version = describe_current_commit(git)
    return {
        "name": remove_lr(config["url"], "https://github.com/", ".git"),
        **(version_info(version, git) if version else {})
    }


def version_info(version: Version, git: GitCallable) -> dict:
    return {
        "version": simplify_version(version),
        "date": human_date(get_author_date(version.sha, git))
    }


def simplify_version(version: Version) -> tuple[str, str]:
    refname, sha = version
    if refname is None:
        return ("commit", sha[:8])
    elif refname.startswith("refs/tags/"):
        return ("tag", remove_prefix(refname, "refs/tags/"))
    elif refname.startswith("refs/heads/"):
        return ("branch", remove_prefix(refname, "refs/heads/"))
    else:
        topic, detail = remove_prefix(refname, "refs/").split("/", 1)
        return (topic, detail)


def get_commit_date(sha: str, git: GitCallable) -> int:
    """Get commit timestamp as Unix epoch seconds."""
    try:
        return int(git("show", sha, "--no-patch", "--format=%ct").strip())
    except Exception:
        git("fetch", "--depth=1", "origin", sha)
        return int(git("show", sha, "--no-patch", "--format=%ct").strip())


def get_author_date(sha: str, git: GitCallable) -> int:
    try:
        return int(git("show", sha, "--no-patch", "--format=%at").strip())
    except Exception:
        git("fetch", "--depth=1", "origin", sha)
    return int(git("show", sha, "--no-patch", "--format=%at").strip())


def parse_version(version: str) -> PEP440Version | tuple:
    """
    Convert a version string into a tuple for comparison.

    Args:
        version: A version string (e.g., "1.2.3", "1.2.3-alpha")

    Returns:
        A tuple with integer and string parts for comparison
    """
    version = strip_possible_prefix(version)
    try:
        return PEP440Version(version)
    except Exception:
        return tuple(
            int(part) if part.isdigit() else part
            for part in re.split(r'[.-]', version)
        )


def strip_possible_prefix(version: str) -> str:
    """Strip possible build prefixes from a tag."""
    return re.sub(r'^(st\d+-|\d+-)', '', version)


def fetch_remote_refs(refs: str, git: GitCallable) -> dict[Ref, Sha]:
    # Fetch remote refs
    ls_remote_output = git(
        "ls-remote", "--sort=v:refname", "origin", refs
    )
    return parse_ref_output(ls_remote_output)


def fetch_remote_tags(refs: str, git: GitCallable) -> dict[Ref, Sha]:
    # Fetch remote tags
    ls_remote_output = git(
        "ls-remote", "--sort=v:refname", "origin", refs
    )
    return parse_ref_output(ls_remote_output, "refs/tags/")


def fetch_local_refs(git: GitCallable) -> dict[Ref, Sha]:
    output = git("show-ref", "--dereference", check=False)
    return parse_ref_output(output)


def filter_tags(tags: dict[Ref, Sha], build: int) -> dict[Ref, Sha]:
    """Filter tags based on package authors strategy and users build number."""
    tags_by_build: defaultdict[int, dict[str, str]]
    if any(re.match(r'^st\d+-', tag) for tag in tags.keys()):
        # Handle tags that start with 'st' followed by digits and a hyphen
        tags_by_build = defaultdict(dict)
        for tag, commit in tags.items():
            if (match := re.match(r'^st(\d+)-', tag)) and (prefix := match.group(1)):
                prefix = 3999 if prefix == "3" else int(prefix)
            else:
                prefix = 0
            tags_by_build[prefix][tag] = commit
        highest_prefix = min(
            (prefix for prefix in tags_by_build.keys() if prefix >= build),
            default=None
        )
        if highest_prefix is not None:
            tags = tags_by_build[highest_prefix]
        else:
            tags = tags_by_build[0]

    elif any(re.match(r'^\d+-', tag) for tag in tags.keys()):
        # Handle tags that start with digits followed by a hyphen
        tags_by_build = defaultdict(dict)
        for tag, commit in tags.items():
            prefix = int(match.group(1)) if (match := re.match(r'^(\d+)-', tag)) else 3000
            tags_by_build[prefix][tag] = commit

        highest_prefix = max(
            (prefix for prefix in tags_by_build.keys() if prefix <= build),
            default=None
        )
        if highest_prefix is not None:
            tags = tags_by_build[highest_prefix]
        else:
            tags = {}

    return tags


def parse_ref_output(stdout: str, remove_prefix: str = "") -> dict[Ref, Sha]:
    # Parse the output and organize tag data
    rv = {}  # {ref: commit_hash}
    for line in stdout.strip().split("\n"):
        if not line:
            continue

        parts = line.split()
        if len(parts) != 2:
            continue

        """
        Example line format:
        c80596e48e4fedd78596a66b3d79c67488f828aa        refs/tags/2.47.1
        f3fad6a5617c802c95b46c4eeada797bc282e7cd        refs/tags/2.47.1^{}
        """
        commit_hash, ref = parts

        ref = ref[len(remove_prefix):]
        # Check if it's a dereferenced tag (^{})
        is_deref = ref.endswith("^{}")
        if is_deref:
            ref = ref[:-3]  # Remove suffix "^{}"
            rv[ref] = commit_hash
        else:
            rv.setdefault(ref, commit_hash)

    return rv


def ensure_repository(
    config: PackageConfiguration, root_dir: str, Git: type[GitCallable]
) -> GitCallable:
    """
    Ensure a local git repository is properly configured for a package.

    """
    # Create/use package directory
    package_name = config['name']
    package_dir = os.path.join(root_dir, package_name)
    os.makedirs(package_dir, exist_ok=True)

    git = Git(package_dir)
    if not os.path.exists(git.git_dir):
        git("init")

    # Always set the remote to ensure it follows configuration changes
    configure_remote(config['url'], git)
    return git


def configure_remote(remote_url: str, git: GitCallable):
    try:
        git("remote", "add", "origin", remote_url)
    except Exception:
        # Remote exists, update URL
        git("remote", "set-url", "origin", remote_url)
