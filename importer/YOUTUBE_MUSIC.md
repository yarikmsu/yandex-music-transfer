# YouTube Music import

Likes every song of the Yandex Music export in `../out` in YouTube Music, liked tracks
and playlist songs alike, so they show up in **Liked music**. A free YouTube Music
account is enough. No playlists are created.

## Cookie

Searching needs no login. Only `check` and `import --apply` act as you, using the cookie
of a signed-in music.youtube.com session.

1. Open a new private window in Chrome (⇧⌘N) and sign in at https://music.youtube.com
   with the account whose Liked music should get the songs.
2. Open DevTools (⌥⌘I), **Network** tab, type `browse` in the filter box, then open
   **Library** in the page so requests appear.
3. Click one of the `browse` requests. Under **Request Headers**, right-click `cookie` and
   choose **Copy value**.
4. Copy `run/ytmusic.env.example` to `run/.ytmusic.env` and paste the cookie after
   `YTMUSIC_COOKIE=` with a text editor, all on one line. If the browser was signed in to
   several Google accounts, also set `YTMUSIC_AUTHUSER` to the request's
   `x-goog-authuser` value.

**The cookie is a full Google login**, not just YouTube: it can reach your Gmail and Drive
too. It is sent only to music.youtube.com, and only by `check` and `import --apply`.
When the import is done, put `paste_here` back into `run/.ytmusic.env`, then sign out in
that private window, which ends the session the cookie belongs to.
YouTube does not support using the web player this way and may block it at any time.

## Steps

```
bash run/ytmusic.sh match            # no login: writes out/ytmusic/matches.csv
                                     # review it, see "Matching" below
bash run/ytmusic.sh check            # cookie works? shows the account it signs in to
bash run/ytmusic.sh import           # dry run: what would be liked, writes not_imported.csv
bash run/ytmusic.sh import --apply   # like the songs (one request per song)
```

## Matching

Each exported track is searched among YouTube Music's songs by title and artist, then
by title alone, then among videos. The best candidate is compared on title, artist
(Cyrillic and Latin spellings both count) and length, the same way the Apple importer
does. Where your reviewed Apple matches in `out/apple/matches.csv` name another artist or
title, for example the original instead of a karaoke copy, YouTube is searched for that
Apple song instead, so your review carries over.

The `status` in `matches.csv` is:

- `matched`: an official album track whose title and artist agree and whose length is
  within 10 seconds; it will be liked
- `check`: a likely candidate, but something differs, or YouTube has only a video of it
  (`youtube_type` is `video`: a music video, live recording or fan upload). Videos are
  never liked unless you approve them
- `missing`: nothing close; not liked

To hear a candidate, open `https://music.youtube.com/watch?v=<video_id>`. To like a `check`
row, change its status to `matched`. To use a different song, paste the `v=` part of its
music.youtube.com link into `video_id`. To leave a track out, set its status to `missing`
or `skip`; reruns of `match` never touch rows already in the file. Keep the file as
UTF-8 CSV when saving.

`import --apply` refuses to start while any row is still `check`, a track has not been
searched yet, or an edited row has an unknown status or a `video_id` that is not
11 characters long, because likes cannot be reordered afterwards (see below). The dry run
lists those rows.

## Order

Liked music lists the most recently liked song first. So songs that are only in
playlists are liked first, then your liked tracks from oldest to newest, and your most
recent Yandex likes end up on top. A song you approve after the first `import --apply`
is still liked on a rerun, but it lands above everything liked before.

## Rate limits

Searches are paced at one every 1.5 seconds, likes at one every 3 seconds, so
~1,800 tracks take one to two hours to match and about as long to like. After an
`HTTP 429` the script slows down and waits the block out, up to 10 minutes between
tries; it never gives up on a rate limit. A like that fails for another reason stops
the run, so nothing is skipped out of order.

## Reruns

- `match` skips tracks already in `matches.csv`; delete a row to search it again.
  It is safe to stop at any time (Ctrl+C) and run again later.
- `import --apply` records every song it liked in `out/ytmusic/state.json` and only
  likes what is missing, so rerunning after an error is safe. It never unlikes,
  dislikes or removes anything.
- `out/ytmusic/not_imported.csv` lists every track that will not be liked, where it was
  (`liked` or the playlist names) and its closest YouTube candidate.
