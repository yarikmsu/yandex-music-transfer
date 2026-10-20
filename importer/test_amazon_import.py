import base64
import io
import json
import os
import unittest
import urllib.error
from unittest import mock

import amazon_import as am

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
        with mock.patch.object(am.urllib.request, "urlopen", return_value=reply) as urlopen:
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
        with mock.patch.object(am.urllib.request, "urlopen", return_value=reply):
            with self.assertRaises(am.ApiError) as raised:
                client.graphql(am.SEARCH_QUERY, {"query": "x"})
        self.assertIn("Bad field", str(raised.exception))

    @mock.patch.object(am.time, "sleep")
    def test_auth_error_says_to_copy_a_fresh_login(self, _sleep):
        client = am.AmazonMusicClient(auth_value())
        reply = http_ok({"data": None, "errors": [{"message": "x", "extensions": {"code": "AUTH_TOKEN_EXPIRED"}}]})
        with mock.patch.object(am.urllib.request, "urlopen", return_value=reply):
            with self.assertRaises(am.ApiError) as raised:
                client.graphql(am.SEARCH_QUERY, {"query": "x"})
        self.assertEqual(str(raised.exception), am.LOGIN_EXPIRED)

    @mock.patch.object(am.time, "sleep")
    def test_refused_request_says_to_copy_a_fresh_login(self, _sleep):
        client = am.AmazonMusicClient(auth_value())
        refused = urllib.error.HTTPError("url", 401, "Unauthorized", {}, io.BytesIO(b'{"errors": []}'))
        with mock.patch.object(am.urllib.request, "urlopen", side_effect=[refused]):
            with self.assertRaises(am.ApiError) as raised:
                client.graphql(am.SEARCH_QUERY, {"query": "x"})
        self.assertEqual(str(raised.exception), am.LOGIN_EXPIRED)


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
