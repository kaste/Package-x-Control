from __future__ import annotations
from collections import deque
from concurrent.futures import Future
from datetime import datetime
from functools import lru_cache
import os
import shutil
import stat
import threading

from typing import Any, Callable, Generic, Iterable, Sequence, TypeVar, overload
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


class _DedupQueue(Generic[T]):
    def __init__(self, items: Iterable[T] | None = None) -> None:
        self._queue: deque[T] = deque()
        self._seen: set[T] = set()
        if items:
            self.extend(items)

    def append(self, item: T) -> None:
        if item not in self._seen:
            self._seen.add(item)
            self._queue.append(item)

    def extend(self, items: Iterable[T]) -> None:
        unseen = [item for item in items if item not in self._seen]
        self._seen.update(unseen)
        self._queue.extend(unseen)

    def pop(self) -> T:
        return self._queue.pop()

    def popleft(self) -> T:
        return self._queue.popleft()


class _DedupQueueL(_DedupQueue[T]):
    def __init__(self, items: Iterable[T] | None = None) -> None:
        self._lock = threading.Lock()
        super().__init__(items)

    def append(self, item: T) -> None:
        with self._lock:
            super().append(item)

    def extend(self, items: Iterable[T]) -> None:
        with self._lock:
            super().extend(items)

    def pop(self) -> T:
        return self._queue.pop()

    def popleft(self) -> T:
        return self._queue.popleft()


class DedupQueue(Generic[T]):
    """A deduplicating queue, optionally thread safe."""
    def __new__(self, items: Iterable[T] | None = None, thread_safe: bool = False):
        return _DedupQueueL(items) if thread_safe else _DedupQueue(items)

    def __init__(                                      # noqa: E704
        self, items: Iterable[T] | None = None, thread_safe: bool = False
    ): ...
    def append(self, item: T) -> None: ...             # noqa: E704
    def extend(self, items: Iterable[T]) -> None: ...  # noqa: E704
    def pop(self) -> T: ...                            # type: ignore[empty-body]  # noqa: E704
    def popleft(self) -> T: ...                        # type: ignore[empty-body]  # noqa: E704


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
