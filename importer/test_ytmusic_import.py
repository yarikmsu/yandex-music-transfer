import csv
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

import ytmusic_import as yi

DOT = {"text": " • "}
OK_HEADER = ["#", "Title", "Artist", "Album", "Year", "Duration", "ID", "Availability"]


def run(text, browse_id=None):
    if browse_id is None:
        return {"text": text}
    return {"text": text, "navigationEndpoint": {"browseEndpoint": {"browseId": browse_id}}}


def meta(*groups):
    """Second-column runs: groups of runs joined by ' • ', a plain string being one run."""
    runs = []
    for group in groups:
        if runs:
            runs.append(DOT)
        runs.extend([run(group)] if isinstance(group, str) else group)
    return runs


def item(title, runs, video_id, video_type=yi.OFFICIAL_TRACK, policy=None):
    """A search result in the shape music.youtube.com returns it."""
    watch = {"videoId": video_id, "watchEndpointMusicSupportedConfigs": {
        "watchEndpointMusicConfig": {"musicVideoType": video_type}}}
    renderer = {
        "flexColumns": [
            {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [run(title)]}}},
            {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": runs}}},
        ],
        "overlay": {"musicItemThumbnailOverlayRenderer": {"content": {"musicPlayButtonRenderer": {
            "playNavigationEndpoint": {"watchEndpoint": watch}}}}},
    }
    if policy:
        renderer["musicItemRendererDisplayPolicy"] = policy
    return {"musicResponsiveListItemRenderer": renderer}


def response(*items, sections=None):
    shelves = sections if sections is not None else [{"musicShelfRenderer": {"contents": list(items)}}]
    return {"contents": {"tabbedSearchResultsRenderer": {"tabs": [{"tabRenderer": {"content": {
        "sectionListRenderer": {"contents": shelves}}}}]}}}


def yt_song(video_id, name, artist, millis, album="", kind="song"):
    attributes = {"name": name, "artistName": artist, "albumName": album, "durationInMillis": millis}
    return {"id": video_id, "type": kind, "attributes": attributes}


def http_ok(payload):
    ok = mock.MagicMock()
    ok.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return ok


def track(title, artist, duration="3:31", album=""):
    return {"title": title, "artist": artist, "album": album, "duration": duration}


def song_item(title, artist, duration, video_id, video_type=yi.OFFICIAL_TRACK):
    return item(title, meta([run(artist, "UC1")], duration), video_id, video_type)


def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


class FakeYouTube:
    """Answers searches from `answers`, keyed by (query, filter), and records every call."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def call(self, endpoint, body):
        self.calls.append((endpoint, body))
        if endpoint == "search":
            return response(*self.answers.get((body["query"], body["params"]), []))
        if endpoint == "account/account_menu":
            return menu("Test User")
        return {}

    def queries(self):
        return [body["query"] for endpoint, body in self.calls if endpoint == "search"]


class InTempDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(tmp.name)


def menu(name, handle=None):
    """The account menu of a signed-in session."""
    header = {"accountName": {"runs": [{"text": name}]}}
    if handle:
        header["channelHandle"] = {"runs": [{"text": handle}]}
    return {"actions": [{"openPopupAction": {"popup": {"multiPageMenuRenderer": {
        "header": {"activeAccountHeaderRenderer": header}}}}}]}


def vid(n):
    """A video ID in YouTube's 11-character format."""
    return f"video{n:06d}"


def match(track_id, status, video_id=""):
    return {**dict.fromkeys(yi.MATCH_FIELDS, ""), "yandex_id": track_id, "status": status,
            "video_id": video_id}


def apple_row(title, artist, duration, status="matched"):
    return {"status": status, "apple_title": title, "apple_artist": artist,
            "apple_album": "", "apple_duration": duration}


