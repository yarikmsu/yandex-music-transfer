"""Like every song of a Yandex Music export (the CSVs written by fetch_tracks.py) in YouTube Music.

Run in the export folder:
  check            confirm the cookie works and show which account it signs in to
  match            search YouTube Music for every exported track; writes ytmusic/matches.csv
  import           show what would be liked, without changing anything
  import --apply   like every matched song, liked or in a playlist, so it shows up in Liked music

Searches need no login. check and import --apply read the music.youtube.com cookie
from YTMUSIC_COOKIE (and YTMUSIC_AUTHUSER when several Google accounts are signed in).
"""
import collections
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.request

from apple_import import (
    STATUS_RANK, ApiError, JsonClient, artist_similarity, classify, format_duration,
    load_collections, load_tracks, print_status_summary, progress, read_csv, score_candidate,
    similarity,
)

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

YT_DIR = "ytmusic"
MATCHES_PATH = os.path.join(YT_DIR, "matches.csv")
STATE_PATH = os.path.join(YT_DIR, "state.json")
NOT_IMPORTED_PATH = os.path.join(YT_DIR, "not_imported.csv")
# The reviewed Apple Music matches, when that import was done first
APPLE_MATCHES_PATH = os.path.join("apple", "matches.csv")
MATCH_FIELDS = [
    "yandex_id", "title", "artist", "album", "duration", "status", "video_id", "youtube_type",
    "youtube_title", "youtube_artist", "youtube_album", "youtube_duration", "score",
]
NOT_IMPORTED_FIELDS = [
    "status", "title", "artist", "duration", "in",
    "closest_title", "closest_artist", "closest_duration", "video_id", "yandex_id",
]
COOKIE_PLACEHOLDER = "paste_here"
ACCOUNT_HEADER = ("actions", 0, "openPopupAction", "popup", "multiPageMenuRenderer", "header",
                  "activeAccountHeaderRenderer")

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


# --- Matching ---

def reference(track: dict, apple_row: dict | None) -> dict:
    """What to look for: the reviewed Apple song where the review changed the names.

    There the user (or the Apple importer's cover pass) chose another recording or
    spelling, such as the original for a karaoke copy, so that choice carries over.
    """
    if not apple_row or apple_row["status"] != "matched":
        return track
    renamed = (artist_similarity(track["artist"], apple_row["apple_artist"]) < 0.8
               or similarity(track["title"], apple_row["apple_title"]) < 0.85)
    if not renamed:
        return track
    return {"title": apple_row["apple_title"], "artist": apple_row["apple_artist"],
            "album": apple_row["apple_album"], "duration": apple_row["apple_duration"]}


def status_of(scores: dict, song: dict) -> str:
    # A video is never imported unreviewed, however well it matches
    status = classify(scores)
    return "check" if status == "matched" and song["type"] != "song" else status


def _rank(best: tuple[dict, dict, str]) -> tuple[int, float]:
    return STATUS_RANK[best[2]], best[1]["total"]


def best_of(reference: dict, candidates: list[dict]) -> tuple[dict, dict, str] | None:
    scored = []
    for song in candidates:
        scores = score_candidate(reference, song)
        scored.append((song, scores, status_of(scores, song)))
    return max(scored, key=_rank, default=None)


def find_match(client: YouTubeMusicClient, reference: dict) -> tuple[dict, dict, str] | None:
    """Songs by title and artist, then by title alone, then videos; stops at a match."""
    with_artist = f"{reference['title']} {reference['artist'].split(', ')[0]}"
    best = None
    for query, kind in ((with_artist, SONGS), (reference["title"], SONGS), (with_artist, VIDEOS)):
        found = [b for b in (best, best_of(reference, search(client, query, kind))) if b]
        best = max(found, key=_rank, default=None)
        if best and best[2] == "matched":
            break
    return best


