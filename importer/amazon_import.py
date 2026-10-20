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
import collections
import csv
import json
import os
import re
import sys
import time

from apple_import import (
    ApiError, JsonClient, format_duration, load_collections, load_tracks, print_status_summary,
    progress, read_csv,
)
from ytmusic_import import best_of, dig, like_order, load_apple_matches, reference

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
STATE_PATH = os.path.join(AMAZON_DIR, "state.json")
NOT_IMPORTED_PATH = os.path.join(AMAZON_DIR, "not_imported.csv")
MATCH_FIELDS = [
    "yandex_id", "title", "artist", "album", "duration", "status", "asin",
    "amazon_title", "amazon_artist", "amazon_album", "amazon_duration", "score",
]
NOT_IMPORTED_FIELDS = [
    "status", "title", "artist", "duration", "in",
    "closest_title", "closest_artist", "closest_duration", "asin", "yandex_id",
]
STATUSES = ("matched", "check", "missing", "skip")
ASIN = re.compile(r"[A-Z0-9]{10}")

SEARCH_QUERY = """
query SearchTracks($query: String!) {
  searchTracks(searchOptions: {searchFilters: [{field: "name", query: $query}]}) {
    edges { node { id title duration album { title } contributingArtists { edges { node { name } } } } }
  }
}"""
USER_QUERY = "query User { user { id } }"
# The only write this importer makes. Never NEUTRAL or DISLIKE: nothing is ever unliked
LIKE_MUTATION = """
mutation Like($trackId: String!, $likeState: String!) {
  setTrackLikeState(trackId: $trackId, likeState: $likeState) { __typename }
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


def write_not_imported(matches: dict, asins: dict, liked: list[str], playlists: list) -> None:
    """Every track that will not be liked, where it was, and the closest Amazon candidate."""
    places = collections.defaultdict(list)
    for track_id in liked:
        places[track_id].append("liked")
    for name, track_ids in playlists:
        for track_id in track_ids:
            places[track_id].append(name)
    rows = [{
        "status": row["status"], "title": row["title"], "artist": row["artist"],
        "duration": row["duration"], "in": ", ".join(dict.fromkeys(places[track_id])),
        "closest_title": row["amazon_title"], "closest_artist": row["amazon_artist"],
        "closest_duration": row["amazon_duration"], "asin": row["asin"], "yandex_id": track_id,
    } for track_id, row in matches.items() if track_id not in asins]
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


def account_id(client: AmazonMusicClient) -> str:
    user_id = dig(client.graphql(USER_QUERY, {}), "user", "id")
    if not user_id:
        raise ApiError("the login is not signed in; copy it again from a signed-in "
                       "music.amazon.com tab")
    return user_id


def cmd_check(client: AmazonMusicClient) -> None:
    print(f"Login works. Signed in to Amazon Music as account {account_id(client)}")


def like_songs(client: AmazonMusicClient, state: dict, songs: list[str]) -> None:
    done = set(state["liked"])
    pending = [s for s in songs if s not in done]
    if state["liked"] and pending:
        print(f"  {len(pending)} songs are new since the last import; "
              "they will appear above the songs liked before.")
    client.pause = max(client.pause, LIKE_PAUSE)
    started = time.monotonic()
    for i, track_asin in enumerate(pending, 1):
        answer = client.graphql(LIKE_MUTATION, {"trackId": track_asin, "likeState": "LIKE"})
        if not answer.get("setTrackLikeState"):
            raise ApiError(f"liking {track_asin} returned no result; stopped so a rerun keeps the order")
        state["liked"].append(track_asin)
        save_state(state)
        if i % 50 == 0 or i == len(pending):
            print(progress(i, len(pending), started, "liked"))
    print(f"  Liked Songs: {len(state['liked'])} songs liked by this importer")


def blockers(tracks: dict, matches: dict) -> list[str]:
    """What keeps import --apply from liking anything yet, one message per problem."""
    problems = []
    unsearched = sum(track_id not in matches for track_id in tracks)
    if unsearched:
        problems.append(f"{unsearched} tracks are not searched yet: run 'match' again.")
    to_check = sum(row["status"] == "check" for row in matches.values())
    if to_check:
        problems.append(f"{to_check} rows in {MATCHES_PATH} are still 'check': change each to "
                        "matched, missing or skip.")
    # A typo would silently leave an approved song out, and adding it later puts it on top
    for row in matches.values():
        where = f"Row {row['yandex_id']} ({row['title']})"
        if row["status"] not in STATUSES:
            problems.append(f"{where}: unknown status '{row['status']}'.")
        elif row["status"] == "matched" and not ASIN.fullmatch(row["asin"]):
            problems.append(f"{where}: '{row['asin']}' is not an Amazon track ID (the 10 "
                            "characters after /tracks/ or trackAsin= in its link).")
    return problems


def cmd_import(client: AmazonMusicClient | None, apply: bool) -> None:
    matches = load_matches()
    if not matches:
        sys.exit("Run 'match' first.")
    liked, playlists = load_collections()
    asins = {track_id: row["asin"] for track_id, row in matches.items()
             if row["status"] == "matched" and ASIN.fullmatch(row["asin"])}
    songs = like_order(liked, playlists, asins)
    write_not_imported(matches, asins, liked, playlists)
    print(f"Liked Songs: {len(songs)} songs from {len(liked)} liked tracks "
          f"and {len(playlists)} playlists")
    print(f"Not liked: {len(matches) - len(asins)} tracks, listed in {NOT_IMPORTED_PATH}")
    problems = blockers(load_tracks(), matches)
    for problem in problems:
        print(problem)
    if not apply:
        print("Nothing changed. Run 'import --apply' to like these songs.")
        return
    if problems:
        # Likes cannot be reordered later, so every track needs its decision first
        sys.exit("Nothing liked: settle the rows above first.")
    print(f"Signed in as account {account_id(client)}")
    like_songs(client, load_state(), songs)
    print("Done.")


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


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command not in ("check", "match", "import"):
        print(__doc__)
        return 2
    apply = "--apply" in argv[2:]
    try:
        # The dry run reads only local files, so it never needs the login
        client = AmazonMusicClient(load_login()) if command != "import" or apply else None
        if command == "check":
            cmd_check(client)
        elif command == "match":
            cmd_match(client)
        else:
            cmd_import(client, apply)
    except ApiError as e:
        print(f"Amazon Music error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