class SearchParsingTest(unittest.TestCase):
    def test_song_result_becomes_a_catalog_song(self):
        found = yi.parse_search(response(item(
            "Линия жизни",
            meta([run("Сплин", "UClifG1f6mAY0Oe0keoumCRA")], [run("25 кадр", "MPREb_9Zv3wvL9ZkP")], "3:03"),
            "crhhkg7vHoM")))
        self.assertEqual(found, [yt_song("crhhkg7vHoM", "Линия жизни", "Сплин", 183000, "25 кадр")])

    def test_several_artists_are_kept_together(self):
        artists = [run("Linkin Park", "UC1"), run(" & "), run("JAY-Z", "UC2")]
        found = yi.parse_search(response(item(
            "Numb / Encore", meta(artists, [run("Collision Course", "MPREb_1")], "3:29"), "v1")))
        self.assertEqual(found[0]["attributes"]["artistName"], "Linkin Park & JAY-Z")

    def test_video_title_with_dash_names_the_artist(self):
        found = yi.parse_search(response(item(
            "Сплин - Линия Жизни (2001 Год)",
            meta([run("Chelovek Prosto", "UCjKU03CSeVHr4zEUi5TFEUg")], "262 views", "3:07"),
            "tC7sPvrUzMY", video_type="MUSIC_VIDEO_TYPE_UGC")))
        self.assertEqual(found, [yt_song("tC7sPvrUzMY", "Линия Жизни (2001 Год)", "Сплин", 187000,
                                         kind="video")])

    def test_video_without_dash_is_credited_to_its_channel(self):
        found = yi.parse_search(response(item(
            "Wonderwall", meta([run("Oasis", "UC3")], "386M views", "4:38"), "bx1",
            video_type="MUSIC_VIDEO_TYPE_OMV")))
        self.assertEqual((found[0]["type"], found[0]["attributes"]["artistName"]), ("video", "Oasis"))

    def test_greyed_out_results_are_dropped(self):
        found = yi.parse_search(response(item(
            "Gone", meta("Band", "3:00"), "v1", policy="MUSIC_ITEM_RENDERER_DISPLAY_POLICY_GREY_OUT")))
        self.assertEqual(found, [])

    def test_empty_response_gives_no_songs(self):
        self.assertEqual(yi.parse_search({}), [])
        no_results = response(sections=[{"itemSectionRenderer": {"contents": []}}])
        self.assertEqual(yi.parse_search(no_results), [])

    def test_hour_long_duration(self):
        self.assertEqual(yi.duration_millis("1:02:03"), 3723000)
        self.assertIsNone(yi.duration_millis("21M plays"))


class ClientTest(unittest.TestCase):
    def test_sapisid_hash_matches_known_value(self):
        digest = hashlib.sha1(b"1700000000 abc https://music.youtube.com").hexdigest()
        self.assertEqual(yi.sapisid_hash("abc", 1700000000), f"SAPISIDHASH 1700000000_{digest}")

    @mock.patch.object(yi.time, "sleep")
    def test_search_sends_no_cookie(self, sleep):
        client = yi.YouTubeMusicClient()
        with mock.patch.object(yi.urllib.request, "urlopen", return_value=http_ok({})) as urlopen:
            self.assertEqual(yi.search(client, "Линия жизни Сплин", yi.SONGS), [])
        sent = urlopen.call_args.args[0]
        self.assertIsNone(sent.get_header("Cookie"))
        self.assertIsNone(sent.get_header("Authorization"))
        self.assertNotIn("key=", sent.full_url)
        body = json.loads(sent.data)
        self.assertEqual((body["query"], body["params"]), ("Линия жизни Сплин", yi.SONGS))
        self.assertEqual(body["context"]["client"]["clientName"], "WEB_REMIX")
        self.assertEqual(sleep.call_args.args[0], yi.SEARCH_PAUSE)

    @mock.patch.object(yi.time, "sleep")
    @mock.patch.object(yi.time, "time", return_value=1700000000)
    @mock.patch.object(yi, "visitor_id", return_value="visitor")
    def test_logged_in_call_signs_each_request(self, _visitor, _time, _sleep):
        cookie = "SAPISID=s; __Secure-3PAPISID=x; SID=secret"
        client = yi.YouTubeMusicClient(cookie, "0")
        with mock.patch.object(yi.urllib.request, "urlopen", return_value=http_ok({})) as urlopen:
            client.call("like/like", {"target": {"videoId": "v1"}})
        sent = urlopen.call_args.args[0]
        self.assertEqual(sent.get_header("Authorization"), yi.sapisid_hash("x", 1700000000))
        self.assertEqual(sent.get_header("Cookie"), cookie)
        self.assertEqual(sent.get_header("X-goog-visitor-id"), "visitor")
        self.assertIn("key=" + yi.WEB_KEY, sent.full_url)
        self.assertTrue(sent.full_url.startswith("https://music.youtube.com/youtubei/v1/like/like?"))
        self.assertEqual(client.pause, yi.LIKE_PAUSE)

    def test_visitor_id_is_read_from_the_home_page(self):
        page = mock.MagicMock()
        page.__enter__.return_value.read.return_value = (
            b'<script>ytcfg.set({"VISITOR_DATA": "Cgt2aXNpdG9y", "X": 1});</script>')
        with mock.patch.object(yi.urllib.request, "urlopen", return_value=page) as urlopen:
            self.assertEqual(yi.visitor_id(), "Cgt2aXNpdG9y")
        self.assertIsNone(urlopen.call_args.args[0].get_header("Cookie"))


