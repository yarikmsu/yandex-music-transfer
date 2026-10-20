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
import json
import os
import sys
import time
import urllib.request

from apple_import import ApiError, JsonClient
from ytmusic_import import dig

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
