from __future__ import annotations

import asyncio
import aiohttp
import json
import os
import re
import time
from collections import deque
from urllib.parse import urljoin
from typing import Generic, Iterable, List, TypeVar

T = TypeVar("T")
PackageDb = List[dict]

OUTPUT_FILE = os.path.abspath("registry.json")
DEFAULT_CHANNEL = (
    "https://raw.githubusercontent.com/wbond/package_control_channel"
    "/refs/heads/master/channel.json"
)
MAX_CONCURRENCY = 32
GLOBAL_TIMEOUT = 60  # seconds


async def main():
    try:
        async with asyncio.timeout(GLOBAL_TIMEOUT):
            db = await fetch_packages()
            with open(OUTPUT_FILE, 'w') as f:
                json.dump(db, f)
            print(f"Saved registry as {OUTPUT_FILE}")
    except asyncio.TimeoutError:
        print(f"Timeout: script took more than {GLOBAL_TIMEOUT} seconds")


async def fetch_packages() -> PackageDb:
    print("Fetching registered packages from packagecontrol.io")
    now = time.monotonic()

    results: dict[str, dict] = {}

    async with aiohttp.ClientSession() as session:
        repos: list[str] = await get_repositories(DEFAULT_CHANNEL, session)
        urls_to_fetch = DedupQueue(repos)
        await asyncio.gather(*[
            asyncio.create_task(drain_queue(urls_to_fetch, results, session))
            for _ in range(MAX_CONCURRENCY)
        ])

    # Reassemble packages
    urls_ordered = DedupQueue(reversed(repos))
    packages = []
    while True:
        try:
            url = urls_ordered.pop()
        except IndexError:
            break
        result = results.get(url, {"packages": [], "includes": []})
        packages.extend(result["packages"])
        for include in reversed(result["includes"]):
            urls_ordered.append(include)

    print(f"{len(packages)} packages in total.")
    elapsed = time.monotonic() - now
    print(f"Prepared packages in {elapsed:.2f} seconds.")
    return packages


async def drain_queue(
    urls_to_fetch: DedupQueue[str],
    results: dict[str, dict],
    session: aiohttp.ClientSession
) -> None:
    while True:
        try:
            location = urls_to_fetch.popleft()
        except IndexError:
            break

        result = await fetch_repo(location, session)
        urls_to_fetch.extend(result["includes"])
        results[location] = result


async def fetch_repo(location: str, session: aiohttp.ClientSession) -> dict:
    try:
        repo_info = await http_get_json(location, session)
    except Exception as e:
        print(f"Error fetching {location}: {e}")
        return {"packages": [], "includes": []}

    if repo_info.get("schema_version", "1.2").startswith("1."):
        return {"packages": [], "includes": []}

    includes = list(resolve_urls(location, repo_info.get("includes", [])))
    repo_info["includes"] = includes
    return repo_info


async def get_repositories(channel_url: str, session: aiohttp.ClientSession) -> list[str]:
    channel_info = await http_get_json(channel_url, session)
    return [
        update_url(url)
        for url in resolve_urls(channel_url, channel_info['repositories'])
    ]


async def http_get_json(location: str, session: aiohttp.ClientSession) -> dict:
    text = await http_get(location, session)
    return json.loads(text)


async def http_get(location: str, session: aiohttp.ClientSession) -> str:
    headers = {'User-Agent': 'Mozilla/5.0'}
    async with session.get(location, headers=headers) as resp:
        resp.raise_for_status()
        return await resp.text()


def resolve_urls(root_url, uris):
    scheme_match = re.match(r'^(file:/|https?:)//', root_url, re.I)
    for url in uris:
        if not url:
            continue
        if url.startswith('//'):
            if scheme_match is not None:
                url = scheme_match.group(1) + url
            else:
                url = 'https:' + url
        elif url.startswith('/') or url.startswith('file:'):
            continue
        elif url.startswith('./') or url.startswith('../'):
            url = urljoin(root_url, url)
        yield url


def update_url(url: str) -> str:
    if not url:
        return url
    url = url.replace('://raw.github.com/', '://raw.githubusercontent.com/')
    url = url.replace('://nodeload.github.com/', '://codeload.github.com/')
    url = re.sub(
        r'^(https://codeload\.github\.com/[^/#?]+/[^/#?]+/)zipball(/.*)$',
        '\\1zip\\2',
        url
    )
    if url in {
        'https://sublime.wbond.net/repositories.json',
        'https://sublime.wbond.net/channel.json',
    }:
        url = 'https://packagecontrol.io/channel_v3.json'
    return url


class DedupQueue(Generic[T]):
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
        for item in items:
            self.append(item)

    def pop(self) -> T:
        return self._queue.pop()

    def popleft(self) -> T:
        return self._queue.popleft()


if __name__ == "__main__":
    asyncio.run(main())
