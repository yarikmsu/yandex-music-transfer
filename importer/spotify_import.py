"""Like every song of a Yandex Music export (the CSVs written by fetch_tracks.py) in Spotify.

Run in the export folder:
  check            confirm the login works and show which account it signs in to
  match            search Spotify for every exported track; writes spotify/matches.csv
  import           show what would be liked, without changing anything
  import --apply   like every matched song, liked or in a playlist, so it shows up in Liked Songs

Every command but the dry run reads the login from SPOTIFY_SP_DC: the sp_dc cookie of a
signed-in open.spotify.com session.
"""
import collections
import csv
import hashlib
import hmac
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

from amazon_import import best_of, other_recording, rank
from apple_import import (
    MAX_ATTEMPTS, ApiError, JsonClient, format_duration, load_collections, load_tracks,
    print_status_summary, progress, read_csv,
)
from ytmusic_import import dig, like_order, load_apple_matches, reference

# Spotify's developer API needs a Premium app owner and refuses the player's own token,
# so this does what the open.spotify.com player does. Its login code and the IDs of its
# requests change with each player release, so they are read from the player's code.
OPEN = "https://open.spotify.com"
CDN = "https://open.spotifycdn.com/cdn/build/web-player/"
PATHFINDER = "https://api-partner.spotify.com"
QUERY_PATH = "/pathfinder/v2/query"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
COOKIE_PLACEHOLDER = "paste_here"
# The player's requests this importer makes; the only write is addToLibrary
OPERATIONS = ("profileAttributes", "searchTracks", "addToLibrary", "fetchLibraryTracks")
LOGIN_REFUSED = ("Spotify did not accept the cookie as a signed-in login. Copy sp_dc again from a "
                 "signed-in open.spotify.com tab into run/.spotify.env and run the same command "
                 "again: it continues where it stopped.")
PLAYER_CHANGED = "the Spotify web player may have changed; try again later or see importer/SPOTIFY.md"

# Seconds between requests; likes go slower, as they change the account
SEARCH_PAUSE = 1.0
LIKE_PAUSE = 2.0
# The token lasts about 50 minutes; a new one is fetched this long before it runs out
TOKEN_MARGIN = 300

SPOTIFY_DIR = "spotify"
MATCHES_PATH = os.path.join(SPOTIFY_DIR, "matches.csv")
STATE_PATH = os.path.join(SPOTIFY_DIR, "state.json")
NOT_IMPORTED_PATH = os.path.join(SPOTIFY_DIR, "not_imported.csv")
MATCH_FIELDS = [
    "yandex_id", "title", "artist", "album", "duration", "status", "spotify_id",
    "spotify_title", "spotify_artist", "spotify_album", "spotify_duration", "score",
]
NOT_IMPORTED_FIELDS = [
    "status", "title", "artist", "duration", "in",
    "closest_title", "closest_artist", "closest_duration", "spotify_id", "yandex_id",
]
STATUSES = ("matched", "check", "missing", "skip")
TRACK_URI = "spotify:track:"
# A track ID alone, its spotify: URI, or a share link (Share > Copy Song Link)
TRACK_ID = re.compile(r"(?:spotify:track:|https?://open\.spotify\.com/(?:intl-[\w-]+/)?track/)?"
                      r"([0-9A-Za-z]{22})(?:\?\S*)?")
# The player's own search page asks for these; more than 10 results are refused
SEARCH_VARIABLES = {"offset": 0, "limit": 10, "numberOfTopResults": 5, "includeAudiobooks": False,
                    "includePreReleases": False, "includeAuthors": False}

STRING_SECRET = re.compile(r"""\{secret:(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)"),version:(\d+)\}""")
NUMBER_SECRET = re.compile(r"\{secret:\[([\d,\s]*)\],version:(\d+)\}")
OPERATION = re.compile(r'\("(\w+)","(?:query|mutation)","([0-9a-f]{64})"')
BUNDLE = re.compile(re.escape(CDN) + r"web-player\.[0-9a-f]+\.js")
# Webpack's chunk file names: .u=e=>""+({id:"name",...}[e]||e)+"."+{id:"hash",...}[e]+".js"
CHUNK_NAMES = re.compile(r'\.u=e=>""\+\(\(\{(.*?)\}\)\[e\]\|\|e\)\+"\."\+\(\{(.*?)\}\)\[e\]')
CHUNK_ENTRY = re.compile(r'(\d+):"([^"]+)"')
SEARCH_CHUNK = "xpui-routes-search"


