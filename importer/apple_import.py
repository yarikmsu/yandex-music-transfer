"""Import a Yandex Music export (the CSVs written by fetch_tracks.py) into Apple Music.

Run in the export folder:
  check            confirm the tokens work and show your Apple Music storefront
  match            search the Apple catalog for every exported track; writes apple/matches.csv
  import           show what would be added, without changing anything
  import --apply   add every matched song, liked or in a playlist, to Favorites
                   (with --playlists, also recreate the playlists)

Tokens are read from APPLE_DEV_TOKEN and APPLE_USER_TOKEN.
"""
import collections
import csv
import difflib
import glob
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# The tokens come from the music.apple.com web player, so requests go where it sends them
API = "https://amp-api.music.apple.com"
WEB_PLAYER_ORIGIN = "https://music.apple.com"
TOKEN_PLACEHOLDER = "paste_here"
# Searches go to the public iTunes Search API instead: same catalog and song IDs, no
# tokens, and about 20 calls a minute versus about 50 an hour on the web-player tokens
ITUNES_API = "https://itunes.apple.com"
ITUNES_PAUSE = 3.5

APPLE_DIR = "apple"
MATCHES_PATH = os.path.join(APPLE_DIR, "matches.csv")
STATE_PATH = os.path.join(APPLE_DIR, "state.json")
MATCH_FIELDS = [
    "yandex_id", "title", "artist", "album", "duration", "status",
    "apple_id", "apple_title", "apple_artist", "apple_album", "apple_duration", "score",
]

SEARCH_LIMIT = 10
CHUNK_SIZE = 50
# Seconds between requests. Apple rate-limits after a few dozen quick searches,
# so each 429 doubles the pause for the rest of the run.
REQUEST_PAUSE = 1.5
MAX_REQUEST_PAUSE = 20
MAX_ATTEMPTS = 8
DURATION_TOLERANCE = 10  # seconds
STATUS_RANK = {"missing": 0, "check": 1, "matched": 2}

BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
DASH_SUFFIX = re.compile(r"\s[-–—]\s.*$")
FEATURING = re.compile(r"\b(feat|ft|featuring)\b.*")
NON_WORD = re.compile(r"[\W_]+")
ARTIST_SEPARATORS = re.compile(r",|&|\bfeat\b\.?|\bft\b\.?", re.IGNORECASE)
COVER_OF = re.compile(
    r"[(\[]\s*(?:as\s+)?(?:made\s+famous\s+by|in\s+the\s+style\s+of|tribute\s+to|"
    r"originally\s+performed\s+by)\s+([^)\]]+)[)\]]",
    re.IGNORECASE,
)
CYRILLIC_TO_LATIN = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
})


# --- Apple Music API ---

class ApiError(Exception):
    pass


def build_url(base: str, path: str, params: dict | None = None) -> str:
    # Apple expects ids[songs]=1,2,3 with literal brackets and commas
    query = "?" + urllib.parse.urlencode(params, safe="[],") if params else ""
    return base + path + query


def _retry_delay(retry_after: str | None, attempt: int, base: int) -> int:
    if retry_after and retry_after.isdigit():
        return int(retry_after)
    return min(base * 2 ** (attempt - 1), 600)


