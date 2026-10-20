import base64
import csv
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import amazon_import as am

OK_HEADER = ["#", "Title", "Artist", "Album", "Year", "Duration", "ID", "Availability"]
APPLE_HEADER = ["yandex_id", "title", "artist", "album", "duration", "status",
                "apple_id", "apple_title", "apple_artist", "apple_album", "apple_duration", "score"]
CONTEXT = {"deviceId": "14534074384711863", "deviceType": "A16ZV8BU3SN1N3", "access_token": "Atna|secret"}


def auth_value(context=None):
    """An authorization header value the way the web player sends it."""
    payload = json.dumps(CONTEXT if context is None else context).encode()
    return "AmznMusic " + base64.b64encode(payload).decode()


def node(asin, title, artists, seconds, album=""):
    """A searchTracks result in the shape gql.music.amazon.dev returns it."""
    edges = [{"role": "PRIMARY", "node": {"id": f"artist-{name}", "name": name}} for name in artists]
    return {"id": asin, "title": title, "isrc": "RUA000000001", "duration": seconds,
            "album": {"id": "album-1", "title": album}, "contributingArtists": {"edges": edges}}


def search_data(*nodes):
    return {"searchTracks": {"edges": [{"node": n} for n in nodes]}}


def amazon_song(asin, name, artist, millis, album=""):
    attributes = {"name": name, "artistName": artist, "albumName": album, "durationInMillis": millis}
    return {"id": asin, "type": "song", "attributes": attributes}


def http_ok(payload):
    ok = mock.MagicMock()
    ok.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return ok


def track(title, artist, duration="3:31", album=""):
    return {"title": title, "artist": artist, "album": album, "duration": duration}


def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


class FakeAmazon:
    """Answers searches from `answers`, keyed by query text, and records every call."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    pause = am.SEARCH_PAUSE

    def graphql(self, query, variables):
        self.calls.append((query, variables))
        if query == am.SEARCH_QUERY:
            return search_data(*self.answers.get(variables["query"], []))
        if query == am.USER_QUERY:
            return {"user": {"id": "amzn1.account.TEST"}}
        if query == am.LIKE_MUTATION:
            return {"setTrackLikeState": {"__typename": "SetTrackLikeStateResponse"}}
        return {}

    def queries(self):
        return [variables["query"] for query, variables in self.calls if query == am.SEARCH_QUERY]

    def liked(self):
        return [variables["trackId"] for query, variables in self.calls if query == am.LIKE_MUTATION]


def asin(n):
    """A track ID in Amazon's 10-character ASIN format."""
    return f"B{n:09d}"


def match(track_id, status, track_asin=""):
    return {**dict.fromkeys(am.MATCH_FIELDS, ""), "yandex_id": track_id, "status": status,
            "asin": track_asin}


class InTempDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(tmp.name)


class SearchParsingTest(unittest.TestCase):
    def test_track_becomes_a_catalog_song(self):
        found = am.parse_search(search_data(node("B07ABCDEF1", "Линия жизни", ["Сплин"], 183, "25 кадр")))
        self.assertEqual(found, [amazon_song("B07ABCDEF1", "Линия жизни", "Сплин", 183000, "25 кадр")])

    def test_several_artists_are_listed_together_once_each(self):
        found = am.parse_search(search_data(node("B1", "Numb / Encore", ["Linkin Park", "JAY-Z", "JAY-Z"], 209)))
        self.assertEqual(found[0]["attributes"]["artistName"], "Linkin Park, JAY-Z")

    def test_unknown_duration_stays_unknown(self):
        found = am.parse_search(search_data(node("B1", "Song", ["Band"], None)))
        self.assertIsNone(found[0]["attributes"]["durationInMillis"])

    def test_empty_results_give_no_songs(self):
        self.assertEqual(am.parse_search({}), [])
        self.assertEqual(am.parse_search({"searchTracks": None}), [])

    def test_result_without_id_is_dropped(self):
        self.assertEqual(am.parse_search(search_data(node("", "Song", ["Band"], 180))), [])


