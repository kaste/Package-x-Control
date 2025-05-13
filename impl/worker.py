from __future__ import annotations
from collections import deque
from concurrent.futures import CancelledError, Future
from functools import partial, wraps
import logging
from queue import SimpleQueue, Empty
import threading
import traceback

from typing import overload, Callable, Generic, Literal, Optional, TypeVar
from typing_extensions import ParamSpec, TypeAlias

from .runtime import assert_it_runs_on_ui, enqueue_on_ui
import sublime


T = TypeVar('T')
P = ParamSpec('P')
R = TypeVar("R")


class TopicTask(Generic[T]):
    """A task scheduled to run for a specific topic."""

    def __init__(self, topic: str, fn: Callable[[], T], name: Optional[str] = None):
        """
        Initialize a new task.

        Args:
            topic: The topic this task belongs to.
            fn: The function to execute.
            name: Optional name for the task (useful for debugging and optimization).
        """
        self.topic = topic
        self.fn = fn
        self.future: Future[T] = Future()
        self.name = name or f"{topic}-{id(self)}"

    @property
    def is_orchestrator(self) -> bool:
        return self.topic.endswith(":orchestrator")

    @property
    def status(self) -> Literal["cancelled", "done", "running", "pending"]:
        """Return a string representation of the task."""
        if self.future.cancelled():
            return "cancelled"
        elif self.future.done():
            return "done"
        elif self.future.running():
            return "running"
        else:
            return "pending"

    def __repr__(self):
        return f"TopicTask(topic={self.topic}, name={self.name}, status={self.status})"


KEEP_ALIVE_TIME = 10.0
MAX_WORKERS = 8
queue: list[TopicTask] = []
running_topics: set[str] = set()
running_workers: list[Worker] = []


