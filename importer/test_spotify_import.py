import csv
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

import spotify_import as sp

# Made-up stand-ins for what the web player's code holds
HASHES = {name: f"{i:064x}" for i, name in enumerate(
    ["profileAttributes", "addToLibrary", "fetchLibraryTracks", "areEntitiesInLibrary", "searchTracks"], 1)}
HOME = ('<script src="https://open.spotifycdn.com/cdn/build/web-player/vendor~web-player.4212c269.js"></script>'
        '<script src="https://open.spotifycdn.com/cdn/build/web-player/web-player.aeaf3de5.js"></script>')
BUNDLE = ("let ej=[{secret:'a\\'b\\\\c',version:61},{secret:\"xyz\",version:60}].map(e=>e)[0];"
          + "".join(f'let p{i}=new aQ.l("{n}","{"mutation" if n == "addToLibrary" else "query"}","{h}",null);'
                    for i, (n, h) in enumerate(HASHES.items()) if n != "searchTracks")
          + '.u=e=>""+(({2706:"xpui-routes-recent-searches",4406:"xpui-routes-search"})[e]||e)+"."'
            '+({2706:"90a401e7",4406:"0bd78592"})[e]+".js",')
SEARCH_CHUNK = f'let s0=new _.l("searchTracks","query","{HASHES["searchTracks"]}",null);'
PAGES = {"https://open.spotify.com/": HOME, sp.CDN + "web-player.aeaf3de5.js": BUNDLE,
         sp.CDN + "xpui-routes-search.0bd78592.js": SEARCH_CHUNK}
NOW = 1_800_000_000
PLAYER = sp.Player("AB", 61, {name: HASHES[name] for name in sp.OPERATIONS})


def http_ok(payload):
    ok = mock.MagicMock()
    ok.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return ok


def refused(code=401):
    return urllib.error.HTTPError("url", code, "Refused", {}, io.BytesIO(b'{"error": "refused"}'))


def params_of(request):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(request.full_url).query))


class TotpTest(unittest.TestCase):
    def test_rfc_6238_sha1_vectors(self):
        key = b"12345678901234567890"
        self.assertEqual(sp.totp(key, 59), "287082")
        self.assertEqual(sp.totp(key, 1111111109), "081804")

    def test_key_xors_each_character_with_its_index(self):
        # ord("A") ^ 9 == 72, ord("B") ^ 10 == 72; the numbers are joined as text
        self.assertEqual(sp.totp_key("AB"), b"7272")

    def test_xor_restarts_after_33_characters(self):
        self.assertEqual(sp.totp_key("A" * 34)[-2:], b"72")

    def test_number_secret_is_the_same_as_its_characters(self):
        self.assertEqual(sp.totp_key([65, 66]), sp.totp_key("AB"))


class PlayerTest(unittest.TestCase):
    def test_main_bundle_is_found_among_the_scripts(self):
        self.assertEqual(sp.bundle_url(HOME), sp.CDN + "web-player.aeaf3de5.js")

    def test_secrets_are_read_in_bundle_order_with_escapes(self):
        self.assertEqual(sp.parse_secrets(BUNDLE), [("a'b\\c", 61), ("xyz", 60)])

    def test_number_secret_is_read_as_a_list(self):
        self.assertEqual(sp.parse_secrets("[{secret:[44,55,47],version:9}]"), [([44, 55, 47], 9)])

    def test_operations_and_search_chunk(self):
        self.assertEqual(sp.parse_operations(BUNDLE)["addToLibrary"], HASHES["addToLibrary"])
        self.assertEqual(sp.search_chunk_url(BUNDLE), sp.CDN + "xpui-routes-search.0bd78592.js")

    def test_player_has_the_first_secret_and_the_four_hashes(self):
        with mock.patch.object(sp, "fetch_text", side_effect=PAGES.__getitem__):
            player = sp.load_player()
        self.assertEqual((player.secret, player.version), ("a'b\\c", 61))
        self.assertEqual(player.hashes, {name: HASHES[name] for name in sp.OPERATIONS})

    def test_missing_search_hash_says_the_player_changed(self):
        pages = {**PAGES, sp.CDN + "xpui-routes-search.0bd78592.js": ""}
        with mock.patch.object(sp, "fetch_text", side_effect=pages.__getitem__):
            with self.assertRaises(sp.ApiError) as raised:
                sp.load_player()
        self.assertIn("searchTracks", str(raised.exception))
        self.assertIn(sp.PLAYER_CHANGED, str(raised.exception))

    def test_page_without_the_bundle_says_the_player_changed(self):
        with mock.patch.object(sp, "fetch_text", return_value="<html></html>"):
            with self.assertRaises(sp.ApiError) as raised:
                sp.load_player()
        self.assertIn(sp.PLAYER_CHANGED, str(raised.exception))

    def test_bundles_are_fetched_without_the_login(self):
        with mock.patch("urllib.request.urlopen", return_value=http_ok("x")) as urlopen:
            sp.fetch_text(sp.CDN + "web-player.aeaf3de5.js")
        sent = urlopen.call_args.args[0]
        self.assertIsNone(sent.get_header("Cookie"))
        self.assertIsNone(sent.get_header("Authorization"))