class ClientTest(unittest.TestCase):
    @mock.patch.object(am.time, "sleep")
    def test_search_sends_the_login_only_to_amazon_music(self, sleep):
        client = am.AmazonMusicClient(auth_value())
        reply = http_ok({"data": search_data(node("B1", "Линия жизни", ["Сплин"], 183))})
        with mock.patch("urllib.request.urlopen", return_value=reply) as urlopen:
            found = am.search(client, "Линия жизни Сплин")
        self.assertEqual([song["id"] for song in found], ["B1"])
        sent = urlopen.call_args.args[0]
        self.assertTrue(sent.full_url.startswith("https://gql.music.amazon.dev"))
        self.assertEqual(sent.get_header("Authorization"), auth_value())
        self.assertEqual(sent.get_header("X-api-key"), am.WEB_API_KEY)
        self.assertEqual(sent.get_header("Device-id"), CONTEXT["deviceId"])
        self.assertEqual(sent.get_header("Device-type"), CONTEXT["deviceType"])
        self.assertIsNone(sent.get_header("Cookie"))
        self.assertEqual(json.loads(sent.data)["variables"], {"query": "Линия жизни Сплин"})
        self.assertEqual(sleep.call_args.args[0], am.SEARCH_PAUSE)

    @mock.patch.object(am.time, "sleep")
    def test_graphql_error_raises(self, _sleep):
        client = am.AmazonMusicClient(auth_value())
        reply = http_ok({"data": None, "errors": [{"message": "Bad field", "extensions": {"code": "BAD"}}]})
        with mock.patch("urllib.request.urlopen", return_value=reply):
            with self.assertRaises(am.ApiError) as raised:
                client.graphql(am.SEARCH_QUERY, {"query": "x"})
        self.assertIn("Bad field", str(raised.exception))

    @mock.patch.object(am.time, "sleep")
    def test_auth_error_says_to_copy_a_fresh_login(self, _sleep):
        client = am.AmazonMusicClient(auth_value())
        reply = http_ok({"data": None, "errors": [{"message": "x", "extensions": {"code": "AUTH_TOKEN_EXPIRED"}}]})
        with mock.patch("urllib.request.urlopen", return_value=reply):
            with self.assertRaises(am.ApiError) as raised:
                client.graphql(am.SEARCH_QUERY, {"query": "x"})
        self.assertEqual(str(raised.exception), am.LOGIN_EXPIRED)

    @mock.patch.object(am.time, "sleep")
    def test_refused_request_says_to_copy_a_fresh_login(self, _sleep):
        client = am.AmazonMusicClient(auth_value())
        refused = urllib.error.HTTPError("url", 401, "Unauthorized", {}, io.BytesIO(b'{"errors": []}'))
        with mock.patch("urllib.request.urlopen", side_effect=[refused]):
            with self.assertRaises(am.ApiError) as raised:
                client.graphql(am.SEARCH_QUERY, {"query": "x"})
        self.assertEqual(str(raised.exception), am.LOGIN_EXPIRED)


class MatchTest(unittest.TestCase):
    def test_same_song_is_matched_from_the_first_search(self):
        fake = FakeAmazon({"Линия жизни Сплин": [node("B1", "Линия жизни", ["Сплин"], 183)]})
        song, _, status = am.find_match(fake, track("Линия жизни", "Сплин", "3:02"))
        self.assertEqual((song["id"], status), ("B1", "matched"))
        self.assertEqual(fake.queries(), ["Линия жизни Сплин"])

    def test_title_alone_finds_an_artist_spelled_otherwise(self):
        fake = FakeAmazon({"Линия жизни": [node("B2", "Линия жизни", ["Splean"], 183)]})
        song, _, status = am.find_match(fake, track("Линия жизни", "Сплин", "3:03"))
        self.assertEqual((song["id"], status), ("B2", "matched"))
        self.assertEqual(fake.queries(), ["Линия жизни Сплин", "Линия жизни"])

    def test_karaoke_copy_is_only_check(self):
        fake = FakeAmazon({"San Francisco Scott McKenzie": [
            node("B3", "San Francisco", ["Scott McKenzie (Karaoke)"], 174, "Zoom Karaoke")]})
        _, _, status = am.find_match(fake, track("San Francisco", "Scott McKenzie", "2:53"))
        self.assertEqual(status, "check")

    def test_match_row_names_the_amazon_song(self):
        exported = track("Линия жизни", "Сплин", "3:02")
        best = am.find_match(FakeAmazon({"Линия жизни Сплин": [
            node("B1", "Линия жизни", ["Сплин"], 183, "25 кадр")]}), exported)
        row = am.match_row("10", exported, best)
        self.assertEqual((row["yandex_id"], row["status"], row["asin"], row["amazon_album"], row["amazon_duration"]),
                         ("10", "matched", "B1", "25 кадр", "3:03"))

    def test_nothing_found_is_missing(self):
        exported = track("Unknown", "Nobody")
        row = am.match_row("10", exported, am.find_match(FakeAmazon(), exported))
        self.assertEqual((row["status"], row["asin"], row["title"]), ("missing", "", "Unknown"))


