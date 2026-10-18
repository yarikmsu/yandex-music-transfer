import csv
import io
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import apple_import as ai

OK_HEADER = ["#", "Title", "Artist", "Album", "Year", "Duration", "ID", "Availability"]
ERROR_HEADER = ["#", "Title", "Artist", "Album", "Year", "Duration", "ID", "Error"]


def song(song_id, name, artist, millis, album=""):
    attributes = {"name": name, "artistName": artist, "albumName": album, "durationInMillis": millis}
    return {"id": song_id, "attributes": attributes}


def track(title, artist, duration="3:31", album=""):
    return {"title": title, "artist": artist, "album": album, "duration": duration}


def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


class FakeClient:
    def __init__(self):
        self.calls = []

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, params, body))
        if path == "/v1/me/library/playlists":
            return {"data": [{"id": "p.1"}]}
        return {}


class InTempDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(tmp.name)


class NormalizeTest(unittest.TestCase):
    def test_drops_version_and_featuring_notes(self):
        self.assertEqual(ai.normalize("In the End (Live)"), "in the end")
        self.assertEqual(ai.normalize("Song - Remastered 2011"), "song")
        self.assertEqual(ai.normalize("Numb feat. Jay-Z"), "numb")

    def test_unifies_ampersand_and_yo(self):
        self.assertEqual(ai.normalize("Heaven & Hell"), "heaven and hell")
        self.assertEqual(ai.normalize("Ёлка"), "елка")

    def test_keeps_a_title_that_is_only_brackets(self):
        self.assertEqual(ai.normalize("(Intro)"), "intro")


class SimilarityTest(unittest.TestCase):
    def test_cyrillic_and_latin_spellings_match(self):
        self.assertGreaterEqual(ai.similarity("Ленинград", "Leningrad"), 0.95)
        self.assertGreaterEqual(ai.similarity("Король и Шут", "Korol i Shut"), 0.95)

    def test_artist_found_among_several_credits(self):
        self.assertEqual(ai.artist_similarity("Linkin Park", "Linkin Park & JAY-Z"), 1.0)
        self.assertEqual(ai.artist_similarity("JAY-Z, Linkin Park", "Linkin Park"), 1.0)

    def test_unrelated_titles_score_low(self):
        self.assertLess(ai.similarity("Heaven and Hell", "Paranoid"), 0.5)


class ClassifyTest(unittest.TestCase):
    def classify(self, exported, candidate):
        return ai.classify(ai.score_candidate(exported, candidate))

    def test_same_song_within_tolerance_is_matched(self):
        status = self.classify(track("In the End", "Linkin Park", "3:31"),
                               song("1", "In the End", "Linkin Park", 216000))
        self.assertEqual(status, "matched")

    def test_much_longer_version_needs_a_check(self):
        status = self.classify(track("In the End", "Linkin Park", "3:31"),
                               song("1", "In the End", "Linkin Park", 300000))
        self.assertEqual(status, "check")

    def test_same_title_by_another_artist_is_missing(self):
        status = self.classify(track("Paranoid", "Black Sabbath"),
                               song("1", "Paranoid", "Garbage", 211000))
        self.assertEqual(status, "missing")

    def test_unusual_transliteration_matches_when_title_and_length_agree(self):
        status = self.classify(track("Линия жизни", "Сплин", "3:02"),
                               song("1", "Линия жизни", "Splean", 182000))
        self.assertEqual(status, "matched")

    def test_unusual_transliteration_with_other_length_needs_a_check(self):
        status = self.classify(track("Линия жизни", "Сплин", "3:02"),
                               song("1", "Линия жизни", "Splean", 190000))
        self.assertEqual(status, "check")

    def test_unknown_duration_does_not_block_a_match(self):
        status = self.classify(track("Рок", "Ленинград", ""), song("1", "Рок", "Leningrad", 200000))
        self.assertEqual(status, "matched")