@mock.patch.object(sp.time, "sleep")
@mock.patch.object(sp.time, "time", return_value=NOW)
class TokenTest(unittest.TestCase):
    def token_reply(self, **changes):
        return http_ok({"accessToken": "TOKEN", "accessTokenExpirationTimestampMs": (NOW + 3000) * 1000,
                        "isAnonymous": False, "clientId": "CLIENT", **changes})

    def test_token_request_carries_the_codes_and_the_cookie(self, _time, _sleep):
        replies = [http_ok({"serverTime": NOW + 10}), self.token_reply()]
        with mock.patch("urllib.request.urlopen", side_effect=replies) as urlopen:
            self.assertEqual(sp.fetch_token("SPDC", PLAYER), ("TOKEN", NOW + 3000.0))
        server_time, token = (call.args[0] for call in urlopen.call_args_list)
        self.assertEqual(server_time.full_url, "https://open.spotify.com/api/server-time")
        self.assertIsNone(server_time.get_header("Cookie"))
        self.assertTrue(token.full_url.startswith("https://open.spotify.com/api/token?"))
        key = sp.totp_key("AB")
        self.assertEqual(params_of(token), {"reason": "init", "productType": "web-player",
                                            "totp": sp.totp(key, NOW), "totpServer": sp.totp(key, NOW + 10),
                                            "totpVer": "61"})
        self.assertEqual(token.get_header("Cookie"), "sp_dc=SPDC")

    def test_server_time_failure_sends_unavailable(self, _time, _sleep):
        replies = [urllib.error.URLError("down"), self.token_reply()]
        with mock.patch("urllib.request.urlopen", side_effect=replies) as urlopen:
            sp.fetch_token("SPDC", PLAYER)
        self.assertEqual(params_of(urlopen.call_args.args[0])["totpServer"], "unavailable")

    def test_anonymous_token_means_the_cookie_is_not_signed_in(self, _time, _sleep):
        replies = [http_ok({"serverTime": NOW}), self.token_reply(isAnonymous=True)]
        with mock.patch("urllib.request.urlopen", side_effect=replies):
            with self.assertRaises(sp.LoginRefused) as raised:
                sp.fetch_token("SPDC", PLAYER)
        self.assertEqual(str(raised.exception), sp.LOGIN_REFUSED)

    def test_refused_token_request_says_to_copy_the_cookie(self, _time, _sleep):
        for code in (401, 403):
            with mock.patch("urllib.request.urlopen", side_effect=[http_ok({"serverTime": NOW}), refused(code)]):
                with self.assertRaises(sp.LoginRefused):
                    sp.fetch_token("SPDC", PLAYER)

    def test_bad_request_says_the_player_changed(self, _time, _sleep):
        with mock.patch("urllib.request.urlopen", side_effect=[http_ok({"serverTime": NOW}), refused(400)]):
            with self.assertRaises(sp.ApiError) as raised:
                sp.fetch_token("SPDC", PLAYER)
        self.assertIsInstance(raised.exception, sp.PlayerChanged)
        self.assertIn(sp.PLAYER_CHANGED, str(raised.exception))

    def test_cookie_never_appears_in_errors(self, _time, _sleep):
        failing = [http_ok({"serverTime": NOW})] + [
            urllib.error.HTTPError("url", 500, "Error", {}, io.BytesIO(b"")) for _ in range(sp.MAX_ATTEMPTS)]
        with mock.patch("urllib.request.urlopen", side_effect=failing):
            with self.assertRaises(sp.ApiError) as raised:
                sp.fetch_token("SPDC", PLAYER)
        self.assertNotIn("SPDC", str(raised.exception))