class MatchCommandTest(InTempDir):
    @mock.patch("builtins.print")
    def test_match_resumes_and_searches_the_reviewed_apple_names(self, _print):
        write_csv("liked_tracks_me.csv", OK_HEADER, [
            ["1", "Done", "A", "", "", "3:00", "10", "OK"],
            ["2", "Shoot To Thrill (Made Famous By AC/DC)", "Iron Man", "", "", "5:20", "11", "OK"],
        ])
        os.makedirs("apple")
        write_csv("apple/matches.csv", APPLE_HEADER, [
            ["11", "", "", "", "", "matched", "1", "Shoot to Thrill", "AC/DC", "Back in Black", "5:17", "1"]])
        os.makedirs(am.AMAZON_DIR)
        am.write_matches({"10": {**dict.fromkeys(am.MATCH_FIELDS, ""), "yandex_id": "10", "status": "skip"}})
        fake = FakeAmazon({"Shoot to Thrill AC/DC": [node("B11", "Shoot to Thrill", ["AC/DC"], 317)]})
        am.cmd_match(fake)
        self.assertEqual(fake.queries(), ["Shoot to Thrill AC/DC"])
        rows = am.load_matches()
        self.assertEqual(list(rows), ["10", "11"])
        self.assertEqual((rows["10"]["status"], rows["11"]["asin"]), ("skip", "B11"))
        self.assertEqual(rows["11"]["title"], "Shoot To Thrill (Made Famous By AC/DC)")


