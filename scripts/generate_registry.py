from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait
import json
import os
import re
import threading
import time
from urllib.parse import urljoin
import urllib.request


from typing import Generic, Iterable, List, TypeVar


T = TypeVar("T")
PackageDb = List[dict]

OUTPUT_FILE = os.path.abspath("registry.json")
DEFAULT_CHANNEL = (
    "https://raw.githubusercontent.com/wbond/package_control_channel"
    "/refs/heads/master/channel.json"
)
MAX_WORKERS = 16


def main():
    db = fetch_packages()
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(db, f)
    print(f"Saved registry as {OUTPUT_FILE}")


def fetch_packages() -> PackageDb:
    print("Fetching registered packages from packagecontrol.io")
    now = time.monotonic()
    repos: list[str] = get_repositories(DEFAULT_CHANNEL)
    urls_to_fetch = DedupQueue(repos, thread_safe=True)
    results: dict[str, dict] = {}

    num_threads = min(MAX_WORKERS, len(repos))
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [
            executor.submit(drain_queue, urls_to_fetch, results)
            for _ in range(num_threads)
        ]
        wait(futures, timeout=60)

    # Collect the results
    # The main repo is the first one.  Run reversed so that others
    # can't override it.
    urls_ordered = DedupQueue(reversed(repos))
    packages = []
    while True:
        try:
            url = urls_ordered.pop()
        except IndexError:
            break
        result = results[url]
        packages.extend(result["packages"])
        for include in reversed(result["includes"]):
            urls_ordered.append(include)

    print(f"{len(packages)} packages in total.")
    elapsed = time.monotonic() - now
    print(f"Prepared packages in {elapsed:.2f} seconds.")
    return packages


def drain_queue(
    urls_to_fetch: DedupQueue[str],
    results: dict[str, dict],
) -> None:
    while True:
        try:
            location = urls_to_fetch.popleft()
        except IndexError:
            break

        try:
            result = fetch_repo(location)
            urls_to_fetch.extend(result["includes"])
        except Exception as e:
            result = {"packages": [], "includes": []}
            print(f"Error fetching {location}: {e}")
        finally:
            results[location] = result


def fetch_repo(location: str) -> dict:
    repo_info = http_get_json(location)
    repo_info["includes"] = list(resolve_urls(
        location, repo_info.get("includes", [])
    ))
    return repo_info


def get_repositories(channel_url: str) -> list[str]:
    channel_info = http_get_json(channel_url)
    return [
        update_url(url)
        for url in resolve_urls(channel_url, channel_info['repositories'])
    ]


def http_get_json(location: str) -> dict:
    json_string = http_get(location)
    return json.loads(json_string)


def http_get(location: str) -> str:
    req = urllib.request.Request(
        location,
        headers={'User-Agent': 'Mozilla/5.0'}
    )
    with urllib.request.urlopen(req) as response:
        return response.read().decode('utf-8')


def resolve_urls(root_url, uris):
    """
    Convert a list of relative uri's to absolute urls/paths.

    :param root_url:
        The root url string

    :param uris:
        An iterable of relative uri's to resolve.

    :returns:
        A generator of resolved URLs
    """

    scheme_match = re.match(r'^(file:/|https?:)//', root_url, re.I)

    for url in uris:
        if not url:
            continue
        if url.startswith('//'):
            if scheme_match is not None:
                url = scheme_match.group(1) + url
            else:
                url = 'https:' + url
        elif url.startswith('/'):
            # We don't allow absolute repositories
            continue
        elif url.startswith('./') or url.startswith('../'):
            url = urljoin(root_url, url)
        yield url


def update_url(url: str) -> str:
    """
    Takes an old, out-dated URL and updates it. Mostly used with GitHub URLs
    since they tend to be constantly evolving their infrastructure.

    :param url:
        The URL to update

    :param debug:
        If debugging is enabled

    :return:
        The updated URL
    """

    if not url:
        return url

    url = url.replace('://raw.github.com/', '://raw.githubusercontent.com/')
    url = url.replace('://nodeload.github.com/', '://codeload.github.com/')
    url = re.sub(
        r'^(https://codeload\.github\.com/[^/#?]+/[^/#?]+/)zipball(/.*)$',
        '\\1zip\\2',
        url
    )

    # Fix URLs from old versions of Package Control since we are going to
    # remove all packages but Package Control from them to force upgrades
    if (
        url == 'https://sublime.wbond.net/repositories.json'
        or url == 'https://sublime.wbond.net/channel.json'
    ):
        url = 'https://packagecontrol.io/channel_v3.json'

    return url


class _DedupQueue(Generic[T]):
    def __init__(self, items: Iterable[T] | None = None) -> None:
        self._queue: deque[T] = deque()
        self._seen: set[T] = set()
        if items:
            self.extend(items)

    def append(self, item: T) -> None:
        if item not in self._seen:
            self._seen.add(item)
            self._queue.append(item)

    def extend(self, items: Iterable[T]) -> None:
        unseen = [item for item in items if item not in self._seen]
        self._seen.update(unseen)
        self._queue.extend(unseen)

    def pop(self) -> T:
        return self._queue.pop()

    def popleft(self) -> T:
        return self._queue.popleft()


class _DedupQueueL(_DedupQueue[T]):
    def __init__(self, items: Iterable[T] | None = None) -> None:
        self._lock = threading.Lock()
        super().__init__(items)

    def append(self, item: T) -> None:
        with self._lock:
            super().append(item)

    def extend(self, items: Iterable[T]) -> None:
        with self._lock:
            super().extend(items)

    def pop(self) -> T:
        return self._queue.pop()

    def popleft(self) -> T:
        return self._queue.popleft()


class DedupQueue(Generic[T]):
    """A deduplicating queue, optionally thread safe."""
    def __new__(self, items: Iterable[T] | None = None, thread_safe: bool = False):
        return _DedupQueueL(items) if thread_safe else _DedupQueue(items)

    def __init__(                                      # noqa: E704
        self, items: Iterable[T] | None = None, thread_safe: bool = False
    ): ...
    def append(self, item: T) -> None: ...             # noqa: E704
    def extend(self, items: Iterable[T]) -> None: ...  # noqa: E704
    def pop(self) -> T: ...                            # type: ignore[empty-body]  # noqa: E704
    def popleft(self) -> T: ...                        # type: ignore[empty-body]  # noqa: E704


if __name__ == "__main__":
    main()
