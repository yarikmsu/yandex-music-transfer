# Amazon Music import

Likes every song of the Yandex Music export in `../out` in Amazon Music, liked tracks
and playlist songs alike, so they show up in **Liked Songs**. No playlists are created
and nothing is added to the library in any other way.

## Login

Amazon Music answers no searches without a login, so every command except the dry-run
`import` uses the authorization value of a signed-in music.amazon.com session.

1. Open a new private window in Chrome (⇧⌘N) and sign in at https://music.amazon.com
   with the account whose Liked Songs should get the songs.
2. Open DevTools (⌥⌘I), **Network** tab, type `gql` in the filter box, then click
   around the page (Library, Home) so requests appear.
3. Click a request to `gql.music.amazon.dev`. Under **Request Headers**, right-click
   `authorization` (its value starts with `AmznMusic `) and choose **Copy value**.
4. Copy `run/amazon.env.example` to `run/.amazon.env` and paste the value after
   `AMAZON_MUSIC_AUTH=` with a text editor, all on one line.

The value is the web player's own Amazon Music access token, not your amazon.com
login: it cannot place orders, and it **expires after about an hour**. When it does, the
command stops with a message; copy a fresh value the same way, paste it over the old
one, and run the same command again: it continues where it stopped. The value is sent
only to gql.music.amazon.dev and never printed. When the import is done, put
`paste_here` back into `run/.amazon.env` and sign out in that private window.
Amazon does not support using the web player this way and may block it at any time.

## Steps

```
bash run/amazon.sh check            # login works? shows the account ID
bash run/amazon.sh match            # writes out/amazon/matches.csv
                                    # review it, see "Matching" below
bash run/amazon.sh import           # dry run, no login: what would be liked
bash run/amazon.sh import --apply   # like the songs (one request per song)
```

## Matching

Each exported track is searched by title and artist, then by title alone. The rules
are the YouTube Music importer's ([YOUTUBE_MUSIC.md](YOUTUBE_MUSIC.md#matching)):
title and artist agreeing is `matched` whatever the length, karaoke and tribute copies
are at most `check`, and where your reviewed Apple matches in `out/apple/matches.csv`
name another artist or title, Amazon is searched for that Apple song instead.

The `status` in `matches.csv` is `matched` (will be liked), `check` (a likely candidate
that needs your decision) or `missing` (nothing close). To hear a candidate, open
`https://music.amazon.com/tracks/<asin>`. To like a `check` row, change its status to
`matched`. To use a different song, paste its track ID into `asin`: the 10 characters
after `/tracks/` in its link, or after `trackAsin=` in an album link with that track
selected. To leave a track out, set its status to `missing` or `skip`; reruns of
`match` never touch rows already in the file. Keep the file as UTF-8 CSV.

`import --apply` refuses to start while any row is still `check`, a track has not been
searched yet, or an edited row has an unknown status or an `asin` that is not a track ID,
because likes cannot be reordered afterwards. The dry run lists those rows.

## Order

Songs that are only in playlists are liked first, then your liked tracks from oldest to
newest, so that your most recent Yandex likes end up on top of Liked Songs, which lists
the latest like first. Look at Liked Songs after the first few likes: if the order is
the other way round, stop with Ctrl+C. A song you approve after the first
`import --apply` is still liked on a rerun, but it lands above everything liked before.

## Rate limits and reruns

Searches are paced at one a second, likes at one every 2 seconds, so ~1,800 tracks take
about an hour to match and about as long to like; expect to copy a fresh login once or
twice. After an `HTTP 429` the script slows down and waits the block out.

- `match` skips tracks already in `matches.csv`; delete a row to search it again.
- `import --apply` records every song it liked in `out/amazon/state.json` and only likes
  what is missing. It never unlikes, dislikes or removes anything; a like that fails
  stops the run, so nothing is skipped out of order.
- `out/amazon/not_imported.csv` lists every track that will not be liked, where it was
  and its closest Amazon candidate.
