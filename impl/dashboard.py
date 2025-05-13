from __future__ import annotations

from concurrent.futures import TimeoutError
from functools import partial
from itertools import chain
import os
from textwrap import wrap
import urllib.parse
import re
from webbrowser import open as open_in_browser

from typing import (
    Callable, Iterable, Iterator, Sequence
)

import sublime
import sublime_plugin

from package_control.activity_indicator import ActivityIndicator
from package_control.package_manager import PackageManager

from .config import (
    BACKUP_DIR, INSTALLED_PACKAGES_PATH, PACKAGE_CONTROL_PREFERENCES,
    PACKAGES_PATH,
)
from .config_management import (
    PackageConfiguration,
    extract_repo_name, get_configuration, process_config
)
from .git_package import GitCallable
from .glue_code import (
    disable_packages_by_name, enable_packages_by_name,
    install_package, install_proprietary_package,
    remove_package_by_name, remove_proprietary_package_by_name
)
from .the_registry import extract_name_from_url, PackageControlEntry
from .runtime import cooperative, AWAIT_UI, AWAIT_WORKER
from .utils import (
    drop_falsy, format_items, human_date, remove_suffix,
    rmfile, rmtree, show_actions_panel, show_input_panel
)
from . import worker
from . import app_state
from .app_state import PackageInfo, State


__all__ = (
    "pxc_dashboard",
    "pxc_listener",
    "pxc_install_package",
    "pxc_update_package",
    "pxc_remove_package",
    "pxc_check_out_package",
    "pxc_toggle_disable_package",
    "pxc_open_packagecontrol_io",
    "pxc_render",
    "pxc_next_package",
    "pxc_previous_package",
)


# Configuration object for dashboard formatting
class Config:
    # Formatting constants
    COLUMN_SPACING = "  "           # Two spaces between columns
    INDENT_WIDTH = 4                # Standard indentation (4 spaces)

    # Default column widths
    TERSE_SECTION_MIN_NAME_WIDTH = 10
    TERSE_SECTION_WIDTH_PERCENTILE = 0.95
    TERSE_SECTION_WIDTH_FACTOR = 1.4
    TERSE_SECTION_MIN_VERSION_WIDTH = 8

    # Wide section configuration
    WIDE_SECTION_MIN_NAME_WIDTH = 25     # Minimum width for package names
    WIDE_SECTION_WIDTH_PERCENTILE = 0.75
    WIDE_SECTION_WIDTH_FACTOR = 1.1
    WIDE_SECTION_MIN_VERSION_WIDTH = 10  # Minimum width for versions


# Default configuration
DEFAULT_CONFIG = Config()
HELP_TEXT = """
; Paste git URL's or URL's from packagecontrol.io or Github et.al. to install.
; Comment a line to disable a package.  Uncomment to enable it.
; [o] open packagecontrol.io
;                                      [u]  update package
; [ctrl+backspace]  delete package     [U]  unpack package
"""
RESERVED_PACKAGES = {
    'Binary', 'Default', 'Text', 'User', 'Package Control',
    'Package x Control',
}


@app_state.register
def render_visible_dashboards(state: State):
    for view in visible_views():
        if view_is_our_dashboard(view):
            render(view, state)


def visible_views(window: sublime.Window = None) -> Iterator[sublime.View]:
    yield from (
        sheets_view
        for window_ in ([window] if window else sublime.windows())
        for group_id in range(window_.num_groups())
        for sheet in window_.selected_sheets_in_group(group_id)
        if (sheets_view := sheet.view())
    )


def view_is_our_dashboard(view: sublime.View) -> bool:
    # Check settings and also if the view is valid and not closed
    return bool(view.settings().get("pxc_dashboard"))


class pxc_dashboard(sublime_plugin.WindowCommand):
    def run(self):
        window = self.window
        view = find_or_create_dashboard(window)
        window.focus_view(view)
        app_state.refresh()


def find_or_create_dashboard(window) -> sublime.View:
    # Check if already open in any group
    for group in range(window.num_groups()):
        for view in window.views_in_group(group):
            if view_is_our_dashboard(view):
                return view

    # If not found, create it
    view = window.new_file()
    prepare_view_settings(view, {
        "pxc_dashboard": True,
        "scratch": True,
        "read_only": True,
        "syntax": "Packages/Package x Control/packages_dashboard.sublime-syntax",
        "title": "Package x Control - Dashboard",
        "word_wrap": False,
        # "line_numbers": False,
        # "gutter": False,
        # "rulers": [],
    })
    return view


