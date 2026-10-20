import io
import json
import os
import unittest
import urllib.error
import urllib.parse
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
        self.assertNotIsInstance(raised.exception, sp.LoginRefused)
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


if __name__ == "__main__":
    unittest.main()