def match_row(track_id: str, track: dict, best) -> dict:
    row = dict.fromkeys(MATCH_FIELDS, "")
    row.update(yandex_id=track_id, status="missing", **track)
    if best is None:
        return row
    song, scores, status = best
    attrs = song["attributes"]
    row.update(
        status=status,
        video_id=song["id"],
        youtube_type=song["type"],
        youtube_title=attrs["name"],
        youtube_artist=attrs["artistName"],
        youtube_album=attrs["albumName"],
        youtube_duration=format_duration(attrs["durationInMillis"]),
        score=f"{scores['total']:.2f}",
    )
    return row


# --- Local files ---

def load_apple_matches() -> dict[str, dict]:
    if not os.path.exists(APPLE_MATCHES_PATH):
        return {}
    return {row["yandex_id"]: row for row in read_csv(APPLE_MATCHES_PATH)}


def load_matches() -> dict[str, dict]:
    if not os.path.exists(MATCHES_PATH):
        return {}
    return {row["yandex_id"]: row for row in read_csv(MATCHES_PATH)}


def write_matches(rows: dict[str, dict]) -> None:
    tmp_path = MATCHES_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MATCH_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows.values())
    os.replace(tmp_path, MATCHES_PATH)


def write_not_imported(matches: dict, video_ids: dict, liked: list[str], playlists: list) -> None:
    """Every track that will not be liked, where it was, and the closest YouTube candidate."""
    places = collections.defaultdict(list)
    for track_id in liked:
        places[track_id].append("liked")
    for name, track_ids in playlists:
        for track_id in track_ids:
            places[track_id].append(name)
    rows = [{
        "status": row["status"], "title": row["title"], "artist": row["artist"],
        "duration": row["duration"], "in": ", ".join(dict.fromkeys(places[track_id])),
        "closest_title": row["youtube_title"], "closest_artist": row["youtube_artist"],
        "closest_duration": row["youtube_duration"], "video_id": row["video_id"],
        "yandex_id": track_id,
    } for track_id, row in matches.items() if track_id not in video_ids]
    tmp_path = NOT_IMPORTED_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=NOT_IMPORTED_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, NOT_IMPORTED_PATH)


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"liked": []}
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp_path, STATE_PATH)


def like_order(liked: list[str], playlists: list[tuple[str, list[str]]],
               video_ids: dict[str, str]) -> list[str]:
    """Video IDs in the order to like them.

    Liked music lists the most recent like first, so tracks only in playlists go
    first and liked tracks follow oldest first (the export lists the newest first).
    A video that several tracks matched is liked once, at its last position.
    """
    liked_set = set(liked)
    playlist_only = [t for _, track_ids in playlists for t in track_ids if t not in liked_set]
    order = [video_ids[t] for t in playlist_only + list(reversed(liked)) if t in video_ids]
    return list(reversed(dict.fromkeys(reversed(order))))


# --- Commands ---

def cmd_match(client: YouTubeMusicClient) -> None:
    tracks = load_tracks()
    done = load_matches()
    search_tracks(client, tracks, load_apple_matches(), done)
    print_status_summary(done.values())


def search_tracks(client: YouTubeMusicClient, tracks: dict, apple: dict, done: dict) -> None:
    """Search every track not yet in matches.csv, adding rows to it and to done."""
    todo = [(track_id, track) for track_id, track in tracks.items() if track_id not in done]
    print(f"{len(tracks)} tracks, {len(todo)} left to search")
    os.makedirs(YT_DIR, exist_ok=True)
    write_header = not os.path.exists(MATCHES_PATH)
    started = time.monotonic()
    # Appending row by row means an interrupted run resumes where it stopped
    with open(MATCHES_PATH, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MATCH_FIELDS)
        if write_header:
            writer.writeheader()
        for i, (track_id, track) in enumerate(todo, 1):
            best = find_match(client, reference(track, apple.get(track_id)))
            done[track_id] = match_row(track_id, track, best)
            writer.writerow(done[track_id])
            f.flush()
            if i % 25 == 0 or i == len(todo):
                print(progress(i, len(todo), started, "searched"))