class LoginTest(unittest.TestCase):
    def login(self, value):
        with mock.patch.dict(os.environ, {"SPOTIFY_SP_DC": value}):
            return sp.load_login()

    def test_bare_value_prefixed_or_quoted(self):
        for value in ("AQB-x_1", "sp_dc=AQB-x_1", "'sp_dc=AQB-x_1'\n"):
            self.assertEqual(self.login(value), "AQB-x_1")

    def test_value_is_picked_from_a_whole_cookie_header(self):
        self.assertEqual(self.login("cookie: sp_t=T1; sp_dc=AQB-x_1; sp_key=K"), "AQB-x_1")

    def test_placeholder_or_empty_exits(self):
        for value in ("", "paste_here", "sp_t=T1; sp_key=K"):
            with self.assertRaises(SystemExit) as raised:
                self.login(value)
            self.assertIn("SPOTIFY_SP_DC", str(raised.exception.code))

    def test_unusual_characters_exit_without_echo(self):
        for value in ("AQB\nxyz", "AQB xyz"):
            with self.assertRaises(SystemExit) as raised:
                self.login(value)
            self.assertIn("SPOTIFY_SP_DC", str(raised.exception.code))
            self.assertNotIn("xyz", str(raised.exception.code))


def tid(n):
    """A track ID in Spotify's 22-character format."""
    return f"{n:022d}"


def track_data(track_id, name, artists, millis, album=""):
    """A searchTracks result in the shape the player's GraphQL returns it."""
    return {"__typename": "Track", "uri": f"spotify:track:{track_id}", "name": name,
            "artists": {"items": [{"profile": {"name": artist}} for artist in artists]},
            "albumOfTrack": {"name": album}, "duration": {"totalMilliseconds": millis}}


def search_data(*tracks):
    return {"searchV2": {"tracksV2": {"items": [{"item": {"data": t}} for t in tracks]}}}


def spotify_song(track_id, name, artist, millis, album=""):
    attributes = {"name": name, "artistName": artist, "albumName": album, "durationInMillis": millis}
    return {"id": track_id, "type": "song", "attributes": attributes}


def track(title, artist, duration="3:31", album=""):
    return {"title": title, "artist": artist, "album": album, "duration": duration}


def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def write_matches(rows):
    with open(sp.MATCHES_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=sp.MATCH_FIELDS)
        writer.writeheader()
        writer.writerows(rows.values())


OK_HEADER = ["#", "Title", "Artist", "Album", "Year", "Duration", "ID", "Availability"]
APPLE_HEADER = ["yandex_id", "title", "artist", "album", "duration", "status",
                "apple_id", "apple_title", "apple_artist", "apple_album", "apple_duration", "score"]


class FakeSpotify:
    """Answers searches from `answers`, keyed by search term, keeps Liked Songs newest first
    (liking a song again moves it to the top, as Spotify does), and records every call."""

    pause = sp.SEARCH_PAUSE

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []
        self.liked_songs = []

    def graphql(self, operation, variables):
        self.calls.append((operation, variables))
        if operation == "searchTracks":
            return search_data(*self.answers.get(variables["searchTerm"], []))
        if operation == "profileAttributes":
            return {"me": {"profile": {"name": "Test User"}}}
        if operation == "addToLibrary":
            for uri in variables["libraryItemUris"]:
                if uri in self.liked_songs:
                    self.liked_songs.remove(uri)
                self.liked_songs.insert(0, uri)
            return {"addLibraryItems": {"__typename": "AddLibraryItemsResponse"}}
        if operation == "fetchLibraryTracks":
            top = self.liked_songs[variables["offset"]:variables["offset"] + variables["limit"]]
            return {"me": {"library": {"tracks": {"items": [{"track": {"_uri": uri}} for uri in top]}}}}
        return {}

    def queries(self):
        return [variables["searchTerm"] for operation, variables in self.calls if operation == "searchTracks"]

    def liked(self):
        return [uri.removeprefix("spotify:track:") for operation, variables in self.calls
                if operation == "addToLibrary" for uri in variables["libraryItemUris"]]


class InTempDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(tmp.name)


class SearchParsingTest(unittest.TestCase):
    def test_track_becomes_a_catalog_song(self):
        found = sp.parse_search(search_data(track_data(tid(1), "Линия жизни", ["Сплин"], 183000, "25 кадр")))
        self.assertEqual(found, [spotify_song(tid(1), "Линия жизни", "Сплин", 183000, "25 кадр")])

    def test_several_artists_are_listed_together_once_each(self):
        found = sp.parse_search(search_data(track_data(tid(1), "Numb / Encore", ["Linkin Park", "JAY-Z", "JAY-Z"], 1)))
        self.assertEqual(found[0]["attributes"]["artistName"], "Linkin Park, JAY-Z")

    def test_other_result_types_and_broken_entries_are_dropped(self):
        no_uri = {**track_data(tid(1), "Song", ["Band"], 1), "uri": None}
        data = {"searchV2": {"tracksV2": {"items": [
            {"item": {"data": {"__typename": "NotFound"}}}, {"item": {"data": no_uri}}, {"item": None}, None]}}}
        self.assertEqual(sp.parse_search(data), [])
        self.assertEqual(sp.parse_search({}), [])
        self.assertEqual(sp.parse_search({"searchV2": None}), [])

    def test_unknown_duration_and_missing_names_stay_unknown(self):
        bare = {"__typename": "Track", "uri": f"spotify:track:{tid(1)}", "name": "Song", "duration": None}
        self.assertEqual(sp.parse_search(search_data(bare)), [spotify_song(tid(1), "Song", "", None)])


@mock.patch.object(sp.time, "sleep")
class ClientTest(unittest.TestCase):
    def client(self, tokens):
        fetch = mock.patch.object(sp, "fetch_token", side_effect=tokens)
        self.fetch_token = fetch.start()
        self.addCleanup(fetch.stop)
        return sp.SpotifyClient("SPDC", PLAYER)

    @mock.patch.object(sp.time, "time", return_value=NOW)
    def test_search_is_a_persisted_query_with_the_token(self, _time, sleep):
        client = self.client([("TOKEN", NOW + 3000)])
        reply = http_ok({"data": search_data(track_data(tid(1), "Линия жизни", ["Сплин"], 183000))})
        with mock.patch("urllib.request.urlopen", return_value=reply) as urlopen:
            found = sp.search(client, "Линия жизни Сплин")
        self.assertEqual([song["id"] for song in found], [tid(1)])
        sent = urlopen.call_args.args[0]
        self.assertEqual(sent.full_url, "https://api-partner.spotify.com/pathfinder/v2/query")
        self.assertEqual(sent.get_method(), "POST")
        self.assertEqual(sent.get_header("Authorization"), "Bearer TOKEN")
        self.assertEqual(sent.get_header("App-platform"), "WebPlayer")
        self.assertIsNone(sent.get_header("Cookie"))
        body = json.loads(sent.data)
        self.assertEqual(body["operationName"], "searchTracks")
        self.assertEqual(body["extensions"], {"persistedQuery": {"version": 1, "sha256Hash": HASHES["searchTracks"]}})
        self.assertEqual(body["variables"], {
            "searchTerm": "Линия жизни Сплин", "offset": 0, "limit": 10, "numberOfTopResults": 5,
            "includeAudiobooks": False, "includePreReleases": False, "includeAuthors": False})
        self.assertEqual(sleep.call_args.args[0], sp.SEARCH_PAUSE)

    def test_token_is_renewed_before_it_expires(self, _sleep):
        client = self.client([("TOKEN", NOW + 3000), ("TOKEN2", NOW + 6000)])
        with mock.patch.object(sp.time, "time", side_effect=[NOW, NOW + 3000 - 299]):
            with mock.patch("urllib.request.urlopen", return_value=http_ok({"data": {}})) as urlopen:
                client.graphql("searchTracks", {})
                client.graphql("searchTracks", {})
        self.assertEqual(self.fetch_token.call_count, 2)
        self.assertEqual([c.args[0].get_header("Authorization") for c in urlopen.call_args_list],
                         ["Bearer TOKEN", "Bearer TOKEN2"])

    @mock.patch.object(sp.time, "time", return_value=NOW)
    def test_refused_request_renews_the_token_once(self, _time, _sleep):
        client = self.client([("TOKEN", NOW + 3000), ("TOKEN2", NOW + 6000)])
        with mock.patch("urllib.request.urlopen", side_effect=[refused(401), http_ok({"data": {"x": 1}})]):
            self.assertEqual(client.graphql("searchTracks", {}), {"x": 1})
        self.assertEqual(self.fetch_token.call_count, 2)

    @mock.patch.object(sp.time, "time", return_value=NOW)
    def test_second_refusal_says_to_copy_the_cookie(self, _time, _sleep):
        client = self.client([("TOKEN", NOW + 3000), ("TOKEN2", NOW + 6000)])
        with mock.patch("urllib.request.urlopen", side_effect=[refused(401), refused(401)]):
            with self.assertRaises(sp.LoginRefused):
                client.graphql("searchTracks", {})

    @mock.patch.object(sp.time, "time", return_value=NOW)
    def test_unknown_hash_says_the_player_changed(self, _time, _sleep):
        client = self.client([("TOKEN", NOW + 3000)])
        reply = http_ok({"errors": [{"message": "PersistedQueryNotFound"}]})
        with mock.patch("urllib.request.urlopen", return_value=reply):
            with self.assertRaises(sp.ApiError) as raised:
                client.graphql("searchTracks", {})
        self.assertIsInstance(raised.exception, sp.PlayerChanged)
        self.assertIn(sp.PLAYER_CHANGED, str(raised.exception))

    @mock.patch.object(sp.time, "time", return_value=NOW)
    def test_other_graphql_errors_are_spotify_errors(self, _time, _sleep):
        client = self.client([("TOKEN", NOW + 3000)])
        with mock.patch("urllib.request.urlopen", return_value=http_ok({"errors": [{"message": "boom"}]})):
            with self.assertRaises(sp.ApiError) as raised:
                client.graphql("searchTracks", {})
        self.assertIn("boom", str(raised.exception))


