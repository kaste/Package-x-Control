import importlib
from unittesting import DeferrableTestCase

from .mockito import unstub
from .parameterized import parameterized as p
plugin = importlib.import_module('Package x Control.impl.worker')


class FakeWorker:
    def __init__(self, orchestrator=False, idle=False):
        self.orchestrator = orchestrator
        self.idle = idle
        self.task = None

    def send(self, task):
        self.task = task


class TestWorker(DeferrableTestCase):
    def teardown(self):
        unstub()


    @p.expand([
        (0, 0, True, ""),
        (1, 0, True, "(*/1)"),
        (4, 0, True, "(*/4)"),
        (4, 0, False, "( /4)"),
        (4, 123, True, "(123/4)"),
        (4, 123, False, "(123/4)"),
    ])
    def test_worker_status_text(self, open_threads, workload, blink_on, expected):
        actual = plugin.worker_status_text(open_threads, workload, blink_on)
        self.assertEqual(expected, actual)


    @p.expand([
        (0, 0, False),
        (1, 0, True),
        (4, 0, True),
        (4, 123, False),
    ])
    def test_status_is_waiting(self, open_threads, workload, expected):
        actual = plugin.status_is_waiting(open_threads, workload)
        self.assertEqual(expected, actual)


    def test_tick_reuses_worker_for_same_kind_only(self):
        original_queue = plugin.queue
        original_running_topics = plugin.running_topics
        original_update_status_bar = plugin.update_status_bar
        normal_task = plugin.TopicTask("normal", lambda: None)
        orchestrator_task = plugin.TopicTask("orchestrator:orchestrator", lambda: None)
        finished_task = plugin.TopicTask("finished", lambda: None)
        worker = FakeWorker(orchestrator=False)
        plugin.queue = [orchestrator_task, normal_task]
        plugin.running_topics = {finished_task.topic}
        plugin.update_status_bar = lambda: None
        try:
            plugin._tick(worker, finished_task)
            remaining_queue = plugin.queue[:]
        finally:
            plugin.queue = original_queue
            plugin.running_topics = original_running_topics
            plugin.update_status_bar = original_update_status_bar

        self.assertEqual(normal_task, worker.task)
        self.assertEqual(False, worker.idle)
        self.assertEqual([orchestrator_task], remaining_queue)


    def test_orchestrators_spawn_when_normal_workers_are_full(self):
        original_workers = plugin.running_workers
        original_spawn = plugin.spawn
        plugin.running_workers = [FakeWorker(orchestrator=False) for _ in range(4)]

        def spawn(orchestrator):
            worker = FakeWorker(orchestrator=orchestrator, idle=True)
            plugin.running_workers.append(worker)
            return worker

        plugin.spawn = spawn
        try:
            worker = plugin.get_idle_worker(orchestrator=True)
        finally:
            plugin.running_workers = original_workers
            plugin.spawn = original_spawn

        self.assertIsNotNone(worker)
        self.assertEqual(True, worker.orchestrator)