class MatchTest(unittest.TestCase):
    def test_official_track_with_same_length_is_matched_from_the_first_search(self):
        fake = FakeYouTube({("Линия жизни Сплин", yi.SONGS): [
            song_item("Линия жизни", "Сплин", "3:03", "crhhkg7vHoM")]})
        song, _, status = yi.find_match(fake, track("Линия жизни", "Сплин", "3:02"))
        self.assertEqual((song["id"], status), ("crhhkg7vHoM", "matched"))
        self.assertEqual(fake.queries(), ["Линия жизни Сплин"])

    def test_video_is_never_more_than_check(self):
        fake = FakeYouTube({("Линия жизни Сплин", yi.VIDEOS): [
            song_item("Сплин - Линия жизни", "Fan Channel", "3:02", "ugc1", "MUSIC_VIDEO_TYPE_UGC")]})
        exported = track("Линия жизни", "Сплин", "3:02")
        row = yi.match_row("10", exported, yi.find_match(fake, exported))
        self.assertEqual((row["status"], row["youtube_type"], row["video_id"]), ("check", "video", "ugc1"))
        self.assertEqual(len(fake.queries()), 3)

    def test_karaoke_copy_credited_to_the_original_artist_is_only_check(self):
        copy = yt_song(vid(1), "San Francisco", "Scott McKenzie (Karaoke)", 174000,
                       "Zoom Karaoke Golden Years 1967")
        exported = track("San Francisco", "Scott McKenzie", "2:53")
        self.assertEqual(yi.best_of(exported, [copy])[2], "check")
        original = yt_song(vid(2), "San Francisco", "Scott McKenzie", 178000, "The Voice of Scott McKenzie")
        self.assertEqual(yi.best_of(exported, [copy, original])[0]["id"], vid(2))

    def test_tribute_copy_is_only_check(self):
        copy = yt_song(vid(1), "St. Anger (As Made Famous By Metallica)", "Metallica", 440000)
        self.assertEqual(yi.best_of(track("St. Anger", "Metallica", "7:21"), [copy])[2], "check")

    def test_exported_karaoke_copy_can_match_itself(self):
        exported = track("Shoot To Thrill (Made Famous By AC/DC)", "Iron Man", "5:20")
        copy = yt_song(vid(1), "Shoot To Thrill (Made Famous By AC/DC)", "Iron Man", 320000)
        self.assertEqual(yi.best_of(exported, [copy])[2], "matched")

    def test_apple_review_that_kept_a_tribute_copy_is_ignored(self):
        exported = track("St. Anger", "Metallica", "7:21")
        tribute = apple_row("St. Anger (as made famous by Metallica)", "Metal Militia", "7:19")
        self.assertIs(yi.reference(exported, tribute), exported)
        exported = track("San Francisco", "Scott McKenzie", "2:53")
        karaoke = apple_row("San Francisco (Be Sure To Wear Some Flowers In Your Hair)",
                            "Scott McKenzie (Karaoke)", "2:54")
        self.assertIs(yi.reference(exported, karaoke), exported)

    def test_reviewed_apple_original_is_searched_instead_of_the_karaoke_copy(self):
        exported = track("Shoot To Thrill (Made Famous By AC/DC)", "Iron Man", "5:20")
        reference = yi.reference(exported, apple_row("Shoot to Thrill", "AC/DC", "5:17"))
        fake = FakeYouTube({("Shoot to Thrill AC/DC", yi.SONGS): [
            song_item("Shoot to Thrill", "AC/DC", "5:18", "orig")]})
        song, _, status = yi.find_match(fake, reference)
        self.assertEqual(fake.queries()[0], "Shoot to Thrill AC/DC")
        self.assertEqual((song["id"], status), ("orig", "matched"))

    def test_same_names_on_apple_keep_the_export_reference(self):
        exported = track("Линия жизни", "Сплин", "3:02")
        self.assertIs(yi.reference(exported, apple_row("Линия жизни", "Сплин", "3:03")), exported)

    def test_apple_row_without_names_keeps_the_export_reference(self):
        exported = track("Shoot To Thrill (Made Famous By AC/DC)", "Iron Man")
        self.assertIs(yi.reference(exported, apple_row("", "", "")), exported)

    def test_unreviewed_apple_row_is_ignored(self):
        exported = track("Shoot To Thrill (Made Famous By AC/DC)", "Iron Man")
        row = apple_row("Shoot to Thrill", "AC/DC", "5:17", status="check")
        self.assertIs(yi.reference(exported, row), exported)
        self.assertIs(yi.reference(exported, None), exported)

    def test_nothing_found_is_missing(self):
        fake = FakeYouTube()
        row = yi.match_row("10", track("Unknown", "Nobody"), yi.find_match(fake, track("Unknown", "Nobody")))
        self.assertEqual((row["status"], row["video_id"]), ("missing", ""))
        self.assertEqual(row["title"], "Unknown")


