import importlib
from unittesting import DeferrableTestCase

from .mockito import unstub
from .parameterized import parameterized as p
plugin = importlib.import_module('Package x Control.impl.the_registry')


class TestConfigManagement(DeferrableTestCase):
    def teardown(self):
        unstub()


    @p.expand([
        (
            [
                {
                    "sublime_text": ">=3000",
                    "tags": True
                }
            ],
            4175,
            "windows-x64",
            "tags/*"
        ),
        (
            [
                {
                    "sublime_text": "<4000",
                    "tags": "st3-"
                },
                {
                    "sublime_text": ">=4000",
                    "tags": True
                }
            ],
            4175,
            "windows-x64",
            "tags/*"
        ),
        (
            [
                {
                    "sublime_text": "<4000",
                    "tags": "st3-"
                },
                {
                    "sublime_text": ">=4000",
                    "tags": True
                }
            ],
            3175,
            "windows-x64",
            "tags/*"  # st3- prefix is handled automatically
        ),
        (
            [
                {
                    "sublime_text": "3154 - 3999",
                    "tags": "3154-"
                },
                {
                    "sublime_text": ">=4132",
                    "tags": "4070-"
                }
            ],
            4175,
            "windows-x64",
            "tags/*"  # 4070 prefix is handled automatically
        ),
        (
            [
                {
                    "sublime_text": "3000 - 3155",
                    "tags": "version/st3.0/"
                },
                {
                    "sublime_text": "3156 - 4085",
                    "tags": "version/st3/"
                },
                {
                    "sublime_text": ">=4086",
                    "tags": "version/st4/"
                }
            ],
            4175,
            "windows-x64",
            "tags/version/st4/*"
        ),
        (
            [
                {
                    "sublime_text": "3000 - 3155",
                    "tags": "version/st3.0/"
                },
                {
                    "sublime_text": "3156 - 4085",
                    "tags": "version/st3/"
                },
                {
                    "sublime_text": ">=4086",
                    "tags": "version/st4/"
                }
            ],
            3175,
            "windows-x64",
            "tags/version/st3/*"
        ),

        (
            [
                {
                    "sublime_text": "*",
                    "branch": "master"
                }
            ],
            4175,
            "windows-x64",
            "heads/master"
        ),
        (
            [  # "tags" key is missing!
                {
                    "sublime_text": "<3000",
                    "details": "https://github.com/vkocubinsky/SublimeTableEditor/tree/st2"
                },
                {
                    "sublime_text": ">2999",
                    "details": "https://github.com/vkocubinsky/SublimeTableEditor/tags"
                }
            ],
            4175,
            "windows-x64",
            "the-default"
        ),

    ])
    def test_extract_user(self, releases, build, platform, expected):
        actual = plugin.compute_refs_from_releases(
            releases, build, platform, default="the-default"
        )
        self.assertEqual(expected, actual)
