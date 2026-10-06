import io
import json
import re
import urllib.error

import pytest

import media_sorter as sorter

# taken before the autouse fixture swaps it for the fake catalogue
REAL_TMDB_SEARCH = sorter.tmdb_search

# As strict as TMDb's search API: every query word must be in the title, and the year must match.
CATALOGUE = {
    "movie": [
        {"title": "Tornado", "release_date": "2025-06-06"},
        {"title": "Zum Glück gibt's Schreiner", "release_date": "2020-02-01"},
        {"title": "Dune: Part Two", "release_date": "2024-02-27"},
        {"title": "Dune", "release_date": "2021-09-15"},
        {"title": "Schreiner", "release_date": "2011-01-01"},
    ],
    "tv": [
        {"name": "Elsbeth", "first_air_date": "2024-02-29"},
        {"name": "The Bear", "first_air_date": "2022-06-23"},
    ],
}


def words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.casefold()))


def fake_tmdb_search(kind: str, params: dict) -> list[dict]:
    date_field = "release_date" if kind == "movie" else "first_air_date"
    title_field = "title" if kind == "movie" else "name"
    year = params.get("year") or params.get("first_air_date_year")
    return [
        entry for entry in CATALOGUE[kind]
        if words(params["query"]) <= words(entry[title_field]) and (not year or entry[date_field][:4] == str(year))
    ]


class StopWatching(Exception):
    pass


@pytest.fixture(autouse=True)
def library(tmp_path, monkeypatch):
    for folder in ("downloads", "movies", "series"):
        (tmp_path / folder).mkdir()
    monkeypatch.setattr(sorter, "DOWNLOADS", tmp_path / "downloads")
    monkeypatch.setattr(sorter, "MOVIES", tmp_path / "movies")
    monkeypatch.setattr(sorter, "SERIES", tmp_path / "series")
    monkeypatch.setattr(sorter, "DRY_RUN", False)
    monkeypatch.setattr(sorter, "PLEX_URL", "")
    for host_path in ("HOST_DOWNLOADS", "HOST_MOVIES", "HOST_SERIES"):
        monkeypatch.setattr(sorter, host_path, "")
    monkeypatch.setattr(sorter, "run_now", False)
    monkeypatch.setattr(sorter, "tmdb_search", fake_tmdb_search)
    sorter.tmdb_lookup.cache_clear()
    return tmp_path


def download(relative_path: str, size: int = 1000):
    path = sorter.DOWNLOADS / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def library_files(library) -> list[str]:
    return sorted(
        str(path.relative_to(library)) for path in library.rglob("*")
        if path.is_file() and not path.is_relative_to(library / "downloads")
    )


def run_rounds(monkeypatch, rounds: int, before_round=None) -> list[set[str]]:
    """Runs watch() for a number of polls and returns the Plex rescans it asked for."""
    rescans = []
    monkeypatch.setattr(sorter, "refresh_plex", lambda plex_types: plex_types and rescans.append(plex_types))
    finished = 0

    def next_poll():
        nonlocal finished
        finished += 1
        if finished == rounds:
            raise StopWatching
        if before_round:
            before_round(finished + 1)

    monkeypatch.setattr(sorter, "sleep_until_next_poll", next_poll)
    with pytest.raises(StopWatching):
        sorter.watch()
    return rescans


# Naming


@pytest.mark.parametrize("name, expected", [
    ("Mission: Impossible (1996)", "Mission - Impossible (1996)"),
    ('What? A "Movie" (2020)', "What A Movie (2020)"),
    ("AC/DC Live (1991)", "ACDC Live (1991)"),
    ("Trailing dot.", "Trailing dot"),
])
def test_safe_filename_drops_characters_windows_and_plex_trip_over(name, expected):
    assert sorter.safe_filename(name) == expected


def test_episode_code():
    assert sorter.episode_code(3, 2) == "S03E02"
    assert sorter.episode_code(1, [1, 2]) == "S01E01-E02"


def test_with_umlauts():
    assert sorter.with_umlauts("Zum Glueck gibts Schreiner") == "Zum Glück gibts Schreiner"
    assert sorter.with_umlauts("Aerger mit Oele") == "Ärger mit Öle"


@pytest.mark.parametrize("path, expected", [
    ("Movie.2020/sample.mkv", True),
    ("Movie.2020/Sample/movie-sample.mkv", True),
    ("Movie.2020/movie.mkv", False),
    ("Samples.Of.Life.2020/movie.mkv", False),
])
def test_is_sample(path, expected):
    assert sorter.is_sample(sorter.Path(path)) == expected


