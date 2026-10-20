"""Like every song of a Yandex Music export (the CSVs written by fetch_tracks.py) in Spotify.

Run in the export folder:
  check            confirm the login works and show which account it signs in to
  match            search Spotify for every exported track; writes spotify/matches.csv
  import           show what would be liked, without changing anything
  import --apply   like every matched song, liked or in a playlist, so it shows up in Liked Songs

Every command but the dry run reads the login from SPOTIFY_SP_DC: the sp_dc cookie of a
signed-in open.spotify.com session.
"""
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
import urllib.request
from typing import NamedTuple

from amazon_import import best_of, other_recording, rank
from apple_import import (
    MAX_ATTEMPTS, ApiError, JsonClient, format_duration, load_tracks, print_status_summary, progress,
    read_csv,
)
from ytmusic_import import dig, load_apple_matches, reference

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
TRACK_URI = "spotify:track:"
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

def load_matches() -> dict[str, dict]:
    if not os.path.exists(MATCHES_PATH):
        return {}
    return {row["yandex_id"]: row for row in read_csv(MATCHES_PATH)}


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