def account_name(client: YouTubeMusicClient) -> str:
    header = dig(client.call("account/account_menu", {}), *ACCOUNT_HEADER)
    name = dig(header, "accountName", "runs", 0, "text")
    if not name:
        raise ApiError("the cookie is not signed in; copy it again from a signed-in "
                       "music.youtube.com tab")
    handle = dig(header, "channelHandle", "runs", 0, "text")
    return f"{name} ({handle})" if handle else name


def cmd_check(client: YouTubeMusicClient) -> None:
    print(f"Cookie works. Signed in to YouTube Music as {account_name(client)}")


def like_songs(client: YouTubeMusicClient, state: dict, songs: list[str]) -> None:
    done = set(state["liked"])
    pending = [s for s in songs if s not in done]
    if state["liked"] and pending:
        print(f"  {len(pending)} songs are new since the last import; "
              "they will appear above the songs liked before.")
    started = time.monotonic()
    for i, video_id in enumerate(pending, 1):
        client.call("like/like", {"target": {"videoId": video_id}})
        state["liked"].append(video_id)
        save_state(state)
        if i % 50 == 0 or i == len(pending):
            print(progress(i, len(pending), started, "liked"))
    print(f"  Liked music: {len(state['liked'])} songs liked by this importer")


def cmd_import(client: YouTubeMusicClient | None, apply: bool) -> None:
    matches = load_matches()
    if not matches:
        sys.exit("Run 'match' first.")
    liked, playlists = load_collections()
    video_ids = {track_id: row["video_id"] for track_id, row in matches.items()
                 if row["status"] == "matched" and row["video_id"]}
    songs = like_order(liked, playlists, video_ids)
    write_not_imported(matches, video_ids, liked, playlists)
    unsearched = sum(track_id not in matches for track_id in load_tracks())
    to_check = sum(row["status"] == "check" for row in matches.values())
    print(f"Liked music: {len(songs)} songs from {len(liked)} liked tracks "
          f"and {len(playlists)} playlists")
    print(f"Not liked: {len(matches) - len(video_ids)} tracks, listed in {NOT_IMPORTED_PATH}")
    if unsearched:
        print(f"{unsearched} tracks are not searched yet: run 'match' again.")
    if to_check:
        print(f"{to_check} rows in {MATCHES_PATH} are still 'check': change each to "
              "matched, missing or skip.")
    if not apply:
        print("Nothing changed. Run 'import --apply' to like these songs.")
        return
    if unsearched or to_check:
        # Likes cannot be reordered later, so every track needs its decision first
        sys.exit("Nothing liked: settle the tracks above first.")
    print(f"Signed in as {account_name(client)}")
    like_songs(client, load_state(), songs)
    print("Done.")


def load_login() -> tuple[str, str]:
    """The cookie and Google account index; messages never include the cookie."""
    cookie = os.environ.get("YTMUSIC_COOKIE", "").strip().strip("'\"")
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[len("cookie:"):].strip()
    if not cookie or cookie == COOKIE_PLACEHOLDER:
        sys.exit("Set YTMUSIC_COOKIE first (see run/.ytmusic.env).")
    if not sapisid_from(cookie):
        sys.exit("YTMUSIC_COOKIE has no SAPISID value. Copy the whole cookie header of a "
                 "request from a signed-in music.youtube.com tab.")
    return cookie, os.environ.get("YTMUSIC_AUTHUSER", "").strip() or "0"


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command not in ("check", "match", "import"):
        print(__doc__)
        return 2
    apply = "--apply" in argv[2:]
    try:
        if command == "match":
            cmd_match(YouTubeMusicClient())
            return 0
        client = YouTubeMusicClient(*load_login()) if command == "check" or apply else None
        if command == "check":
            cmd_check(client)
        else:
            cmd_import(client, apply)
    except ApiError as e:
        print(f"YouTube Music error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