class PickBestTest(unittest.TestCase):
    def test_prefers_the_version_with_matching_length(self):
        candidates = [song("live", "In the End", "Linkin Park", 300000),
                      song("studio", "In the End", "Linkin Park", 212000)]
        best, _ = ai.pick_best(track("In the End", "Linkin Park", "3:31"), candidates)
        self.assertEqual(best["id"], "studio")

    def test_no_candidates_gives_none(self):
        self.assertIsNone(ai.pick_best(track("X", "Y"), []))


class BuildUrlTest(unittest.TestCase):
    def test_keeps_id_brackets_and_commas_literal(self):
        url = ai.build_url(ai.API, "/v1/me/favorites", {"ids[songs]": "1,2"})
        self.assertEqual(url, ai.API + "/v1/me/favorites?ids[songs]=1,2")

    def test_encodes_search_terms(self):
        url = ai.build_url(ai.ITUNES_API, "/search", {"term": "Рок Ленинград"})
        self.assertEqual(url, ai.ITUNES_API + "/search?term=%D0%A0%D0%BE%D0%BA+"
                         "%D0%9B%D0%B5%D0%BD%D0%B8%D0%BD%D0%B3%D1%80%D0%B0%D0%B4")


class ItunesSearchTest(unittest.TestCase):
    def test_tokens_are_never_sent_to_itunes(self):
        self.assertEqual(ai.ItunesSearchClient().headers, {})

    def test_results_become_streamable_catalog_songs(self):
        results = [
            {"wrapperType": "track", "trackId": 178480, "trackName": "In the End (Live)",
             "artistName": "LINKIN PARK", "collectionName": "Live in Texas",
             "trackTimeMillis": 211000, "isStreamable": True},
            {"wrapperType": "track", "trackId": 5, "trackName": "Sold only", "isStreamable": False},
            {"wrapperType": "collection", "collectionId": 7},
        ]
        itunes = mock.Mock()
        itunes.request.return_value = {"results": results}
        songs = ai.search_songs(itunes, "us", "In the End Linkin Park")
        self.assertEqual(songs, [song("178480", "In the End (Live)", "LINKIN PARK", 211000,
                                      "Live in Texas")])
        params = itunes.request.call_args.args[2]
        self.assertEqual((params["country"], params["entity"]), ("us", "song"))

    @mock.patch.object(ai.time, "sleep")
    def test_403_counts_as_a_rate_limit(self, sleep):
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b'{"results": []}'
        throttled = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
        itunes = ai.ItunesSearchClient()
        with mock.patch.object(ai.urllib.request, "urlopen", side_effect=[throttled, ok]):
            self.assertEqual(itunes.request("GET", "/search"), {"results": []})
        self.assertEqual(itunes.pause, ai.ITUNES_PAUSE * 2)


def itunes_track(track_id, name, artist, millis):
    return {"wrapperType": "track", "trackId": track_id, "trackName": name,
            "artistName": artist, "collectionName": "", "trackTimeMillis": millis}


def fake_itunes(artists, catalog):
    """An iTunes client answering artist searches with `artists` and lookups with `catalog`."""
    itunes = mock.Mock()
    itunes.request.side_effect = lambda method, path, params: (
        {"results": artists} if path == "/search" else {"results": catalog})
    return itunes


def row(track_id, status):
    return {**dict.fromkeys(ai.MATCH_FIELDS, ""), "yandex_id": track_id, "status": status}


