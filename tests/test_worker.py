import importlib
from unittesting import DeferrableTestCase

from .mockito import unstub
from .parameterized import parameterized as p
plugin = importlib.import_module('Package x Control.impl.worker')


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
