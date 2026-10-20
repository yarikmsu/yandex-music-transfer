"""Like every song of a Yandex Music export (the CSVs written by fetch_tracks.py) in Amazon Music.

Run in the export folder:
  check            confirm the login works and show which account it signs in to
  match            search Amazon Music for every exported track; writes amazon/matches.csv
  import           show what would be liked, without changing anything
  import --apply   like every matched song, liked or in a playlist, so it shows up in Liked Songs

Every command but the dry run reads the login from AMAZON_MUSIC_COOKIE: the cookie header
of a request the signed-in music.amazon.com player sends to gql.music.amazon.com.
"""
import collections
import csv
import json
import os
import re
import sys
import time

from apple_import import (
    BRACKETS, DASH_SUFFIX, STATUS_RANK, ApiError, JsonClient, format_duration, load_collections,
    load_tracks, print_status_summary, progress, read_csv, score_candidate,
)
from ytmusic_import import dig, like_order, load_apple_matches, reference, status_of

# Amazon's documented Music API is a closed beta; these are the calls its own web
# player makes (see also SongMirror, github.com/ahnafnafee/songmirror)
GQL = "https://gql.music.amazon.com"
WEB_PLAYER = "https://music.amazon.com"
# The web player's public client ID, from its requests; not a secret
WEB_API_KEY = "amzn1.application.4ff5579ca2e3407aba989a1f5dbdaf69"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
COOKIE_PLACEHOLDER = "paste_here"
# The amazon.com session cookies the player signs in with (US marketplace); the
# browser's analytics and experiment cookies are dropped
SESSION_COOKIES = ("session-id", "session-id-time", "ubid-main", "at-main", "sess-at-main", "x-main",
                   "session-token", "sst-main", "at-main-music", "am-token")
# Without any of these the player answers as if signed out
REQUIRED_COOKIES = ("session-id", "ubid-main", "at-main", "sess-at-main", "x-main")
LOGIN_REFUSED = ("Amazon Music did not accept the cookie as a signed-in login. Copy it again from "
                 "a signed-in music.amazon.com tab into run/.amazon.env and run the same command "
                 "again: it continues where it stopped.")

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
# Notes that name another recording of the song; "Remastered" and the like do not
RECORDING_NOTE = re.compile(r"\b(live|remix|mix|demo|acoustic|instrumental|unplugged)\b", re.IGNORECASE)

# A "name" filter matches words of the track title only; an artist filter narrows that down
SEARCH_QUERY = """
query SearchTracks($options: SearchTrackOptions!) {
  searchTracks(searchOptions: $options) {
    edges { node { id title duration album { title } contributingArtists { edges { node { name } } } } }
  }
}"""
USER_QUERY = "query User { user { id name } }"
# Liked Songs, latest like first
LATEST_LIKES_QUERY = "query LatestLikes { user { tracks(limit: 5) { edges { node { id } } } } }"
# The only write this importer makes. Never NEUTRAL or DISLIKE: nothing is ever unliked
LIKE_MUTATION = """
mutation Like($trackId: String!, $likeState: String!) {
  setTrackLikeState(trackId: $trackId, likeState: $likeState) { __typename }
}"""


# --- Amazon Music API ---

class LoginRefused(ApiError):
    def __init__(self):
        super().__init__(LOGIN_REFUSED)


def fetch_session(cookie: str) -> dict:
    """The device, session and CSRF values the player's config gives this login."""
    player = JsonClient(WEB_PLAYER, {"Cookie": cookie, "Origin": WEB_PLAYER, "Referer": WEB_PLAYER + "/",
                                     "User-Agent": USER_AGENT, "Accept": "*/*"}, 0)
    player.service = "Amazon Music"
    config = player.request("POST", "/config.json")
    if not config.get("customerId"):
        raise LoginRefused()
    return config


class AmazonMusicClient(JsonClient):
    """GraphQL as the signed-in web player; every call needs the login."""

    service = "Amazon Music"

    def __init__(self, cookie: str):
        self.cookie = cookie
        super().__init__(GQL, {}, SEARCH_PAUSE)
        self._use(fetch_session(cookie))

    def _use(self, session: dict) -> None:
        csrf = session.get("csrf") or {}
        self.headers = {
            "Content-Type": "application/json", "Accept": "application/json",
            "Origin": WEB_PLAYER, "Referer": WEB_PLAYER + "/", "User-Agent": USER_AGENT,
            "Cookie": self.cookie, "x-api-key": WEB_API_KEY,
            "x-amzn-device-id": session["deviceId"], "x-amzn-device-type": session["deviceType"],
            "x-amzn-session-id": session["sessionId"],
            "music-territory": session.get("musicTerritory") or "US",
            "csrf-token": str(csrf.get("token", "")), "csrf-rnd": str(csrf.get("rnd", "")),
            "csrf-ts": str(csrf.get("ts", "")),
        }

    def graphql(self, query: str, variables: dict) -> dict:
        try:
            return self._graphql(query, variables)
        except LoginRefused:
            # The CSRF values expire; the player fetches new ones the same way
            self._use(fetch_session(self.cookie))
            return self._graphql(query, variables)

    def _graphql(self, query: str, variables: dict) -> dict:
        try:
            body = self.request("POST", "/", body={"query": query, "variables": variables})
        except ApiError as e:
            if e.status in (401, 403):
                raise LoginRefused() from None
            raise
        errors = body.get("errors") or []
        codes = [str(dig(error, "extensions", "code") or error.get("code") or "") for error in errors]
        if any(code.startswith("AUTH_") for code in codes):
            raise LoginRefused()
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


