import importlib
from unittesting import DeferrableTestCase

from .mockito import unstub
from .parameterized import parameterized as p
plugin = importlib.import_module('Package x Control.impl.git_package')


class TestCheckOutput(DeferrableTestCase):
    def teardown(self):
        unstub()

    @p.expand([
        ("1.2, 1.3, 1.4", "4070", "1.2, 1.3, 1.4"),

        ("1.2, st3-1.3, 1.3, 1.4", "4070", "1.2, 1.3, 1.4"),
        ("1.2, st3-1.3, 1.3, 1.4", "3070", "st3-1.3"),

        ("1.2, st3070-1.3, 1.3, st4070-1.4, 1.4, 1.5", "4092", "1.2, 1.3, 1.4, 1.5"),
        ("1.2, st3070-1.3, 1.3, st4070-1.4, 1.4, 1.5", "4070", "st4070-1.4"),
        ("1.2, st3070-1.3, 1.3, st4070-1.4, 1.4, 1.5", "4060", "st4070-1.4"),
        ("1.2, st3070-1.3, 1.3, st4070-1.4, 1.4, 1.5", "3050", "st3070-1.3"),

        ("1.2, st3070-1.3, 1.3, st4070-1.4, 1.4, st4070-1.5", "4060", "st4070-1.4, st4070-1.5"),

        ("1.2, 4070-1.3, 4070-1.4", "4080", "4070-1.3, 4070-1.4"),
        ("1.2, 4070-1.3, 4070-1.4", "4070", "4070-1.3, 4070-1.4"),
        ("1.2, 4070-1.3, 4070-1.4", "4060", "1.2"),
        ("1.2, 3070-1.3, 4070-1.4", "4060", "3070-1.3"),
    ])
    def test_filter_tags(self, available_tags, build, filtered_tags):
        actual = plugin.filter_tags(
            {n: 1 for n in available_tags.split(", ")},
            int(build)
        )
        self.assertEqual(
            {n: 1 for n in filtered_tags.split(", ")},
            actual
        )