# TMDb search


def test_search_attempts_go_from_exact_to_loose_and_loose_ones_need_the_year():
    assert sorter.search_attempts("Zum Glueck gibts Schreiner", 2020) == [
        ("Zum Glueck gibts Schreiner", 2020),
        ("Zum Glück gibts Schreiner", 2020),
        ("Glück gibts Schreiner", 2020),
        ("Zum gibts Schreiner", 2020),
        ("Zum Glück Schreiner", 2020),
        ("Zum Glück gibts", 2020),
        ("Zum Glueck gibts Schreiner", None),
        ("Zum Glück gibts Schreiner", None),
    ]


def test_search_attempts_without_a_year_never_leave_words_out():
    assert sorter.search_attempts("The Bear", None) == [("The Bear", None)]


def test_tmdb_lookup_finds_a_german_title_written_without_umlauts_and_apostrophe():
    assert sorter.tmdb_lookup("movie", "Zum Glueck gibts Schreiner", 2020) == "Zum Glück gibt's Schreiner (2020)"


def test_tmdb_lookup_does_not_guess_a_different_movie_by_leaving_words_out():
    assert sorter.tmdb_lookup("movie", "Zum Glueck gibts Schreiner", 1999) is None


def test_tmdb_lookup_falls_back_to_no_year_when_the_year_is_off():
    assert sorter.tmdb_lookup("movie", "Tornado", 2024) == "Tornado (2025)"


def test_tmdb_lookup_makes_the_name_safe_for_a_folder():
    assert sorter.tmdb_lookup("movie", "Dune Part Two", 2024) == "Dune - Part Two (2024)"


def test_tmdb_lookup_for_series():
    assert sorter.tmdb_lookup("tv", "Elsbeth", None) == "Elsbeth (2024)"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def sent_requests(monkeypatch):
    """Lets the real tmdb_search run against a fake TMDb and collects what it sends."""
    monkeypatch.setattr(sorter, "tmdb_search", REAL_TMDB_SEARCH)
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        return FakeResponse(json.dumps({"results": []}).encode())

    monkeypatch.setattr(sorter.urllib.request, "urlopen", fake_urlopen)
    return requests


def test_tmdb_search_sends_a_read_access_token_as_bearer(monkeypatch, sent_requests):
    monkeypatch.setattr(sorter, "TMDB_KEY", ' "eyJhbGciOiJIUzI1NiJ9.token" ')
    sorter.tmdb_search("movie", {"query": "Tornado"})

    assert sent_requests[0].get_header("Authorization") == "Bearer eyJhbGciOiJIUzI1NiJ9.token"
    assert "api_key" not in sent_requests[0].full_url


def test_tmdb_search_sends_a_v3_key_as_parameter(monkeypatch, sent_requests):
    monkeypatch.setattr(sorter, "TMDB_KEY", "0123456789abcdef")
    sorter.tmdb_search("movie", {"query": "Tornado"})

    assert "api_key=0123456789abcdef" in sent_requests[0].full_url
    assert sent_requests[0].get_header("Authorization") is None