def search(client: AmazonMusicClient, title: str, artist: str | None = None) -> list[dict]:
    filters = [{"field": "name", "query": title}]
    if artist:
        filters.append({"field": "artist", "query": artist})
    return parse_search(client.graphql(SEARCH_QUERY, {"options": {"searchFilters": filters}}))


# --- Login ---

def load_login() -> str:
    """The session cookies of the copied cookie header; messages never include them."""
    cookie = os.environ.get("AMAZON_MUSIC_COOKIE", "").strip().strip("'\"")
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[len("cookie:"):].strip().strip("'\"")
    if not cookie or cookie == COOKIE_PLACEHOLDER:
        sys.exit("Set AMAZON_MUSIC_COOKIE first (see run/.amazon.env).")
    # Python would refuse to send such a header and print it, whole, in the error
    if not (cookie.isascii() and cookie.isprintable()):
        sys.exit("AMAZON_MUSIC_COOKIE contains a line break or another unusual character. "
                 "Copy it again, as one line.")
    values = {}
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name in SESSION_COOKIES and value:
            values[name] = value
    missing = [name for name in REQUIRED_COOKIES if name not in values]
    if missing:
        sys.exit(f"AMAZON_MUSIC_COOKIE has no {', '.join(missing)} value. Copy the whole cookie "
                 "header of a request from a signed-in music.amazon.com tab.")
    return "; ".join(f"{name}={value}" for name, value in values.items())


# --- Matching (the statuses are YouTube Music's: ytmusic_import.status_of) ---

def other_recording(song: dict, reference: dict) -> bool:
    """Whether a live, remix or other version note sets the song apart from the exported track."""
    def notes(title: str) -> set[str]:
        # Only the notes in brackets or after a dash: "Live Forever" is a name, not a live recording
        extras = " ".join(BRACKETS.findall(title) + DASH_SUFFIX.findall(title))
        return {note.lower() for note in RECORDING_NOTE.findall(extras)}
    return notes(song["attributes"]["name"]) != notes(reference["title"])


def rank(reference: dict, best: tuple[dict, dict, str]) -> tuple:
    song, scores, status = best
    # Amazon often carries live and remixed versions of a length closer to the export's
    # than the album recording, so the same recording counts before the length does
    return STATUS_RANK[status], not other_recording(song, reference), scores["total"]


def best_of(reference: dict, candidates: list[dict]) -> tuple[dict, dict, str] | None:
    scored = []
    for song in candidates:
        scores = score_candidate(reference, song)
        scored.append((song, scores, status_of(scores, song, reference)))
    return max(scored, key=lambda b: rank(reference, b), default=None)


def find_match(client: AmazonMusicClient, reference: dict) -> tuple[dict, dict, str] | None:
    """Songs by title and first artist, then by title alone, which finds artists Amazon
    spells otherwise; stops at a match of the same recording."""
    first_artist = reference["artist"].split(", ")[0]
    best = None
    for artist in ([first_artist] if first_artist else []) + [None]:
        found = best_of(reference, search(client, reference["title"], artist))
        # On a tie the first search's song stays, as it names the artist too
        if found and (best is None or rank(reference, found) > rank(reference, best)):
            best = found
        if best and best[2] == "matched" and not other_recording(best[0], reference):
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


def account_name(client: AmazonMusicClient) -> str:
    user = dig(client.graphql(USER_QUERY, {}), "user") or {}
    if not user.get("id"):
        raise LoginRefused()
    return user.get("name") or user["id"]


def cmd_check(client: AmazonMusicClient) -> None:
    print(f"Cookie works. Signed in to Amazon Music as {account_name(client)}")


def latest_like(client: AmazonMusicClient) -> str | None:
    return dig(client.graphql(LATEST_LIKES_QUERY, {}), "user", "tracks", "edges", 0, "node", "id")


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
        # The order assumes Liked Songs lists the latest like first; confirm it before going on
        if i == 2 and latest_like(client) != track_asin:
            raise ApiError("Liked Songs does not list the latest like first, so the newest Yandex "
                           "likes would end up at the bottom. Stopped after 2 likes; see 'Order' "
                           "in importer/AMAZON_MUSIC.md.")
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
    print(f"Signed in as {account_name(client)}")
    like_songs(client, load_state(), songs)
    print("Done.")


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
