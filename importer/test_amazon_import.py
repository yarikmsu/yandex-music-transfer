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
SESSION_COOKIES = "session-id=142-1-2; ubid-main=131-4-5; at-main=Atza|secretA; sess-at-main=secretS; x-main=secretX"
# What the browser sends: the session cookies among analytics and experiment ones
COPIED_COOKIE = ("session-id=142-1-2; am-loader-experiment=EXP; ubid-main=131-4-5; lc-main=en_US; "
                 "at-main=Atza|secretA; sess-at-main=secretS; x-main=secretX; __Secure-id_dbp=Dbsc|secretD")
SESSION = {"deviceId": "14226794074016810", "deviceType": "A16ZV8BU3SN1N3", "sessionId": "142-1-2",
           "customerId": "CUSTOMER", "musicTerritory": "US",
           "csrf": {"token": "csrftoken=", "rnd": "918336372", "ts": "1792472957"}}


def node(asin, title, artists, seconds, album=""):
    """A searchTracks result in the shape gql.music.amazon.com returns it."""
    edges = [{"node": {"name": name}} for name in artists]
    return {"id": asin, "title": title, "duration": seconds,
            "album": {"title": album}, "contributingArtists": {"edges": edges}}


def search_data(*nodes):
    return {"searchTracks": {"edges": [{"node": n} for n in nodes]}}


def amazon_song(asin, name, artist, millis, album=""):
    attributes = {"name": name, "artistName": artist, "albumName": album, "durationInMillis": millis}
    return {"id": asin, "type": "song", "attributes": attributes}


def http_ok(payload):
    ok = mock.MagicMock()
    ok.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return ok


def refused():
    return urllib.error.HTTPError("url", 401, "Unauthorized", {}, io.BytesIO(b'{"errors": []}'))


def signed_in_client():
    with mock.patch.object(am, "fetch_session", return_value=SESSION):
        return am.AmazonMusicClient(SESSION_COOKIES)


def track(title, artist, duration="3:31", album=""):
    return {"title": title, "artist": artist, "album": album, "duration": duration}


def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def write_matches(rows):
    with open(am.MATCHES_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=am.MATCH_FIELDS)
        writer.writeheader()
        writer.writerows(rows.values())


def filters_of(variables):
    """(title, artist or None) of a search request."""
    filters = variables["options"]["searchFilters"]
    return filters[0]["query"], filters[1]["query"] if len(filters) > 1 else None


class FakeAmazon:
    """Answers searches from `answers`, keyed by (title, artist or None), keeps Liked Songs
    newest first (a song liked before keeps its place), and records every call."""

    pause = am.SEARCH_PAUSE

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []
        self.liked_songs = []

    def graphql(self, query, variables):
        self.calls.append((query, variables))
        if query == am.SEARCH_QUERY:
            return search_data(*self.answers.get(filters_of(variables), []))
        if query == am.USER_QUERY:
            return {"user": {"id": "amzn1.account.TEST", "name": "Test User"}}
        if query == am.LIKE_MUTATION:
            if variables["trackId"] not in self.liked_songs:
                self.liked_songs.insert(0, variables["trackId"])
            return {"setTrackLikeState": {"__typename": "SetUserTrackLikeStateResponse"}}
        if query == am.LATEST_LIKES_QUERY:
            return {"user": {"tracks": {"edges": [{"node": {"id": s}} for s in self.liked_songs]}}}
        return {}

    def queries(self):
        return [filters_of(variables) for query, variables in self.calls if query == am.SEARCH_QUERY]

    def liked(self):
        return [variables["trackId"] for query, variables in self.calls if query == am.LIKE_MUTATION]


class InTempDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(tmp.name)


def asin(n):
    """A track ID in Amazon's 10-character ASIN format."""
    return f"B{n:09d}"


def match(track_id, status, track_asin=""):
    return {**dict.fromkeys(am.MATCH_FIELDS, ""), "yandex_id": track_id, "status": status,
            "asin": track_asin}


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