@mock.patch("builtins.print")
class ImportTest(InTempDir):
    def setUp(self):
        super().setUp()
        write_csv("liked_tracks_me.csv", OK_HEADER, [
            ["1", "New like", "A", "", "", "3:00", "10", "OK"],
            ["2", "Old like", "A", "", "", "3:00", "11", "OK"],
        ])
        write_csv("playlist_me_Mix.csv", OK_HEADER, [
            ["1", "Mix song", "B", "", "", "3:00", "12", "OK"],
            ["2", "New like", "A", "", "", "3:00", "10", "OK"],
            ["3", "Lost song", "C", "", "", "3:00", "13", "OK"],
        ])
        os.makedirs(am.AMAZON_DIR)
        self.rows = {"10": match("10", "matched", asin(10)), "11": match("11", "matched", asin(11)),
                     "12": match("12", "matched", asin(12)), "13": match("13", "missing")}
        am.write_matches(self.rows)
        self.fake = FakeAmazon()

    def assert_refused(self):
        am.write_matches(self.rows)
        with self.assertRaises(SystemExit):
            am.cmd_import(self.fake, apply=True)
        self.assertEqual(self.fake.calls, [])

    def test_apply_likes_newest_last_and_only_ever_sets_like(self, _print):
        am.cmd_import(self.fake, apply=True)
        self.assertEqual(self.fake.liked(), [asin(12), asin(11), asin(10)])
        self.assertEqual({query for query, _ in self.fake.calls}, {am.USER_QUERY, am.LIKE_MUTATION})
        self.assertEqual({v["likeState"] for q, v in self.fake.calls if q == am.LIKE_MUTATION}, {"LIKE"})
        self.assertEqual(am.load_state()["liked"], [asin(12), asin(11), asin(10)])

    def test_likes_go_slower_than_searches(self, _print):
        am.cmd_import(self.fake, apply=True)
        self.assertEqual(self.fake.pause, am.LIKE_PAUSE)

    def test_apply_refuses_while_rows_need_a_check(self, _print):
        self.rows["13"] = match("13", "check", asin(13))
        self.assert_refused()

    def test_apply_refuses_while_tracks_are_unsearched(self, _print):
        del self.rows["13"]
        self.assert_refused()

    def test_apply_refuses_an_unknown_status(self, _print):
        self.rows["12"] = match("12", "Matched", asin(12))
        self.assert_refused()

    def test_apply_refuses_a_matched_row_without_a_valid_track_id(self, _print):
        self.rows["12"] = match("12", "matched", "https://music.amazon.com/tracks/B000000012")
        self.assert_refused()

    def test_rerun_continues_after_the_last_like(self, _print):
        am.save_state({"liked": [asin(12)]})
        am.cmd_import(self.fake, apply=True)
        self.assertEqual(self.fake.liked(), [asin(11), asin(10)])
        self.assertEqual(am.load_state()["liked"], [asin(12), asin(11), asin(10)])

    def test_failed_like_stops_the_run_and_keeps_progress(self, _print):
        def graphql(query, variables):
            if variables.get("trackId") == asin(11):
                raise am.ApiError("HTTP 400")
            return FakeAmazon.graphql(self.fake, query, variables)

        self.fake.graphql = graphql
        with self.assertRaises(am.ApiError):
            am.cmd_import(self.fake, apply=True)
        self.assertEqual(am.load_state()["liked"], [asin(12)])

    def test_like_without_a_result_stops_the_run(self, _print):
        self.fake.graphql = lambda query, variables: (
            {} if query == am.LIKE_MUTATION else FakeAmazon.graphql(self.fake, query, variables))
        with self.assertRaises(am.ApiError):
            am.cmd_import(self.fake, apply=True)
        self.assertFalse(os.path.exists(am.STATE_PATH))

    def test_dry_run_needs_no_login(self, _print):
        with mock.patch.dict(os.environ, {"AMAZON_MUSIC_AUTH": ""}):
            self.assertEqual(am.main(["amazon_import.py", "import"]), 0)
            with self.assertRaises(SystemExit):
                am.main(["amazon_import.py", "import", "--apply"])

    def test_dry_run_likes_nothing_and_writes_the_report(self, _print):
        am.cmd_import(None, apply=False)
        self.assertFalse(os.path.exists(am.STATE_PATH))
        report = am.read_csv(am.NOT_IMPORTED_PATH)
        self.assertEqual([(r["yandex_id"], r["status"], r["in"]) for r in report], [("13", "missing", "Mix")])


class CheckTest(unittest.TestCase):
    def test_account_id_is_read_from_the_user(self):
        self.assertEqual(am.account_id(FakeAmazon()), "amzn1.account.TEST")

    def test_answer_without_a_user_raises(self):
        client = mock.Mock()
        client.graphql.return_value = {"user": None}
        with self.assertRaises(am.ApiError):
            am.account_id(client)


class LoginTest(unittest.TestCase):
    def load(self, value):
        with mock.patch.dict(os.environ, {"AMAZON_MUSIC_AUTH": value}):
            return am.load_login()

    def assert_rejected_without_echo(self, value):
        with self.assertRaises(SystemExit) as raised:
            self.load(value)
        message = str(raised.exception.code)
        self.assertNotIn("secret", message)
        self.assertNotIn(value.split()[-1], message)

    def test_copied_header_value_is_accepted(self):
        self.assertEqual(self.load(auth_value()), auth_value())

    def test_copied_header_name_and_quotes_are_dropped(self):
        self.assertEqual(self.load(f"authorization: '{auth_value()}'"), auth_value())

    def test_placeholder_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.load("paste_here")

    def test_value_of_another_kind_is_rejected_without_echoing_it(self):
        self.assert_rejected_without_echo("Bearer secretvalue")

    def test_value_without_an_access_token_is_rejected_without_echoing_it(self):
        self.assert_rejected_without_echo(auth_value({"deviceId": "secret1", "deviceType": "secret2"}))

    def test_value_that_is_not_base64_json_is_rejected_without_echoing_it(self):
        self.assert_rejected_without_echo("AmznMusic secret!!not-base64")

    def test_value_with_a_line_break_is_rejected_without_echoing_it(self):
        self.assert_rejected_without_echo(auth_value() + "\rsecret")


if __name__ == "__main__":
    unittest.main()
