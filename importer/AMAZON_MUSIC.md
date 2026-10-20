# Amazon Music import

Likes every song of the Yandex Music export in `../out` in Amazon Music, liked tracks
and playlist songs alike, so they show up in **Liked Songs**. No playlists are created
and nothing is added to the library in any other way.

## Login

Amazon Music answers no searches without a login, so every command except the dry-run
`import` uses the cookie of a signed-in music.amazon.com session.

1. Open a new private window in Chrome (⇧⌘N) and sign in at https://music.amazon.com
   with the account whose Liked Songs should get the songs.
2. Open DevTools (⌥⌘I), **Network** tab, type `gql` in the filter box, then click
   around the page (Library, Home) so requests appear.
3. Click a request to `gql.music.amazon.com`. Under **Request Headers**, right-click
   `cookie` and choose **Copy value**.
4. Copy `run/amazon.env.example` to `run/.amazon.env` and paste the cookie after
   `AMAZON_MUSIC_COOKIE=` with a text editor, all on one line.

**The cookie is your amazon.com login, shopping included**: the web player signs in with
nothing narrower. The importer keeps only the session cookies (`session-id`, `ubid-main`,
`at-main`, `sess-at-main`, `x-main` and a few related ones) and drops the rest, sends them
only to music.amazon.com and gql.music.amazon.com, and never prints them. It reads the
device and session details the player uses from music.amazon.com/config.json itself.
When the import is done, put `paste_here` back into `run/.amazon.env`, then sign out in
that private window, which ends the session the cookie belongs to. If a command says
the cookie was not accepted, copy it again and run the same command: it continues where
it stopped. Only the US store (music.amazon.com) is supported.
Amazon does not support using the web player this way and may block it at any time.

## Steps

```
bash run/amazon.sh check            # cookie works? shows the account it signs in to
bash run/amazon.sh match            # writes out/amazon/matches.csv
                                    # review it, see "Matching" below
bash run/amazon.sh import           # dry run, no login: what would be liked
bash run/amazon.sh import --apply   # like the songs (one request per song)
```

## Matching

Each exported track is searched by title with its first artist, then by title alone
(Amazon may spell the artist otherwise). The rules are the YouTube Music importer's
([YOUTUBE_MUSIC.md](YOUTUBE_MUSIC.md#matching)): title and artist agreeing is `matched`
whatever the length, karaoke and tribute copies are at most `check`, and where your
reviewed Apple matches in `out/apple/matches.csv` name another artist or title, Amazon is
searched for that Apple song instead. Amazon often has live and remixed versions of a
song too; one whose title notes, in brackets or after a dash, say Live, Mix, Remix, Demo,
Acoustic, Instrumental or Unplugged, while your track's do not (or the other way round),
is chosen only when nothing else matches. Such a word in the song's own name, as in
"Live Forever", is no note. Remastered versions count as the same recording.

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
the latest like first. After the second like of a run the importer reads Liked Songs back
and stops if the latest like is not on top, so a different order costs two likes, not
hundreds. A song you approve after the first `import --apply` is still liked on a rerun,
but it lands above everything liked before.

## Rate limits and reruns

Searches are paced at one a second, likes at one every 2 seconds, so ~1,800 tracks take
about an hour to match and about as long to like. After an `HTTP 429` the script slows
down and waits the block out.

- `match` skips tracks already in `matches.csv`; delete a row to search it again.
- `import --apply` records every song it liked in `out/amazon/state.json` and only likes
  what is missing. It never unlikes, dislikes or removes anything; a like that fails
  stops the run, so nothing is skipped out of order.
- `out/amazon/not_imported.csv` lists every track that will not be liked, where it was
  and its closest Amazon candidate.
