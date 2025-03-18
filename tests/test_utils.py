import importlib
from unittesting import DeferrableTestCase

from .mockito import unstub
from .parameterized import parameterized as p
plugin = importlib.import_module('Package x Control.impl.utils')


class TestUtils(DeferrableTestCase):
    def teardown(self):
        unstub()


    @p.expand([
        (18000, "5 hours ago"),         # 5 hours ago
        (432000, "Tue Aug 25 19:20"),   # 5 days ago
        (1728000, "Mon Aug 10 19:20"),  # 3 weeks ago
        (13000000, "Thu Apr 2 08:13"),  # 5 months ago
        (31449600, "Aug 31 2008"),      # 12 months ago
        (37500000, "Jun 22 2008"),      # 1 year, 2 months ago
        (55188000, "Dec 1 2007"),       # 1 year, 9 months ago
        (630000000, "Sep 13 1989"),     # 20 years ago
    ])
    def test_human_date(self, delta, expected):
        # Test using reference time: 2009-08-30 19:20:00
        TEST_DATE_NOW = 1251660000
        actual = plugin.human_date(TEST_DATE_NOW - delta, TEST_DATE_NOW)
        self.assertEqual(actual, expected)
