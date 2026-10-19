"""Like every song of a Yandex Music export (the CSVs written by fetch_tracks.py) in YouTube Music.

Run in the export folder:
  check            confirm the cookie works and show which account it signs in to
  match            search YouTube Music for every exported track; writes ytmusic/matches.csv
  import           show what would be liked, without changing anything
  import --apply   like every matched song, liked or in a playlist, so it shows up in Liked music

Searches need no login. check and import --apply read the music.youtube.com cookie
from YTMUSIC_COOKIE (and YTMUSIC_AUTHUSER when several Google accounts are signed in).
"""
import hashlib
import json
import re
import time
import urllib.request

from apple_import import ApiError, JsonClient

# YouTube Music has no public API; these are the calls its own web player makes,
# as documented by ytmusicapi (github.com/sigma67/ytmusicapi)
ORIGIN = "https://music.youtube.com"
API_PATH = "/youtubei/v1/"
# The web player's public API key, which ytmusicapi sends with signed-in calls
WEB_KEY = "AIzaSyC9XL3ZjWddXya6X74dJoCTL-WEYFDNX30"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
# Search filters ("params") for songs and for videos
SONGS = "EgWKAQIIAWoMEA4QChADEAQQCRAF"
VIDEOS = "EgWKAQIQAWoMEA4QChADEAQQCRAF"
# Official album tracks; music videos, live uploads and fan uploads have other types
OFFICIAL_TRACK = "MUSIC_VIDEO_TYPE_ATV"
GREYED_OUT = "MUSIC_ITEM_RENDERER_DISPLAY_POLICY_GREY_OUT"

# Seconds between requests. Likes go slower than searches: they act on a whole
# Google account, and a burst of them is more likely to look like abuse.
SEARCH_PAUSE = 1.5
LIKE_PAUSE = 3.0

DURATION = re.compile(r"\d+(?::\d\d)+")
VIDEO_TITLE_DASH = re.compile(r"\s[-–—]\s")
YTCFG = re.compile(r"ytcfg\.set\s*\(\s*({.+?})\s*\)\s*;")


# --- YouTube Music API ---

def sapisid_hash(sapisid: str, now: int) -> str:
    """The Authorization value the web player derives from the SAPISID cookie."""
    digest = hashlib.sha1(f"{now} {sapisid} {ORIGIN}".encode()).hexdigest()
    return f"SAPISIDHASH {now}_{digest}"


def sapisid_from(cookie: str) -> str | None:
    values = dict(part.strip().partition("=")[::2] for part in cookie.split(";"))
    return values.get("__Secure-3PAPISID") or values.get("SAPISID")


def visitor_id() -> str:
    """The visitor ID the web player sends along, read from the public home page."""
    request = urllib.request.Request(ORIGIN, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as resp:
        page = resp.read().decode("utf-8", errors="replace")
    found = YTCFG.search(page)
    return json.loads(found.group(1)).get("VISITOR_DATA", "") if found else ""


class YouTubeMusicClient(JsonClient):
    """Without a cookie it can only search; with one it acts as the signed-in account."""

    service = "YouTube Music"

    def __init__(self, cookie: str | None = None, auth_user: str = "0"):
        headers = {"Content-Type": "application/json", "Origin": ORIGIN, "User-Agent": USER_AGENT}
        self.sapisid = sapisid_from(cookie) if cookie else None
        if cookie:
            headers.update({"Cookie": cookie, "X-Goog-AuthUser": auth_user, "X-Origin": ORIGIN})
            visitor = visitor_id()
            if visitor:
                headers["X-Goog-Visitor-Id"] = visitor
        super().__init__(ORIGIN, headers, LIKE_PAUSE if cookie else SEARCH_PAUSE)
        version = "1." + time.strftime("%Y%m%d", time.gmtime()) + ".01.00"
        self.context = {"client": {"clientName": "WEB_REMIX", "clientVersion": version,
                                   "hl": "en", "gl": "US"}}

    def call(self, endpoint: str, body: dict) -> dict:
        params = {"alt": "json", "prettyPrint": "false"}
        if self.sapisid:
            params["key"] = WEB_KEY
            # The hash includes the time, so each request gets a fresh one
            self.headers["Authorization"] = sapisid_hash(self.sapisid, int(time.time()))
        return self.request("POST", API_PATH + endpoint, params, {**body, "context": self.context})


def dig(data, *path):
    """The value at path in nested dicts and lists, or None when a step is missing."""
    for key in path:
        try:
            data = data[key]
        except (KeyError, IndexError, TypeError):
            return None
    return data


def duration_millis(text: str) -> int | None:
    if not DURATION.fullmatch(text):
        return None
    seconds = 0
    for part in text.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds * 1000


def _column_runs(renderer: dict, index: int) -> list[dict]:
    return dig(renderer, "flexColumns", index, "musicResponsiveListItemFlexColumnRenderer",
               "text", "runs") or []


def _run_groups(runs: list[dict]) -> list[list[dict]]:
    """Runs split at the ' • ' separators: artists, then album or views, then duration."""
    groups = [[]]
    for r in runs:
        if r["text"] == " • ":
            groups.append([])
        else:
            groups[-1].append(r)
    return groups


def as_song(renderer: dict) -> dict | None:
    """A search result in the catalog-song shape apple_import's matching reads."""
    if renderer.get("musicItemRendererDisplayPolicy") == GREYED_OUT:
        return None
    watch = dig(renderer, "overlay", "musicItemThumbnailOverlayRenderer", "content",
                "musicPlayButtonRenderer", "playNavigationEndpoint", "watchEndpoint") or {}
    if not watch.get("videoId"):
        return None
    video_type = dig(watch, "watchEndpointMusicSupportedConfigs", "watchEndpointMusicConfig",
                     "musicVideoType")
    kind = "song" if video_type == OFFICIAL_TRACK else "video"
    title = "".join(r["text"] for r in _column_runs(renderer, 0)).strip()
    groups = _run_groups(_column_runs(renderer, 1))
    artist = "".join(r["text"] for r in groups[0]).strip()
    album = next((r["text"] for group in groups for r in group
                  if (dig(r, "navigationEndpoint", "browseEndpoint", "browseId") or "").startswith("MPRE")), "")
    millis = next((duration_millis(g[0]["text"]) for g in reversed(groups)
                   if len(g) == 1 and duration_millis(g[0]["text"])), None)
    if kind == "video" and VIDEO_TITLE_DASH.search(title):
        # Uploads are usually titled "Artist - Title" and credited to the uploader's channel
        artist, title = (part.strip() for part in VIDEO_TITLE_DASH.split(title, maxsplit=1))
    attributes = {"name": title, "artistName": artist, "albumName": album, "durationInMillis": millis}
    return {"id": watch["videoId"], "type": kind, "attributes": attributes}


def parse_search(response: dict) -> list[dict]:
    tabs = dig(response, "contents", "tabbedSearchResultsRenderer", "tabs") or []
    sections = dig(tabs, 0, "tabRenderer", "content", "sectionListRenderer", "contents") or []
    songs = []
    for section in sections:
        for entry in dig(section, "musicShelfRenderer", "contents") or []:
            song = as_song(entry.get("musicResponsiveListItemRenderer", {}))
            if song:
                songs.append(song)
    return songs


def search(client: YouTubeMusicClient, query: str, kind: str) -> list[dict]:
    """Songs (kind=SONGS) or videos (kind=VIDEOS) YouTube Music finds for the query."""
    return parse_search(client.call("search", {"query": query, "params": kind}))