class JsonClient:
    """JSON over HTTPS with a pause between requests."""

    service = "Apple"

    def __init__(self, base: str, headers: dict, pause: float, rate_limit_codes=(429,)):
        self.base = base
        self.headers = headers
        self.pause = pause
        self.rate_limit_codes = rate_limit_codes

    def _slow_down(self) -> None:
        self.pause = min(self.pause * 2, MAX_REQUEST_PAUSE)
        print(f"  Rate limited by {self.service}; now one request every {self.pause:g}s")

    def request(self, method: str, path: str, params: dict | None = None, body=None) -> dict:
        """Send one request. Rate limits are waited out however long they last,
        server and network errors are retried MAX_ATTEMPTS times, others raise."""
        url = build_url(self.base, path, params)
        data = json.dumps(body).encode() if body is not None else None
        if data is None and method != "GET":
            data = b""  # an explicit empty body gives the POST a Content-Length
        rate_limits = failures = 0
        while True:
            req = urllib.request.Request(url, data=data, headers=self.headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                time.sleep(self.pause)
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code in self.rate_limit_codes:
                    rate_limits += 1
                    if rate_limits == 1:
                        self._slow_down()  # once per request, however long the limit lasts
                    wait = _retry_delay(e.headers.get("Retry-After"), rate_limits, 60)
                elif e.code >= 500:
                    failures += 1
                    wait = _retry_delay(None, failures, 5)
                else:
                    detail = e.read()[:300].decode(errors="replace")
                    raise ApiError(f"{method} {path}: HTTP {e.code} {detail}") from None
                print(f"  HTTP {e.code}, retrying in {wait}s...")
            except (urllib.error.URLError, TimeoutError) as e:
                failures += 1
                wait = _retry_delay(None, failures, 5)
                print(f"  Network error ({e}), retrying in {wait}s...")
            if failures >= MAX_ATTEMPTS:
                raise ApiError(f"{method} {path}: gave up after {failures} failed attempts")
            time.sleep(wait)


class AppleMusicClient(JsonClient):
    def __init__(self, developer_token: str, user_token: str):
        headers = {
            "Authorization": f"Bearer {developer_token}",
            "Media-User-Token": user_token,
            "Origin": WEB_PLAYER_ORIGIN,
            "Content-Type": "application/json",
        }
        super().__init__(API, headers, REQUEST_PAUSE)


class ItunesSearchClient(JsonClient):
    """The public iTunes Search API; it never sees the Apple Music tokens."""

    def __init__(self):
        # It is reported to answer 403 rather than 429 when called too fast
        super().__init__(ITUNES_API, {}, ITUNES_PAUSE, rate_limit_codes=(403, 429))


def get_storefront(client: AppleMusicClient) -> dict:
    return client.request("GET", "/v1/me/storefront")["data"][0]


def as_song(result: dict) -> dict:
    """An iTunes Search result in the shape of an Apple Music catalog song."""
    attributes = {
        "name": result.get("trackName", ""),
        "artistName": result.get("artistName", ""),
        "albumName": result.get("collectionName", ""),
        "durationInMillis": result.get("trackTimeMillis"),
    }
    return {"id": str(result["trackId"]), "attributes": attributes}


def songs_from(results: list[dict]) -> list[dict]:
    # Songs sold on iTunes but not streamable cannot go into Apple Music playlists
    return [as_song(r) for r in results
            if r.get("wrapperType") == "track" and r.get("trackId") and r.get("isStreamable", True)]


def search_songs(itunes: JsonClient, storefront: str, term: str) -> list[dict]:
    params = {"term": term, "country": storefront, "media": "music",
              "entity": "song", "limit": SEARCH_LIMIT}
    return songs_from(itunes.request("GET", "/search", params).get("results", []))


def artist_songs(itunes: JsonClient, storefront: str, artist: str) -> list[dict]:
    """Up to 200 catalog songs of the artist whose name matches best, or none."""
    params = {"term": artist, "country": storefront, "media": "music",
              "entity": "musicArtist", "limit": 5}
    found = itunes.request("GET", "/search", params).get("results", [])
    best = max(found, key=lambda a: artist_similarity(artist, a.get("artistName", "")), default=None)
    if best is None or artist_similarity(artist, best.get("artistName", "")) < 0.8:
        return []
    params = {"id": best["artistId"], "entity": "song", "limit": 200, "country": storefront}
    return songs_from(itunes.request("GET", "/lookup", params).get("results", []))


def song_refs(song_ids: list[str]) -> list[dict]:
    return [{"id": song_id, "type": "songs"} for song_id in song_ids]


# --- Matching ---

def _words(text: str) -> str:
    return " ".join(NON_WORD.sub(" ", text.replace("&", " and ")).split())


def normalize(text: str) -> str:
    """Lowercase core of a title or name, without version notes such as "(Live)"."""
    text = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    core = FEATURING.sub("", DASH_SUFFIX.sub("", BRACKETS.sub(" ", text)))
    return _words(core) or _words(text)


def similarity(a: str, b: str) -> float:
    """Fuzzy 0..1 match that also compares Cyrillic and Latin spellings."""
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    plain = difflib.SequenceMatcher(None, a, b).ratio()
    latin = difflib.SequenceMatcher(
        None, a.translate(CYRILLIC_TO_LATIN), b.translate(CYRILLIC_TO_LATIN)
    ).ratio()
    return max(plain, latin)


def artist_similarity(yandex_artists: str, apple_artist: str) -> float:
    """Best match between any exported artist and any artist Apple credits."""
    apple_names = [apple_artist] + ARTIST_SEPARATORS.split(apple_artist)
    yandex_names = yandex_artists.split(", ")
    return max(similarity(y, a) for y in yandex_names for a in apple_names)


def parse_duration(text: str) -> int | None:
    minutes, sep, seconds = text.partition(":")
    if not (sep and minutes.isdigit() and seconds.isdigit()):
        return None
    return int(minutes) * 60 + int(seconds)


def format_duration(millis: int | None) -> str:
    if not millis:
        return ""
    seconds = millis // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def score_candidate(track: dict, song: dict) -> dict:
    attrs = song.get("attributes", {})
    expected = parse_duration(track["duration"])
    actual = attrs.get("durationInMillis")
    diff = abs(expected - actual // 1000) if expected and actual else None
    duration = 0.5 if diff is None else max(0.0, 1 - diff / 30)
    title = similarity(track["title"], attrs.get("name", ""))
    artist = artist_similarity(track["artist"], attrs.get("artistName", ""))
    album = similarity(track["album"], attrs.get("albumName", ""))
    total = 0.45 * title + 0.3 * artist + 0.15 * duration + 0.1 * album
    return {"title": title, "artist": artist, "diff": diff, "total": total}


def classify(scores: dict) -> str:
    duration_ok = scores["diff"] is None or scores["diff"] <= DURATION_TOLERANCE
    if scores["title"] >= 0.85 and scores["artist"] >= 0.8 and duration_ok:
        return "matched"
    # Same title and length is strong evidence even when Apple transliterates
    # the artist unusually (Сплин -> Splean)
    same_length = scores["diff"] is not None and scores["diff"] <= 2
    if scores["title"] >= 0.95 and scores["artist"] >= 0.6 and same_length:
        return "matched"
    if scores["title"] >= 0.6 and scores["artist"] >= 0.5:
        return "check"
    return "missing"


def pick_best(track: dict, candidates: list[dict]) -> tuple[dict, dict] | None:
    scored = [(song, score_candidate(track, song)) for song in candidates]
    return max(scored, key=lambda pair: (STATUS_RANK[classify(pair[1])], pair[1]["total"]),
               default=None)


def find_match(itunes: JsonClient, storefront: str, track: dict):
    first_artist = track["artist"].split(", ")[0]
    candidates = search_songs(itunes, storefront, f"{track['title']} {first_artist}")
    best = pick_best(track, candidates)
    if best is None or classify(best[1]) != "matched":
        # Apple may spell the artist differently (e.g. in Latin), so try the title alone
        candidates += search_songs(itunes, storefront, track["title"])
        best = pick_best(track, candidates)
    return best


def original_of(track: dict) -> dict | None:
    """The original recording a karaoke or tribute copy names in its title, if any."""
    found = COVER_OF.search(track["title"])
    if not found:
        return None
    title = " ".join(BRACKETS.sub(" ", track["title"]).strip(" -").split())
    # The copy's length says nothing about the original's, so it is left out
    return {"title": title, "artist": found.group(1).strip(), "album": "", "duration": ""}


def rematch_covers(itunes: JsonClient, storefront: str, tracks: dict, rows: dict) -> int:
    """Replace karaoke and tribute copies with the original their title names.

    This includes copies Apple carries too, as long as the row does not already point
    to the original artist; 'skip' rows are left alone. Returns the number of replaced rows.
    """
    improved = 0
    for track_id, row in rows.items():
        if row["status"] == "skip" or track_id not in tracks:
            continue
        original = original_of(tracks[track_id])
        if original is None or artist_similarity(original["artist"], row["apple_artist"]) >= 0.8:
            continue
        candidates = search_songs(itunes, storefront, f"{original['title']} {original['artist']}")
        best = pick_best(original, candidates)
        if best and classify(best[1]) == "matched":
            rows[track_id] = match_row(track_id, tracks[track_id], best)
            improved += 1
    return improved


def rematch_by_artist(itunes: JsonClient, storefront: str, tracks: dict, rows: dict) -> int:
    """Look for 'check' and 'missing' tracks in their artist's catalog.

    Search ranking sometimes buries the original under covers, while the artist's
    own song list has it. Rows only ever improve; 'skip' rows are left alone.
    Returns the number of improved rows.
    """
    pending = collections.defaultdict(list)
    for track_id, row in rows.items():
        if row["status"] in ("check", "missing") and track_id in tracks:
            pending[tracks[track_id]["artist"].split(", ")[0]].append(track_id)
    improved = 0
    for artist, track_ids in pending.items():
        songs = artist_songs(itunes, storefront, artist)
        for track_id in track_ids:
            best = pick_best(tracks[track_id], songs)
            if best and STATUS_RANK[classify(best[1])] > STATUS_RANK[rows[track_id]["status"]]:
                rows[track_id] = match_row(track_id, tracks[track_id], best)
                improved += 1
    return improved


def match_row(track_id: str, track: dict, best) -> dict:
    row = dict.fromkeys(MATCH_FIELDS, "")
    row.update(yandex_id=track_id, status="missing", **track)
    if best is None:
        return row
    song, scores = best
    attrs = song.get("attributes", {})
    row.update(
        status=classify(scores),
        apple_id=song["id"],
        apple_title=attrs.get("name", ""),
        apple_artist=attrs.get("artistName", ""),
        apple_album=attrs.get("albumName", ""),
        apple_duration=format_duration(attrs.get("durationInMillis")),
        score=f"{scores['total']:.2f}",
    )
    return row


# --- Export files and local state ---

def read_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_tracks() -> dict[str, dict]:
    """Every exported track that has a title, keyed by Yandex ID.

    Tracks Yandex marks unavailable have no title in the main CSVs; their
    details come from the unavailable_*.csv files, which are read too.
    """
    tracks = {}
    for path in sorted(glob.glob("*.csv")):
        for row in read_csv(path):
            if row["Title"].strip():
                tracks.setdefault(row["ID"], {
                    "title": row["Title"], "artist": row["Artist"],
                    "album": row["Album"], "duration": row["Duration"],
                })
    return tracks


def load_collections() -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Liked track IDs and (playlist name, track IDs) pairs, in export order."""
    liked = glob.glob("liked_tracks_*.csv")
    if len(liked) != 1:
        sys.exit("Expected exactly one liked_tracks_<user>.csv in the export folder.")
    user = liked[0][len("liked_tracks_"):-len(".csv")]
    prefix = f"playlist_{user}_"
    playlists = [
        (path[len(prefix):-len(".csv")], [row["ID"] for row in read_csv(path)])
        for path in sorted(glob.glob(prefix + "*.csv"))
    ]
    return [row["ID"] for row in read_csv(liked[0])], playlists


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


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"playlists": {}, "favorites": []}
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp_path, STATE_PATH)


def unique(items) -> list:
    return list(dict.fromkeys(items))


def chunks(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


# --- Commands ---

def progress(done_count: int, total: int, started: float, verb: str) -> str:
    minutes_left = (time.monotonic() - started) / done_count * (total - done_count) / 60
    return f"  {done_count}/{total} {verb}, about {minutes_left:.0f} min left"


def print_status_summary(rows) -> None:
    counts = collections.Counter(row["status"] for row in rows)
    print("Results: " + ", ".join(f"{s}={counts.get(s, 0)}" for s in STATUS_RANK))


def cmd_check(client: AppleMusicClient) -> None:
    storefront = get_storefront(client)
    name = storefront.get("attributes", {}).get("name", "")
    print(f"Tokens work. Apple Music storefront: {storefront['id']} ({name})")


def cmd_match(client: AppleMusicClient) -> None:
    tracks = load_tracks()
    done = load_matches()
    storefront = get_storefront(client)["id"]
    itunes = ItunesSearchClient()
    search_tracks(itunes, storefront, tracks, done)
    print_status_summary(done.values())
    later_passes = [
        ("Covers: looking up the originals named in karaoke and tribute titles", rematch_covers),
        ("Artists: looking in the catalogs of artists with unmatched tracks", rematch_by_artist),
    ]
    for description, rematch in later_passes:
        print(f"{description}...")
        print(f"  {rematch(itunes, storefront, tracks, done)} tracks improved")
        write_matches(done)
    print_status_summary(done.values())


def search_tracks(itunes: JsonClient, storefront: str, tracks: dict, done: dict) -> None:
    """Search every track not yet in matches.csv, adding rows to it and to done."""
    todo = [(track_id, track) for track_id, track in tracks.items() if track_id not in done]
    print(f"Storefront {storefront}: {len(tracks)} tracks, {len(todo)} left to search")
    os.makedirs(APPLE_DIR, exist_ok=True)
    write_header = not os.path.exists(MATCHES_PATH)
    started = time.monotonic()
    # Appending row by row means an interrupted run resumes where it stopped
    with open(MATCHES_PATH, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MATCH_FIELDS)
        if write_header:
            writer.writeheader()
        for i, (track_id, track) in enumerate(todo, 1):
            done[track_id] = match_row(track_id, track, find_match(itunes, storefront, track))
            writer.writerow(done[track_id])
            f.flush()
            if i % 25 == 0 or i == len(todo):
                print(progress(i, len(todo), started, "searched"))


def import_playlist(client: AppleMusicClient, state: dict, name: str, songs: list[str]) -> None:
    if not songs:
        print(f"  '{name}': no matched tracks, skipped")
        return
    entry = state["playlists"].get(name)
    if entry is None:
        first = songs[:CHUNK_SIZE]
        body = {
            "attributes": {"name": name, "description": "Imported from Yandex Music"},
            "relationships": {"tracks": {"data": song_refs(first)}},
        }
        created = client.request("POST", "/v1/me/library/playlists", body=body)
        entry = state["playlists"][name] = {"id": created["data"][0]["id"], "songs": first}
        save_state(state)
    added = set(entry["songs"])
    for chunk in chunks([s for s in songs if s not in added], CHUNK_SIZE):
        path = f"/v1/me/library/playlists/{entry['id']}/tracks"
        client.request("POST", path, body={"data": song_refs(chunk)})
        entry["songs"].extend(chunk)
        save_state(state)
    print(f"  '{name}': {len(entry['songs'])} tracks")


def import_favorites(client: AppleMusicClient, state: dict, songs: list[str]) -> None:
    added = set(state["favorites"])
    pending = [s for s in songs if s not in added]
    started = time.monotonic()
    # The web API takes only one song per favorites request
    for i, song_id in enumerate(pending, 1):
        client.request("POST", "/v1/me/favorites", params={"ids[songs]": song_id})
        state["favorites"].append(song_id)
        save_state(state)
        if i % 50 == 0 or i == len(pending):
            print(progress(i, len(pending), started, "favorited"))
    print(f"  Favorites: {len(state['favorites'])} songs")


def cmd_import(client: AppleMusicClient, apply: bool, with_playlists: bool) -> None:
    matches = load_matches()
    if not matches:
        sys.exit("Run 'match' first.")
    apple_ids = {track_id: row["apple_id"] for track_id, row in matches.items()
                 if row["status"] == "matched" and row["apple_id"]}
    liked, playlists = load_collections()
    planned = [(name, unique(apple_ids[t] for t in ids if t in apple_ids), len(ids))
               for name, ids in playlists]
    # Playlist songs first and likes oldest first, so Favorites sorted by date added
    # ends with what was liked most recently
    favorites = unique([song for _, songs, _ in planned for song in songs]
                       + [apple_ids[t] for t in reversed(liked) if t in apple_ids])
    print(f"Favorites: {len(favorites)} songs from {len(liked)} liked tracks "
          f"and {len(playlists)} playlists")
    if with_playlists:
        for name, songs, total in planned:
            print(f"Playlist '{name}': {len(songs)} of {total} tracks")
    unreviewed = sum(row["status"] == "check" for row in matches.values())
    print(f"Skipped: {unreviewed} tracks marked 'check' in {MATCHES_PATH}")
    if not apply:
        print("Nothing changed. Run 'import --apply' to add this to Apple Music.")
        return
    state = load_state()
    if with_playlists:
        for name, songs, _ in planned:
            import_playlist(client, state, name, songs)
    import_favorites(client, state, favorites)
    print("Done.")


def load_tokens() -> tuple[str, str]:
    tokens = [os.environ.get(name, "").strip() for name in ("APPLE_DEV_TOKEN", "APPLE_USER_TOKEN")]
    if not all(tokens) or TOKEN_PLACEHOLDER in tokens:
        sys.exit("Set APPLE_DEV_TOKEN and APPLE_USER_TOKEN first (see run/.apple.env).")
    return tokens[0].removeprefix("Bearer "), tokens[1]


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command not in ("check", "match", "import"):
        print(__doc__)
        return 2
    client = AppleMusicClient(*load_tokens())
    try:
        if command == "check":
            cmd_check(client)
        elif command == "match":
            cmd_match(client)
        else:
            cmd_import(client, apply="--apply" in argv[2:], with_playlists="--playlists" in argv[2:])
    except ApiError as e:
        print(f"Apple Music error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