class MatchTest(unittest.TestCase):
    def test_first_search_names_the_artist_and_a_match_stops(self):
        fake = FakeSpotify({"Линия жизни Сплин": [track_data(tid(1), "Линия жизни", ["Сплин"], 183000)]})
        song, _, status = sp.find_match(fake, track("Линия жизни", "Сплин", "3:02"))
        self.assertEqual((song["id"], status), (tid(1), "matched"))
        self.assertEqual(fake.queries(), ["Линия жизни Сплин"])

    def test_title_alone_is_searched_when_the_artist_finds_nothing(self):
        fake = FakeSpotify({"Линия жизни": [track_data(tid(2), "Линия жизни", ["Splean"], 183000)]})
        song, _, status = sp.find_match(fake, track("Линия жизни", "Сплин", "3:03"))
        self.assertEqual((song["id"], status), (tid(2), "matched"))
        self.assertEqual(fake.queries(), ["Линия жизни Сплин", "Линия жизни"])

    def test_first_of_several_artists_names_the_search(self):
        fake = FakeSpotify()
        sp.find_match(fake, track("Numb / Encore", "Linkin Park, JAY-Z"))
        self.assertEqual(fake.queries()[0], "Numb / Encore Linkin Park")

    def test_live_first_hit_does_not_end_the_search(self):
        fake = FakeSpotify({"Song Band": [track_data(tid(1), "Song (Live)", ["Band"], 211000)],
                            "Song": [track_data(tid(2), "Song", ["Band"], 216000)]})
        song, _, status = sp.find_match(fake, track("Song", "Band", "3:31"))
        self.assertEqual((song["id"], status), (tid(2), "matched"))
        self.assertEqual(fake.queries(), ["Song Band", "Song"])

    def test_tie_keeps_the_first_search_song(self):
        fake = FakeSpotify({"Song Band": [track_data(tid(1), "Song (Live)", ["Band"], 211000)],
                            "Song": [track_data(tid(2), "Song (Live)", ["Band"], 211000)]})
        song, _, _ = sp.find_match(fake, track("Song", "Band", "3:31"))
        self.assertEqual(song["id"], tid(1))


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
        os.makedirs(sp.SPOTIFY_DIR)
        write_matches({"10": {**dict.fromkeys(sp.MATCH_FIELDS, ""), "yandex_id": "10", "status": "skip"}})
        fake = FakeSpotify({"Shoot to Thrill AC/DC": [
            track_data(tid(11), "Shoot to Thrill", ["AC/DC"], 317000, "Back in Black")]})
        sp.cmd_match(fake)
        self.assertEqual(fake.queries(), ["Shoot to Thrill AC/DC"])
        rows = sp.load_matches()
        self.assertEqual(list(rows), ["10", "11"])
        self.assertEqual(rows["10"]["status"], "skip")
        self.assertEqual({key: rows["11"][key] for key in ("title", "status", "spotify_id", "spotify_title",
                                                         "spotify_artist", "spotify_album", "spotify_duration")},
                         {"title": "Shoot To Thrill (Made Famous By AC/DC)", "status": "matched",
                          "spotify_id": tid(11), "spotify_title": "Shoot to Thrill", "spotify_artist": "AC/DC",
                          "spotify_album": "Back in Black", "spotify_duration": "5:17"})
        with open(sp.MATCHES_PATH, encoding="utf-8-sig") as f:
            self.assertEqual(f.readline().strip(), ",".join(sp.MATCH_FIELDS))

    def failing_search(self, error):
        write_csv("liked_tracks_me.csv", OK_HEADER, [["1", "Linia", "Splean", "", "", "3:00", "10", "OK"]])
        fake = FakeSpotify()

        def graphql(operation, variables):
            raise error

        fake.graphql = graphql
        with self.assertRaises(sp.ApiError) as raised:
            sp.cmd_match(fake)
        return str(raised.exception)

    @mock.patch("builtins.print")
    def test_failed_search_names_the_track(self, _print):
        message = self.failing_search(sp.ApiError("Bad field"))
        self.assertIn("Linia", message)
        self.assertIn("Bad field", message)
        self.assertIn("'10,,,,,missing'", message)

    @mock.patch("builtins.print")
    def test_refused_login_while_searching_keeps_its_advice(self, _print):
        self.assertEqual(self.failing_search(sp.LoginRefused()), sp.LOGIN_REFUSED)

    @mock.patch("builtins.print")
    def test_changed_player_while_searching_does_not_suggest_skipping(self, _print):
        # Skipping would mark a good track missing when a rerun alone fixes it
        message = self.failing_search(sp.PlayerChanged("searchTracks"))
        self.assertIn(sp.PLAYER_CHANGED, message)
        self.assertNotIn("missing", message)


