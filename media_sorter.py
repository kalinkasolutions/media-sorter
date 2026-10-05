#!/usr/bin/env python3
"""Watches the download folder and files finished movies and series into the Plex libraries.

Movies:  MOVIES/Title (Year)/<original file name>
Series:  SERIES/Show (Year)/Season 01/Show (Year) - S01E02.mkv

Anything that isn't a recognisable movie or episode is left where it is.
"""

import contextlib
import functools
import json
import logging
import os
import re
import shutil
import signal
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from guessit import guessit

VERSION = os.environ.get("VERSION", "dev")

DOWNLOADS = Path(os.environ.get("DOWNLOADS", "/mnt/shared"))
MOVIES = Path(os.environ.get("MOVIES", "/storage/movies"))
SERIES = Path(os.environ.get("SERIES", "/storage/series"))
TMDB_KEY = os.environ.get("TMDB_KEY", "")
PLEX_URL = os.environ.get("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
# a dry run moves nothing, so it needn't wait for downloads to settle
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5" if DRY_RUN else "30"))
SETTLE_SECONDS = int(os.environ.get("SETTLE_SECONDS", "0" if DRY_RUN else "300"))

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv"}
# JDownloader's in-progress downloads
UNFINISHED_SUFFIXES = {".part"}

log = logging.getLogger("media-sorter")

# set by SIGUSR1: `docker kill -s USR1 media-sorter`
run_now = False


def request_run_now(signum, frame) -> None:
    global run_now
    run_now = True


@dataclass(frozen=True)
class Placement:
    source: Path
    destination: Path
    plex_type: str  # Plex's section type: "movie" or "show"


class NotRecognised(Exception):
    pass


class MoveFailed(Exception):
    pass


def files_in(item: Path) -> list[Path]:
    if item.is_file():
        return [item]
    return [path for path in item.rglob("*") if path.is_file()]


def snapshot(item: Path) -> tuple[int, int]:
    files = files_in(item)
    return len(files), sum(path.stat().st_size for path in files)


def is_unfinished(item: Path) -> bool:
    return any(path.suffix.lower() in UNFINISHED_SUFFIXES for path in files_in(item))


def is_sample(path: Path) -> bool:
    return re.search(r"\bsample\b", str(path), re.IGNORECASE) is not None


def videos_in(item: Path) -> list[Path]:
    videos = [path for path in files_in(item) if path.suffix.lower() in VIDEO_EXTENSIONS and not is_sample(path)]
    return sorted(videos, key=lambda path: path.stat().st_size, reverse=True)


def safe_filename(name: str) -> str:
    name = name.replace(": ", " - ")
    return re.sub(r'[<>:"/\\|?*]', "", name).strip(" .")


def episode_code(season: int, episode: int | list[int]) -> str:
    episodes = episode if isinstance(episode, list) else [episode]
    return f"S{season:02}" + "-".join(f"E{number:02}" for number in episodes)


def tmdb_search(kind: str, params: dict) -> list[dict]:
    key = TMDB_KEY.strip().strip("\"'")
    # TMDb hands out two credentials: the short v3 API key and the long "Read Access Token" (a JWT)
    if key.startswith("eyJ"):
        headers = {"Authorization": f"Bearer {key}"}
    else:
        headers, params = {}, {**params, "api_key": key}
    url = f"https://api.themoviedb.org/3/search/{kind}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
            return json.load(response)["results"]
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise SystemExit("TMDb rejected TMDB_KEY (401): check it at https://www.themoviedb.org/settings/api")
        raise


def with_umlauts(title: str) -> str:
    for spelled_out, umlaut in [("ae", "ä"), ("oe", "ö"), ("ue", "ü"), ("Ae", "Ä"), ("Oe", "Ö"), ("Ue", "Ü")]:
        title = title.replace(spelled_out, umlaut)
    return title


def search_attempts(title: str, year: int | None) -> list[tuple[str, int | None]]:
    """The (query, year) pairs to try in order, from exact to loose; loose queries must match the year."""
    spellings = list(dict.fromkeys([title, with_umlauts(title)]))
    if not year:
        return [(spelling, None) for spelling in spellings]
    words = spellings[-1].split()
    # one word TMDb doesn't know, like "gibts" for "gibt's", fails the whole search
    one_word_left_out = [" ".join(words[:i] + words[i + 1:]) for i in range(len(words))] if len(words) >= 3 else []
    return [(query, year) for query in spellings + one_word_left_out] + [(spelling, None) for spelling in spellings]


@functools.cache
def tmdb_lookup(kind: str, title: str, year: int | None) -> str | None:
    """Returns the canonical "Title (Year)" for a movie or tv show, or None if TMDb doesn't know it."""
    year_param = "year" if kind == "movie" else "first_air_date_year"
    for query, year_filter in search_attempts(title, year):
        params = {"query": query}
        if year_filter:
            params[year_param] = year_filter
        results = tmdb_search(kind, params)
        if results:
            best = results[0]
            name = best["title"] if kind == "movie" else best["name"]
            date = best.get("release_date" if kind == "movie" else "first_air_date") or ""
            return safe_filename(f"{name} ({date[:4]})" if date else name)
    return None


def plan(item: Path) -> list[Placement]:
    videos = videos_in(item)
    if not videos:
        raise NotRecognised("no video file")

    # guessit reads the folder names too, which often carry the info the file name lacks
    guesses = {video: guessit(str(video.relative_to(DOWNLOADS))) for video in videos}
    main = guesses[videos[0]]

    if main.get("type") == "episode":
        return [plan_episode(video, guess) for video, guess in guesses.items()]
    return [plan_movie(videos[0], main)]


def plan_movie(video: Path, guess: dict) -> Placement:
    if "title" not in guess:
        raise NotRecognised(f"no title in {video.name}")
    # guessit splits "Dune.Part.Two" into title "Dune" and part 2
    queries = [f"{guess['title']} Part {guess['part']}", guess["title"]] if "part" in guess else [guess["title"]]
    name = next(filter(None, (tmdb_lookup("movie", query, guess.get("year")) for query in queries)), None)
    if not name:
        raise NotRecognised(f"TMDb has no movie '{guess['title']}' ({guess.get('year')})")
    return Placement(video, MOVIES / name / video.name, "movie")


def plan_episode(video: Path, guess: dict) -> Placement:
    if guess.get("type") != "episode" or not {"title", "season", "episode"} <= guess.keys():
        raise NotRecognised(f"no season/episode in {video.name}")
    if isinstance(guess["season"], list):
        raise NotRecognised(f"{video.name} spans several seasons")
    show = tmdb_lookup("tv", guess["title"], guess.get("year"))
    if not show:
        raise NotRecognised(f"TMDb has no series '{guess['title']}'")
    season_folder = SERIES / show / f"Season {guess['season']:02}"
    file_name = f"{show} - {episode_code(guess['season'], guess['episode'])}{video.suffix.lower()}"
    return Placement(video, season_folder / file_name, "show")


def file_into_library(item: Path, placements: list[Placement]) -> None:
    for placement in placements:
        log.info("%s -> %s", placement.source.relative_to(DOWNLOADS), placement.destination)
    if DRY_RUN:
        return

    clashes = [placement.destination for placement in placements if placement.destination.exists()]
    if clashes:
        raise NotRecognised(f"already in the library: {', '.join(map(str, clashes))}")

    for placement in placements:
        try:
            placement.destination.parent.mkdir(parents=True, exist_ok=True)
            # copyfile, not copy2: the file should take the library's permissions and ACLs, not the download's
            shutil.move(placement.source, placement.destination, copy_function=shutil.copyfile)
        except OSError as error:
            # a half-copied file would look like it's already in the library on the next attempt
            with contextlib.suppress(OSError):
                placement.destination.unlink(missing_ok=True)
            raise MoveFailed(f"{placement.source.name} -> {placement.destination}: {error.strerror or error}")

    # what's left is .url/.txt/.html/.nfo, samples and extracted archives
    if item.is_dir():
        try:
            shutil.rmtree(item)
        except OSError as error:
            log.error("Filed %s, but could not delete the download folder: %s", item.name, error.strerror or error)


def plex_request(path: str) -> dict:
    request = urllib.request.Request(
        f"{PLEX_URL}{path}",
        headers={"X-Plex-Token": PLEX_TOKEN, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read()
    return json.loads(body) if body else {}


def refresh_plex(plex_types: set[str]) -> None:
    if not PLEX_URL or not plex_types or DRY_RUN:
        return
    sections = plex_request("/library/sections")["MediaContainer"].get("Directory", [])
    for section in sections:
        if section["type"] in plex_types:
            log.info("Rescanning Plex library %s", section["title"])
            plex_request(f"/library/sections/{section['key']}/refresh")


def sleep_until_next_poll() -> None:
    for _ in range(POLL_SECONDS):
        if run_now:
            return
        time.sleep(1)


def watch() -> None:
    global run_now
    first_seen_unchanged: dict[Path, tuple[tuple[int, int], float]] = {}
    # remembered so an unrecognised download is only retried once its content changes
    skipped: dict[Path, tuple[int, int]] = {}
    reported_unfinished: set[Path] = set()

    while True:
        touched_libraries: set[str] = set()
        # unchanged since the previous poll is still required, so a download being written is left alone
        settle_seconds = 0 if run_now else SETTLE_SECONDS
        if run_now:
            log.info("Triggered: not waiting for downloads to settle, retrying everything")
            skipped.clear()
            run_now = False
        present = set(DOWNLOADS.iterdir())

        for item in sorted(present):
            if item.name.startswith("."):
                continue
            try:
                current = snapshot(item)
            except FileNotFoundError:
                continue
            if skipped.get(item) == current:
                continue

            seen = first_seen_unchanged.get(item)
            if seen is None or seen[0] != current:
                first_seen_unchanged[item] = (current, time.monotonic())
                continue
            if is_unfinished(item):
                if item not in reported_unfinished:
                    log.info("Waiting for %s: still downloading", item.name)
                    reported_unfinished.add(item)
                continue
            if time.monotonic() - seen[1] < settle_seconds:
                continue

            try:
                placements = plan(item)
                file_into_library(item, placements)
                touched_libraries |= {placement.plex_type for placement in placements}
                first_seen_unchanged.pop(item, None)
                if DRY_RUN:
                    skipped[item] = current
            except NotRecognised as reason:
                log.warning("Leaving %s: %s", item.name, reason)
                skipped[item] = current
            except MoveFailed as reason:
                log.error("Move failed for %s: %s", item.name, reason)
                # retried once the download changes or on a trigger, not every poll
                skipped[item] = current
                # files moved before the failure should still show up in Plex
                touched_libraries |= {placement.plex_type for placement in placements}
            except Exception:
                # network trouble and the like: try again next round
                log.exception("Failed on %s", item.name)

        try:
            refresh_plex(touched_libraries)
        except Exception:
            log.exception("Plex rescan failed")

        for gone in set(first_seen_unchanged) - present:
            del first_seen_unchanged[gone]
        for gone in set(skipped) - present:
            del skipped[gone]
        reported_unfinished &= present

        sleep_until_next_poll()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not TMDB_KEY:
        raise SystemExit("TMDB_KEY is not set")
    signal.signal(signal.SIGUSR1, request_run_now)
    log.info(
        "media-sorter %s%s: watching %s (%d entries), checking every %ds, settle time %ds",
        VERSION, " (dry run)" if DRY_RUN else "", DOWNLOADS,
        len(list(DOWNLOADS.iterdir())), POLL_SECONDS, SETTLE_SECONDS,
    )
    watch()
