# Spotify import

Likes every song of the Yandex Music export in `../out` in Spotify, liked tracks and
playlist songs alike, so they show up in **Liked Songs**. No playlists are created and
nothing is added to the library in any other way. A free account is enough.

## Login

Spotify's developer API works only for apps whose owner has Premium, so the importer signs
in the way the open.spotify.com web player does, with its `sp_dc` cookie. Every command
except the dry-run `import` needs it.

1. Open a new private window in Chrome (⇧⌘N) and sign in at https://open.spotify.com
   with the account whose Liked Songs should get the songs.
2. Open DevTools (⌥⌘I), the **Application** tab, then **Storage** → **Cookies** →
   `https://open.spotify.com`, and type `sp_dc` in the filter box.
3. Click the `sp_dc` row and copy its value. If there is no `sp_dc`, the window is not
   signed in yet.
4. Copy `run/spotify.env.example` to `run/.spotify.env` and paste the value after
   `SPOTIFY_SP_DC=` with a text editor, on one line. `sp_dc=…` or a whole copied `cookie`
   header works too.

The cookie is your Spotify login. The importer sends it only to open.spotify.com, to get
the web player's token (which lasts about 50 minutes and is renewed as the run goes on),
and never prints it. When the import is done, put `paste_here` back into
`run/.spotify.env`, then sign out in that private window, which ends the session the
cookie belongs to. If a command says the cookie was not accepted, copy it again and run
the same command: it continues where it stopped.

Spotify does not support using the web player this way and may block it at any time. Its
login code and the IDs of its requests change with each player release, so the importer
reads them from the player's current code on every run. An error saying the web player
may have changed means a release broke that; a rerun later, or an importer update, is the
way out.

## Steps

```
bash run/spotify.sh check            # cookie works? shows the account it signs in to
bash run/spotify.sh match            # writes out/spotify/matches.csv
                                     # review it, see "Matching" below
bash run/spotify.sh import           # dry run, no login: what would be liked
bash run/spotify.sh import --apply   # like the songs (one request per song)
```

## Matching

Each exported track is searched by title with its first artist, then by title alone
(Spotify may spell the artist otherwise). The rules are the Amazon importer's
([AMAZON_MUSIC.md](AMAZON_MUSIC.md#matching)): title and artist agreeing is `matched`,
karaoke and tribute copies are at most `check`, live and remixed versions are chosen only
when nothing else matches, and your reviewed Apple matches in `out/apple/matches.csv` are
searched for where they name another artist or title.

The `status` in `matches.csv` is `matched` (will be liked), `check` (a likely candidate
that needs your decision) or `missing` (nothing close). To hear a candidate, open
`https://open.spotify.com/track/<spotify_id>`. To like a `check` row, change its status to
`matched`. To use a different song, paste its link (**Share** → **Copy Song Link**) or
its track ID, the 22 characters after `/track/`, into `spotify_id`. To leave a track out,
set its status to `missing` or `skip`; reruns of `match` never touch rows already in the
file. Keep the file as UTF-8 CSV.

`import --apply` refuses to start while any row is still `check`, a track has not been
searched yet, or an edited row has an unknown status or a `spotify_id` that is not a
track, because likes cannot be reordered afterwards. The dry run lists those rows.

## Order

Songs that are only in playlists are liked first, then your liked tracks from oldest to
newest, so that your most recent Yandex likes end up on top of Liked Songs, which lists
the latest like first. Liking a song that is already liked moves it back to the top, so:

- Each song is liked once per import; `out/spotify/state.json` keeps track, and a rerun
  only likes what is missing.
- Songs you had liked before the import are liked again at their place in the Yandex
  order; liked songs that are not in the export stay below everything.
- After the first like of each run the importer reads Liked Songs back and stops if that
  song is not on top.

A song you approve after the first `import --apply` is still liked on a rerun, but it
lands above everything liked before.

## Rate limits and reruns

Searches are paced at one a second, likes at one every 2 seconds, so ~1,800 tracks take
about an hour to match and about as long to like. After an `HTTP 429` the script slows
down and waits the block out.

- Spotify sometimes answers a request with an error of its own (such as `Failed call to
  search-api`, which came after a few hundred searches in testing). Such a request is
  tried up to 8 times, waiting longer each time, about 10 minutes in all. If it still
  fails, the run stops. A search names the track and the line that skips it; a like
  stops so the order stays right.
- `match` skips tracks already in `matches.csv`; delete a row to search it again.
- `import --apply` records every song it liked in `out/spotify/state.json` and only likes
  what is missing. It never unlikes or removes anything; a like that fails stops the run,
  so nothing is skipped out of order.
- `out/spotify/not_imported.csv` lists every track that will not be liked, where it was
  and its closest Spotify candidate.