# --- Spotify web player ---

class LoginRefused(ApiError):
    def __init__(self):
        super().__init__(LOGIN_REFUSED)


class SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """urllib copies every header onto a redirect, the cookie and the token too, so only
    redirects that stay on the same HTTPS host are followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if new.scheme != "https" or new.hostname != old.hostname:
            raise urllib.error.HTTPError(req.full_url, code, "redirect to another host refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Player(NamedTuple):
    """What the current web player's code gives: its login secret and request hashes."""
    secret: str | list[int]
    version: int
    hashes: dict[str, str]


def totp_key(secret: str | list[int]) -> bytes:
    """The HMAC key the player derives from a secret of its code."""
    codes = [ord(c) for c in secret] if isinstance(secret, str) else secret
    return "".join(str(code ^ (i % 33 + 9)) for i, code in enumerate(codes)).encode()


def totp(key: bytes, seconds: float) -> str:
    """The 6-digit time-based code (RFC 6238, SHA-1, 30-second steps)."""
    digest = hmac.new(key, struct.pack(">Q", int(seconds // 30)), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return "%06d" % ((struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000)


def bundle_url(home_html: str) -> str | None:
    found = BUNDLE.search(home_html)
    return found.group() if found else None


def parse_secrets(js: str) -> list[tuple[str | list[int], int]]:
    """The login secrets in the order the bundle lists them; the player uses the first."""
    found = [(m.start(), re.sub(r"\\(.)", r"\1", m.group(1) or m.group(2) or ""), int(m.group(3)))
             for m in STRING_SECRET.finditer(js)]
    found += [(m.start(), [int(n) for n in m.group(1).split(",") if n.strip()], int(m.group(2)))
              for m in NUMBER_SECRET.finditer(js)]
    return [(secret, version) for _, secret, version in sorted(found, key=lambda f: f[0])]


def parse_operations(js: str) -> dict[str, str]:
    return dict(OPERATION.findall(js))


def search_chunk_url(js: str) -> str | None:
    """The lazily loaded part of the player that holds the search requests."""
    found = CHUNK_NAMES.search(js)
    if not found:
        return None
    names = dict(CHUNK_ENTRY.findall(found.group(1)))
    hashes = dict(CHUNK_ENTRY.findall(found.group(2)))
    chunk_id = next((i for i, name in names.items() if name == SEARCH_CHUNK), None)
    if chunk_id not in hashes:
        return None
    return f"{CDN}{SEARCH_CHUNK}.{hashes[chunk_id]}.js"


def fetch_text(url: str) -> str:
    """A public page or script of the player, fetched without the login."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as e:
        raise ApiError(f"fetching {url} failed: {e}") from None


def load_player() -> Player:
    url = bundle_url(fetch_text(OPEN + "/"))
    if not url:
        raise ApiError(f"open.spotify.com links no web player code; {PLAYER_CHANGED}")
    js = fetch_text(url)
    secrets = parse_secrets(js)
    hashes = parse_operations(js)
    chunk = search_chunk_url(js)
    if chunk:
        hashes.update(parse_operations(fetch_text(chunk)))
    missing = ([] if secrets else ["login secret"]) + [name for name in OPERATIONS if name not in hashes]
    if missing:
        raise ApiError(f"no {', '.join(missing)} in the web player code; {PLAYER_CHANGED}")
    secret, version = secrets[0]
    return Player(secret, version, {name: hashes[name] for name in OPERATIONS})


def server_time() -> float | None:
    """Spotify's clock, which the player sends a second code for; None if it does not answer."""
    try:
        with urllib.request.urlopen(urllib.request.Request(
                OPEN + "/api/server-time", headers={"User-Agent": USER_AGENT}), timeout=10) as resp:
            return float(json.loads(resp.read())["serverTime"])
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError):
        return None


def fetch_token(sp_dc: str, player: Player) -> tuple[str, float]:
    """A web player access token for the login, and when it expires (epoch seconds)."""
    key = totp_key(player.secret)
    remote = server_time()
    params = {"reason": "init", "productType": "web-player", "totp": totp(key, time.time()),
              "totpServer": totp(key, remote) if remote else "unavailable",
              "totpVer": str(player.version)}
    client = JsonClient(OPEN, {"Cookie": f"sp_dc={sp_dc}", "User-Agent": USER_AGENT,
                               "Accept": "application/json"}, 0)
    client.service = "Spotify"
    try:
        answer = client.request("GET", "/api/token", params)
    except ApiError as e:
        if e.status in (401, 403):
            raise LoginRefused() from None
        if e.status == 400:
            # What a code built from an outdated secret gets
            raise ApiError(f"open.spotify.com refused the login code; {PLAYER_CHANGED}", 400) from None
        raise
    if answer.get("isAnonymous") is not False or not answer.get("accessToken"):
        raise LoginRefused()
    return answer["accessToken"], answer["accessTokenExpirationTimestampMs"] / 1000


class SpotifyClient(JsonClient):
    """The player's GraphQL, signed in; the token is renewed from the cookie as it runs out."""

    service = "Spotify"

    def __init__(self, sp_dc: str, player: Player):
        self.sp_dc = sp_dc
        self.player = player
        super().__init__(PATHFINDER, {}, SEARCH_PAUSE)
        self._renew()

    def _renew(self) -> None:
        token, self.expires = fetch_token(self.sp_dc, self.player)
        self.headers = {"Authorization": f"Bearer {token}", "App-Platform": "WebPlayer",
                        "Content-Type": "application/json", "Accept": "application/json",
                        "User-Agent": USER_AGENT}

    def graphql(self, operation: str, variables: dict) -> dict:
        if time.time() > self.expires - TOKEN_MARGIN:
            self._renew()
        body = {"operationName": operation, "variables": variables,
                "extensions": {"persistedQuery": {"version": 1, "sha256Hash": self.player.hashes[operation]}}}
        try:
            answer = self.request("POST", QUERY_PATH, body=body)
        except ApiError as e:
            if e.status not in (401, 403):
                raise
            # A token can be revoked early; the player then fetches a new one the same way
            self._renew()
            try:
                answer = self.request("POST", QUERY_PATH, body=body)
            except ApiError as e:
                if e.status in (401, 403):
                    raise LoginRefused() from None
                raise
        errors = answer.get("errors") or []
        if "PersistedQueryNotFound" in json.dumps(errors):
            raise ApiError(f"{operation}: {PLAYER_CHANGED}")
        if errors:
            messages = (str(error.get("message", error)) if isinstance(error, dict) else str(error)
                        for error in errors)
            raise ApiError(f"{operation}: {'; '.join(messages)}")
        return answer.get("data") or {}


def as_song(data) -> dict | None:
    """A search result in the catalog-song shape apple_import's matching reads."""
    uri = dig(data, "uri")
    if dig(data, "__typename") != "Track" or not isinstance(uri, str) or not uri.startswith(TRACK_URI):
        return None
    names = [dig(artist, "profile", "name") for artist in dig(data, "artists", "items") or []]
    attributes = {
        "name": data.get("name") or "",
        "artistName": ", ".join(dict.fromkeys(name for name in names if name)),
        "albumName": dig(data, "albumOfTrack", "name") or "",
        "durationInMillis": dig(data, "duration", "totalMilliseconds"),
    }
    return {"id": uri[len(TRACK_URI):], "type": "song", "attributes": attributes}


def parse_search(data: dict) -> list[dict]:
    items = dig(data, "searchV2", "tracksV2", "items") or []
    return [song for song in (as_song(dig(item, "item", "data")) for item in items) if song]


def search(client: SpotifyClient, query: str) -> list[dict]:
    return parse_search(client.graphql("searchTracks", {"searchTerm": query, **SEARCH_VARIABLES}))


# --- Login ---

def load_login() -> str:
    """The sp_dc value, however it was copied; messages never include it."""
    value = os.environ.get("SPOTIFY_SP_DC", "").strip().strip("'\"")
    if value.lower().startswith("cookie:"):
        value = value[len("cookie:"):].strip()
    if ";" in value or "=" in value:
        parts = (part.strip().partition("=") for part in value.split(";"))
        value = next((v for name, _, v in parts if name == "sp_dc"), "")
    if not value or value == COOKIE_PLACEHOLDER:
        sys.exit("Set SPOTIFY_SP_DC first: the sp_dc cookie value (see run/.spotify.env).")
    # Python would refuse to send such a header and print it, whole, in the error
    if not (value.isascii() and value.isprintable()) or " " in value:
        sys.exit("SPOTIFY_SP_DC contains a space, a line break or another unusual character. "
                 "Copy it again, as one line.")
    return value


# --- Matching (Amazon's rules: amazon_import.best_of) ---

def find_match(client: SpotifyClient, reference: dict) -> tuple[dict, dict, str] | None:
    """Songs by title and first artist, then by title alone, which finds artists Spotify
    spells otherwise; stops at a match of the same recording."""
    first_artist = reference["artist"].split(", ")[0]
    best = None
    for query in ([f"{reference['title']} {first_artist}"] if first_artist else []) + [reference["title"]]:
        found = best_of(reference, search(client, query))
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
        spotify_id=song["id"],
        spotify_title=attrs["name"],
        spotify_artist=attrs["artistName"],
        spotify_album=attrs["albumName"],
        spotify_duration=format_duration(attrs["durationInMillis"]),
        score=f"{scores['total']:.2f}",
    )
    return row


# --- Local files ---

def track_id_of(value: str) -> str | None:
    found = TRACK_ID.fullmatch(value.strip())
    return found.group(1) if found else None


def load_matches() -> dict[str, dict]:
    if not os.path.exists(MATCHES_PATH):
        return {}
    return {row["yandex_id"]: row for row in read_csv(MATCHES_PATH)}


def write_not_imported(matches: dict, ids: dict, liked: list[str], playlists: list) -> None:
    """Every track that will not be liked, where it was, and the closest Spotify candidate."""
    places = collections.defaultdict(list)
    for track_id in liked:
        places[track_id].append("liked")
    for name, track_ids in playlists:
        for track_id in track_ids:
            places[track_id].append(name)
    rows = [{
        "status": row["status"], "title": row["title"], "artist": row["artist"],
        "duration": row["duration"], "in": ", ".join(dict.fromkeys(places[track_id])),
        "closest_title": row["spotify_title"], "closest_artist": row["spotify_artist"],
        "closest_duration": row["spotify_duration"], "spotify_id": row["spotify_id"], "yandex_id": track_id,
    } for track_id, row in matches.items() if track_id not in ids]
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

def cmd_match(client: SpotifyClient) -> None:
    tracks = load_tracks()
    done = load_matches()
    search_tracks(client, tracks, load_apple_matches(), done)
    print_status_summary(done.values())


def search_tracks(client: SpotifyClient, tracks: dict, apple: dict, done: dict) -> None:
    """Search every track not yet in matches.csv, adding rows to it and to done."""
    todo = [(track_id, track) for track_id, track in tracks.items() if track_id not in done]
    print(f"{len(tracks)} tracks, {len(todo)} left to search")
    os.makedirs(SPOTIFY_DIR, exist_ok=True)
    write_header = not os.path.exists(MATCHES_PATH)
    started = time.monotonic()
    # Appending row by row means an interrupted run, or a refused login, resumes where it stopped
    with open(MATCHES_PATH, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MATCH_FIELDS)
        if write_header:
            writer.writeheader()
        for i, (track_id, track) in enumerate(todo, 1):
            try:
                best = find_match(client, reference(track, apple.get(track_id)))
            except LoginRefused:
                raise
            except ApiError as e:
                # A rerun stops at the same track, so say which one and how to get past it
                raise ApiError(f"searching '{track['title']}' by {track['artist']} (Yandex track "
                               f"{track_id}) failed: {e}. To skip it, add the line "
                               f"'{track_id},,,,,missing' to {MATCHES_PATH}.", e.status) from None
            done[track_id] = match_row(track_id, track, best)
            writer.writerow(done[track_id])
            f.flush()
            if i % 25 == 0 or i == len(todo):
                print(progress(i, len(todo), started, "searched"))


def account_name(client: SpotifyClient) -> str:
    name = dig(client.graphql("profileAttributes", {}), "me", "profile", "name")
    if not name:
        raise LoginRefused()
    return name


def cmd_check(client: SpotifyClient) -> None:
    print(f"Cookie works. Signed in to Spotify as {account_name(client)}")


def latest_like(client: SpotifyClient) -> str | None:
    uri = dig(client.graphql("fetchLibraryTracks", {"offset": 0, "limit": 1}),
              "me", "library", "tracks", "items", 0, "track", "_uri")
    return track_id_of(uri) if isinstance(uri, str) else None


def like_songs(client: SpotifyClient, state: dict, songs: list[str]) -> None:
    # Liking a song again moves it to the top, so every song is liked once per import
    done = set(state["liked"])
    pending = [s for s in songs if s not in done]
    if state["liked"] and pending:
        print(f"  {len(pending)} songs are new since the last import; "
              "they will appear above the songs liked before.")
    client.pause = max(client.pause, LIKE_PAUSE)
    started = time.monotonic()
    for i, song in enumerate(pending, 1):
        try:
            answer = client.graphql("addToLibrary", {"libraryItemUris": [TRACK_URI + song], "interactionId": None})
        except LoginRefused:
            raise
        except ApiError as e:
            raise ApiError(f"liking {OPEN}/track/{song} failed: {e}; stopped so a rerun keeps the order",
                           e.status) from None
        if not answer.get("addLibraryItems"):
            raise ApiError(f"liking {song} returned no result; stopped so a rerun keeps the order")
        state["liked"].append(song)
        save_state(state)
        # The order assumes Liked Songs lists the latest like first; as a like always lands
        # on top, one look settles it
        if i == 1 and latest_like(client) != song:
            raise ApiError("Liked Songs did not show the song just liked on top, so the newest Yandex "
                           "likes would not end up first. Stopped after 1 like; see 'Order' in "
                           "importer/SPOTIFY.md.")
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
        elif row["status"] == "matched" and track_id_of(row["spotify_id"]) is None:
            problems.append(f"{where}: '{row['spotify_id']}' is not a Spotify track ID (the 22 "
                            "characters after /track/ in its link).")
    return problems


def cmd_import(client: SpotifyClient | None, apply: bool) -> None:
    matches = load_matches()
    if not matches:
        sys.exit("Run 'match' first.")
    liked, playlists = load_collections()
    ids = {track_id: track_id_of(row["spotify_id"]) for track_id, row in matches.items()
           if row["status"] == "matched" and track_id_of(row["spotify_id"])}
    songs = like_order(liked, playlists, ids)
    write_not_imported(matches, ids, liked, playlists)
    print(f"Liked Songs: {len(songs)} songs from {len(liked)} liked tracks "
          f"and {len(playlists)} playlists")
    print(f"Not liked: {len(matches) - len(ids)} tracks, listed in {NOT_IMPORTED_PATH}")
    problems = blockers(load_tracks(), matches)
    for problem in problems:
        print(problem)
    if not apply:
        print("Nothing changed. Run 'import --apply' to like these songs.")
        return
    if problems:
        # A like cannot be moved down later, so every track needs its decision first
        sys.exit("Nothing liked: settle the rows above first.")
    print(f"Signed in as {account_name(client)}")
    like_songs(client, load_state(), songs)
    print("Done.")


def main(argv: list[str]) -> int:
    urllib.request.install_opener(urllib.request.build_opener(SameHostRedirects))
    command = argv[1] if len(argv) > 1 else ""
    if command not in ("check", "match", "import"):
        print(__doc__)
        return 2
    apply = "--apply" in argv[2:]
    try:
        # The dry run reads only local files, so it never needs the login
        client = SpotifyClient(load_login(), load_player()) if command != "import" or apply else None
        if command == "check":
            cmd_check(client)
        elif command == "match":
            cmd_match(client)
        else:
            cmd_import(client, apply)
    except ApiError as e:
        print(f"Spotify error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
