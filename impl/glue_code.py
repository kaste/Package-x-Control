from __future__ import annotations
from concurrent.futures import Future
from contextlib import contextmanager
from functools import partial
import os
import re
import shutil
import urllib.parse

import sublime

from package_control.package_disabler import PackageDisabler
from package_control.package_tasks import PackageTaskRunner
from package_control.activity_indicator import ActivityIndicator

from .config import BUILD, PACKAGE_CONTROL_OVERRIDE, PACKAGES_REPOSITORY, ROOT_DIR
from .config_management import (
    PackageConfiguration,
    add_package_to_configuration,
    get_configuration,
    process_config,
    remove_package_from_configuration,
)
from .git_package import PackageInfo, check_package, update_package, GitCallable
from .repository import (
    add_package_to_repository,
    create_package_entry,
    recreate_repository,
    remove_package_from_repository
)
from .pc_repository import reverse_lookup
from .runtime import gather
from .utils import drop_falsy, remove_suffix
from . import worker


_check_package  = partial(check_package,  root_dir=ROOT_DIR, st_build=BUILD, Git=GitCallable)  # noqa: E221, E241, E501


def get_update_info(entry: PackageConfiguration) -> PackageInfo:
    return update_package(entry, ROOT_DIR, BUILD, GitCallable)


def sync_managed_packages_with_package_control() -> None:
    def check_package_(entry: PackageConfiguration) -> Future[PackageInfo]:
        return worker.add_task(entry["name"], _check_package, entry)

    config_data = get_configuration()
    packages = gather([check_package_(entry) for entry in process_config(config_data)])
    installed_packages = [
        create_package_entry(package_info)
        for package_info in packages
        if package_info["version"]
    ]
    recreate_repository(installed_packages, PACKAGES_REPOSITORY)
    overwrite_package_control_data([p["name"] for p in installed_packages])
    # fire-and-forget
    worker.add_task(
        "cleanup_orphaned_packages", cleanup_orphaned_packages, [p["name"] for p in packages]
    )


def cleanup_orphaned_packages(packages: list[str]) -> None:
    orphaned_directories = [
        full_path
        for name in os.listdir(ROOT_DIR)
        if not name.startswith(".")
        if name not in packages
        if (full_path := os.path.join(ROOT_DIR, name))
        if os.path.isdir(full_path)
    ]
    for directory in orphaned_directories:
        shutil.rmtree(directory, ignore_errors=True)


def update_all_managed_packages() -> None:
    def update_info_(entry: PackageConfiguration) -> Future[PackageInfo]:
        return worker.add_task(entry["name"], get_update_info, entry)

    with ActivityIndicator('Preparing...') as progress:
        config_data = get_configuration()
        packages = [
            create_package_entry(package_info)
            for package_info in gather(
                [update_info_(entry) for entry in process_config(config_data)]
            )
            if package_info["status"] == "needs-update"
        ]
        recreate_repository(packages, PACKAGES_REPOSITORY)
        installed_packages = [package["name"] for package in packages]
        overwrite_package_control_data(installed_packages)
        call_the_pc(installed_packages, progress, unattended=True)


def install_package(entry: PackageConfiguration):
    with ActivityIndicator('Preparing...') as progress:
        package_info = get_update_info(entry)
        if package_info["version"]:
            add_package_to_configuration(entry)
            package = create_package_entry(package_info)
            add_package_to_repository(package, PACKAGES_REPOSITORY)
            add_package_to_package_control_data(package["name"])
            call_the_pc([package["name"]], progress, unattended=False)


def install_package_from_name(name: str):
    package_entry: PackageConfiguration
    url = None
    if name.startswith("https://packagecontrol.io/packages/"):
        name = urllib.parse.unquote(name[35:])
    elif url := parse_url_from_clipboard(name):
        name = remove_suffix(url.rsplit("/", 1)[1], ".git")

    if package_control_entry := reverse_lookup(name):
        if "git_url" in package_control_entry:
            package_entry = {
                "name": package_control_entry["name"],
                "url": url or package_control_entry["git_url"],
                "refs": package_control_entry["refs"],
                "unpacked": False
            }
            install_package(package_entry)
        else:
            # delegate to Package Control to install proprietary package
            sublime.run_command("install_packages", {
                "packages": [package_control_entry["name"]]
            })
    elif url:
        package_entry = {
            "name": name,
            "url": url,
            "refs": "tags/*",
            "unpacked": False
        }
        install_package(package_entry)