def prepare_view_settings(view: sublime.View, options: dict[str, object]) -> None:
    special_setters: dict[str, Callable] = {
        "syntax": view.set_syntax_file,
        "title": view.set_name,
        "scratch": view.set_scratch,
        "read_only": view.set_read_only,
    }
    settings = view.settings()
    for k, v in options.items():
        if k in special_setters:
            special_setters[k](v)
        else:
            settings.set(k, v)


class pxc_listener(sublime_plugin.EventListener):
    def on_activated(self, view):
        # Refresh only if it's our dashboard and maybe needs updating
        if view_is_our_dashboard(view):
            app_state.refresh()

    def on_text_command(self, view, command_name, args):
        if command_name == "toggle_comment" and view_is_our_dashboard(view):
            return ("pxc_toggle_disable_package", None)
        return None


class pxc_install_package(sublime_plugin.TextCommand):
    @cooperative
    def run(self, edit, name: str = None):
        view = self.view
        window = view.window()
        assert window

        if not app_state.state["initial_fetch_of_package_control_io"].done():
            yield AWAIT_WORKER
            try:
                app_state.state["initial_fetch_of_package_control_io"].result(0.5)
            except TimeoutError:
                with ActivityIndicator() as progress:
                    for msg, timeout in (
                        ("Waiting for packagecontrol.io...", 4.0),
                        ("🙄...", 4.0),
                        ("😐...", 3.0),
                        ("🤔...", 2.0),
                        ("😒...", 10.0),
                    ):
                        progress.set_label(msg)
                        try:
                            app_state.state["initial_fetch_of_package_control_io"].result(timeout)
                        except TimeoutError:
                            pass
                        else:
                            break
                    else:
                        print(
                            "Could not fetch packagecontrol repository.  This is technically "
                            "not required but helps with configuring the right refs and custom "
                            "package names.  Continue anyway."
                        )

            yield AWAIT_UI

        registered_packages = app_state.state["registered_packages"]

        if name is None:
            name = sublime.get_clipboard(size_limit=1024)
            if not name:
                view.show_popup("Nothing in the clipboard")
                return

        def install_package_fx_(entry: PackageConfiguration):
            name = entry['name']
            print("Install", name, entry)
            maybe_handover_control_from_pc(name)
            install_package(entry)
            ensure_package_is_enabled(name)
            log_fx_(name)

        def install_proprietary_package_fx_(name: str):
            print("Install", name)
            install_proprietary_package(name)
            ensure_package_is_enabled(name)
            log_fx_(name)

        def maybe_handover_control_from_pc(name: str):
            s = sublime.load_settings(PACKAGE_CONTROL_PREFERENCES)
            installed_packages = set(s.get("installed_packages", []))
            if name in installed_packages:
                installed_packages.discard(name)
                s.set("installed_packages", sorted(installed_packages, key=lambda s: s.lower()))
                sublime.save_settings(PACKAGE_CONTROL_PREFERENCES)

        def ensure_package_is_enabled(name: str):
            if name in app_state.state["disabled_packages"]:
                enable_packages_by_name([name])

        def log_fx_(name: str):
            message = f"Installed {name}."
            app_state.state["status_messages"].append(message)
            app_state.refresh()

        def lookup_by_encoded_name_in_url(name: str) -> PackageControlEntry | None:
            for p in registered_packages.values():
                if "git_url" in p:
                    if extract_name_from_url(p["git_url"]) == name:  # type: ignore[typeddict-item]
                        return p
            return None

        if url := parse_url_from_user_input(name):
            final_name = remove_suffix(url.rsplit("/", 1)[1], ".git")
            refs = parse_refs_from_user_input(name)
            package_control_entry = lookup_by_encoded_name_in_url(final_name)
        else:
            final_name = (
                urllib.parse.unquote(name[35:])
                if name.startswith("https://packagecontrol.io/packages/")
                else name
            )
            refs = None
            package_control_entry = registered_packages.get(final_name)

        package_entry: PackageConfiguration
        if package_control_entry:
            if "git_url" in package_control_entry:
                package_entry = {
                    "name": package_control_entry["name"],
                    "url": url or package_control_entry["git_url"],  # type: ignore[typeddict-item]
                    "refs": refs or package_control_entry["refs"],   # type: ignore[typeddict-item]
                    "unpacked": False
                }
                worker.add_task("package_control_fx", install_package_fx_, package_entry)
            else:
                worker.add_task(
                    "package_control_fx",
                    install_proprietary_package_fx_,
                    package_control_entry["name"]
                )
        elif url:
            def install_from_git(package_entry: PackageConfiguration, new_name: str = None):
                if new_name:
                    package_entry["name"] = new_name
                worker.add_task("package_control_fx", install_package_fx_, package_entry)

            kont = partial(install_from_git, {
                "name": final_name,
                "url": url,
                "refs": refs or "tags/*",
                "unpacked": False
            })
            show_actions_panel(window, [
                (
                    f"Install {final_name}",
                    kont
                ),
                (
                    "Enter a different name for the package",
                    lambda: show_input_panel(window, "Name:", final_name, kont)
                )
            ])
        else:
            truncated_name = (name[:67] + "...") if len(name) > 70 else name
            view.show_popup(
                f"'{truncated_name}' neither looks like a git url "
                "nor is it a name found in the package registry."
            )


