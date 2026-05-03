import importlib
from unittesting import DeferrableTestCase

from .mockito import unstub
from .parameterized import parameterized as p
plugin = importlib.import_module('Package x Control.impl.dashboard')


class TestDashboard(DeferrableTestCase):
    def teardown(self):
        unstub()


    @p.expand([
        (
            "https://packagecontrol.io/packages/Solarized%20Color%20Scheme",
            "Solarized Color Scheme",
        ),
        (
            "https://packages.sublimetext.io/packages/LSP-pylsp",
            "LSP-pylsp",
        ),
        (
            "https://packages.sublimetext.io/packages/Theme%20-%20DefaultPlus/",
            "Theme - DefaultPlus",
        ),
        (
            "https://packages.sublimetext.io/packages/LSP-pylsp?foo=bar#readme",
            "LSP-pylsp",
        ),
    ])
    def test_parse_package_name_from_package_catalog_url(self, url, expected):
        actual = plugin.parse_package_name_from_package_catalog_url(url)
        self.assertEqual(expected, actual)


    @p.expand([
        "https://github.com/sublimehq/Packages",
        "https://packages.sublimetext.io/",
        "LSP-pylsp",
    ])
    def test_parse_package_name_from_package_catalog_url_rejects_others(self, url):
        actual = plugin.parse_package_name_from_package_catalog_url(url)
        self.assertEqual("", actual)