class MatchCommandTest(InTempDir):
    @mock.patch("builtins.print")
    def test_match_resumes_without_searching_done_tracks(self, _print):
        write_csv("liked_tracks_me.csv", OK_HEADER, [
            ["1", "Done", "A", "", "", "3:00", "10", "OK"],
            ["2", "Linia", "B", "", "", "3:00", "11", "OK"],
        ])
        os.makedirs(yi.YT_DIR)
        yi.write_matches({"10": {**dict.fromkeys(yi.MATCH_FIELDS, ""), "yandex_id": "10", "status": "skip"}})
        fake = FakeYouTube({("Linia B", yi.SONGS): [song_item("Linia", "B", "3:00", "v11")]})
        yi.cmd_match(fake)
        self.assertEqual(fake.queries(), ["Linia B"])
        rows = yi.load_matches()
        self.assertEqual(list(rows), ["10", "11"])
        self.assertEqual((rows["10"]["status"], rows["11"]["video_id"]), ("skip", "v11"))


class LikeOrderTest(unittest.TestCase):
    def test_newest_like_ends_last_and_playlist_only_songs_come_first(self):
        # liked_tracks lists the newest like first; "10" is also in the playlist
        order = yi.like_order(["10", "11"], [("Mix", ["12", "10"])],
                              {"10": "v10", "11": "v11", "12": "v12"})
        self.assertEqual(order, ["v12", "v11", "v10"])

    def test_video_matched_twice_is_liked_at_its_latest_position(self):
        order = yi.like_order(["10", "11", "12"], [("Mix", ["13"])],
                              {"10": "vA", "11": "vB", "12": "vA", "13": "vA"})
        self.assertEqual(order, ["vB", "vA"])

    def test_unmatched_tracks_are_left_out(self):
        self.assertEqual(yi.like_order(["10", "11"], [], {"11": "v11"}), ["v11"])


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
        os.makedirs(yi.YT_DIR)
        self.rows = {"10": match("10", "matched", vid(10)), "11": match("11", "matched", vid(11)),
                     "12": match("12", "matched", vid(12)), "13": match("13", "missing")}
        yi.write_matches(self.rows)
        self.fake = FakeYouTube()

    def liked(self):
        return [body["target"]["videoId"] for endpoint, body in self.fake.calls if endpoint == "like/like"]

    def test_apply_likes_in_order_and_only_ever_calls_like(self, _print):
        yi.cmd_import(self.fake, apply=True)
        self.assertEqual(self.liked(), [vid(12), vid(11), vid(10)])
        self.assertEqual({endpoint for endpoint, _ in self.fake.calls}, {"account/account_menu", "like/like"})
        self.assertEqual(yi.load_state()["liked"], [vid(12), vid(11), vid(10)])

    def test_apply_refuses_while_rows_need_a_check(self, _print):
        self.rows["13"] = match("13", "check", vid(13))
        yi.write_matches(self.rows)
        with self.assertRaises(SystemExit):
            yi.cmd_import(self.fake, apply=True)
        self.assertEqual(self.fake.calls, [])

    def test_apply_refuses_while_tracks_are_unsearched(self, _print):
        del self.rows["13"]
        yi.write_matches(self.rows)
        with self.assertRaises(SystemExit):
            yi.cmd_import(self.fake, apply=True)
        self.assertEqual(self.fake.calls, [])

    def test_apply_refuses_an_unknown_status(self, _print):
        self.rows["12"] = match("12", "Matched", vid(12))
        yi.write_matches(self.rows)
        with self.assertRaises(SystemExit):
            yi.cmd_import(self.fake, apply=True)
        self.assertEqual(self.fake.calls, [])

    def test_apply_refuses_a_matched_row_without_a_valid_video_id(self, _print):
        for bad in ("", "https://music.youtube.com/watch?v=v12v12v12v1"):
            self.rows["12"] = match("12", "matched", bad)
            yi.write_matches(self.rows)
            with self.assertRaises(SystemExit):
                yi.cmd_import(self.fake, apply=True)
        self.assertEqual(self.fake.calls, [])

    def test_rerun_continues_after_the_last_like(self, _print):
        yi.save_state({"liked": [vid(12)]})
        yi.cmd_import(self.fake, apply=True)
        self.assertEqual(self.liked(), [vid(11), vid(10)])
        self.assertEqual(yi.load_state()["liked"], [vid(12), vid(11), vid(10)])

    def test_failed_like_stops_the_run_and_keeps_progress(self, _print):
        def call(endpoint, body):
            if body.get("target", {}).get("videoId") == vid(11):
                raise yi.ApiError("like/like: HTTP 400")
            return FakeYouTube.call(self.fake, endpoint, body)

        self.fake.call = call
        with self.assertRaises(yi.ApiError):
            yi.cmd_import(self.fake, apply=True)
        self.assertEqual(yi.load_state()["liked"], [vid(12)])

    def test_dry_run_likes_nothing_and_writes_the_report(self, _print):
        yi.cmd_import(None, apply=False)
        self.assertFalse(os.path.exists(yi.STATE_PATH))
        report = yi.read_csv(yi.NOT_IMPORTED_PATH)
        self.assertEqual([(r["yandex_id"], r["status"], r["in"]) for r in report], [("13", "missing", "Mix")])

    def test_report_names_every_place_a_track_is_in(self, _print):
        self.rows["10"] = match("10", "skip")
        yi.write_matches(self.rows)
        yi.cmd_import(None, apply=False)
        report = {r["yandex_id"]: r["in"] for r in yi.read_csv(yi.NOT_IMPORTED_PATH)}
        self.assertEqual(report["10"], "liked, Mix")


