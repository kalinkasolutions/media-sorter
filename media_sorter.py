#!/usr/bin/env python3
"""Watches the download folder and files finished movies, series and games into their libraries.

Movies:  MOVIES/Title (Year)/<original file name>
Series:  SERIES/Show (Year)/season_01/Show.s01.e02.Episode.Title.1080p.HEVC.mkv
Games:   GAMES/Game (Year)/Game-Group.iso, other files keep their names and subfolders
Updates: GAMES/Game (Year)/update-301524-302256/<original file names>

Anything that isn't a recognisable movie or episode is left where it is.

An HTTP API on API_PORT (default 14141) names a video the same way, for tools that convert or rename
files: POST /identify {"path": ..., "codec": "HEVC", "resolution": "1080p"} (codec and resolution
optional), GET /health. No authentication, so it must only be reachable from the internal network.
"""

import contextlib
import filecmp
import functools
import json
import logging
import os
import re
import shutil
import signal
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from guessit import guessit

VERSION = os.environ.get("VERSION", "dev")

DOWNLOADS = Path(os.environ.get("DOWNLOADS", "/mnt/shared"))
MOVIES = Path(os.environ.get("MOVIES", "/storage/movies"))
SERIES = Path(os.environ.get("SERIES", "/storage/series"))
GAMES = Path(os.environ.get("GAMES", "/storage/games"))
# the same folders as the host sees them, only for the log; compose passes them in
HOST_DOWNLOADS = os.environ.get("HOST_DOWNLOADS", "")
HOST_MOVIES = os.environ.get("HOST_MOVIES", "")
HOST_SERIES = os.environ.get("HOST_SERIES", "")
HOST_GAMES = os.environ.get("HOST_GAMES", "")
TMDB_KEY = os.environ.get("TMDB_KEY", "")
IGDB_CLIENT_ID = os.environ.get("IGDB_CLIENT_ID", "")
IGDB_CLIENT_SECRET = os.environ.get("IGDB_CLIENT_SECRET", "")
PLEX_URL = os.environ.get("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
# a dry run moves nothing, so it needn't wait for downloads to settle
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5" if DRY_RUN else "30"))
SETTLE_SECONDS = int(os.environ.get("SETTLE_SECONDS", "0" if DRY_RUN else "300"))
API_PORT = int(os.environ.get("API_PORT", "14141"))

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv"}
# disc images and installers; the folder holding one is a game
GAME_EXTENSIONS = {".iso", ".exe", ".msi", ".bin", ".cue", ".nsp", ".xci", ".nsz", ".pkg", ".chd", ".rvz", ".wbfs"}
# the download site's links, not part of the game
LINK_EXTENSIONS = {".url", ".htm", ".html"}
# where a release name stops being the game's name: "Phantom.Blade.Zero.Deluxe.Edition.Hypervisor"
RELEASE_NOISE = re.compile(
    r"\b(?:\w+ edition|goty|hypervisor|crack|update|repack|multi\d*|incl|build \d+|v\d[\d.]*)\b.*",
    re.IGNORECASE,
)
# IGDB's game_type for a main game, remake, remaster, expanded game and port; not DLCs, mods or bundles
MAIN_GAME_TYPES = {0, 8, 9, 10, 11}
# JDownloader's in-progress downloads
UNFINISHED_SUFFIXES = {".part"}
# a download folder containing this file is left alone; JDownloader needs the folder's name kept
SKIP_MARKER = ".skip"
# names an unfinished copy in the library, so a leftover from a killed container is plainly safe to delete
PARTIAL_SUFFIX = ".media-sorter.partial"

log = logging.getLogger("media-sorter")

# set by SIGUSR1: `docker kill -s USR1 media-sorter`
run_now = False


def request_run_now(signum, frame) -> None:
    global run_now
    run_now = True


def stop(signum, frame) -> None:
    # as PID 1 an unhandled SIGTERM is ignored and `docker stop` waits 10s for its SIGKILL;
    # stopping mid-copy is safe, move_into_library only ever leaves a hidden *.media-sorter.partial behind
    log.info("Stopping on %s", signal.Signals(signum).name)
    raise SystemExit(0)


@dataclass(frozen=True)
class Placement:
    source: Path
    destination: Path
    plex_type: str | None  # Plex's section type: "movie" or "show"; None for a game, Plex has no games


@dataclass(frozen=True)
class TmdbMatch:
    name: str  # "Title (Year)", safe as a folder name
    id: int


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


def shown(path: Path) -> str:
    for root, host_root in ((DOWNLOADS, HOST_DOWNLOADS), (MOVIES, HOST_MOVIES), (SERIES, HOST_SERIES), (GAMES, HOST_GAMES)):
        if host_root and path.is_relative_to(root):
            return str(Path(host_root) / path.relative_to(root))
    return str(path)


def safe_filename(name: str) -> str:
    # a colon becomes a space, as in the existing library: "2001 A Space Odyssey (1968)", "2 22 (2017)"
    name = re.sub(r'[<>:"/\\|?*]', lambda match: " " if match.group() == ":" else "", name)
    return re.sub(r"\s+", " ", name).strip(" .")


def folder_key(name: str) -> str:
    # letters and digits only, so "gibt's" matches "gibts" and "Spider-Man" matches "Spiderman"
    return "".join(re.findall(r"[^\W_]+", name.casefold()))


def without_year(name: str) -> str:
    return re.sub(r"\s*\(\d{4}\)$", "", name)


def tmdb_id_in(folder_name: str) -> int | None:
    tag = re.search(r"\{tmdb-(\d+)\}", folder_name)
    return int(tag[1]) if tag else None


def library_folder(library: Path, kind: str, match: TmdbMatch) -> Path:
    """The folder for match, reusing one tagged with its TMDb id like "Scrubs (2001) {tmdb-4556}",
    one only spelled differently like "2 22 (2017)", or one without the year that TMDb takes for the same title."""
    if (library / match.name).is_dir():
        return library / match.name
    folders = [path for path in library.iterdir() if path.is_dir()] if library.is_dir() else []
    untagged = []
    for folder in folders:
        folder_id = tmdb_id_in(folder.name)
        if folder_id == match.id:
            return folder
        if folder_id is None:
            untagged.append(folder)
    for folder in untagged:
        if folder_key(folder.name) == folder_key(match.name):
            return folder
    # a bare "Scrubs" holds whichever show TMDb finds first for that name, so it gets only that show's episodes
    for folder in untagged:
        if folder_key(folder.name) == folder_key(without_year(match.name)) and tmdb_lookup(kind, folder.name, None) == match:
            return folder
    return library / match.name


def season_number_in(folder_name: str) -> int | None:
    found = re.fullmatch(r"season[ _.-]*(\d+)", folder_name, re.IGNORECASE)
    return int(found[1]) if found else None


def season_folder(show_folder: Path, season: int) -> Path:
    """The show's folder for season, reusing one spelled differently, like "Season 4" for "season_04"."""
    if show_folder.is_dir():
        for existing in sorted(show_folder.iterdir()):
            if existing.is_dir() and season_number_in(existing.name) == season:
                return existing
    return show_folder / f"season_{season:02}"


def episode_code(season: int, episodes: list[int]) -> str:
    return f"s{season:02}." + "-".join(f"e{number:02}" for number in episodes)


def dotted(text: str) -> str:
    return ".".join(safe_filename(text).split())


def codec_label(video_codec: str | None) -> str | None:
    if not isinstance(video_codec, str):
        return None
    return {"H.265": "HEVC", "H.264": "H264"}.get(video_codec, video_codec.replace(".", ""))


def tmdb_get(path: str, params: dict) -> dict | None:
    """The TMDb API's answer for path, or None if TMDb has nothing there."""
    key = TMDB_KEY.strip().strip("\"'")
    # TMDb hands out two credentials: the short v3 API key and the long "Read Access Token" (a JWT)
    if key.startswith("eyJ"):
        headers = {"Authorization": f"Bearer {key}"}
    else:
        headers, params = {}, {**params, "api_key": key}
    url = f"https://api.themoviedb.org/3/{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise SystemExit("TMDb rejected TMDB_KEY (401): check it at https://www.themoviedb.org/settings/api")
        if error.code == 404:
            return None
        raise


def tmdb_search(kind: str, params: dict) -> list[dict]:
    return tmdb_get(f"search/{kind}", params)["results"]


def tmdb_season_episodes(show_id: int, season: int) -> list[dict]:
    season_details = tmdb_get(f"tv/{show_id}/season/{season}", {})
    return season_details["episodes"] if season_details else []


@functools.cache
def episode_titles(show_id: int, season: int) -> dict[int, str]:
    return {episode["episode_number"]: episode["name"] for episode in tmdb_season_episodes(show_id, season) if episode.get("name")}


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
    if len(words) >= 3:
        one_word_left_out = [" ".join(words[:i] + words[i + 1:]) for i in range(len(words))]
    elif len(words) == 2 and words[-1].isdigit():
        # releases call the first film of a series "Pusher 1", TMDb just "Pusher"
        one_word_left_out = [words[0]]
    else:
        one_word_left_out = []
    return [(query, year) for query in spellings + one_word_left_out] + [(spelling, None) for spelling in spellings]


@functools.cache
def tmdb_lookup(kind: str, title: str, year: int | None) -> TmdbMatch | None:
    """Returns the canonical "Title (Year)" and id of a movie or tv show, or None if TMDb doesn't know it."""
    year_param = "year" if kind == "movie" else "first_air_date_year"
    date_field = "release_date" if kind == "movie" else "first_air_date"
    for query, year_filter in search_attempts(title, year):
        params = {"query": query}
        if year_filter:
            params[year_param] = year_filter
        results = tmdb_search(kind, params)
        if year_filter:
            # TMDb's year filter takes any release, so The Eagle Huntress (2016) passes 2026 for a festival screening
            results = [result for result in results if released_near(result.get(date_field), year_filter)] or results
        if results:
            best = results[0]
            name = best["title"] if kind == "movie" else best["name"]
            date = best.get(date_field) or ""
            return TmdbMatch(safe_filename(f"{name} ({date[:4]})" if date else name), best["id"])
    return None


def released_near(date: str | None, year: int) -> bool:
    """Whether a first release date lies within a year of year; German releases often come a year later."""
    return bool(date) and date[:4].isdigit() and abs(int(date[:4]) - year) <= 1


def episode_number_in_season(stem: str, season: int) -> int | None:
    """601 in "tvs-mf-x264-601" for season 6; the season must lead, so 1080p, x264 or a year never count."""
    numbers = re.findall(rf"(?<![0-9A-Za-z])0?{season}(\d{{2}})(?![0-9A-Za-z])", stem)
    return int(numbers[-1]) if numbers else None


def guess_release(relative_path: str) -> dict:
    """guessit's reading of a download, with the episode it misses in "Show.S06.../tvs-mf-x264-601.mkv"."""
    guess = dict(guessit(relative_path))
    season = guess.get("season")
    if guess.get("type") == "episode" and isinstance(season, int) and guess.get("episode") is None:
        episode = episode_number_in_season(Path(relative_path).stem, season)
        if episode is not None:
            guess["episode"] = episode
    return guess


def plan(item: Path) -> list[Placement]:
    game_roots = game_roots_in(item)
    # games carry cutscene videos, and a movie can come with a stray setup.exe
    if game_roots and not is_mostly_video(item):
        return [placement for root in game_roots for placement in plan_game(root)]

    videos = videos_in(item)
    if not videos:
        raise NotRecognised("no video or game file")

    # guessit reads the folder names too, which often carry the info the file name lacks
    guesses = {video: guess_release(str(video.relative_to(DOWNLOADS))) for video in videos}
    main = guesses[videos[0]]

    if main.get("type") == "episode":
        return [plan_episode(video, guess) for video, guess in guesses.items()]
    return [plan_movie(videos[0], main)]


def plan_movie(video: Path, guess: dict) -> Placement:
    return Placement(video, library_folder(MOVIES, "movie", movie_match(video, guess)) / video.name, "movie")


def movie_match(video: Path, guess: dict) -> TmdbMatch:
    if "title" not in guess:
        raise NotRecognised(f"no title in {video.name}")
    # guessit splits "Dune.Part.Two" into title "Dune" and part 2
    queries = [f"{guess['title']} Part {guess['part']}", guess["title"]] if "part" in guess else [guess["title"]]
    match = next(filter(None, (tmdb_lookup("movie", query, guess.get("year")) for query in queries)), None)
    if not match:
        year = f" ({guess['year']})" if guess.get("year") else ""
        raise NotRecognised(f"TMDb has no movie '{guess['title']}'{year}")
    return match


def plan_episode(video: Path, guess: dict) -> Placement:
    show = show_match(video, guess)
    folder = season_folder(library_folder(SERIES, "tv", show), guess["season"])
    file_name = episode_file_name(show, guess) + video.suffix.lower()
    return Placement(video, folder / file_name, "show")


def show_match(video: Path, guess: dict) -> TmdbMatch:
    if guess.get("type") != "episode" or not {"title", "season", "episode"} <= guess.keys():
        raise NotRecognised(f"no season/episode in {video.name}")
    if isinstance(guess["season"], list):
        raise NotRecognised(f"{video.name} spans several seasons")
    show = tmdb_lookup("tv", guess["title"], guess.get("year"))
    if not show:
        raise NotRecognised(f"TMDb has no series '{guess['title']}'")
    return show


def episode_numbers(guess: dict) -> list[int]:
    return guess["episode"] if isinstance(guess["episode"], list) else [guess["episode"]]


def episode_file_name(show: TmdbMatch, guess: dict) -> str:
    """Like "Modern.Family.s04.e02.Schooled.1080p.HEVC", leaving out what TMDb or the release doesn't say."""
    season = guess["season"]
    episodes = episode_numbers(guess)
    titles = episode_titles(show.id, season)
    title = "+".join(dict.fromkeys(filter(None, (dotted(titles[number]) for number in episodes if number in titles))))
    parts = [dotted(without_year(show.name)), episode_code(season, episodes), title, guess.get("screen_size"), codec_label(guess.get("video_codec"))]
    return ".".join(filter(None, parts))


def release_path(path: str) -> str:
    """The part of path guessit should read: below the downloads folder, else the file and two folders above it."""
    for root in filter(None, (HOST_DOWNLOADS, str(DOWNLOADS))):
        if Path(path).is_relative_to(root):
            return str(Path(path).relative_to(root))
    return str(Path(*Path(path).parts[-3:]))


def split_name(name: str) -> tuple[str, int | None]:
    """"Modern Family (2009)" -> ("Modern Family", 2009)."""
    year = re.search(r"\((\d{4})\)$", name)
    return without_year(name), int(year[1]) if year else None


def identify(path: str, codec: str | None = None, resolution: str | None = None) -> dict:
    """What the API tells about a video; codec and resolution replace the release's, for a converted copy."""
    video = Path(path)
    guess = guess_release(release_path(path))
    # in a library, "Elsbeth/Season 3/4sf-elsbeth-sd-s03e02.mkv": the show folder names the show, the file doesn't
    if guess.get("type") == "episode" and season_number_in(video.parent.name) is not None:
        show_folder = guessit(video.parent.parent.name)
        guess.update({key: show_folder[key] for key in ("title", "year") if key in show_folder})
    if codec:
        guess["video_codec"] = codec
    if resolution:
        guess["screen_size"] = resolution
    quality = {"resolution": guess.get("screen_size"), "codec": codec_label(guess.get("video_codec"))}

    if guess.get("type") == "episode":
        show = show_match(video, guess)
        series, year = split_name(show.name)
        titles = episode_titles(show.id, guess["season"])
        episodes = episode_numbers(guess)
        return {
            "kind": "episode", "tmdb_id": show.id, "series": series, "year": year,
            "season": guess["season"], "episodes": episodes,
            "episode_title": " + ".join(dict.fromkeys(titles[number] for number in episodes if number in titles)) or None,
            **quality,
            "file_name": episode_file_name(show, guess) + video.suffix.lower(),
            "library_folder": shown(season_folder(library_folder(SERIES, "tv", show), guess["season"])),
        }

    movie = movie_match(video, guess)
    title, year = split_name(movie.name)
    return {
        "kind": "movie", "tmdb_id": movie.id, "title": title, "year": year, **quality,
        # the sorter keeps a movie's file name, so this is only a suggestion for tools that rename
        "file_name": " ".join(filter(None, [movie.name, quality["resolution"], quality["codec"]])) + video.suffix.lower(),
        "library_folder": shown(library_folder(MOVIES, "movie", movie)),
    }


API_USAGE = 'use POST /identify {"path": ..., "codec": "HEVC", "resolution": "1080p"} or GET /health'


class ApiHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self.reply(200, {"version": VERSION})
        else:
            self.reply(404, {"error": API_USAGE})

    def do_POST(self) -> None:
        if self.path != "/identify":
            return self.reply(404, {"error": API_USAGE})
        try:
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            path, codec, resolution = request["path"], request.get("codec"), request.get("resolution")
            if not all(isinstance(value, str) for value in (path, codec or "", resolution or "")):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return self.reply(400, {"error": API_USAGE})
        try:
            self.reply(200, identify(path, codec, resolution))
        except NotRecognised as reason:
            self.reply(404, {"error": str(reason)})
        # SystemExit: tmdb_get's answer to a rejected key must not end the API's thread
        except (Exception, SystemExit) as error:
            log.exception("API request failed for %s", path)
            self.reply(502, {"error": str(error)})

    def reply(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        log.debug("API: " + format, *args)


def serve_api(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("", port), ApiHandler)
    threading.Thread(target=server.serve_forever, name="api", daemon=True).start()
    return server


def is_mostly_video(item: Path) -> bool:
    total = sum(path.stat().st_size for path in files_in(item))
    return sum(path.stat().st_size for path in videos_in(item)) * 2 > total


def game_roots_in(item: Path) -> list[Path]:
    """The outermost folders holding a game file, one per game; a folder like "Games" can hold several."""
    holders = {path.parent for path in files_in(item) if path.suffix.lower() in GAME_EXTENSIONS}
    if item.is_file():
        return [item] if holders else []
    return sorted(holder for holder in holders if not any(holder != other and holder.is_relative_to(other) for other in holders))


def release_title_and_group(release: str) -> tuple[str, str | None]:
    """"Baba.Is.You-ElAmigos - more games on GLOAD.cc" -> ("Baba Is You", "ElAmigos")."""
    name = re.split(r"\s+-\s+", release)[0]
    group = None
    # two capitals tell a group like "ElAmigos" or "TENOKE" from the end of "Half-Life"
    found = re.search(r"-([A-Za-z0-9]+)$", name)
    if found and sum(letter.isupper() for letter in found[1]) >= 2:
        group, name = found[1], name[:found.start()]
    name = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", " ", name)
    # dots and underscores separate words, but not the digits of a version like 1.5
    name = re.sub(r"(?<!\d)[._]|[._](?!\d)", " ", name)
    name = RELEASE_NOISE.sub("", name)
    return " ".join(name.split()), group


def release_name(root: Path) -> str:
    """The name of the innermost folder with a release group, as the game may sit in one called "game" or "bin"."""
    names = [root.stem if root.is_file() else root.name] + [folder.name for folder in root.parents if folder.is_relative_to(DOWNLOADS) and folder != DOWNLOADS]
    return next((name for name in names if release_title_and_group(name)[1]), names[0])


def update_folder_name(release: str) -> str | None:
    """"Phantom Blade Zero update 301524 - 302256 - more games on GLOAD.cc" -> "update-301524-302256"."""
    name = re.sub(r"\s+-\s+more games on .*$", "", release, flags=re.IGNORECASE)
    found = re.search(r"\bupdate\b(.*)", name, re.IGNORECASE)
    if not found:
        return None
    versions = re.findall(r"v?\d[\d.]*\d|v?\d", found[1]) or re.findall(r"\bv\d[\d.]*\d", name)
    return "-".join(["update", *versions])


def plan_game(root: Path) -> list[Placement]:
    release = release_name(root)
    title, group = release_title_and_group(release)
    if not title:
        raise NotRecognised(f"no game name in {root.name}")
    match = igdb_lookup(title)
    if not match:
        raise NotRecognised(f"IGDB has no game '{title}'")
    folder = game_folder(match)
    update = update_folder_name(release)
    if update:
        folder /= update
    files = [path for path in files_in(root) if path.suffix.lower() not in LINK_EXTENSIONS]
    # an update keeps its file names; several discs too, so they don't all become the same file
    images = [] if update else [path for path in files if path.suffix.lower() == ".iso"]
    image_name = f"{without_year(match)}-{group}.iso" if group else f"{without_year(match)}.iso"

    def destination(path: Path) -> Path:
        if images == [path]:
            return folder / image_name
        return folder / (path.name if root.is_file() else path.relative_to(root))

    return [Placement(path, destination(path), None) for path in files]


def game_folder(match: str) -> Path:
    """The folder for match, reusing one spelled differently or without the year."""
    if GAMES.is_dir():
        for folder in sorted(GAMES.iterdir()):
            if folder.is_dir() and folder_key(folder.name) in (folder_key(match), folder_key(without_year(match))):
                return folder
    return GAMES / match


@functools.cache
def igdb_token() -> str:
    form = urllib.parse.urlencode({"client_id": IGDB_CLIENT_ID, "client_secret": IGDB_CLIENT_SECRET, "grant_type": "client_credentials"})
    try:
        with urllib.request.urlopen("https://id.twitch.tv/oauth2/token", data=form.encode(), timeout=15) as response:
            return json.load(response)["access_token"]
    except urllib.error.HTTPError as error:
        if error.code in (400, 401, 403):
            raise SystemExit(f"Twitch rejected IGDB_CLIENT_ID/IGDB_CLIENT_SECRET ({error.code}): check them at https://dev.twitch.tv/console/apps")
        raise


def igdb_post(body: str) -> list[dict]:
    request = urllib.request.Request(
        "https://api.igdb.com/v4/games",
        data=body.encode(),
        headers={"Client-ID": IGDB_CLIENT_ID, "Authorization": f"Bearer {igdb_token()}"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def igdb_search(query: str) -> list[dict]:
    if not IGDB_CLIENT_ID:
        raise NotRecognised("IGDB_CLIENT_ID is not set")
    query = query.replace('"', "")
    body = f'search "{query}"; fields name,first_release_date,game_type; where version_parent = null; limit 10;'
    try:
        return igdb_post(body)
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
        # the token runs out after about two months
        igdb_token.cache_clear()
        return igdb_post(body)


@functools.cache
def igdb_lookup(title: str) -> str | None:
    """The game's "Name (Year)" from IGDB, or None if IGDB doesn't know it."""
    words = title.split()
    for word_count in range(len(words), 0, -1):
        query = " ".join(words[:word_count])
        results = igdb_search(query)
        exact = [result for result in results if folder_key(result["name"]) == folder_key(query)]
        # with words left out, only the exact name is safe: "Graveyard" is not "Graveyard Keeper"
        candidates = results if word_count == len(words) else exact
        candidates = sorted(candidates, key=lambda result: (result not in exact, result.get("game_type") not in MAIN_GAME_TYPES))
        if candidates:
            return game_name(candidates[0])
    return None


def game_name(result: dict) -> str:
    released = result.get("first_release_date")
    year = f" ({time.gmtime(released).tm_year})" if released else ""
    return safe_filename(result["name"] + year)


def is_identical_copy(source: Path, destination: Path) -> bool:
    return destination.is_file() and source.is_file() and filecmp.cmp(source, destination, shallow=False)


def move_into_library(source: Path, destination: Path) -> None:
    """Copies under a hidden name and renames it, so the library never holds a half-copied file under the real name."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    # leftovers of a killed container; only one copy is ever in flight, so none of them is still being written
    for leftover in destination.parent.glob(f".*{PARTIAL_SUFFIX}"):
        leftover.unlink(missing_ok=True)
    partial = destination.with_name(f".{destination.name}{PARTIAL_SUFFIX}")
    try:
        # copyfile, not copy2: the file should take the library's permissions and ACLs, not the download's
        shutil.copyfile(source, partial)
        partial.replace(destination)
    finally:
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)
    source.unlink()


def file_into_library(item: Path, placements: list[Placement]) -> None:
    if DRY_RUN:
        for line in move_lines(placements):
            log.info("Would move %s", line)
        return

    # a stop between the copy and deleting the download leaves an identical copy behind: finish that move
    for placement in placements:
        if is_identical_copy(placement.source, placement.destination):
            placement.source.unlink()
            log.info("Already filed %s -> %s, deleted the download", placement.source.relative_to(DOWNLOADS), shown(placement.destination))
    placements = [placement for placement in placements if placement.source.exists()]

    clashes = [placement.destination for placement in placements if placement.destination.exists()]
    if clashes:
        more = f" and {len(clashes) - 3} more" if len(clashes) > 3 else ""
        raise NotRecognised(f"already in the library: {', '.join(map(shown, clashes[:3]))}{more}")

    for placement in placements:
        try:
            move_into_library(placement.source, placement.destination)
        except OSError as error:
            raise MoveFailed(f"{placement.source.name} -> {shown(placement.destination)}: {error.strerror or error}")
        if placement.plex_type:
            log.info("Moved %s", move_line(placement))
        # only movies: a season folder holds other episodes, not other versions
        if placement.plex_type == "movie":
            versions = len(videos_in(placement.destination.parent))
            if versions > 1:
                # the Grafana alert "media-sorter duplicate" matches this wording; change both together
                log.info("Duplicate: copied %s, the folder now has %d versions", placement.destination.parent.name, versions)

    for line in game_move_lines(placements):
        log.info("Moved %s", line)

    # what's left is .url/.txt/.html/.nfo, samples and extracted archives
    if item.is_dir():
        try:
            shutil.rmtree(item)
        except OSError as error:
            log.error("Filed %s, but could not delete the download folder: %s", item.name, error.strerror or error)


def move_line(placement: Placement) -> str:
    return f"{placement.source.relative_to(DOWNLOADS)} -> {shown(placement.destination)}"


def game_move_lines(placements: list[Placement]) -> list[str]:
    """One line per game rather than per file, as a game can have thousands."""
    placements_per_game: dict[Path, list[Placement]] = {}
    for placement in placements:
        if placement.plex_type is None:
            game = GAMES / placement.destination.relative_to(GAMES).parts[0]
            placements_per_game.setdefault(game, []).append(placement)
    lines = []
    for game, game_placements in placements_per_game.items():
        if len(game_placements) == 1:
            lines.append(move_line(game_placements[0]))
        else:
            source = Path(os.path.commonpath([placement.source for placement in game_placements]))
            lines.append(f"{source.relative_to(DOWNLOADS)} -> {shown(game)} ({len(game_placements)} files)")
    return lines


def move_lines(placements: list[Placement]) -> list[str]:
    return [move_line(placement) for placement in placements if placement.plex_type] + game_move_lines(placements)


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


def startup_message(entries: int) -> str:
    return (
        f"media-sorter {VERSION}{' (dry run)' if DRY_RUN else ''}: watching {shown(DOWNLOADS)} "
        f"({entries} {'entry' if entries == 1 else 'entries'}), "
        f"checking every {POLL_SECONDS}s, settle time {SETTLE_SECONDS}s, API on port {API_PORT}"
    )


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
            if (item / SKIP_MARKER).exists():
                log.info("Skipping %s: it has a %s file", item.name, SKIP_MARKER)
                # deleting the marker changes the snapshot, which makes it a candidate again
                skipped[item] = current
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
                touched_libraries |= {placement.plex_type for placement in placements} - {None}
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
                touched_libraries |= {placement.plex_type for placement in placements} - {None}
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
    signal.signal(signal.SIGTERM, stop)
    serve_api(API_PORT)
    log.info(startup_message(len(list(DOWNLOADS.iterdir()))))
    watch()