class ArtistPassTest(unittest.TestCase):
    def setUp(self):
        self.tracks = {
            "1": track("The Dope Show", "Marilyn Manson", "3:40"),
            "2": track("Tainted Love", "Marilyn Manson", "3:20"),
            "3": track("Sweet Dreams", "Marilyn Manson", "4:51"),
        }
        self.catalog = [itunes_track(11, "The Dope Show", "Marilyn Manson", 220000),
                        itunes_track(12, "Sweet Dreams (Are Made of This)", "Marilyn Manson", 291000)]
        self.artists = [{"artistId": 251375, "artistName": "Marilyn Manson"}]

    def test_buried_original_is_found_in_the_artist_catalog(self):
        rows = {"1": row("1", "missing"), "2": row("2", "matched")}
        improved = ai.rematch_by_artist(fake_itunes(self.artists, self.catalog), "us", self.tracks, rows)
        self.assertEqual(improved, 1)
        self.assertEqual((rows["1"]["status"], rows["1"]["apple_id"]), ("matched", "11"))
        self.assertEqual(rows["2"]["status"], "matched")

    def test_skipped_rows_and_unimproved_rows_are_left_alone(self):
        rows = {"1": row("1", "skip"), "2": row("2", "missing")}
        ai.rematch_by_artist(fake_itunes(self.artists, self.catalog), "us", self.tracks, rows)
        self.assertEqual(rows["1"], row("1", "skip"))
        self.assertEqual(rows["2"], row("2", "missing"))

    def test_catalog_of_a_differently_named_artist_is_not_used(self):
        other = [{"artistId": 1, "artistName": "Marilyn Monroe Tribute"}]
        self.assertEqual(ai.artist_songs(fake_itunes(other, self.catalog), "us", "Marilyn Manson"), [])


class CoverPassTest(unittest.TestCase):
    def test_original_artist_is_read_from_the_title(self):
        cases = {
            "Big Balls  (Made Famous By AC/DC)": ("Big Balls", "AC/DC"),
            "Doll Parts - (Tribute to Hole)": ("Doll Parts", "Hole"),
            "Don't Tread On Me (In The Style Of Metallica) [Karaoke Version]":
                ("Don't Tread On Me", "Metallica"),
            "Flick Of The Switch (as made famous by AC/DC)": ("Flick Of The Switch", "AC/DC"),
        }
        for title, expected in cases.items():
            original = ai.original_of(track(title, "Karaoke Band"))
            self.assertEqual((original["title"], original["artist"]), expected, title)

    def test_ordinary_titles_name_no_original(self):
        self.assertIsNone(ai.original_of(track("Welcome Home (Sanitarium)", "Metallica")))

    def test_copy_is_replaced_by_the_named_original(self):
        tracks = {"1": track("Don't Tread On Me (In the Style of Metallica)", "Karaoke Carpool", "4:08")}
        rows = {"1": row("1", "missing")}
        itunes = mock.Mock()
        itunes.request.return_value = {"results": [
            itunes_track(7, "Don't Tread On Me", "Metallica", 240000)]}
        self.assertEqual(ai.rematch_covers(itunes, "us", tracks, rows), 1)
        self.assertEqual((rows["1"]["status"], rows["1"]["apple_id"]), ("matched", "7"))
        self.assertEqual(rows["1"]["artist"], "Karaoke Carpool")  # the export side stays as it was

    def test_copy_matched_exactly_is_replaced_too_but_originals_are_not_searched_again(self):
        tracks = {"1": track("Mistress For Christmas (as made famous by AC/DC)", "The Rock Heroes"),
                  "2": track("Shoot To Thrill (Made Famous By AC/DC)", "Iron Man")}
        rows = {"1": {**row("1", "matched"), "apple_artist": "The Rock Heroes"},
                "2": {**row("2", "matched"), "apple_artist": "AC/DC"}}
        itunes = mock.Mock()
        itunes.request.return_value = {"results": [
            itunes_track(9, "Mistress for Christmas", "AC/DC", 239000)]}
        self.assertEqual(ai.rematch_covers(itunes, "us", tracks, rows), 1)
        self.assertEqual(itunes.request.call_count, 1)
        self.assertEqual((rows["1"]["apple_artist"], rows["1"]["apple_id"]), ("AC/DC", "9"))


