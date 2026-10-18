# Yandex Music exporter

`fetch_tracks.py` comes from
[Ermegilius/yMusic_playlist_importer](https://github.com/Ermegilius/yMusic_playlist_importer)
at commit `84c0631` and is MIT-licensed (see [LICENSE](LICENSE)). Upstream's
[USAGE.md](https://github.com/Ermegilius/yMusic_playlist_importer/blob/84c0631/USAGE.md)
documents the API calls it makes and the CSV files it writes.

One change was made: liked tracks that belong to no album (your own uploads, for
example) have no `albumId`, which stopped the upstream version with
`KeyError: 'albumId'`. They are now requested by their bare ID. The git history
shows the diff.

`run/run.sh` checks the script's SHA-256 before every run and refuses to start if it
differs from the reviewed version, so an unnoticed edit never runs with your token.
After reviewing a change, update `EXPECTED_SHA` there.
