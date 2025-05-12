from __future__ import annotations
from collections import deque
from concurrent.futures import Future
from functools import partial, wraps
import logging
from queue import SimpleQueue, Empty
import threading
import traceback

from typing import Callable, TypeVar, Generic, Optional, Any
from typing_extensions import ParamSpec, TypeAlias

from .runtime import assert_it_runs_on_ui, enqueue_on_ui
import sublime


T = TypeVar('T')
P = ParamSpec('P')


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
    def status(self):
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
running_topics = set()
running_workers: list[Worker] = []


def add_task(topic: str, fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> Future[T]:
    return add(topic, partial(fn, *args, **kwargs))


def add(topic: str, fn: Callable[[], T]) -> Future[T]:
    task = TopicTask(topic, fn)
    enqueue_on_ui(_add, task)
    return task.future


def _add(task: TopicTask):
    global queue, running_topics
    assert_it_runs_on_ui()

    if task.topic not in running_topics and (worker := get_idle_worker()):
        schedule(worker, task)
    else:
        queue.append(task)


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


def _cancel_topic(topic: str):
    global queue
    assert_it_runs_on_ui()

    queue = [task for task in queue if task.topic != topic]


def _did_shutdown(w):
    global running_workers
    assert_it_runs_on_ui()

    try:
        running_workers.remove(w)
    except ValueError:
        pass


def schedule(w: Worker, task: TopicTask) -> bool:
    global running_topics
    if task.future.set_running_or_notify_cancel():
        running_topics.add(task.topic)
        w.idle = False
        w.send(task)
        return True
    return False


def get_idle_worker():
    global running_workers
    for w in running_workers:
        if w.idle:
            return w
    if len(running_workers) < MAX_WORKERS:
        return spawn()


def spawn():
    global running_workers
    w = Worker()
    w.start()
    running_workers.append(w)
    return w


class Worker(threading.Thread):
    def __init__(self):
        super().__init__()
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