def test_tmdb_search_stops_on_a_rejected_key(monkeypatch, sent_requests):
    def unauthorised(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(sorter.urllib.request, "urlopen", unauthorised)
    with pytest.raises(SystemExit, match="TMDb rejected TMDB_KEY"):
        sorter.tmdb_search("movie", {"query": "Tornado"})


# Planning


def test_plan_movie_names_the_folder_and_keeps_the_file_name(library):
    item = sorter.DOWNLOADS / "Tornado 2025 GERMAN DL 1080p WEB H264-MGE - by Someone"
    video = download(f"{item.name}/tornado.2025.german.dl.1080p.web.h264-mge.mkv", size=5000)
    download(f"{item.name}/tornado.2025.sample.mkv", size=9000)
    download(f"{item.name}/info.txt")

    assert sorter.plan(item) == [
        sorter.Placement(video, sorter.MOVIES / "Tornado (2025)" / video.name, "movie"),
    ]


def test_plan_movie_with_a_part_in_its_title(library):
    item = sorter.DOWNLOADS / "Dune.Part.Two.2024.1080p.WEB-DL.x265-GRP"
    video = download(f"{item.name}/movie.mkv")

    assert sorter.plan(item)[0].destination == sorter.MOVIES / "Dune - Part Two (2024)" / "movie.mkv"


def test_plan_season_pack_puts_every_episode_into_its_season_folder(library):
    item = sorter.DOWNLOADS / "Elsbeth S03 German DL WEB x264-4SF - serienfans org"
    for episode in ("02", "20"):
        download(f"{item.name}/4sf-elsbeth-sd-s03e{episode}.mkv")

    destinations = sorted(placement.destination for placement in sorter.plan(item))
    season = sorter.SERIES / "Elsbeth (2024)" / "Season 03"
    assert destinations == [season / "Elsbeth (2024) - S03E02.mkv", season / "Elsbeth (2024) - S03E20.mkv"]


def test_plan_episode_takes_the_season_from_the_folder_when_the_file_lacks_it(library):
    item = sorter.DOWNLOADS / "The.Bear.S02E05.1080p.WEB.h264-GRP"
    download(f"{item.name}/abc123.mkv")

    assert sorter.plan(item)[0].destination == sorter.SERIES / "The Bear (2022)" / "Season 02" / "The Bear (2022) - S02E05.mkv"


def test_plan_leaves_a_folder_without_video(library):
    item = sorter.DOWNLOADS / "Well Dweller-ElAmigos"
    download(f"{item.name}/setup.exe")
    download(f"{item.name}/game.part01.rar")

    with pytest.raises(sorter.NotRecognised, match="no video file"):
        sorter.plan(item)


def test_plan_leaves_a_movie_tmdb_does_not_know(library):
    item = sorter.DOWNLOADS / "Totally.Unknown.Film.2023.1080p"
    download(f"{item.name}/movie.mkv")

    with pytest.raises(sorter.NotRecognised, match="TMDb has no movie"):
        sorter.plan(item)


# Moving


def test_file_into_library_moves_the_video_and_deletes_the_download(library):
    item = sorter.DOWNLOADS / "Tornado.2025.1080p"
    video = download(f"{item.name}/tornado.mkv", size=4321)
    download(f"{item.name}/link.url")
    download(f"{item.name}/site.html")

    sorter.file_into_library(item, sorter.plan(item))

    moved = sorter.MOVIES / "Tornado (2025)" / "tornado.mkv"
    assert moved.stat().st_size == 4321
    assert not item.exists()
    assert not video.exists()


def test_file_into_library_in_a_dry_run_moves_nothing(library, monkeypatch):
    monkeypatch.setattr(sorter, "DRY_RUN", True)
    item = sorter.DOWNLOADS / "Tornado.2025.1080p"
    download(f"{item.name}/tornado.mkv")

    sorter.file_into_library(item, sorter.plan(item))

    assert item.exists()
    assert library_files(library) == []


def test_file_into_library_never_overwrites(library):
    item = sorter.DOWNLOADS / "Tornado.2025.1080p"
    download(f"{item.name}/tornado.mkv")
    existing = sorter.MOVIES / "Tornado (2025)" / "tornado.mkv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"already here")

    with pytest.raises(sorter.NotRecognised, match="already in the library"):
        sorter.file_into_library(item, sorter.plan(item))

    assert existing.read_bytes() == b"already here"
    assert item.exists()


def test_failed_move_removes_the_partial_copy_and_keeps_the_download(library, monkeypatch):
    item = sorter.DOWNLOADS / "Tornado.2025.1080p"
    video = download(f"{item.name}/tornado.mkv")

    def disk_full(source, destination, copy_function):
        sorter.Path(destination).write_bytes(b"half")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sorter.shutil, "move", disk_full)
    with pytest.raises(sorter.MoveFailed, match="No space left on device"):
        sorter.file_into_library(item, sorter.plan(item))

    assert library_files(library) == []
    assert video.exists()


