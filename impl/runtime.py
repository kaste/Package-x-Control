from __future__ import annotations
from concurrent.futures import Future, ThreadPoolExecutor, wait
from functools import partial, wraps
import threading
from typing import Callable, Collection, TypeVar
from typing_extensions import ParamSpec, TypeAlias

import sublime


T = TypeVar('T')
P = ParamSpec('P')


UI_THREAD_NAME: str | None = None
WORKER_THREAD_NAME: str | None = None
executor = ThreadPoolExecutor(max_workers=1)


def determine_thread_names() -> None:
    def ui_callback() -> None:
        global UI_THREAD_NAME
        UI_THREAD_NAME = threading.current_thread().name

    def worker_callback() -> None:
        global WORKER_THREAD_NAME
        WORKER_THREAD_NAME = threading.current_thread().name

    sublime.set_timeout(ui_callback)
    sublime.set_timeout_async(worker_callback)


def it_runs_on_worker() -> bool:
    return threading.current_thread().name == WORKER_THREAD_NAME


def assert_it_runs_on_worker() -> None:
    if not it_runs_on_worker():
        raise RuntimeError("MUST run on worker")


def ensure_on_worker(fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> None:
    if it_runs_on_worker():
        fn(*args, **kwargs)
    else:
        enqueue_on_worker(fn, *args, **kwargs)


def enqueue_on_worker(fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> None:
    sublime.set_timeout_async(partial(fn, *args, **kwargs))


def on_worker(fn: Callable[P, T]) -> Callable[P, None]:
    @wraps(fn)
    def wrapped(*a: P.args, **kw: P.kwargs) -> None:
        ensure_on_worker(fn, *a, **kw)
    return wrapped


def it_runs_on_ui() -> bool:
    return threading.current_thread().name == UI_THREAD_NAME


def assert_it_runs_on_ui() -> None:
    if not it_runs_on_ui():
        raise RuntimeError("MUST run on UI thread")


def ensure_on_ui(fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> None:
    if it_runs_on_ui():
        fn(*args, **kwargs)
    else:
        run_on_ui(fn, *args, **kwargs)


def run_on_ui(fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> None:
    sublime.set_timeout(partial(fn, *args, **kwargs))


def on_ui(fn):
    # type: (Callable[P, T]) -> Callable[P, None]
    @wraps(fn)
    def wrapped(*a: P.args, **kw: P.kwargs) -> None:
        ensure_on_ui(fn, *a, **kw)
    return wrapped


def run_on_new_thread(fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> None:
    threading.Thread(target=fn, args=args, kwargs=kwargs).start()


def run_on_executor(fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> Future[T]:
    return executor.submit(fn, *args, **kwargs)


def gather(futures: Collection[Future[T]], timeout=None) -> list[T]:
    wait(futures, timeout)
    return [f.result() for f in futures]