@mock.patch.object(am.time, "sleep")
class ClientTest(unittest.TestCase):
    def test_requests_carry_the_session_the_player_uses(self, sleep):
        client = signed_in_client()
        reply = http_ok({"data": search_data(node("B1", "Линия жизни", ["Сплин"], 183))})
        with mock.patch("urllib.request.urlopen", return_value=reply) as urlopen:
            found = am.search(client, "Линия жизни", "Сплин")
        self.assertEqual([song["id"] for song in found], ["B1"])
        sent = urlopen.call_args.args[0]
        self.assertTrue(sent.full_url.startswith("https://gql.music.amazon.com/"))
        self.assertEqual(sent.get_header("Cookie"), SESSION_COOKIES)
        self.assertEqual(sent.get_header("X-api-key"), am.WEB_API_KEY)
        self.assertEqual(sent.get_header("X-amzn-device-id"), SESSION["deviceId"])
        self.assertEqual(sent.get_header("X-amzn-session-id"), SESSION["sessionId"])
        self.assertEqual(sent.get_header("Csrf-token"), SESSION["csrf"]["token"])
        self.assertEqual(sent.get_header("Music-territory"), "US")
        filters = json.loads(sent.data)["variables"]["options"]["searchFilters"]
        self.assertEqual(filters, [{"field": "name", "query": "Линия жизни"}, {"field": "artist", "query": "Сплин"}])
        self.assertEqual(sleep.call_args.args[0], am.SEARCH_PAUSE)

    def test_title_alone_is_one_filter(self, _sleep):
        client = signed_in_client()
        with mock.patch("urllib.request.urlopen", return_value=http_ok({"data": {}})) as urlopen:
            am.search(client, "Линия жизни")
        filters = json.loads(urlopen.call_args.args[0].data)["variables"]["options"]["searchFilters"]
        self.assertEqual(filters, [{"field": "name", "query": "Линия жизни"}])

    def test_session_comes_from_the_player_config(self, _sleep):
        with mock.patch("urllib.request.urlopen", return_value=http_ok(SESSION)) as urlopen:
            self.assertEqual(am.fetch_session(SESSION_COOKIES), SESSION)
        sent = urlopen.call_args.args[0]
        self.assertEqual((sent.full_url, sent.get_method()), ("https://music.amazon.com/config.json", "POST"))
        self.assertEqual(sent.get_header("Cookie"), SESSION_COOKIES)

    def test_config_without_a_customer_means_the_cookie_is_not_signed_in(self, _sleep):
        with mock.patch("urllib.request.urlopen", return_value=http_ok({**SESSION, "customerId": ""})):
            with self.assertRaises(am.ApiError) as raised:
                am.fetch_session(SESSION_COOKIES)
        self.assertEqual(str(raised.exception), am.LOGIN_REFUSED)

    def test_config_refusing_the_cookie_says_to_copy_it_again(self, _sleep):
        with mock.patch("urllib.request.urlopen", side_effect=refused()):
            with self.assertRaises(am.ApiError) as raised:
                am.fetch_session(SESSION_COOKIES)
        self.assertEqual(str(raised.exception), am.LOGIN_REFUSED)

    def test_config_without_the_device_is_an_amazon_error(self, _sleep):
        config = {key: value for key, value in SESSION.items() if key != "deviceId"}
        with mock.patch("urllib.request.urlopen", return_value=http_ok(config)):
            with self.assertRaises(am.ApiError) as raised:
                am.fetch_session(SESSION_COOKIES)
        self.assertIn("deviceId", str(raised.exception))

    def test_refused_request_renews_the_session_once_and_retries(self, _sleep):
        client = signed_in_client()
        renewed = {**SESSION, "csrf": {"token": "fresh=", "rnd": "1", "ts": "2"}}
        replies = [refused(), http_ok({"data": {"user": {"id": "u"}}})]
        with mock.patch.object(am, "fetch_session", return_value=renewed) as fetch, \
                mock.patch("urllib.request.urlopen", side_effect=replies) as urlopen:
            self.assertEqual(client.graphql(am.USER_QUERY, {}), {"user": {"id": "u"}})
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(urlopen.call_args.args[0].get_header("Csrf-token"), "fresh=")

    def test_refused_after_renewal_says_to_copy_the_cookie_again(self, _sleep):
        client = signed_in_client()
        with mock.patch.object(am, "fetch_session", return_value=SESSION), \
                mock.patch("urllib.request.urlopen", side_effect=[refused(), refused()]):
            with self.assertRaises(am.ApiError) as raised:
                client.graphql(am.USER_QUERY, {})
        self.assertEqual(str(raised.exception), am.LOGIN_REFUSED)

    def test_auth_error_in_the_answer_counts_as_refused(self, _sleep):
        client = signed_in_client()
        anonymous = {"data": None, "errors": [{"extensions": {"code": "AUTH_CLIENT_NOT_ANONYMOUS_ALLOW_LISTED"}}]}
        with mock.patch.object(am, "fetch_session", return_value=SESSION), \
                mock.patch("urllib.request.urlopen", side_effect=[http_ok(anonymous), http_ok(anonymous)]):
            with self.assertRaises(am.ApiError) as raised:
                client.graphql(am.USER_QUERY, {})
        self.assertEqual(str(raised.exception), am.LOGIN_REFUSED)

    def test_other_graphql_error_raises_its_message(self, _sleep):
        client = signed_in_client()
        reply = http_ok({"data": None, "errors": [{"message": "Bad field", "extensions": {"code": "BAD"}}]})
        with mock.patch("urllib.request.urlopen", return_value=reply):
            with self.assertRaises(am.ApiError) as raised:
                client.graphql(am.USER_QUERY, {})
        self.assertIn("Bad field", str(raised.exception))


