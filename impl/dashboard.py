from __future__ import annotations

from collections import deque
from concurrent.futures import as_completed
from datetime import datetime, timezone
from itertools import chain
import os
from textwrap import wrap
import urllib.parse
import re
from webbrowser import open as open_in_browser

from typing import (
    Callable, Iterable, Iterator, Literal, NamedTuple, TypedDict,
    Optional, Sequence
)
from typing_extensions import TypeAlias

import sublime
import sublime_plugin

from package_control.package_manager import PackageManager

from .config import (
    BUILD, PACKAGE_CONTROL_PREFERENCES, PACKAGES_PATH, PLATFORM,
    ROOT_DIR, SUBLIME_PREFERENCES
)
from .config_management import (
    PackageConfiguration,
    extract_repo_name, get_configuration, process_config
)
from .git_package import (
    GitCallable, Version,
    check_for_updates, ensure_repository, describe_current_commit, get_commit_date
)
from .glue_code import (
    disable_packages_by_name, enable_packages_by_name,
    install_package, install_proprietary_package,
    remove_package_by_name, remove_proprietary_package_by_name
)
from .pc_repository import extract_name_from_url, fetch_packages, PackageDb, PackageControlEntry
from .runtime import ensure_on_ui
from .utils import (
    drop_falsy, format_items, human_date, remove_prefix, remove_suffix
)
from . import worker


__all__ = (
    "pxc_dashboard",
    "pxc_listener",
    "pxc_install_package",
    "pxc_update_package",
    "pxc_remove_package",
    "pxc_toggle_disable_package",
    "pxc_open_packagecontrol_io",
    "pxc_render",
    "pxc_next_package",
    "pxc_previous_package",
)


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
dashboard_views: set[sublime.View] = set()
state: State = {
    "installed_packages": [],
    "package_controlled_packages": [],
    "unmanaged_packages": [],
    "disabled_packages": [],
    "status_messages": deque([], 10),
    "registered_packages": {}
}


@ensure_on_ui
def set_state(partial_state: State):
    state.update(partial_state)
    render_visible_dashboards()


def render_visible_dashboards():
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
        refresh()


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
    dashboard_views.add(view)
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
            # Ensure `dashboard_views` is kept up-to-date, e.g. after reloads.
            dashboard_views.add(view)
            refresh()

    def on_pre_close(self, view):
        dashboard_views.discard(view)

    def on_text_command(self, view, command_name, args):
        if command_name == "toggle_comment" and view_is_our_dashboard(view):
            return ("pxc_toggle_disable_package", None)
        return None


class pxc_install_package(sublime_plugin.TextCommand):
    def run(self, edit, name: str = None):
        view = self.view
        registered_packages = state["registered_packages"]

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
            if name in state["disabled_packages"]:
                enable_packages_by_name([name])

        def log_fx_(name: str):
            message = f"Installed {name}."
            state["status_messages"].append(message)
            refresh()

        def lookup_by_encoded_name_in_url(name: str) -> PackageControlEntry | None:
            for p in registered_packages.values():
                if extract_name_from_url(p.get("git_url")) == name:  # type: ignore[arg-type]
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
            package_entry = {
                "name": final_name,
                "url": url,
                "refs": refs or "tags/*",
                "unpacked": False
            }
            worker.add_task("package_control_fx", install_package_fx_, package_entry)
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
            state["status_messages"].append(message)
            refresh()

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
        installed_packages = state["installed_packages"]
        package_controlled_packages = state["package_controlled_packages"]

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
            state["status_messages"].append(message)
            refresh()

        def remove_proprietary_package_fx_(name: str):
            remove_proprietary_package_by_name(name)
            message = f"Removed {name}."
            state["status_messages"].append(message)
            refresh()


        for package in get_selected_packages(view):
            result = find_by_name(package)
            print("result", result, package)
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


