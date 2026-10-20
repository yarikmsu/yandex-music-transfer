"""Like every song of a Yandex Music export (the CSVs written by fetch_tracks.py) in Spotify.

Run in the export folder:
  check            confirm the login works and show which account it signs in to
  match            search Spotify for every exported track; writes spotify/matches.csv
  import           show what would be liked, without changing anything
  import --apply   like every matched song, liked or in a playlist, so it shows up in Liked Songs

Every command but the dry run reads the login from SPOTIFY_SP_DC: the sp_dc cookie of a
signed-in open.spotify.com session.
"""
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

from apple_import import MAX_ATTEMPTS, ApiError, JsonClient

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