class MatchTest(unittest.TestCase):
    def test_same_song_is_matched_from_the_first_search(self):
        fake = FakeAmazon({("Линия жизни", "Сплин"): [node("B1", "Линия жизни", ["Сплин"], 183)]})
        song, _, status = am.find_match(fake, track("Линия жизни", "Сплин", "3:02"))
        self.assertEqual((song["id"], status), ("B1", "matched"))
        self.assertEqual(fake.queries(), [("Линия жизни", "Сплин")])

    def test_title_alone_finds_an_artist_spelled_otherwise(self):
        fake = FakeAmazon({("Линия жизни", None): [node("B2", "Линия жизни", ["Splean"], 183)]})
        song, _, status = am.find_match(fake, track("Линия жизни", "Сплин", "3:03"))
        self.assertEqual((song["id"], status), ("B2", "matched"))
        self.assertEqual(fake.queries(), [("Линия жизни", "Сплин"), ("Линия жизни", None)])

    def test_first_of_several_artists_filters_the_search(self):
        fake = FakeAmazon()
        am.find_match(fake, track("Numb / Encore", "Linkin Park, JAY-Z"))
        self.assertEqual(fake.queries()[0], ("Numb / Encore", "Linkin Park"))

    def test_album_recording_beats_a_live_one_of_closer_length(self):
        live = node("B1", "In the End (Live)", ["Linkin Park"], 211)
        album = node("B2", "In the End", ["Linkin Park"], 216)
        fake = FakeAmazon({("In the End", "Linkin Park"): [live, album]})
        song, _, status = am.find_match(fake, track("In the End", "Linkin Park", "3:31"))
        self.assertEqual((song["id"], status), ("B2", "matched"))

    def test_title_alone_is_searched_when_only_another_recording_matched(self):
        fake = FakeAmazon({
            ("Touch Me", "The Doors"): [node("B1", "Touch Me (Doors Only Mix)", ["The Doors"], 193)],
            ("Touch Me", None): [node("B2", "Touch Me", ["The Doors"], 195)],
        })
        song, _, _ = am.find_match(fake, track("Touch Me", "The Doors", "3:12"))
        self.assertEqual(song["id"], "B2")

    def test_live_recording_is_still_matched_when_it_is_all_there_is(self):
        fake = FakeAmazon({("In the End", "Linkin Park"): [node("B1", "In the End (Live)", ["Linkin Park"], 211)]})
        song, _, status = am.find_match(fake, track("In the End", "Linkin Park", "3:31"))
        self.assertEqual((song["id"], status), ("B1", "matched"))

    def test_exported_live_track_prefers_the_live_recording(self):
        live = node("B1", "In the End (Live)", ["Linkin Park"], 216)
        album = node("B2", "In the End", ["Linkin Park"], 216)
        fake = FakeAmazon({("In the End (Live)", "Linkin Park"): [album, live]})
        song, _, _ = am.find_match(fake, track("In the End (Live)", "Linkin Park", "3:36"))
        self.assertEqual(song["id"], "B1")

    def test_live_in_the_song_name_is_not_a_version_note(self):
        live = node("B1", "Live Forever (Live at Knebworth)", ["Oasis"], 277)
        album = node("B2", "Live Forever (Remastered)", ["Oasis"], 284)
        fake = FakeAmazon({("Live Forever", "Oasis"): [live, album]})
        song, _, _ = am.find_match(fake, track("Live Forever", "Oasis", "4:36"))
        self.assertEqual(song["id"], "B2")

    def test_version_note_after_a_dash_counts(self):
        live = node("B1", "In the End - Live in Texas", ["Linkin Park"], 211)
        album = node("B2", "In the End", ["Linkin Park"], 216)
        fake = FakeAmazon({("In the End", "Linkin Park"): [live, album]})
        song, _, _ = am.find_match(fake, track("In the End", "Linkin Park", "3:31"))
        self.assertEqual(song["id"], "B2")

    def test_karaoke_copy_is_only_check(self):
        fake = FakeAmazon({("San Francisco", "Scott McKenzie"): [
            node("B3", "San Francisco", ["Scott McKenzie (Karaoke)"], 174, "Zoom Karaoke")]})
        _, _, status = am.find_match(fake, track("San Francisco", "Scott McKenzie", "2:53"))
        self.assertEqual(status, "check")

    def test_match_row_names_the_amazon_song(self):
        exported = track("Линия жизни", "Сплин", "3:02")
        best = am.find_match(FakeAmazon({("Линия жизни", "Сплин"): [
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
        write_matches({"10": {**dict.fromkeys(am.MATCH_FIELDS, ""), "yandex_id": "10", "status": "skip"}})
        fake = FakeAmazon({("Shoot to Thrill", "AC/DC"): [node("B11", "Shoot to Thrill", ["AC/DC"], 317)]})
        am.cmd_match(fake)
        self.assertEqual(fake.queries(), [("Shoot to Thrill", "AC/DC")])
        rows = am.load_matches()
        self.assertEqual(list(rows), ["10", "11"])
        self.assertEqual((rows["10"]["status"], rows["11"]["asin"]), ("skip", "B11"))
        self.assertEqual(rows["11"]["title"], "Shoot To Thrill (Made Famous By AC/DC)")

    def failing_search(self, error):
        write_csv("liked_tracks_me.csv", OK_HEADER, [["1", "Linia", "Splean", "", "", "3:00", "10", "OK"]])
        fake = FakeAmazon()

        def graphql(query, variables):
            raise error

        fake.graphql = graphql
        with self.assertRaises(am.ApiError) as raised:
            am.cmd_match(fake)
        return str(raised.exception)

    @mock.patch("builtins.print")
    def test_failed_search_names_the_track(self, _print):
        message = self.failing_search(am.ApiError("Bad field"))
        self.assertIn("Linia", message)
        self.assertIn("10", message)
        self.assertIn("Bad field", message)

    @mock.patch("builtins.print")
    def test_refused_login_while_searching_keeps_its_advice(self, _print):
        self.assertEqual(self.failing_search(am.LoginRefused()), am.LOGIN_REFUSED)


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
        write_matches(self.rows)
        self.fake = FakeAmazon()

    def assert_refused(self):
        write_matches(self.rows)
        with self.assertRaises(SystemExit):
            am.cmd_import(self.fake, apply=True)
        self.assertEqual(self.fake.calls, [])

    def test_apply_likes_newest_last_and_only_ever_sets_like(self, _print):
        am.cmd_import(self.fake, apply=True)
        self.assertEqual(self.fake.liked(), [asin(12), asin(11), asin(10)])
        self.assertEqual({query for query, _ in self.fake.calls},
                         {am.USER_QUERY, am.LIKE_MUTATION, am.LATEST_LIKES_QUERY})
        self.assertEqual({v["likeState"] for q, v in self.fake.calls if q == am.LIKE_MUTATION}, {"LIKE"})
        self.assertEqual(am.load_state()["liked"], [asin(12), asin(11), asin(10)])

    def test_likes_go_slower_than_searches(self, _print):
        am.cmd_import(self.fake, apply=True)
        self.assertEqual(self.fake.pause, am.LIKE_PAUSE)

    def test_liked_songs_in_another_order_stops_after_a_few_likes(self, _print):
        newest_first = FakeAmazon.graphql

        def graphql(query, variables):
            answer = newest_first(self.fake, query, variables)
            if query == am.LATEST_LIKES_QUERY:
                answer["user"]["tracks"]["edges"].reverse()
            return answer

        self.fake.graphql = graphql
        with self.assertRaises(am.ApiError):
            am.like_songs(self.fake, {"liked": []}, [asin(n) for n in range(1, 10)])
        self.assertEqual(am.load_state()["liked"], [asin(n) for n in range(1, am.ORDER_CHECK_LIMIT + 1)])

    def test_song_liked_before_does_not_stop_the_run(self, _print):
        self.fake.liked_songs = [asin(2)]
        am.like_songs(self.fake, {"liked": []}, [asin(1), asin(2), asin(3)])
        self.assertEqual(self.fake.liked(), [asin(1), asin(2), asin(3)])

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
        with self.assertRaises(am.ApiError) as raised:
            am.cmd_import(self.fake, apply=True)
        self.assertEqual(am.load_state()["liked"], [asin(12)])
        self.assertIn(asin(11), str(raised.exception))

    def test_like_without_a_result_stops_the_run(self, _print):
        self.fake.graphql = lambda query, variables: (
            {} if query == am.LIKE_MUTATION else FakeAmazon.graphql(self.fake, query, variables))
        with self.assertRaises(am.ApiError):
            am.cmd_import(self.fake, apply=True)
        self.assertFalse(os.path.exists(am.STATE_PATH))

    def test_dry_run_needs_no_login(self, _print):
        with mock.patch.dict(os.environ, {"AMAZON_MUSIC_COOKIE": ""}):
            self.assertEqual(am.main(["amazon_import.py", "import"]), 0)
            with self.assertRaises(SystemExit):
                am.main(["amazon_import.py", "import", "--apply"])

    def test_dry_run_likes_nothing_and_writes_the_report(self, _print):
        am.cmd_import(None, apply=False)
        self.assertFalse(os.path.exists(am.STATE_PATH))
        report = am.read_csv(am.NOT_IMPORTED_PATH)
        self.assertEqual([(r["yandex_id"], r["status"], r["in"]) for r in report], [("13", "missing", "Mix")])


class CheckTest(unittest.TestCase):
    def test_account_name_is_read_from_the_user(self):
        self.assertEqual(am.account_name(FakeAmazon()), "Test User")

    def test_answer_without_a_user_raises(self):
        client = mock.Mock()
        client.graphql.return_value = {"user": None}
        with self.assertRaises(am.ApiError):
            am.account_name(client)


class LoginTest(unittest.TestCase):
    def load(self, value):
        with mock.patch.dict(os.environ, {"AMAZON_MUSIC_COOKIE": value}):
            return am.load_login()

    def assert_rejected_without_echo(self, value):
        with self.assertRaises(SystemExit) as raised:
            self.load(value)
        self.assertNotIn("secret", str(raised.exception.code))

    def test_only_the_session_cookies_are_kept(self):
        self.assertEqual(self.load(COPIED_COOKIE), SESSION_COOKIES)

    def test_copied_header_name_and_quotes_are_dropped(self):
        self.assertEqual(self.load(f"cookie: '{COPIED_COOKIE}'"), SESSION_COOKIES)

    def test_placeholder_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.load("paste_here")

    def test_cookie_without_a_session_cookie_is_rejected_without_echoing_it(self):
        with self.assertRaises(SystemExit) as raised:
            self.load(COPIED_COOKIE.replace("x-main=secretX; ", ""))
        self.assertIn("x-main", str(raised.exception.code))
        self.assertNotIn("secret", str(raised.exception.code))

    def test_cookie_with_a_line_break_is_rejected_without_echoing_it(self):
        self.assert_rejected_without_echo(COPIED_COOKIE + "\rsecret")


if __name__ == "__main__":
    unittest.main()
