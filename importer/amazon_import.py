"""Like every song of a Yandex Music export (the CSVs written by fetch_tracks.py) in Amazon Music.

Run in the export folder:
  check            confirm the login works
  match            search Amazon Music for every exported track; writes amazon/matches.csv
  import           show what would be liked, without changing anything
  import --apply   like every matched song, liked or in a playlist, so it shows up in Liked Songs

Every command but the dry run reads the login from AMAZON_MUSIC_AUTH: the authorization
header of a request the signed-in music.amazon.com player sends to gql.music.amazon.dev.
"""
import base64
import csv
import json
import os
import sys
import time

from apple_import import (
    ApiError, JsonClient, format_duration, load_tracks, print_status_summary, progress, read_csv,
)
from ytmusic_import import best_of, dig, load_apple_matches, reference

# Amazon's documented Music API is a closed beta; these are the calls its own web
# player makes, as used by SongMirror (github.com/ahnafnafee/songmirror)
GQL = "https://gql.music.amazon.dev"
WEB_PLAYER = "https://music.amazon.com"
# The web player's public client ID, from its script bundle; not a secret
WEB_API_KEY = "amzn1.application.e1dc16675f9f4c78b31927d5bfd5c229"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
AUTH_PREFIX = "AmznMusic "
AUTH_PLACEHOLDER = "paste_here"
LOGIN_EXPIRED = ("Amazon Music refused the login; it lasts about an hour. Copy a fresh "
                 "authorization value into run/.amazon.env and run the same command again: "
                 "it continues where it stopped.")

# Seconds between requests; likes go slower, as they change the account
SEARCH_PAUSE = 1.0
LIKE_PAUSE = 2.0

AMAZON_DIR = "amazon"
MATCHES_PATH = os.path.join(AMAZON_DIR, "matches.csv")
MATCH_FIELDS = [
    "yandex_id", "title", "artist", "album", "duration", "status", "asin",
    "amazon_title", "amazon_artist", "amazon_album", "amazon_duration", "score",
]

SEARCH_QUERY = """
query SearchTracks($query: String!) {
  searchTracks(searchOptions: {searchFilters: [{field: "name", query: $query}]}) {
    edges { node { id title duration album { title } contributingArtists { edges { node { name } } } } }
  }
}"""


# --- Amazon Music API ---

def auth_context(auth: str) -> dict | None:
    """The device and token JSON inside an AmznMusic authorization value, if it is one."""
    if not auth.startswith(AUTH_PREFIX):
        return None
    try:
        context = json.loads(base64.b64decode(auth[len(AUTH_PREFIX):], validate=True))
    except ValueError:
        return None
    if not isinstance(context, dict):
        return None
    if not all(context.get(key) for key in ("access_token", "deviceId", "deviceType")):
        return None
    return context


class AmazonMusicClient(JsonClient):
    """GraphQL as the signed-in web player; every call needs the login."""

    service = "Amazon Music"

    def __init__(self, auth: str):
        context = auth_context(auth)
        headers = {
            "Content-Type": "application/json", "Accept": "application/json",
            "Origin": WEB_PLAYER, "Referer": WEB_PLAYER + "/", "User-Agent": USER_AGENT,
            "authorization": auth, "x-api-key": WEB_API_KEY,
            "device-id": context["deviceId"], "device-type": context["deviceType"],
        }
        super().__init__(GQL, headers, SEARCH_PAUSE)

    def graphql(self, query: str, variables: dict) -> dict:
        try:
            body = self.request("POST", "/", body={"query": query, "variables": variables})
        except ApiError as e:
            if e.status in (401, 403):
                raise ApiError(LOGIN_EXPIRED, e.status) from None
            raise
        errors = body.get("errors") or []
        codes = [str(dig(error, "extensions", "code") or error.get("code") or "") for error in errors]
        if any(code.startswith("AUTH_") for code in codes):
            raise ApiError(LOGIN_EXPIRED)
        if errors:
            raise ApiError("; ".join(str(error.get("message", error)) for error in errors))
        return body.get("data") or {}


def as_song(node: dict) -> dict | None:
    """A search result in the catalog-song shape apple_import's matching reads."""
    if not node.get("id"):
        return None
    names = [dig(edge, "node", "name") for edge in dig(node, "contributingArtists", "edges") or []]
    seconds = node.get("duration")
    attributes = {
        "name": node.get("title") or "",
        "artistName": ", ".join(dict.fromkeys(name for name in names if name)),
        "albumName": dig(node, "album", "title") or "",
        "durationInMillis": int(seconds * 1000) if isinstance(seconds, (int, float)) else None,
    }
    return {"id": node["id"], "type": "song", "attributes": attributes}


def parse_search(data: dict) -> list[dict]:
    edges = dig(data, "searchTracks", "edges") or []
    return [song for song in (as_song(edge.get("node") or {}) for edge in edges) if song]


def search(client: AmazonMusicClient, query: str) -> list[dict]:
    return parse_search(client.graphql(SEARCH_QUERY, {"query": query}))


# --- Matching (the rules are YouTube Music's: ytmusic_import.best_of) ---

def find_match(client: AmazonMusicClient, reference: dict) -> tuple[dict, dict, str] | None:
    """Songs by title and artist, then by title alone; stops at a match."""
    with_artist = f"{reference['title']} {reference['artist'].split(', ')[0]}"
    candidates, best = [], None
    for query in (with_artist, reference["title"]):
        candidates += search(client, query)
        best = best_of(reference, candidates)
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
        asin=song["id"],
        amazon_title=attrs["name"],
        amazon_artist=attrs["artistName"],
        amazon_album=attrs["albumName"],
        amazon_duration=format_duration(attrs["durationInMillis"]),
        score=f"{scores['total']:.2f}",
    )
    return row


# --- Local files ---

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


# --- Commands ---

def cmd_match(client: AmazonMusicClient) -> None:
    tracks = load_tracks()
    done = load_matches()
    search_tracks(client, tracks, load_apple_matches(), done)
    print_status_summary(done.values())


def search_tracks(client: AmazonMusicClient, tracks: dict, apple: dict, done: dict) -> None:
    """Search every track not yet in matches.csv, adding rows to it and to done."""
    todo = [(track_id, track) for track_id, track in tracks.items() if track_id not in done]
    print(f"{len(tracks)} tracks, {len(todo)} left to search")
    os.makedirs(AMAZON_DIR, exist_ok=True)
    write_header = not os.path.exists(MATCHES_PATH)
    started = time.monotonic()
    # Appending row by row means an interrupted run, or an expired login, resumes where it stopped
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


# --- Login ---

def load_login() -> str:
    """The authorization value; messages never include it."""
    auth = os.environ.get("AMAZON_MUSIC_AUTH", "").strip().strip("'\"")
    if auth.lower().startswith("authorization:"):
        auth = auth[len("authorization:"):].strip().strip("'\"")
    if not auth or auth == AUTH_PLACEHOLDER:
        sys.exit("Set AMAZON_MUSIC_AUTH first (see run/.amazon.env).")
    # Python would refuse to send such a header and print it, whole, in the error
    if not (auth.isascii() and auth.isprintable()):
        sys.exit("AMAZON_MUSIC_AUTH contains a line break or another unusual character. "
                 "Copy it again, as one line.")
    if auth_context(auth) is None:
        sys.exit("AMAZON_MUSIC_AUTH is not the authorization value of a gql.music.amazon.dev "
                 "request (it starts with 'AmznMusic '). Copy it again from a signed-in "
                 "music.amazon.com tab.")
    return auth
