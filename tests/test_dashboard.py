import importlib
from collections import namedtuple
from unittesting import DeferrableTestCase

from .mockito import unstub
from .parameterized import parameterized as p
plugin = importlib.import_module('Package x Control.impl.dashboard')


VersionDescription = namedtuple("VersionDescription", "kind specifier date")


CHANNEL_PR_DIFF = """diff --git a/repository/m.json b/repository/m.json
index 67c9256e852..f7a1c0b235d 100644
--- a/repository/m.json
+++ b/repository/m.json
@@ -519,6 +519,17 @@
 			]
 		},
+		{
+			"name": "MarkdownImagePaste",
+			"details": "https://github.com/ricetim/MarkdownImagePaste",
+			"labels": ["markdown", "image", "clipboard", "paste"],
+			"releases": [
+				{
+					"sublime_text": ">=4000",
+					"tags": true
+				}
+			]
+		},
 		{
 			"name": "Markdown Numbered Headers",
"""


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


    @p.expand([
        "https://github.com/sublimehq/package_control_channel/pull/9347",
        "https://github.com/sublimehq/package_control_channel/pull/9347/changes",
        "https://github.com/sublimehq/package_control_channel/pull/9347.diff",
    ])
    def test_parse_package_control_channel_pr_url(self, url):
        actual = plugin.parse_package_control_channel_pr_url(url)
        self.assertEqual(
            "https://github.com/sublimehq/package_control_channel/pull/9347.diff",
            actual
        )


    @p.expand([
        "https://github.com/sublimehq/Packages/pull/9347",
        "https://github.com/sublimehq/package_control_channel/issues/9347",
        "https://github.com/sublimehq/package_control_channel/pull/not-a-number",
    ])
    def test_parse_package_control_channel_pr_url_rejects_others(self, url):
        actual = plugin.parse_package_control_channel_pr_url(url)
        self.assertEqual("", actual)


    def test_parse_added_package_from_channel_diff(self):
        actual = plugin.parse_added_package_from_channel_diff(CHANNEL_PR_DIFF)
        self.assertEqual("MarkdownImagePaste", actual["name"])
        self.assertEqual(
            "https://github.com/ricetim/MarkdownImagePaste",
            actual["details"]
        )
        self.assertEqual(">=4000", actual["releases"][0]["sublime_text"])
        self.assertEqual(True, actual["releases"][0]["tags"])


    def test_package_info_from_channel_pr_url(self):
        original_http_get_text = plugin.http_get_text
        plugin.http_get_text = lambda _: CHANNEL_PR_DIFF
        try:
            package_info = plugin.package_info_from_channel_pr_url("https://example.com")
        finally:
            plugin.http_get_text = original_http_get_text

        entry, compatibility = package_info
        self.assertEqual({
            "name": "MarkdownImagePaste",
            "url": "https://github.com/ricetim/MarkdownImagePaste.git",
            "refs": "tags/*",
            "unpacked": False
        }, entry)
        self.assertEqual("", compatibility)


    def test_calculate_shared_terse_name_width(self):
        package_controlled_packages = [{"name": "Short"}]
        unmanaged_packages = [{"name": "Much Longer Package"}]

        actual = plugin.calculate_shared_terse_name_width([
            package_controlled_packages,
            unmanaged_packages
        ])

        self.assertEqual(
            plugin.calculate_terse_section_widths(unmanaged_packages)[0],
            actual
        )


    def test_format_package_terse_uses_active_date_separator(self):
        version = VersionDescription("tag", "1.7.7", 1574899200)

        actual = plugin.format_package_terse(
            {"name": "AutoHotkey", "version": version},
            20,
            10,
            {"disabled_packages": [], "registered_packages": {}}
        )

        self.assertIn("/ Nov 28 2019", actual)


    def test_format_package_terse_uses_disabled_date_separator(self):
        version = VersionDescription("tag", "1.7.7", 1574899200)

        actual = plugin.format_package_terse(
            {"name": "AutoHotkey", "version": version},
            20,
            10,
            {"disabled_packages": ["AutoHotkey"], "registered_packages": {}}
        )

        self.assertIn("Nov 28 2019", actual)
        self.assertNotIn("/ Nov 28 2019", actual)
        self.assertNotIn("| Nov 28 2019", actual)
