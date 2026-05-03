import importlib
from unittesting import DeferrableTestCase

from .mockito import unstub
from .parameterized import parameterized as p
plugin = importlib.import_module('Package x Control.impl.worker')


class TestWorker(DeferrableTestCase):
    def teardown(self):
        unstub()


    @p.expand([
        (0, 0, ""),
        (1, 0, "(*/1)"),
        (4, 0, "(*/4)"),
        (4, 123, "(123/4)"),
    ])
    def test_worker_status_text(self, open_threads, workload, expected):
        actual = plugin.worker_status_text(open_threads, workload)
        self.assertEqual(expected, actual)