class ExportFilesTest(InTempDir):
    def setUp(self):
        super().setUp()
        write_csv("liked_tracks_me.csv", OK_HEADER, [
            ["1", "Song", "Artist", "Album", "2000", "3:00", "10", "OK"],
            ["2", "", "", "", "", "", "11", "Unavailable (no-rights)"],
        ])
        write_csv("playlist_me_Road Trip.csv", OK_HEADER, [
            ["1", "Other", "Band", "", "", "4:00", "12", "OK"],
        ])
        write_csv("unavailable_tracks_me.csv", ERROR_HEADER, [
            ["1", "Hidden", "Singer", "", "", "", "11", "no-rights"],
        ])

    def test_tracks_include_details_of_unavailable_ones(self):
        tracks = ai.load_tracks()
        self.assertEqual(sorted(tracks), ["10", "11", "12"])
        self.assertEqual(tracks["11"]["title"], "Hidden")

    def test_collections_keep_order_and_playlist_names(self):
        liked, playlists = ai.load_collections()
        self.assertEqual(liked, ["10", "11"])
        self.assertEqual(playlists, [("Road Trip", ["12"])])

    def test_rewritten_matches_keep_row_order(self):
        os.makedirs(ai.APPLE_DIR)
        rows = {"b": row("b", "matched"), "a": row("a", "check")}
        ai.write_matches(rows)
        self.assertEqual(list(ai.load_matches()), ["b", "a"])
        self.assertEqual(ai.load_matches()["a"]["status"], "check")


class ImportTest(InTempDir):
    def setUp(self):
        super().setUp()
        os.makedirs(ai.APPLE_DIR)
        self.client = FakeClient()
        self.state = ai.load_state()

    def paths(self):
        return [path for _, path, _, _ in self.client.calls]

    @mock.patch.object(ai, "CHUNK_SIZE", 2)
    def test_playlist_is_created_once_and_extended_on_rerun(self):
        ai.import_playlist(self.client, self.state, "Mix", ["a", "b", "c"])
        ai.import_playlist(self.client, ai.load_state(), "Mix", ["a", "b", "c", "d"])
        tracks_path = "/v1/me/library/playlists/p.1/tracks"
        self.assertEqual(self.paths(), ["/v1/me/library/playlists", tracks_path, tracks_path])
        self.assertEqual(self.client.calls[-1][3], {"data": ai.song_refs(["d"])})
        self.assertEqual(ai.load_state()["playlists"]["Mix"]["songs"], ["a", "b", "c", "d"])

    def test_playlist_without_matches_is_not_created(self):
        ai.import_playlist(self.client, self.state, "Empty", [])
        self.assertEqual(self.client.calls, [])

    def test_favorites_go_one_song_per_request_and_skip_songs_added_before(self):
        self.state["favorites"] = ["a"]
        ai.import_favorites(self.client, self.state, ["a", "b", "c", "d"])
        sent = [params["ids[songs]"] for _, _, params, _ in self.client.calls]
        self.assertEqual(sent, ["b", "c", "d"])
        self.assertEqual(ai.load_state()["favorites"], ["a", "b", "c", "d"])

    def write_export(self):
        write_csv("liked_tracks_me.csv", OK_HEADER, [
            ["1", "New like", "A", "", "", "3:00", "10", "OK"],
            ["2", "Old like", "A", "", "", "3:00", "11", "OK"],
        ])
        write_csv("playlist_me_Mix.csv", OK_HEADER, [
            ["1", "Mix song", "B", "", "", "3:00", "12", "OK"],
            ["2", "New like", "A", "", "", "3:00", "10", "OK"],
        ])
        ai.write_matches({
            "10": {**row("10", "matched"), "apple_id": "s10"},
            "11": {**row("11", "matched"), "apple_id": "s11"},
            "12": {**row("12", "matched"), "apple_id": "s12"},
        })

    def test_import_favorites_liked_and_playlist_songs_without_creating_playlists(self):
        self.write_export()
        ai.cmd_import(self.client, apply=True, with_playlists=False)
        self.assertNotIn("/v1/me/library/playlists", self.paths())
        sent = [params["ids[songs]"] for _, _, params, _ in self.client.calls]
        self.assertEqual(sent, ["s12", "s10", "s11"])

    def test_dry_run_changes_nothing(self):
        self.write_export()
        ai.cmd_import(self.client, apply=False, with_playlists=True)
        self.assertEqual(self.client.calls, [])
        self.assertFalse(os.path.exists(ai.STATE_PATH))


