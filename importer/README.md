# Apple Music import

Imports the Yandex Music export in `../out` into Apple Music: every matched song,
liked or in a playlist, becomes a Favorite. With `--playlists`, the exported playlists
are recreated too.

## Tokens

The script uses the two tokens of your signed-in music.apple.com web session.

1. Sign in at https://music.apple.com in Chrome and open DevTools (⌥⌘I), **Network** tab.
2. Type `amp-api` in the filter box, then open your Library in the page so requests appear.
3. Click any request and, under **Request Headers**, copy `authorization`:
   everything after `Bearer ` → `APPLE_DEV_TOKEN`.
4. In the **Application** tab, open **Storage → Cookies → https://music.apple.com** and copy
   the value of the `media-user-token` cookie → `APPLE_USER_TOKEN`.
   It exists only while you are signed in.
5. Copy `run/apple.env.example` to `run/.apple.env` and paste both into it with a text editor.

The user token gives full access to your Apple Music library. When the import is done,
put `paste_here` back into `run/.apple.env` and sign out of music.apple.com.
Apple does not support using web-player tokens this way and may block it at any time.

## Steps

```
bash run/apple.sh check            # tokens work? shows your storefront (country)
bash run/apple.sh match            # read-only: writes out/apple/matches.csv
                                   # review it, see "Matching" below
bash run/apple.sh import           # dry run: what would be added
bash run/apple.sh import --apply   # add the songs to Favorites (one request per song)
bash run/apple.sh import --apply --playlists   # ...and recreate the playlists
```

## Matching

Each exported track is searched by title and artist, then by title alone if needed.
Search ranking sometimes buries an original under covers, so a second pass then looks
for the remaining tracks in their artist's own song list (up to 200 songs per artist).
The best candidate is compared on title, artist (Cyrillic and Latin spellings both count)
and duration. Its `status` in `matches.csv` is:

- `matched`: title and artist agree and the length is within 10 seconds; imported
- `check`: a likely candidate, but something differs (often a live or remastered version); skipped
- `missing`: nothing close in your storefront; skipped

To import a `check` row, change its status to `matched`. To use a different song,
paste its ID from the music.apple.com song link (`?i=<id>`) into `apple_id`.
To rule a track out for good, set its status to `skip`; reruns of `match` never touch it.
Keep the file as UTF-8 CSV when saving.

## Rate limits

Catalog searches with the web-player tokens are blocked after a few dozen per hour, so
`match` searches through Apple's public iTunes Search API instead. It covers the same
catalog with the same song IDs, needs no tokens, and allows about 20 calls a minute,
so ~1,800 tracks take about two hours. The tokens are used only for the storefront and `import`.

Both APIs are paced. After an `HTTP 429` (or 403 from iTunes) the script slows down and
waits out the block, up to 10 minutes between tries. It never gives up on a rate limit.

## Reruns

- `match` skips tracks already in `matches.csv`; delete a row to search it again.
  It is safe to stop at any time (Ctrl+C) and run again later.
- `import --apply` records what it created in `out/apple/state.json` and only adds
  what is missing, so rerunning after an error or after editing matches is safe.
  Delete a playlist's entry there to have it created again.