class LoginTest(unittest.TestCase):
    def test_cookie_without_sapisid_is_rejected_without_echoing_it(self):
        with mock.patch.dict(os.environ, {"YTMUSIC_COOKIE": "SID=secretvalue"}):
            with self.assertRaises(SystemExit) as raised:
                yi.load_login()
        self.assertNotIn("secretvalue", str(raised.exception.code))

    def test_cookie_with_a_line_break_is_rejected_without_echoing_it(self):
        with mock.patch.dict(os.environ, {"YTMUSIC_COOKIE": "SAPISID=s;\rSID=secretvalue"}):
            with self.assertRaises(SystemExit) as raised:
                yi.load_login()
        self.assertNotIn("secretvalue", str(raised.exception.code))

    def test_placeholder_cookie_is_rejected(self):
        with mock.patch.dict(os.environ, {"YTMUSIC_COOKIE": "paste_here"}), self.assertRaises(SystemExit):
            yi.load_login()

    def test_copied_header_name_and_default_account_are_accepted(self):
        env = {"YTMUSIC_COOKIE": "cookie: SAPISID=s; SID=x"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("YTMUSIC_AUTHUSER", None)
            self.assertEqual(yi.load_login(), ("SAPISID=s; SID=x", "0"))

    def test_account_name_is_read_from_the_menu(self):
        client = mock.Mock()
        client.call.return_value = menu("Test User", "@testuser")
        self.assertEqual(yi.account_name(client), "Test User (@testuser)")
        self.assertEqual(client.call.call_args.args[0], "account/account_menu")

    def test_signed_out_menu_raises(self):
        client = mock.Mock()
        client.call.return_value = {}
        with self.assertRaises(yi.ApiError):
            yi.account_name(client)


if __name__ == "__main__":
    unittest.main()