def match(track_id, status, spotify_id=""):
    return {**dict.fromkeys(sp.MATCH_FIELDS, ""), "yandex_id": track_id, "status": status,
            "spotify_id": spotify_id}


class TrackIdTest(unittest.TestCase):
    def test_id_link_and_uri_give_the_id(self):
        for value in (tid(7), f"https://open.spotify.com/track/{tid(7)}?si=abc", f"spotify:track:{tid(7)}",
                      f" https://open.spotify.com/intl-de/track/{tid(7)} "):
            self.assertEqual(sp.track_id_of(value), tid(7))

    def test_anything_else_is_no_track_id(self):
        for value in ("B07ABCDEF1", f"https://open.spotify.com/album/{tid(7)}", "", tid(7) + "x"):
            self.assertIsNone(sp.track_id_of(value))


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
        os.makedirs(sp.SPOTIFY_DIR)
        self.rows = {"10": match("10", "matched", tid(10)), "11": match("11", "matched", tid(11)),
                     "12": match("12", "matched", tid(12)), "13": match("13", "missing")}
        write_matches(self.rows)
        self.fake = FakeSpotify()

    def assert_refused(self):
        write_matches(self.rows)
        with self.assertRaises(SystemExit):
            sp.cmd_import(self.fake, apply=True)
        self.assertEqual(self.fake.calls, [])

    def test_apply_likes_playlist_only_songs_then_liked_oldest_first(self, _print):
        sp.cmd_import(self.fake, apply=True)
        self.assertEqual(self.fake.liked(), [tid(12), tid(11), tid(10)])
        self.assertEqual({operation for operation, _ in self.fake.calls},
                         {"profileAttributes", "addToLibrary", "fetchLibraryTracks"})
        self.assertEqual(sp.load_state()["liked"], [tid(12), tid(11), tid(10)])

    def test_likes_go_slower_than_searches(self, _print):
        sp.cmd_import(self.fake, apply=True)
        self.assertEqual(self.fake.pause, sp.LIKE_PAUSE)

    def test_pasted_link_in_spotify_id_is_liked_as_its_id(self, _print):
        self.rows["12"] = match("12", "matched", f"https://open.spotify.com/track/{tid(12)}?si=abc")
        write_matches(self.rows)
        sp.cmd_import(self.fake, apply=True)
        self.assertEqual(self.fake.liked()[0], tid(12))

    def test_malformed_spotify_id_blocks_apply(self, _print):
        self.rows["12"] = match("12", "matched", "B000000012")
        self.assert_refused()
        printed = " ".join(str(c.args[0]) for c in _print.call_args_list if c.args)
        self.assertIn("Row 12", printed)
        self.assertIn("not a Spotify track ID", printed)

    def test_apply_refuses_while_rows_need_a_check(self, _print):
        self.rows["13"] = match("13", "check", tid(13))
        self.assert_refused()

    def test_apply_refuses_while_tracks_are_unsearched(self, _print):
        del self.rows["13"]
        self.assert_refused()

    def test_apply_refuses_an_unknown_status(self, _print):
        self.rows["12"] = match("12", "Matched", tid(12))
        self.assert_refused()

    def test_rerun_likes_only_what_is_missing(self, _print):
        sp.save_state({"liked": [tid(12), tid(11)]})
        sp.cmd_import(self.fake, apply=True)
        self.assertEqual(self.fake.liked(), [tid(10)])
        self.assertEqual(sp.load_state()["liked"], [tid(12), tid(11), tid(10)])

    def test_order_is_checked_once_after_the_first_like(self, _print):
        sp.like_songs(self.fake, {"liked": []}, [tid(n) for n in range(1, 6)])
        operations = [operation for operation, _ in self.fake.calls]
        self.assertEqual(operations.count("fetchLibraryTracks"), 1)
        self.assertEqual(operations[:2], ["addToLibrary", "fetchLibraryTracks"])

    def test_wrong_song_on_top_stops_after_the_first_like(self, _print):
        self.fake.graphql = lambda operation, variables: (
            {"me": {"library": {"tracks": {"items": [{"track": {"_uri": f"spotify:track:{tid(99)}"}}]}}}}
            if operation == "fetchLibraryTracks" else FakeSpotify.graphql(self.fake, operation, variables))
        with self.assertRaises(sp.ApiError) as raised:
            sp.like_songs(self.fake, {"liked": []}, [tid(n) for n in range(1, 6)])
        self.assertIn("Order", str(raised.exception))
        self.assertIn("importer/SPOTIFY.md", str(raised.exception))
        self.assertEqual(self.fake.liked(), [tid(1)])
        self.assertEqual(sp.load_state()["liked"], [tid(1)])

    def test_song_liked_before_the_import_is_liked_again_in_its_place(self, _print):
        self.fake.liked_songs = [f"spotify:track:{tid(11)}"]
        sp.cmd_import(self.fake, apply=True)
        self.assertEqual(self.fake.liked_songs, [f"spotify:track:{tid(n)}" for n in (10, 11, 12)])

    def test_failed_like_names_the_track_link(self, _print):
        def graphql(operation, variables):
            if variables.get("libraryItemUris") == [f"spotify:track:{tid(11)}"]:
                raise sp.ApiError("boom")
            return FakeSpotify.graphql(self.fake, operation, variables)

        self.fake.graphql = graphql
        with self.assertRaises(sp.ApiError) as raised:
            sp.cmd_import(self.fake, apply=True)
        self.assertIn(f"https://open.spotify.com/track/{tid(11)}", str(raised.exception))
        self.assertIn("keeps the order", str(raised.exception))
        self.assertEqual(sp.load_state()["liked"], [tid(12)])

    def test_refused_login_while_liking_keeps_its_advice(self, _print):
        def graphql(operation, variables):
            if operation == "addToLibrary":
                raise sp.LoginRefused()
            return FakeSpotify.graphql(self.fake, operation, variables)

        self.fake.graphql = graphql
        with self.assertRaises(sp.ApiError) as raised:
            sp.cmd_import(self.fake, apply=True)
        self.assertEqual(str(raised.exception), sp.LOGIN_REFUSED)

    def test_like_without_a_result_stops_the_run(self, _print):
        self.fake.graphql = lambda operation, variables: (
            {} if operation == "addToLibrary" else FakeSpotify.graphql(self.fake, operation, variables))
        with self.assertRaises(sp.ApiError):
            sp.cmd_import(self.fake, apply=True)
        self.assertFalse(os.path.exists(sp.STATE_PATH))

    def test_dry_run_likes_nothing_and_writes_the_report(self, _print):
        sp.cmd_import(None, apply=False)
        self.assertFalse(os.path.exists(sp.STATE_PATH))
        report = read_report()
        self.assertEqual([(r["yandex_id"], r["status"], r["in"]) for r in report], [("13", "missing", "Mix")])

    def test_dry_run_needs_no_login(self, _print):
        with mock.patch.dict(os.environ, {"SPOTIFY_SP_DC": ""}), \
                mock.patch.object(sp, "SpotifyClient") as client, mock.patch.object(sp, "load_player") as player:
            self.assertEqual(sp.main(["spotify_import.py", "import"]), 0)
            with self.assertRaises(SystemExit):
                sp.main(["spotify_import.py", "import", "--apply"])
        client.assert_not_called()
        player.assert_not_called()