def test_failed_cleanup_still_counts_as_filed(library, monkeypatch, caplog):
    item = sorter.DOWNLOADS / "Tornado.2025.1080p"
    download(f"{item.name}/tornado.mkv")

    def permission_denied(path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(sorter.shutil, "rmtree", permission_denied)
    sorter.file_into_library(item, sorter.plan(item))

    assert library_files(library) == ["movies/Tornado (2025)/tornado.mkv"]
    assert "could not delete the download folder: Permission denied" in caplog.text


# Watching


def test_watch_files_a_settled_download_and_rescans_plex(library, monkeypatch):
    monkeypatch.setattr(sorter, "SETTLE_SECONDS", 0)
    download("Tornado.2025.1080p/tornado.mkv")
    download("The.Bear.S02E05.1080p/the.bear.s02e05.mkv")

    rescans = run_rounds(monkeypatch, rounds=2)

    assert library_files(library) == [
        "movies/Tornado (2025)/tornado.mkv",
        "series/The Bear (2022)/Season 02/The Bear (2022) - S02E05.mkv",
    ]
    assert rescans == [{"movie", "show"}]


def test_watch_waits_while_a_download_is_still_changing(library, monkeypatch):
    monkeypatch.setattr(sorter, "SETTLE_SECONDS", 0)
    download("Tornado.2025.1080p/tornado.mkv", size=100)

    def keep_growing(round_number):
        download("Tornado.2025.1080p/tornado.mkv", size=100 * round_number)

    run_rounds(monkeypatch, rounds=3, before_round=keep_growing)

    assert library_files(library) == []


def test_watch_waits_for_the_settle_time(library, monkeypatch):
    monkeypatch.setattr(sorter, "SETTLE_SECONDS", 300)
    download("Tornado.2025.1080p/tornado.mkv")

    run_rounds(monkeypatch, rounds=3)

    assert library_files(library) == []


def test_watch_waits_for_part_files(library, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(sorter, "SETTLE_SECONDS", 0)
    download("Tornado.2025.1080p/tornado.mkv")
    download("Tornado.2025.1080p/tornado.part2.rar.part")

    run_rounds(monkeypatch, rounds=3)

    assert library_files(library) == []
    assert caplog.text.count("Waiting for Tornado.2025.1080p: still downloading") == 1


def test_trigger_skips_the_settle_time(library, monkeypatch):
    monkeypatch.setattr(sorter, "SETTLE_SECONDS", 300)
    download("Tornado.2025.1080p/tornado.mkv")

    def trigger(round_number):
        sorter.request_run_now(None, None)

    run_rounds(monkeypatch, rounds=2, before_round=trigger)

    assert library_files(library) == ["movies/Tornado (2025)/tornado.mkv"]


def test_skip_marker_keeps_a_download_out_until_it_is_deleted(library, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(sorter, "SETTLE_SECONDS", 0)
    download("Tornado.2025.1080p/tornado.mkv")
    marker = download("Tornado.2025.1080p/.skip", size=0)

    def delete_marker_after_round_three(round_number):
        if round_number == 4:
            marker.unlink()

    run_rounds(monkeypatch, rounds=6, before_round=delete_marker_after_round_three)

    assert caplog.text.count("Skipping Tornado.2025.1080p: it has a .skip file") == 1
    assert library_files(library) == ["movies/Tornado (2025)/tornado.mkv"]


def test_skip_marker_keeps_a_settled_download_in_place(library, monkeypatch):
    monkeypatch.setattr(sorter, "SETTLE_SECONDS", 0)
    download("Tornado.2025.1080p/tornado.mkv")
    download("Tornado.2025.1080p/.skip", size=0)

    run_rounds(monkeypatch, rounds=4)

    assert library_files(library) == []


def test_unrecognised_download_is_reported_once_and_retried_after_a_trigger(library, monkeypatch, caplog):
    monkeypatch.setattr(sorter, "SETTLE_SECONDS", 0)
    download("Games/setup.exe")

    def trigger_in_round_four(round_number):
        if round_number == 4:
            sorter.request_run_now(None, None)

    run_rounds(monkeypatch, rounds=4, before_round=trigger_in_round_four)

    assert caplog.text.count("Leaving Games: no video file") == 2


def test_failed_move_is_logged_once(library, monkeypatch, caplog):
    monkeypatch.setattr(sorter, "SETTLE_SECONDS", 0)
    download("Tornado.2025.1080p/tornado.mkv")

    def read_only(source, destination, copy_function):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(sorter.shutil, "move", read_only)
    run_rounds(monkeypatch, rounds=4)

    assert caplog.text.count("Move failed for Tornado.2025.1080p") == 1
    assert "Read-only file system" in caplog.text


# Log lines


@pytest.fixture
def host_paths(monkeypatch):
    monkeypatch.setattr(sorter, "HOST_DOWNLOADS", "/mnt/shared/jdownloader")
    monkeypatch.setattr(sorter, "HOST_MOVIES", "/mnt/movies")
    monkeypatch.setattr(sorter, "HOST_SERIES", "/mnt/series")


def test_shown_names_paths_as_the_host_sees_them(host_paths):
    assert sorter.shown(sorter.DOWNLOADS) == "/mnt/shared/jdownloader"
    assert sorter.shown(sorter.MOVIES / "Tornado (2025)" / "t.mkv") == "/mnt/movies/Tornado (2025)/t.mkv"
    assert sorter.shown(sorter.SERIES / "Elsbeth (2024)") == "/mnt/series/Elsbeth (2024)"


def test_shown_keeps_the_container_path_without_host_paths():
    assert sorter.shown(sorter.MOVIES / "Tornado (2025)") == str(sorter.MOVIES / "Tornado (2025)")


def test_startup_message(host_paths, monkeypatch):
    monkeypatch.setattr(sorter, "VERSION", "0.0.2")
    monkeypatch.setattr(sorter, "POLL_SECONDS", 30)
    monkeypatch.setattr(sorter, "SETTLE_SECONDS", 300)

    assert sorter.startup_message(3) == (
        "media-sorter 0.0.2: watching /mnt/shared/jdownloader (3 entries), checking every 30s, settle time 300s"
    )
    assert "(1 entry)" in sorter.startup_message(1)


def test_moved_is_logged_after_the_move_with_the_host_path(library, host_paths, caplog):
    caplog.set_level("INFO")
    item = sorter.DOWNLOADS / "Tornado.2025.1080p"
    download(f"{item.name}/tornado.mkv")

    sorter.file_into_library(item, sorter.plan(item))

    assert "Moved Tornado.2025.1080p/tornado.mkv -> /mnt/movies/Tornado (2025)/tornado.mkv" in caplog.text


def test_dry_run_says_would_move(library, host_paths, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(sorter, "DRY_RUN", True)
    item = sorter.DOWNLOADS / "Tornado.2025.1080p"
    download(f"{item.name}/tornado.mkv")

    sorter.file_into_library(item, sorter.plan(item))

    assert "Would move Tornado.2025.1080p/tornado.mkv -> /mnt/movies/Tornado (2025)/tornado.mkv" in caplog.text
    assert "Moved" not in caplog.text


def test_nothing_is_logged_as_moved_when_it_is_already_in_the_library(library, host_paths, caplog):
    caplog.set_level("INFO")
    item = sorter.DOWNLOADS / "Tornado.2025.1080p"
    download(f"{item.name}/tornado.mkv")
    existing = sorter.MOVIES / "Tornado (2025)" / "tornado.mkv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"already here")

    with pytest.raises(sorter.NotRecognised, match=r"already in the library: /mnt/movies/Tornado \(2025\)/tornado.mkv"):
        sorter.file_into_library(item, sorter.plan(item))

    assert "Moved" not in caplog.text


def test_unknown_movie_without_a_year_does_not_say_none(library):
    item = sorter.DOWNLOADS / "Totally.Unknown.Film.1080p"
    download(f"{item.name}/movie.mkv")

    with pytest.raises(sorter.NotRecognised) as error:
        sorter.plan(item)

    assert str(error.value) == "TMDb has no movie 'Totally Unknown Film'"


# Plex


def test_refresh_plex_rescans_only_the_libraries_that_got_files(monkeypatch):
    monkeypatch.setattr(sorter, "PLEX_URL", "http://plex:32400")
    requested = []
    sections = {"MediaContainer": {"Directory": [
        {"key": "1", "type": "movie", "title": "Filme"},
        {"key": "2", "type": "show", "title": "Serien"},
        {"key": "3", "type": "artist", "title": "Musik"},
    ]}}

    def plex_request(path):
        requested.append(path)
        return sections if path == "/library/sections" else {}

    monkeypatch.setattr(sorter, "plex_request", plex_request)
    sorter.refresh_plex({"movie"})

    assert requested == ["/library/sections", "/library/sections/1/refresh"]


def test_refresh_plex_does_nothing_in_a_dry_run(monkeypatch):
    monkeypatch.setattr(sorter, "PLEX_URL", "http://plex:32400")
    monkeypatch.setattr(sorter, "DRY_RUN", True)
    monkeypatch.setattr(sorter, "plex_request", lambda path: pytest.fail("Plex was called"))

    sorter.refresh_plex({"movie"})
