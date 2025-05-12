from __future__ import annotations
from concurrent.futures import Future, ThreadPoolExecutor, wait
from functools import partial, wraps
import inspect
import time
import threading
from typing import Callable, Collection, Generator, Literal, TypeVar
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
        enqueue_on_ui(fn, *args, **kwargs)


def enqueue_on_ui(fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> None:
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


AWAIT_UI:         Literal["AWAIT_UI"]         = 'AWAIT_UI'          # noqa: E221, E241
AWAIT_WORKER:     Literal["AWAIT_WORKER"]     = 'AWAIT_WORKER'      # noqa: E221, E241
ENSURE_ON_UI:     Literal["ENSURE_ON_UI"]     = 'ENSURE_ON_UI'      # noqa: E221, E241
ENSURE_ON_WORKER: Literal["ENSURE_ON_WORKER"] = 'ENSURE_ON_WORKER'  # noqa: E221, E241
Coop: TypeAlias = Generator[
    Literal["AWAIT_UI", "AWAIT_WORKER", "ENSURE_ON_UI", "ENSURE_ON_WORKER"],
    "timer",
    None
]


def cooperative(fn):
    # type: (Callable[P, Coop]) -> Callable[P, None]
    """Mark given function as cooperative.

    `fn` must return `Coop` t.i. it must yield AWAIT_UI, AWAIT_WORKER,
    ENSURE_ON_UI, or ENSURE_ON_WORKER at some point.

    Every yield answers with a timer object which can be used to
    measure the time since the continuation started.

    E.g. don't block the UI for too long:
        timer = yield AWAIT_UI
        ... do something ...
        if timer.elapsed > 100:  # [milliseconds]
            yield AWAIT_UI

    When calling `fn` it will run on the same thread as the caller
    until the function yields.  It then schedules a task on the
    desired thread which will continue execution the function.

    It is thus cooperative in the sense that all other tasks
    already queued will get a chance to run before we continue.
    It is "async" in the sense that the function does not run
    from start to end in a blocking manner but can be suspended.

    However, it is sync till the first yield (but you could of
    course yield on the first line!), only then execution returns
    to the call site.

    Be aware that, if the call site and the thread you request are
    _not_ the same, you can get concurrent execution afterwards!
    This is a side-effect of running two threads.
    """
    def tick(gen: Coop, initial_call=False) -> None:
        try:
            # workaround mypy marking `send(None)` as error
            # https://github.com/python/mypy/issues/11023#issuecomment-1255901328
            if initial_call:
                rv = next(gen)
            else:
                rv = gen.send(timer())
        except StopIteration:
            return
        except Exception as ex:
            raise ex from None

        if rv == ENSURE_ON_UI:
            ensure_on_ui(tick, gen)
        elif rv == ENSURE_ON_WORKER:
            ensure_on_worker(tick, gen)
        elif rv == AWAIT_UI:
            enqueue_on_ui(tick, gen)
        elif rv == AWAIT_WORKER:
            enqueue_on_worker(tick, gen)

    @wraps(fn)
    def decorated(*args: P.args, **kwargs: P.kwargs) -> None:
        gen = fn(*args, **kwargs)
        if inspect.isgenerator(gen):
            tick(gen, initial_call=True)

    return decorated


class timer:
    UI_BLOCK_TIME = 17

    def __init__(self) -> None:
        """Create a new timer and start it."""
        self.start = time.perf_counter()

    @property
    def elapsed(self) -> float:
        """Get the elapsed time in milliseconds."""
        return (time.perf_counter() - self.start) * 1000

    def exceeded(self, ms: float) -> bool:
        """Check if the elapsed time has exceeded the given milliseconds."""
        return self.elapsed > ms

    def exhausted_ui_budget(self) -> bool:
        """Check if the elapsed time has exceeded the UI_BLOCK_TIME of 17ms."""
        return self.exceeded(self.UI_BLOCK_TIME)

    def reset(self) -> None:
        """Reset the timer to the current time."""
        self.start = time.perf_counter()


def gather(futures: Collection[Future[T]], timeout=None) -> list[T]:
    wait(futures, timeout)
    return [f.result() for f in futures]
