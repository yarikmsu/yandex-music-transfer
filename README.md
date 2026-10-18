# Yandex Music to Apple Music

Moves a Yandex Music library to Apple Music. It exports your liked tracks and
playlists to CSV, finds each track in the Apple Music catalog, and adds the
matches to your Apple Music Favorites. It can also recreate the playlists.

Both steps run on your machine in a locked-down Docker container, using only the
Python standard library.

## Requirements

- Docker
- A Yandex Music account and an Apple Music subscription
- Chrome or another browser with developer tools, to copy the Apple Music tokens

## 1. Export from Yandex Music

1. Sign in to Yandex in your browser and open
   `https://oauth.yandex.ru/authorize?response_type=token&client_id=23cabbbdc6cd418abb4b39c32c41195d`.
   It redirects to an error page; copy the `access_token` value from its URL
   (the part after `access_token=` up to `&`).
   That `client_id` belongs to the official Yandex Music app and is widely used
   by open-source tools; Yandex has not published it for this purpose.
2. `cp run/env.example run/.env` and paste the token into `run/.env`.
3. `bash run/run.sh` writes `out/liked_tracks_<name>.csv` and one
   `out/playlist_<name>_<title>.csv` per playlist.
4. Revoke the token: in Yandex ID → Security, or by changing your password.
   Put `your_token_here` back into `run/.env`.

The exporter is a copy of
[Ermegilius/yMusic_playlist_importer](https://github.com/Ermegilius/yMusic_playlist_importer)
with one fix; see [exporter/README.md](exporter/README.md).

## 2. Import into Apple Music

1. `cp run/apple.env.example run/.apple.env` and paste the two tokens of your
   signed-in music.apple.com session into it. [importer/README.md](importer/README.md)
   shows where to find them.
2. Run:

   ```
   bash run/apple.sh check            # tokens work? shows your storefront
   bash run/apple.sh match            # read-only: writes out/apple/matches.csv
   bash run/apple.sh import           # dry run
   bash run/apple.sh import --apply   # add the matched songs to Favorites
   ```

   Matching about 1,800 tracks takes roughly two hours because of search rate
   limits. Review `out/apple/matches.csv` before importing. Rows marked `check` or
   `missing` are skipped until you change them; [importer/README.md](importer/README.md)
   explains the statuses, the `--playlists` option and how reruns work.
3. Put `paste_here` back into `run/.apple.env` and sign out of music.apple.com.

## Safety

- The container has a read-only file system, no Linux capabilities and no
  privilege escalation. It runs as your user and sees only the script (read-only)
  and `out/`.
- `run/run.sh` refuses to run if the exporter differs from the reviewed version.
- Tokens arrive through env files, never on the command line. `run/.env`,
  `run/.apple.env` and `out/` (your library) are git-ignored.
- The Apple Music tokens are sent only to Apple Music. Catalog searches go to the
  public iTunes Search API with no tokens at all.
- Neither service officially supports access this way, so either may block it
  at any time.

## Tests

The importer's unit tests run offline:

```
docker run --rm --network none -v "$PWD/importer:/app:ro" -w /app python:3.13-slim python -B -m unittest
```

## License

[MIT](LICENSE). The Yandex Music exporter in `exporter/` is by
[Ermegilius](https://github.com/Ermegilius), also under the MIT license, with its own
copyright notice in [exporter/LICENSE](exporter/LICENSE).
