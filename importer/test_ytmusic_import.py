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
        return {}

    def queries(self):
        return [body["query"] for endpoint, body in self.calls if endpoint == "search"]


class InTempDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(tmp.name)


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


if __name__ == "__main__":
    unittest.main()