class RateLimitTest(unittest.TestCase):
    @mock.patch.object(ai.time, "sleep")
    def test_429_waits_retries_and_slows_later_requests(self, sleep):
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b'{"ok": 1}'
        limited = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
        client = ai.AppleMusicClient("dev", "user")
        with mock.patch.object(ai.urllib.request, "urlopen", side_effect=[limited, ok]):
            self.assertEqual(client.request("GET", "/v1/test"), {"ok": 1})
        self.assertEqual(client.pause, ai.REQUEST_PAUSE * 2)
        self.assertEqual(sleep.call_args_list, [mock.call(60), mock.call(client.pause)])

    @mock.patch.object(ai.time, "sleep")
    def test_long_limit_slows_down_only_once(self, sleep):
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b"{}"
        limited = [urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None) for _ in range(3)]
        client = ai.AppleMusicClient("dev", "user")
        with mock.patch.object(ai.urllib.request, "urlopen", side_effect=limited + [ok]):
            client.request("GET", "/v1/test")
        self.assertEqual(client.pause, ai.REQUEST_PAUSE * 2)
        self.assertEqual(sleep.call_args_list[:3], [mock.call(60), mock.call(120), mock.call(240)])

    @mock.patch.object(ai.time, "sleep")
    def test_rate_limit_is_waited_out_past_max_attempts(self, sleep):
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b"{}"
        limited = [urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
                   for _ in range(ai.MAX_ATTEMPTS + 2)]
        client = ai.AppleMusicClient("dev", "user")
        with mock.patch.object(ai.urllib.request, "urlopen", side_effect=limited + [ok]):
            self.assertEqual(client.request("GET", "/v1/test"), {})
        self.assertEqual(max(c.args[0] for c in sleep.call_args_list), 600)

    @mock.patch.object(ai.time, "sleep")
    def test_server_errors_give_up_after_max_attempts(self, sleep):
        failing = [urllib.error.HTTPError("url", 503, "Unavailable", {}, None)
                   for _ in range(ai.MAX_ATTEMPTS)]
        client = ai.AppleMusicClient("dev", "user")
        with mock.patch.object(ai.urllib.request, "urlopen", side_effect=failing):
            with self.assertRaises(ai.ApiError):
                client.request("GET", "/v1/test")

    def test_bad_request_is_not_retried(self):
        rejected = urllib.error.HTTPError("url", 400, "Bad Request", {}, io.BytesIO(b"bad"))
        client = ai.AppleMusicClient("dev", "user")
        with mock.patch.object(ai.urllib.request, "urlopen", side_effect=[rejected]):
            with self.assertRaises(ai.ApiError):
                client.request("GET", "/v1/test")


class TokensTest(unittest.TestCase):
    def test_placeholder_is_rejected(self):
        env = {"APPLE_DEV_TOKEN": "paste_here", "APPLE_USER_TOKEN": "x"}
        with mock.patch.dict(os.environ, env), self.assertRaises(SystemExit):
            ai.load_tokens()

    def test_bearer_prefix_is_accepted(self):
        env = {"APPLE_DEV_TOKEN": "Bearer abc", "APPLE_USER_TOKEN": "xyz"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(ai.load_tokens(), ("abc", "xyz"))


if __name__ == "__main__":
    unittest.main()
