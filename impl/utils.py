from __future__ import annotations
from concurrent.futures import Future
from datetime import datetime
from functools import lru_cache
import os
import shutil
import stat

from typing import Any, Callable, Iterable, Sequence, TypeVar, overload
from typing_extensions import ParamSpec, TypeAlias

import sublime

P = ParamSpec("P")
T = TypeVar("T")


@overload
def drop_falsy(it: list[T | None]) -> list[T]: ...  # noqa: E704
@overload
def drop_falsy(it: tuple[T | None, ...]) -> tuple[T, ...]: ...  # noqa: E704
@overload
def drop_falsy(it: set[T | None]) -> set[T]: ...  # noqa: E704
@overload
def drop_falsy(it: Iterable[T | None]) -> Iterable[T]: ...  # noqa: E704
def drop_falsy(it: Iterable[T | None]) -> Iterable[T]:  # noqa: E302
    """
    Drop falsy values (None, False, 0, '', etc.) from an iterable.
    Preserve the container type.
    """
    rv = filter(None, it)
    if isinstance(it, list):
        return list(rv)
    elif isinstance(it, tuple):
        return tuple(rv)
    elif isinstance(it, set):
        return set(rv)
    return rv


def remove_prefix(s: str, prefix: str) -> str:
    if s.startswith(prefix):
        return s[len(prefix):]
    return s


def remove_suffix(s: str, suffix: str) -> str:
    if s.endswith(suffix):
        return s[:-len(suffix)]
    return s


def remove_lr(s: str, left: str, right: str) -> str:
    return remove_suffix(remove_prefix(s, left), right)