class Topic:
    def __init__(self, name: str):
        self.name = name

    def enqueue(self, fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        return add_task(self.name, fn, *args, **kwargs)

    def replace(self, fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> Future[R]:
        return replace_or_add_task(self.name, fn, *args, **kwargs)

    def __call__(
        self, remove_pending: bool = False
    ) -> Callable[[Callable[P, R]], Callable[P, Future[R]]]:
        def decorator(fn: Callable[P, R]) -> Callable[P, Future[R]]:
            topic_name = self.name

            if remove_pending:
                @wraps(fn)
                def decorated(*args: P.args, **kwargs: P.kwargs) -> Future[R]:
                    return replace_or_add_task(topic_name, fn, *args, **kwargs)
            else:
                @wraps(fn)
                def decorated(*args: P.args, **kwargs: P.kwargs) -> Future[R]:
                    return add_task(topic_name, fn, *args, **kwargs)

            return decorated

        return decorator


class Orchestrator(Topic):
    def __init__(self, name: str):
        super().__init__(f"{name}:orchestrator")


def topic(*, reduce_pending: bool = False) -> Callable[[Callable[P, R]], Callable[P, Future[R]]]:
    def decorator(fn: Callable[P, R]) -> Callable[P, Future[R]]:
        topic_name = fn.__name__

        if reduce_pending:
            @wraps(fn)
            def decorated(*args: P.args, **kwargs: P.kwargs) -> Future[R]:
                return replace_or_add_task(topic_name, fn, *args, **kwargs)
        else:
            @wraps(fn)
            def decorated(*args: P.args, **kwargs: P.kwargs) -> Future[R]:
                return add_task(topic_name, fn, *args, **kwargs)

        return decorated

    return decorator


@overload
def orchestrator(*, reduce_pending: bool = False) -> Callable[[Callable[P, R]], Callable[P, Future[R]]]: ...  # noqa: E501, E704

@overload  # noqa: E302
def orchestrator(fn: Callable[P, R], /) -> Callable[P, Future[R]]: ...  # noqa: E704

def orchestrator(  # noqa: E302
    fn=None, /, *, reduce_pending: bool = False
) -> Callable[[Callable[P, R]], Callable[P, Future[R]]] | Callable[P, Future[R]]:
    def decorator(fn: Callable[P, R]) -> Callable[P, Future[R]]:
        topic_name = f"{fn.__name__}:orchestrator"

        if reduce_pending:
            @wraps(fn)
            def decorated(*args: P.args, **kwargs: P.kwargs) -> Future[R]:
                return replace_or_add_task(topic_name, fn, *args, **kwargs)
        else:
            @wraps(fn)
            def decorated(*args: P.args, **kwargs: P.kwargs) -> Future[R]:
                return add_task(topic_name, fn, *args, **kwargs)

        return decorated

    if fn is not None:
        return decorator(fn)
    return decorator


PackageControlFx = Topic("package_control_fx")


def replace_or_add_task(
    topic: str, fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs
) -> Future[R]:
    task = TopicTask(topic, partial(fn, *args, **kwargs))
    enqueue_on_ui(_replace_or_add_task, task)
    return task.future


def add_task(
    topic: str, fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs
) -> Future[R]:
    task = TopicTask(topic, partial(fn, *args, **kwargs))
    enqueue_on_ui(_add_task, task)
    return task.future


def _replace_or_add_task(task: TopicTask):
    global queue, running_topics
    assert_it_runs_on_ui()

    for task_ in queue:
        if task.topic == task_.topic:
            print(f"Removed one redundant task from {task.topic}")
            queue.remove(task_)
            move_future_resolution_along(task_, task.future)
    _add_task(task)


def move_future_resolution_along(task: TopicTask, target: Future):
    def tell_other_task(_):
        if task.status != "pending":
            return
        try:
            result = target.result()
        except CancelledError:
            task.future.cancel()
        except Exception as e:
            task.future.set_exception(e)
        else:
            task.future.set_result(result)

    target.add_done_callback(tell_other_task)


def _add_task(task: TopicTask):
    global queue, running_topics
    assert_it_runs_on_ui()

    if task.topic not in running_topics and (worker := get_idle_worker(task.is_orchestrator)):
        # print(f"use worker {worker} for {task.topic}")
        schedule(worker, task)
    else:
        queue.append(task)
    # print("running_topics:", running_topics, "queue length:", len(queue))


def _tick(w, task):
    global queue, running_topics
    assert_it_runs_on_ui()

    running_topics.discard(task.topic)
    for task in queue:
        if task.topic not in running_topics:
            queue.remove(task)
            if schedule(w, task):
                break
    else:
        w.idle = True
    # print("running_topics:", running_topics, "queue length:", len(queue))


def _cancel_topic(topic: str):
    global queue
    assert_it_runs_on_ui()

    for task in queue:
        if task.topic == topic:
            task.future.cancel()
            print(f"Cancelled {task}", task.fn)


def _did_shutdown(w):
    global running_workers
    assert_it_runs_on_ui()

    # print(f"shutdown worker {len(running_workers)}/{MAX_WORKERS}:", w)
    try:
        running_workers.remove(w)
    except ValueError:
        pass
    # if not running_workers:
    #     print(f"workers running: {len(running_workers)}/{MAX_WORKERS}")


def schedule(w: Worker, task: TopicTask) -> bool:
    global running_topics
    if task.future.set_running_or_notify_cancel():
        running_topics.add(task.topic)
        w.idle = False
        w.send(task)
        return True
    return False


def get_idle_worker(orchestrator: bool):
    global running_workers
    for w in running_workers:
        if w.idle and w.orchestrator == orchestrator:
            return w
    # if orchestrator or len(running_workers) < MAX_WORKERS:
    if sum(1 for w in running_workers if not w.orchestrator) < MAX_WORKERS:
        return spawn(orchestrator)


def spawn(orchestrator: bool):
    global running_workers
    w = Worker(orchestrator)
    w.start()
    running_workers.append(w)
    # print(f"spawn worker {len(running_workers)}/{MAX_WORKERS}:", w)
    return w


class Worker(threading.Thread):
    def __init__(self, orchestrator: bool = False):
        super().__init__()
        self.orchestrator = orchestrator
        self.queue: SimpleQueue[TopicTask] = SimpleQueue()
        self.idle = True

    def send(self, task):
        self.queue.put(task)

    def run(self):
        try:
            while task := self.queue.get(timeout=KEEP_ALIVE_TIME):
                try:
                    rv = task.fn()
                    task.future.set_result(rv)
                except Exception as e:
                    print("Exception in worker", task, e)
                    traceback.print_exc()
                    task.future.set_exception(e)
                    enqueue_on_ui(_cancel_topic, task.topic)
                finally:
                    enqueue_on_ui(_tick, self, task)
        except Empty:
            pass
        finally:
            enqueue_on_ui(_did_shutdown, self)