def read_report():
    with open(sp.NOT_IMPORTED_PATH, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


@mock.patch("builtins.print")
class CheckTest(unittest.TestCase):
    def test_check_names_the_account(self, printed):
        sp.cmd_check(FakeSpotify())
        printed.assert_called_with("Cookie works. Signed in to Spotify as Test User")

    def test_profile_without_a_name_means_not_signed_in(self, _print):
        fake = FakeSpotify()
        fake.graphql = lambda operation, variables: {"me": None}
        with self.assertRaises(sp.LoginRefused):
            sp.cmd_check(fake)


class RedirectTest(unittest.TestCase):
    def redirect(self, url, to):
        request = urllib.request.Request(url, headers={"Cookie": "sp_dc=SPDC"})
        return sp.SameHostRedirects().redirect_request(request, io.BytesIO(b""), 302, "Found", {}, to)

    def test_redirect_to_another_host_is_refused(self):
        for to in ("https://evil.example/api/token", "http://open.spotify.com/api/token"):
            with self.assertRaises(urllib.error.HTTPError):
                self.redirect("https://open.spotify.com/api/token", to)

    def test_redirect_on_the_same_host_is_followed(self):
        followed = self.redirect("https://open.spotify.com/", "https://open.spotify.com/intl-de/")
        self.assertEqual(followed.full_url, "https://open.spotify.com/intl-de/")

    @mock.patch("builtins.print")
    def test_main_installs_the_redirect_guard(self, _print):
        with mock.patch("urllib.request.install_opener") as install:
            sp.main(["spotify_import.py"])
        handlers = install.call_args.args[0].handlers
        self.assertTrue(any(isinstance(h, sp.SameHostRedirects) for h in handlers))


@mock.patch("builtins.print")
class MainTest(unittest.TestCase):
    def test_unknown_command_shows_the_usage(self, printed):
        self.assertEqual(sp.main(["spotify_import.py"]), 2)
        printed.assert_called_with(sp.__doc__)

    def test_spotify_errors_are_reported(self, printed):
        with mock.patch.object(sp, "load_login", return_value="SPDC"), \
                mock.patch.object(sp, "load_player", side_effect=sp.ApiError("boom")):
            self.assertEqual(sp.main(["spotify_import.py", "check"]), 1)
        printed.assert_called_with("Spotify error: boom")


if __name__ == "__main__":
    unittest.main()