def human_date(ts: float, now_ts: float | None = None) -> str:
    """
    Format a timestamp in a human-readable format similar to Git.

    Port of: https://github.com/git/git/commit/acdd37769de8b0fe37a74bfc0475b63bdc55e9dc

    Args:
        ts: Unix timestamp to format

    Returns:
        A human-readable string representing the time
    """
    if now_ts is None:
        # Use current time
        now = datetime.now()
        now_ts = now.timestamp()
        now_tm = now.timetuple()
    else:
        now = datetime.fromtimestamp(now_ts)
        now_tm = now.timetuple()

    target_time = datetime.fromtimestamp(ts)
    target_tm = target_time.timetuple()

    # Create a struct to track which parts to hide
    hide = {
        'year': False,
        'date': False,
        'wday': False,
        'time': False,
        'seconds': True,  # Always hide seconds for human-readable
        'tz': True       # Assuming local timezone for simplicity
    }

    # Hide year if it's the current year
    hide['year'] = (target_tm.tm_year == now_tm.tm_year)

    # Same year logic
    if hide['year']:
        if target_tm.tm_mon == now_tm.tm_mon:
            if target_tm.tm_mday > now_tm.tm_mday:
                # Future date: could happen due to timezones
                pass
            elif target_tm.tm_mday == now_tm.tm_mday:
                # Same day - show relative time
                hide['date'] = True
                hide['wday'] = True

                # For same day, use relative "... ago" format
                seconds_ago = now_ts - ts
                minutes_ago = int(seconds_ago // 60)
                hours_ago = int(seconds_ago // 3600)
                if minutes_ago < 60:
                    return f"{minutes_ago} minutes ago"
                elif hours_ago < 24:
                    return f"{hours_ago} hours ago"
            elif target_tm.tm_mday + 5 > now_tm.tm_mday:
                # Within 5 days - show weekday but hide date
                hide['date'] = True

    # Hide weekday and time if showing year
    if not hide['year']:
        hide['wday'] = True
        hide['time'] = True

    # Build the output string
    result = ""

    # Add weekday if not hidden
    if not hide['wday']:
        weekday = target_time.strftime("%a")
        result += f"{weekday} "

    # Add month/day if not hidden
    if not hide['date']:
        month = target_time.strftime("%b")
        day = target_time.day  # No leading zeros by default
        result += f"{month} {day} "

    # Add time if not hidden
    if not hide['time']:
        result += target_time.strftime("%H:%M")
    else:
        result = result.rstrip()  # Remove trailing space

    # Add year if not hidden
    if not hide['year']:
        result += f" {target_tm.tm_year}"

    return result


def format_items(items: list[str], sep: str = ", ", last_sep: str = " and ") -> str:
    if len(items) == 1:
        return items[0]
    return f"{sep.join(items[:-1])}{last_sep}{items[-1]}"


def future(val: T) -> Future[T]:
    f: Future[T] = Future()
    f.set_result(val)
    return f


ValueCallback = Callable[[str], None]
CancelCallback = Callable[[], None]


def show_input_panel(
    window: sublime.Window,
    caption: str,
    initial_text: str,
    on_done: ValueCallback,
    on_change: ValueCallback | None = None,
    on_cancel: CancelCallback | None = None,
    select_text: bool = True
) -> sublime.View:
    v = window.show_input_panel(caption, initial_text, on_done, on_change, on_cancel)
    if select_text:
        v.run_command("select_all")
    return v


ActionType: TypeAlias = "tuple[str, Callable[[], Any]]"
QuickPanelItems: TypeAlias = "Iterable[str | sublime.QuickPanelItem]"


def show_panel(
    window,  # type: sublime.Window
    items,  # type: QuickPanelItems
    on_done,  # type: Callable[[int], None]
    on_cancel=lambda: None,  # type: Callable[[], None]
    on_highlight=lambda _: None,  # type: Callable[[int], None]
    selected_index=-1,  # type: int
    flags=sublime.MONOSPACE_FONT
):
    # (...) -> None
    def _on_done(idx):
        # type: (int) -> None
        if idx == -1:
            on_cancel()
        else:
            on_done(idx)

    # `on_highlight` also gets called `on_done`. We
    # reduce the side-effects here using `lru_cache`.
    @lru_cache(1)
    def _on_highlight(idx):
        # type: (int) -> None
        on_highlight(idx)

    window.show_quick_panel(
        list(items),
        _on_done,
        on_highlight=_on_highlight,
        selected_index=selected_index,
        flags=flags
    )


def show_actions_panel(
    window: sublime.Window, actions: Sequence[ActionType], select: int = -1
) -> None:
    def on_selection(idx):
        # type: (int) -> None
        description, action = actions[idx]
        action()

    show_panel(
        window,
        (action[0] for action in actions),
        on_selection,
        selected_index=select
    )


def rmtree(target_dir: str) -> bool:
    def remove_readonly_bit_and_retry(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    shutil.rmtree(target_dir, onerror=remove_readonly_bit_and_retry)
    return not os.path.exists(target_dir)


def rmfile(target_file: str) -> bool:
    try:
        os.remove(target_file)
    except OSError:
        pass
    return not os.path.exists(target_file)


if os.name == "nt":
    try:
        isjunction = os.path.isjunction  # type: ignore[attr-defined]
    except AttributeError:
        import os
        import ctypes
        from ctypes import wintypes

        # Constants
        FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
        IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003

        # ctypes setup
        GetFileAttributesW = ctypes.windll.kernel32.GetFileAttributesW
        GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
        GetFileAttributesW.restype = wintypes.DWORD

        CreateFileW = ctypes.windll.kernel32.CreateFileW
        CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
            wintypes.HANDLE
        ]
        CreateFileW.restype = wintypes.HANDLE

        DeviceIoControl = ctypes.windll.kernel32.DeviceIoControl
        DeviceIoControl.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID
        ]
        DeviceIoControl.restype = wintypes.BOOL

        CloseHandle = ctypes.windll.kernel32.CloseHandle
        CloseHandle.argtypes = [wintypes.HANDLE]
        CloseHandle.restype = wintypes.BOOL

        # IOCTL constants
        FSCTL_GET_REPARSE_POINT = 0x000900A8
        FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        OPEN_EXISTING = 3
        GENERIC_READ = 0x80000000
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

        # REPARSE_DATA_BUFFER (simplified)
        class REPARSE_DATA_BUFFER(ctypes.Structure):
            _fields_ = [
                ('ReparseTag', wintypes.DWORD),
                ('ReparseDataLength', wintypes.USHORT),
                ('Reserved', wintypes.USHORT),
                ('Rest', ctypes.c_byte * 0x3FC)  # Max reparse buffer size
            ]

        def isjunction(path: str) -> bool:
            if not os.path.exists(path):
                return False

            attrs = GetFileAttributesW(path)
            if attrs == 0xFFFFFFFF or not (attrs & FILE_ATTRIBUTE_REPARSE_POINT):
                return False

            handle = CreateFileW(
                path,
                GENERIC_READ,
                0,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                None
            )

            if handle == INVALID_HANDLE_VALUE:
                return False

            try:
                buf = REPARSE_DATA_BUFFER()
                bytes_returned = wintypes.DWORD()
                res = DeviceIoControl(
                    handle,
                    FSCTL_GET_REPARSE_POINT,
                    None, 0,
                    ctypes.byref(buf), ctypes.sizeof(buf),
                    ctypes.byref(bytes_returned),
                    None
                )
                if not res:
                    return False
                return buf.ReparseTag == IO_REPARSE_TAG_MOUNT_POINT
            finally:
                CloseHandle(handle)

else:
    def isjunction(path: str) -> bool:
        return False