class pxc_toggle_disable_package(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        to_enable, to_disable = [], []
        for package in get_selected_packages(view):
            if package in RESERVED_PACKAGES:
                view.show_popup("Can't toggle built-ins.")
                continue
            if package in state["disabled_packages"]:
                to_enable.append(package)
            else:
                to_disable.append(package)

        def fx_(enable: bool, package_names: list[str]):
            fn = enable_packages_by_name if enable else disable_packages_by_name
            fn(package_names)
            En = "En" if enable else "Dis"
            message = f"{En}abled {format_items(package_names)}."
            state["status_messages"].append(message)
            refresh()

        if to_enable:
            worker.add_task("package_control_fx", fx_, True, to_enable)
        if to_disable:
            worker.add_task("package_control_fx", fx_, False, to_disable)


HUBS = ["https://github.com/", "https://gitlab.com/", "https://bitbucket.org/"]


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
    for p in state["installed_packages"]:
        if p["name"] == name:
            return p
    return None


def is_managed_by_us(name: str) -> bool:
    for p in state["installed_packages"]:
        if p["name"] == name:
            return True
    return False


class pxc_open_packagecontrol_io(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        not_registered_packages = []
        for package in get_selected_packages(view):
            if state["registered_packages"].get(package):
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
    return f"=== {title}\n\n" + "\n\n".join(formatted_packages)


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
            " ` new version available"
            if update_ver.kind == 'tag'
            else " ` update available"
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
    return max(int(percentile_width * factor), minimum)


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


# --- State Refresher


def refresh() -> None:
    """Fetches the latest state (if necessary) and renders the view."""
    global state
    fast_state(state, set_state)
    worker.add_task("fetch_packages", fetch_registered_packages, state, set_state)
    worker.add_task("refresh_disabled_packages", refresh_disabled_packages, state, set_state)
    worker.add_task("refresh_our_packages", refresh_our_packages, state, set_state)
    worker.add_task("refresh_installed_packages", refresh_installed_packages, state, set_state)
    worker.add_task("refresh_unmanaged_packages", refresh_unmanaged_packages, state, set_state)


StateSetter: TypeAlias = Callable[[State], None]


def fetch_registered_packages(state: State, set_state: StateSetter):
    @ensure_on_ui
    def printer(message: str):
        d = state["status_messages"]
        d.append(message)
        set_state({"status_messages": d})

    printer(f"[{datetime.now():%d.%m.%Y %H:%M}]")
    packages = fetch_packages(BUILD, PLATFORM, printer)
    set_state({"registered_packages": packages})


def refresh_disabled_packages(state: State, set_state: StateSetter):
    s = sublime.load_settings(SUBLIME_PREFERENCES)
    disabled_packages = s.get("ignored_packages") or []
    set_state({"disabled_packages": disabled_packages})


def refresh_our_packages(state: State, set_state: StateSetter):
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

    def fetch_package_info(entry: PackageConfiguration) -> PackageInfo:
        git = ensure_repository(entry, ROOT_DIR, GitCallable)
        return {
            "name": entry["name"],
            "checked_out": False,
            **current_version_of_git_repo(git.repo_path),
            **new_version_from_git_repo(entry)
        }

    for f in as_completed([
        worker.add_task(entry["name"], fetch_package_info, entry)
        for entry in entries
    ]):
        info = f.result()
        package_name = info["name"]
        for i, p in enumerate(packages):
            if p["name"] == package_name:
                packages[i] = info
                break
        set_state({"installed_packages": packages})


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


def fetch_package_info(package_name: str) -> PackageInfo:
    return {
        "name": package_name,
        "checked_out": False,
        **current_version_of_git_repo(os.path.join(PACKAGES_PATH, package_name))
    }


def current_version_of_git_repo(repo_path: str) -> dict:
    git = GitCallable(repo_path)
    if not os.path.exists(git.git_dir):
        return {}
    version = describe_current_commit(git)
    return {"version": git_version_to_description(version, git)}


def new_version_from_git_repo(entry: PackageConfiguration) -> dict:
    git = ensure_repository(entry, ROOT_DIR, GitCallable)
    if not os.path.exists(git.git_dir):
        return {}
    info = check_for_updates(entry["refs"], BUILD, git)
    if info["status"] == "needs-update":
        return {"update_available": git_version_to_description(info["version"], git)}
    else:
        return {}


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


def refresh_installed_packages(state: State, set_state: StateSetter):
    pm = PackageManager()
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
        version = metadata.get("version")
        calendar_version = is_calendar_version(version) if version else False
        release_time = metadata.get("release_time")
        info = {
            "name": package_name,
            "version": VersionDescription(
                "tag" if version and not calendar_version else "",
                version if version and not calendar_version else "",
                (
                    datetime_to_ts(release_time)
                    if release_time else
                    calendar_version_to_timestamp(version)
                    if calendar_version else
                    None
                )
            ),
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
    return datetime.utcfromtimestamp(ts).strftime("%b %d %Y")