class pxc_update_package(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view

        def fx_(entry: PackageConfiguration):
            install_package(entry)
            message = f"Updated {entry['name']}."
            app_state.state["status_messages"].append(message)
            app_state.refresh()

        config_data = get_configuration()
        entries = process_config(config_data)
        for package in get_selected_packages(view):
            package_info = grab_package_info_by_name(package)
            if not package_info:
                view.show_popup("[u] is only implemented for INSTALLED PACKAGES at the moment")
                continue

            if not package_info["update_available"]:
                view.show_popup(f"no update available for {package}")
                continue

            name = extract_repo_name(package)
            for entry in entries:
                if entry["name"] == name:
                    worker.add_task("package_control_fx", fx_, entry)
                    break
            else:
                print(f"fatal: {name} not found in the PxC-settings")
                view.show_popup(f"Huh?  {name} not found in the PxC-settings")
                continue


class pxc_remove_package(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        installed_packages = app_state.state["installed_packages"]
        package_controlled_packages = app_state.state["package_controlled_packages"]

        def find_by_name(name: str) -> tuple[str, PackageInfo] | None:
            for section, pkgs in [
                ("controlled_by_us", installed_packages),
                ("controlled_by_pc", package_controlled_packages)
            ]:
                for info in pkgs:
                    if info["name"] == name:
                        return section, info
            return None

        def remove_package_fx_(name: str):
            remove_package_by_name(name)
            message = f"Removed {name}."
            app_state.state["status_messages"].append(message)
            app_state.refresh()

        def remove_proprietary_package_fx_(name: str):
            remove_proprietary_package_by_name(name)
            message = f"Removed {name}."
            app_state.state["status_messages"].append(message)
            app_state.refresh()

        for package in get_selected_packages(view):
            result = find_by_name(package)
            if result is None:
                view.show_popup(f"Can only remove installed packages, and {package} is not.")
                continue
            section, info = result
            if info["checked_out"]:
                view.show_popup("Not implemented for packages that are checked out.")
                continue
            if section == "controlled_by_us":
                worker.add_task("package_control_fx", remove_package_fx_, info["name"])
            elif section == "controlled_by_pc":
                worker.add_task("package_control_fx", remove_proprietary_package_fx_, info["name"])
            else:
                raise RuntimeError("this else should be unreachable")


class pxc_check_out_package(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        window = view.window()
        assert window

        installed_packages = app_state.state["installed_packages"]
        package_controlled_packages = app_state.state["package_controlled_packages"]

        def find_by_name(name: str) -> tuple[str, PackageInfo] | None:
            for section, pkgs in [
                ("controlled_by_us", installed_packages),
                ("controlled_by_pc", package_controlled_packages)
            ]:
                for info in pkgs:
                    if info["name"] == name:
                        return section, info
            return None

        def checkout_package_fx_(name: str, git_url: str):
            target_dir = os.path.join(PACKAGES_PATH, name)
            if os.path.exists(target_dir):
                pm = PackageManager()
                if not pm.backup_package_dir(name):
                    view.show_popup(
                        "fatal: the target dir already exists and could not be backed up."
                    )
                    return
                print(f"{name} backed up into {BACKUP_DIR}")

                if not rmtree(target_dir):
                    view.show_popup(
                        "fatal: the target dir could not be removed."
                    )
                    return

            if window.folders() or len(window.views()) > 1:
                target_window = open_new_window()
            else:
                target_window = window
            clone_package_to_window(target_window, git_url, target_dir)

            package_file = os.path.join(INSTALLED_PACKAGES_PATH, f"{name}.sublime-package")
            if os.path.exists(package_file):
                if not rmfile(package_file):
                    print("Failed to remove {package_file}.  Should work anyway.")

            message = f"Unpacked {name}."
            app_state.state["status_messages"].append(message)
            app_state.refresh()

        config_data = get_configuration()
        entries = process_config(config_data)
        for package in get_selected_packages(view):
            result = find_by_name(package)
            if result is None:
                view.show_popup(
                    f"Can only remove check out managed packages, and {package} is not."
                )
                continue
            section, info = result
            if info["checked_out"]:
                view.show_popup(f"{package} is already checked out.")
                continue

            if section == "controlled_by_us":
                for entry in entries:
                    if entry["name"] == package:
                        worker.add_task(
                            "package_control_fx",
                            checkout_package_fx_,
                            package,
                            entry["url"]
                        )
                        break
                else:
                    print(f"fatal: {package} not found in the PxC-settings")
                    view.show_popup(f"Huh?  {package} not found in the PxC-settings")
                    continue
            elif section == "controlled_by_pc":
                registered_packages = app_state.state["registered_packages"]
                package_control_entry = registered_packages.get(package)
                if not package_control_entry:
                    view.show_popup(f"fatal: {package} not found in the package registry.")
                    continue
                elif "git_url" not in package_control_entry:
                    view.show_popup(
                        f"fatal: {package} is proprietary and I don't have a git url for it."
                    )
                    continue
                else:
                    worker.add_task(
                        "package_control_fx",
                        checkout_package_fx_,
                        package,
                        package_control_entry["git_url"]  # type: ignore[typeddict-item]
                    )
            else:
                raise RuntimeError("this else should be unreachable")


def clone_package_to_window(window, git_url: str, target_dir: str):
    with ActivityIndicator("Cloning package") as progress:
        git = GitCallable(".")
        git("clone", git_url, target_dir)
        # Set this late to ensure `target_dir` actually exists
        window.set_project_data({
            "folders": [dict(follow_symlinks=True, path=target_dir)]
        })
        progress.set_label("Cloned repo successfully.")
        if not window.is_sidebar_visible():
            window.run_command("toggle_side_bar")


def open_new_window():
    # type: () -> sublime.Window
    sublime.run_command("new_window")
    return sublime.active_window()


class pxc_toggle_disable_package(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        to_enable, to_disable = [], []
        for package in get_selected_packages(view):
            if package in RESERVED_PACKAGES:
                view.show_popup("Can't toggle built-ins.")
                continue
            if package in app_state.state["disabled_packages"]:
                to_enable.append(package)
            else:
                to_disable.append(package)

        def fx_(enable: bool, package_names: list[str]):
            fn = enable_packages_by_name if enable else disable_packages_by_name
            fn(package_names)
            En = "En" if enable else "Dis"
            message = f"{En}abled {format_items(package_names)}."
            app_state.state["status_messages"].append(message)
            app_state.refresh()

        if to_enable:
            worker.add_task("package_control_fx", fx_, True, to_enable)
        if to_disable:
            worker.add_task("package_control_fx", fx_, False, to_disable)


HUBS = [
    "https://github.com/", "https://gitlab.com/",
    "https://bitbucket.org/", "https://codeberg.org/"
]


def parse_url_from_user_input(clip_content: str) -> str:
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


def parse_refs_from_user_input(clip_content: str) -> str:
    if not clip_content:
        return ""

    """
    https://github.com/timbrel/GitSavvy/pull/1750 -> refs/pull/1750/head
    https://github.com/timbrel/GitSavvy/releases/tag/2.50.0 -> refs/tags/2.50.0
    """
    if match := re.search(r"/pull/(\d+)$", clip_content):
        return f"pull/{match.group(1)}/head"
    if match := re.search(r"/releases/tag/([^/]+)$", clip_content):
        return f"tags/{match.group(1)}"
    return ""


def grab_package_info_by_name(name: str) -> PackageInfo | None:
    for p in app_state.state["installed_packages"]:
        if p["name"] == name:
            return p
    return None


def is_managed_by_us(name: str) -> bool:
    for p in app_state.state["installed_packages"]:
        if p["name"] == name:
            return True
    return False


class pxc_open_packagecontrol_io(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        not_registered_packages = []
        for package in get_selected_packages(view):
            if app_state.state["registered_packages"].get(package):
                quoted_name = urllib.parse.quote(package)
                url = f"https://packagecontrol.io/packages/{quoted_name}"
                open_in_browser(url)
            else:
                not_registered_packages.append(package)

        if not_registered_packages:
            if len(not_registered_packages) == 1:
                message = f"{format_items(not_registered_packages)} is "
            else:
                message = f"{format_items(not_registered_packages)} are "
            message += "not registered at packagecontrol.io"
            view.show_popup(message)


class pxc_next_package(sublime_plugin.TextCommand):
    def run(self, edit):
        regions = self.view.find_by_selector("entity.name.package")
        if not regions:
            return

        current_pos = self.view.sel()[0].begin()
        for region in regions:
            if region.begin() > current_pos:
                next_region = region
                break
        else:
            next_region = regions[0]

        self.view.sel().clear()
        self.view.sel().add(next_region)
        self.view.show(next_region)


class pxc_previous_package(sublime_plugin.TextCommand):
    def run(self, edit):
        regions = self.view.find_by_selector("entity.name.package")
        if not regions:
            return

        current_pos = self.view.sel()[0].begin()
        for region in reversed(regions):
            if region.begin() < current_pos:
                previous_region = region
                break
        else:
            previous_region = regions[-1]

        self.view.sel().clear()
        self.view.sel().add(previous_region)
        self.view.show(previous_region)


def get_selected_packages(view: sublime.View) -> list[str]:
    """
    Returns the package name on the line of the single cursor in the view.
    Uses view.line and checks for region intersection.
    """
    frozen_sel = list(view.sel())
    package_regions = view.find_by_selector("entity.name.package")
    selected_packages = []
    for s in frozen_sel:
        expanded_selection = view.line(s)
        for region in package_regions:
            if expanded_selection.intersects(region):
                selected_packages.append(view.substr(region))
    return selected_packages


def flash(view: sublime.View, message: str):
    window = view.window()
    if window:
        window.status_message(message)


def render(view: sublime.View, current_state: State, config: Config = DEFAULT_CONFIG) -> None:
    """Renders the dashboard content into the view based on the state."""

    sections = drop_falsy((
        render_wide_section(
            "INSTALLED PACKAGES",
            current_state.get("installed_packages", []),
            current_state, config
        ),

        render_terse_section(
            "PACKAGES BY PACKAGE CONTROL",
            current_state.get("package_controlled_packages", []),
            current_state, config=config
        ),

        render_terse_section(
            "UNMANAGED PACKAGES",
            current_state.get("unmanaged_packages", []),
            current_state, mark_registered_packages=True, config=config
        )
    ))

    status_messages: Sequence[str] = current_state.get("status_messages", [])
    footer_text = ""
    if status_messages:
        messages = chain.from_iterable([wrap(msg, width=75) for msg in status_messages])
        footer_text = "\n" + "\n".join(f"; {msg}" for msg in messages)

    final_text = (
        "\n\n"
        + "\n\n\n".join(sections)
        + "\n\n"
        + HELP_TEXT
        + footer_text
        + "\n"
    )
    # Update the view with the new content
    view.run_command("pxc_render", {"text": final_text})


class pxc_render(sublime_plugin.TextCommand):
    """Helper command to replace view content."""
    def run(self, edit, text: str) -> None:
        view = self.view
        region = sublime.Region(0, view.size())
        old_content = view.substr(region)
        if text == old_content:
            return

        frozen_sel = [
            (view.rowcol(s.a), view.rowcol(s.b))
            for s in view.sel()
        ]
        view.set_read_only(False)
        view.replace(edit, region, text)
        view.set_read_only(True)
        sel = [
            sublime.Region(view.text_point(*a), view.text_point(*b))
            for a, b in frozen_sel
        ]
        view.sel().clear()
        view.sel().add_all(sel)


# --- Formatting Functions ---


def render_wide_section(
    title: str, packages: list[PackageInfo], state: State,
    config: Config = DEFAULT_CONFIG
) -> str:
    """
    Formats a section with wide layout - more space between entries,
    aligned date column, update lines shown.
    """
    if not packages:
        return ""

    name_width, version_width = calculate_wide_section_widths(packages, config)
    formatted_packages = [
        format_package_wide(pkg, name_width, version_width, state, config)
        for pkg in packages
    ]
    return (
        f"=== {title}\n\n"
        + "\n".join(
            # emphasize packages that have updates (i.e. they're multi-line)
            # by surrounding blank lines
            (
                f"{p}\n" if i == 0 else
                f"\n{p}" if i == len(formatted_packages) - 1 else
                f"\n{p}\n"
            )
            if "\n" in p
            else p
            for i, p in enumerate(formatted_packages)
        ).replace("\n\n\n", "\n\n")
    )


def format_package_wide(
    pkg: PackageInfo, name_width: int, version_width: int,
    state: State, config: Config = DEFAULT_CONFIG
) -> str:
    """
    Formats a single package for wide section display.
    Uses more vertical space with blank lines between packages.
    """
    lines = []

    # Check if package is disabled
    is_disabled = is_package_disabled(pkg.get('name', ''), state)
    indent = (";" if is_disabled else "").rjust(config.INDENT_WIDTH)

    # Get package name
    name = pkg.get('name', '')
    name_column = name.ljust(name_width)
    actual_name_width = len(name_column)

    # Format version text
    ver = pkg.get('version')
    version_text = ""
    date_column = ""

    if pkg.get('checked_out'):
        version_text = "(checked out)"
    elif ver:
        if ver.kind == 'tag':
            version_text = f"tag: {ver.specifier}"
        elif ver.kind == 'branch':
            version_text = f"branch: {ver.specifier}"
        else:
            version_text = ver.specifier

        if ver.date:
            date_column = f"/ {human_date(ver.date)}"

    version_column = version_text.ljust(version_width)
    actual_version_width = len(version_column)

    main_line = config.COLUMN_SPACING.join((
        f"{indent}{name_column}",
        version_column,
        date_column
    ))
    lines.append(main_line)

    # Add update line if needed
    if update_ver := pkg.get('update_available'):
        # Determine update prefix text based on version type
        indent = "".ljust(config.INDENT_WIDTH)
        update_prefix = (
            " ` install available"
            if not ver else
            " ` new version available"
            if update_ver.kind == 'tag' else
            " ` update available"
        )
        name_column = update_prefix.ljust(actual_name_width)

        # Format update version and date
        if update_ver.kind == "tag" and ver and ver.kind == "tag":
            # omit the "tag: " prefix
            update_version_text = f"     {update_ver.specifier}"
        else:
            update_version_text = f"   {update_ver.specifier}"
        version_column = update_version_text.ljust(actual_version_width)

        date_column = ""
        if update_ver.date:
            date_column = f"/ {human_date(update_ver.date)}"

        update_line = config.COLUMN_SPACING.join((
            f"{indent}{name_column}",
            version_column,
            date_column
        ))
        lines.append(update_line)

    return "\n".join(lines)


def render_terse_section(
    title: str, packages: list[PackageInfo], state: State,
    mark_registered_packages: bool = False,
    config: Config = DEFAULT_CONFIG
) -> str:
    """
    Formats a section with terse layout - compact, no extra space,
    simpler alignment, no update lines.
    """
    if not packages:
        return ""

    name_width, version_width = calculate_terse_section_widths(packages, config)
    formatted_packages = [
        format_package_terse(
            pkg, name_width, version_width, state, mark_registered_packages, config
        )
        for pkg in packages
    ]
    return f"=== {title}\n\n" + "\n".join(formatted_packages)


def format_package_terse(
    pkg: PackageInfo, name_width: int, version_width: int,
    state: State, mark_registered_packages: bool = False, config: Config = DEFAULT_CONFIG
) -> str:
    """
    Formats a single package for terse section display (formerly sections 2 and 3).
    More compact layout without updates.
    """
    # Check if package is disabled
    registered_packages = state['registered_packages']
    name = pkg.get('name', '')
    is_disabled = is_package_disabled(pkg.get('name', ''), state)
    marker = (
        ";" if is_disabled else
        "*" if mark_registered_packages and name in registered_packages else
        ""
    )
    indent = marker.rjust(config.INDENT_WIDTH)

    # Get package name
    name_column = name.ljust(name_width)
    actual_name_width = len(name_column)

    # Format version text
    version_text = ""
    date_text = ""

    if pkg.get('checked_out'):
        version_text = "(checked out)"
    elif ver := pkg.get('version'):
        version_text = ver.specifier

        # Add date if available
        if ver.date:
            date_str = human_date(ver.date)
            if date_str:
                date_text = f"/ {date_str}"

    # Build line using proper column formatting
    if actual_name_width > name_width:
        return (
            f"{indent}{name_column}  {version_text}".ljust(name_width + version_width)
            + config.COLUMN_SPACING + date_text
        )
    version_part = version_text.rjust(version_width)
    return config.COLUMN_SPACING.join((
        f"{indent}{name_column}",
        version_part,
        date_text
    ))


def is_package_disabled(pkg_name: str, state: State) -> bool:
    """Check if a package is disabled."""
    return pkg_name in state.get("disabled_packages", [])


# --- Layout Calculation Functions ---


def calculate_version_width(packages: list[PackageInfo]) -> int:
    return max(
        (
            len(ver.specifier)
            for pkg in packages
            for ver in (pkg.get('version'), pkg.get('update_available'))
            if ver and ver.kind == 'tag'
        ),
        default=0
    )


def calculate_column_widths(
    packages: list[PackageInfo], config: Config = DEFAULT_CONFIG
) -> tuple[int, int]:
    """
    Calculates base column widths based on package content.
    Only considers tag versions (e.g. not branches) for version width.

    Returns a tuple of (name_width, version_width).
    """
    # Extract name lengths
    name_lengths = (len(pkg.get('name', '')) for pkg in packages)
    name_width = max(*name_lengths, config.TERSE_SECTION_MIN_NAME_WIDTH)

    # Extract version lengths
    version_lengths = (
        len(ver.specifier) + 5  # 5 == prefix length "tag: "
        for pkg in packages
        for ver in (pkg.get('version'), pkg.get('update_available'))
        if ver and ver.kind == 'tag'
    )
    version_width = max(*version_lengths, config.TERSE_SECTION_MIN_VERSION_WIDTH)

    return name_width, version_width


def weighted_length(strings: Iterable[str], percentile: float, factor: float, minimum: int) -> int:
    sorted_lengths = sorted(map(len, strings))
    if not sorted_lengths:
        return minimum
    percentile_idx = int(percentile * len(sorted_lengths))
    percentile_width = sorted_lengths[min(percentile_idx, len(sorted_lengths) - 1)]
    return max(min(int(percentile_width * factor), sorted_lengths[-1]), minimum)


def calculate_wide_section_widths(
    packages: list[PackageInfo], config: Config = DEFAULT_CONFIG
) -> tuple[int, int]:
    """Calculate column widths for wide section format."""
    name_width = weighted_length(
        [pkg['name'] for pkg in packages] or [''],
        config.WIDE_SECTION_WIDTH_PERCENTILE,
        config.WIDE_SECTION_WIDTH_FACTOR,
        config.WIDE_SECTION_MIN_NAME_WIDTH
    )
    version_width = max(
        calculate_version_width(packages) + 5,  # 5 == prefix length "tag: "
        config.WIDE_SECTION_MIN_VERSION_WIDTH
    )
    return name_width, version_width


def calculate_terse_section_widths(
    packages: list[PackageInfo], config: Config = DEFAULT_CONFIG
) -> tuple[int, int]:
    """Calculate column widths for terse section format."""
    name_width = weighted_length(
        [pkg['name'] for pkg in packages] or [''],
        config.TERSE_SECTION_WIDTH_PERCENTILE,
        config.TERSE_SECTION_WIDTH_FACTOR,
        config.TERSE_SECTION_MIN_NAME_WIDTH
    )
    # name_lengths = (len(pkg.get('name', '')) for pkg in packages)
    # name_width = max(*name_lengths, config.TERSE_SECTION_MIN_NAME_WIDTH)
    version_width = max(calculate_version_width(packages), config.TERSE_SECTION_MIN_VERSION_WIDTH)
    return name_width, version_width