def remove_package_by_name(name: str):
    remove_package_from_configuration(name)
    remove_package_from_repository(name, PACKAGES_REPOSITORY)
    remove_package_from_package_control_data(name)
    sublime.run_command("remove_packages", {"packages": [name]})


def disable_packages_by_name(names: list[str]) -> None:
    unique_packages = set(names)
    disabled = PackageDisabler.disable_packages({PackageDisabler.DISABLE: unique_packages})

    num_packages = len(unique_packages)
    num_disabled = len(disabled)
    if num_packages == num_disabled:
        if num_packages == 1:
            message = 'Package %s successfully disabled.' % names[0]
        else:
            message = '%d packages have been disabled.' % num_disabled
    else:
        message = '%d of %d packages have been disabled.' % (num_disabled, num_packages)

    sublime.status_message(message)


def enable_packages_by_name(names: list[str]) -> None:
    unique_packages = set(names)
    PackageDisabler.reenable_packages({PackageDisabler.ENABLE: unique_packages})

    if len(unique_packages) == 1:
        message = 'Package %s successfully enabled.' % names[0]
    else:
        message = '%d packages have been enabled.' % len(unique_packages)

    sublime.status_message(message)


HUBS = [
    "https://github.com/",
    "https://gitlab.com/",
    "https://bitbucket.org/"
]


def parse_url_from_clipboard(clip_content):
    # type: (str) -> str
    if not clip_content:
        return ""

    if (
        clip_content.endswith(".git")
        and re.match(r"^(https?|git)://|git@", clip_content)
    ):
        return clip_content

    for hub in HUBS:
        if clip_content.startswith(hub):
            path = clip_content[len(hub):]
            try:
                owner, name = drop_falsy(path.split("/")[:2])
            except ValueError:
                return ""
            else:
                return "{}{}/{}.git".format(hub, owner, name)
    return ""


def call_the_pc(packages: list[str], progress: ActivityIndicator, unattended: bool = True):
    """Delegate to Package Control to install or upgrade packages."""
    upgrader = PackageTaskRunner()
    tasks = upgrader.create_package_tasks(
        actions=(upgrader.INSTALL, upgrader.UPGRADE, upgrader.DOWNGRADE, upgrader.REINSTALL),
        include_packages=packages,
        ignore_packages=upgrader.ignored_packages()  # don't upgrade disabled packages
    )
    to_install, to_upgrade = [], []
    for task in tasks:
        if task.action == upgrader.INSTALL:
            to_install.append(task)
        elif task.action in (upgrader.UPGRADE, upgrader.DOWNGRADE, upgrader.REINSTALL):
            to_upgrade.append(task)

    if to_install:
        upgrader.run_install_tasks(to_install, progress, unattended)
    if to_upgrade:
        upgrader.run_upgrade_tasks(to_upgrade, progress, unattended)


@contextmanager
def mutate_package_control_data():
    # PACKAGE_CONTROL_OVERRIDE runs besides the standard settings
    # machinery and must be read and updated manually.
    with open(PACKAGE_CONTROL_OVERRIDE, "r", encoding="utf-8") as f:
        text = f.read()
    value = sublime.decode_value(text)
    installed_packages = value.get("installed_packages", [])
    copy = installed_packages.copy()

    yield installed_packages
    if installed_packages == copy:
        return

    with open(PACKAGE_CONTROL_OVERRIDE, 'w', encoding='utf-8') as f:
        f.write(sublime_encode_value(value, update_text=text))


def overwrite_package_control_data(package_names: list[str]) -> None:
    """
    Write the list of installed packages to the Package Control override file.

    This is used to ensure that Package Control recognizes the packages
    installed by us.  Otherwise, Package Control may try to remove them
    when it runs its own installation process.
    """
    with mutate_package_control_data() as installed_packages:
        installed_packages[:] = package_names


def add_package_to_package_control_data(package_name: str) -> None:
    with mutate_package_control_data() as installed_packages:
        if package_name not in installed_packages:
            installed_packages.append(package_name)


def remove_package_from_package_control_data(package_name: str) -> None:
    with mutate_package_control_data() as installed_packages:
        try:
            installed_packages.remove(package_name)
        except ValueError:
            return


if BUILD >= 4156:
    sublime_encode_value = sublime.encode_value
else:
    def sublime_encode_value(
        val: object, pretty: bool = False, update_text: str = None
    ) -> str:
        return sublime.encode_value(val, pretty=pretty)
