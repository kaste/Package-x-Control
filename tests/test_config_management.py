import importlib
from unittesting import DeferrableTestCase

from .mockito import unstub
from .parameterized import parameterized as p
plugin = importlib.import_module('Package x Control.impl.config_management')


class TestConfigManagement(DeferrableTestCase):
    def teardown(self):
        unstub()


    @p.expand([
        ("https://github.com/alexkuz/SublimeLinter-inline-errors.git", "alexkuz"),
        ("git@github.com:alexkuz/SublimeLinter-inline-errors.git", "alexkuz"),
        ("alexkuz/SublimeLinter-inline-errors", "alexkuz"),
    ])
    def test_extract_user(self, url, user):
        self.assertEqual(user, plugin.extract_user(url))


    @p.expand(
        [
            (
                "https://github.com/alexkuz/SublimeLinter-inline-errors.git",
                "SublimeLinter-inline-errors",
            ),
            (
                "git@github.com:alexkuz/SublimeLinter-inline-errors.git",
                "SublimeLinter-inline-errors",
            ),
            ("alexkuz/SublimeLinter-inline-errors", "SublimeLinter-inline-errors"),
        ]
    )
    def test_extract_repo_name(self, url, repo_name):
        self.assertEqual(repo_name, plugin.extract_repo_name(url))


    @p.expand([
        (
            "alexkuz/SublimeLinter-inline-errors",
            "https://github.com/alexkuz/SublimeLinter-inline-errors.git"
        ),
        (
            "https://github.com/alexkuz/SublimeLinter-inline-errors.git",
            "https://github.com/alexkuz/SublimeLinter-inline-errors.git"
        ),
        (
            "git@github.com:alexkuz/SublimeLinter-inline-errors.git",
            "git@github.com:alexkuz/SublimeLinter-inline-errors.git"
        ),
    ])
    def test_expand_git_url(self, url, expected):
        self.assertEqual(expected, plugin.expand_git_url(url))


    @p.expand([
        (
            "kaste/Outline",
            {
                "name": "Outline",
                "url": "https://github.com/kaste/Outline.git",
                "refs": "tags/*",
                "unpacked": False
            }),
        (
            "https://github.com/kaste/Outline.git",
            {
                "name": "Outline",
                "url": "https://github.com/kaste/Outline.git",
                "refs": "tags/*",
                "unpacked": False
            }),
        (
            "/foo/bar.git",
            {
                "name": "bar",
                "url": "/foo/bar.git",
                "refs": "tags/*",
                "unpacked": False
            }),
        (
            {
                "url": "https://github.com/kaste/Outline.git",
            },
            {
                "name": "Outline",
                "url": "https://github.com/kaste/Outline.git",
                "refs": "tags/*",
                "unpacked": False
            }),
        (
            {
                "url": "https://github.com/kaste/Outline.git",
                "refs": "fool",
                "unpacked": True
            },
            {
                "name": "Outline",
                "url": "https://github.com/kaste/Outline.git",
                "refs": "fool",
                "unpacked": True
            }),
    ])
    def test_normalize_entry(self, entry, expected):
        self.assertEqual(expected, plugin.normalize_config_entry(entry))


    @p.expand([
        ("kaste Outline",),
        ("kaste",),
        ("./foo/bar",),
        (R"c:\foo\bar",),
        (42,),
        ({},),

        ({
            "name": "Outline",
            },),
        ({
            "name": "Outline",
            "url": "kaste Outline",
            },),
        ({
            "name": "Outline",
            "url": "kaste",
            },),
        ({
            "name": "Outline",
            "url": "./foo/bar",
            },),
        ({
            "name": "Outline",
            "url": R"c:\foo\bar",
            },),
        ({
            "name": "Outline",
            "url": 42,
            },),
        ({
            "name": "Outline",
            "url": {},
            },),
    ])
    def test_invalid_entries(self, entry):
        with self.assertRaises(ValueError):
            actual = plugin.normalize_config_entry(entry)
            print("actual", actual)


    @p.expand([
        (
            {
                "name": "Outline",
                "url": "https://github.com/kaste/Outline.git",
                "refs": "tags/*",
                "unpacked": False
            },
            "kaste/Outline"),
        (
            {
                "name": "Outline",
                "url": "/kaste/Outline.git",
                "refs": "tags/*",
                "unpacked": False
            },
            "/kaste/Outline.git"),

    ])
    def test_simplify_entry(self, entry, expected):
        self.assertEqual(expected, plugin.simplify_entry(entry))


    @p.expand([
        ({
            "name": "Inline Outline",
            "url": "https://github.com/kaste/Outline.git",
            "refs": "tags/*",
            "unpacked": False
            },),
        ({
            "name": "Outline",
            "url": "https://github.com/kaste/Outline.git",
            "refs": "dang",
            "unpacked": False
            },),
        ({
            "name": "Outline",
            "url": "https://github.com/kaste/Outline.git",
            "refs": "tags/*",
            "unpacked": True
            },),
    ])
    def test_cant_simplify_entry(self, entry):
        self.assertEqual(entry, plugin.simplify_entry(entry))




    @p.expand([
        ({
            "name": "Outline",
            "url": "https://github.com/kaste/Outline.git",
            "refs": "tags/*",
            "unpacked": False
            },),
        ({
            "name": "Renamed",
            "url": "https://github.com/kaste/Outline.git",
            "refs": "tags/*",
            "unpacked": False
            },),
        ({
            "name": "Renamed",
            "url": "https://github.com/fork/Outline.git",
            "refs": "tags/*",
            "unpacked": False
            },),
    ])
    def test_report_conflicting_entry(self, entry):
        config = ["kaste/Outline"]
        self.assertTrue(plugin.add_entry_to_configuration(entry, config, dry_run=True))


    @p.expand([
        ({
            "name": "Outline",
            "url": "https://github.com/kaste/Outline.git",
            "refs": "tags/*",
            "unpacked": False
            },),
        ({
            "name": "Renamed",
            "url": "https://github.com/kaste/Outline.git",
            "refs": "tags/*",
            "unpacked": False
            },),
        ({
            "name": "Renamed",
            "url": "https://github.com/fork/Outline.git",
            "refs": "tags/*",
            "unpacked": False
            },),
    ])
    def test_add_entry_to_configuration(self, entry):
        config = ["kaste/Outline"]
        plugin.add_entry_to_configuration(entry, config)
        self.assertEqual(1, len(config))


    @p.expand([
        ({
            "name": "Outline",
            "url": "https://github.com/kaste/Outline.git",
            "refs": "tags/*",
            "unpacked": False
            }, ["kaste/Outline"]),
        ({
            "name": "Outline",
            "url": "https://github.com/fork/Outline.git",
            "refs": "tags/*",
            "unpacked": False
            }, ["fork/Outline"]),
    ])
    def test_add_entry_to_configuration_simplifies_entry(self, entry, expected):
        config = ["kaste/Outline"]
        plugin.add_entry_to_configuration(entry, config)
        self.assertEqual(expected, config)


    @p.expand([
        (["kaste/Outline"],),
        ([{
            "name": "Outline",
            "url": "https://github.com/kaste/Outline.git",
            "refs": "tags/*",
            "unpacked": False
            }],),
    ])
    def test_remove_entry_by_name(self, config):
        name = "Outline"
        plugin.remove_entry_by_name(name, config)
        self.assertEqual([], config)


    @p.expand([
        (["kaste/Outline", "kaste/Outline"],),
        (["kaste/Outline", "fork/Outline"],),
        (["kaste/Outline", "/foo/bar/Outline"],),
        ([
            "kaste/Outline",
            {
                "name": "Outline",
                "url": "https://github.com/kaste/Outline.git",
                "refs": "tags/*",
                "unpacked": False
                }],),
        ([
            "kaste/Outline",
            {
                "name": "Renamed",
                "url": "https://github.com/kaste/Outline.git",
                "refs": "tags/*",
                "unpacked": False
                }],),
        ([
            "kaste/Outline",
            {
                "name": "Renamed",
                "url": "https://github.com/fork/Outline.git",
                "refs": "tags/*",
                "unpacked": False
                }],),
    ])
    def test_invalid_config(self, config):
        with self.assertRaises(ValueError):
            plugin.process_config(config)
