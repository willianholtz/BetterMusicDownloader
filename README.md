# YouTube / Spotify → MP3 (Vivaldi extension)

Click the extension icon on any YouTube (or YouTube Music) video — or on a
Spotify track, album or playlist — and the audio is downloaded and converted to
**MP3 (VBR ~245–285 kbps, lame -V0)** into your music folder, with metadata and
cover art embedded.

```
Vivaldi (extension)  --HTTP-->  local service 127.0.0.1:8765  -->  yt-dlp  -->  ffmpeg  -->  .mp3
```

## Why a local service is needed

A browser cannot download and convert YouTube audio on its own: the stream
requires deciphering the player signature, and there is no ffmpeg access inside
an extension. So the extension is just the UI; the heavy lifting is done by a
local service (`server/ytflac_server.py`) running yt-dlp + ffmpeg.

## Spotify

Track, album and playlist links work with the same button. The audio does **not**
come from Spotify (that would require breaking DRM): the server reads the public
metadata of the page (title, artists, date, cover), finds the matching video on
YouTube by **duration** and writes the MP3 with:

- title, artists, album, album artist, date, track/disc number;
- ISRC (from Deezer) and the 640×640 Spotify cover;
- the Spotify link in `comment`/`description`.

Albums and playlists are downloaded track by track into a subfolder named after
the collection. Album/track info comes from iTunes (which gets the original
release right) and the ISRC from Deezer — Spotify's public embed does not expose
the album of a standalone track.

## Quality

YouTube delivers at most **Opus ~160 kbps** (free) or **~256 kbps** (Premium, with
`cookies.txt`). The service downloads the best available track and converts it to
**MP3 VBR lame -V0** (~245–285 kbps), which is transparent for that source.

- Converting to MP3 does **not** create quality that did not exist — the source
  is already compressed. The gain is universal compatibility (every player plays
  MP3) and reasonable size (~7 MB per track instead of ~45 MB for FLAC).
- For truly lossless quality, use the original source (CD, Bandcamp, Qobuz…)
  or export cookies from a **YouTube Premium** account in the popup → Settings.
- The selector uses `bestaudio/best`: picks Opus 251 (160 kbps) by default.

## Installation

```bash
cd ~/Documentos/GitHub/youtube-flac
bash install.sh
```

The script:
1. checks `python3` and `ffmpeg`;
2. downloads `yt-dlp` into `~/.local/bin` if missing;
3. installs and enables the `ytflac.service` (systemd `--user`);
4. creates `~/Música/YouTube FLAC`;
5. shows how to load the extension in Vivaldi.

### Load in Vivaldi

1. `vivaldi://extensions`
2. enable **Developer mode**
3. **Load unpacked** → pick `~/Documentos/GitHub/youtube-flac/extension`

## Usage

- Open the video (or track/album/playlist) → click the icon → **Download as MP3**.
- Or right-click the page/link → **Download audio as MP3**.
- The popup shows progress; when done, **save .mp3** stores the file wherever you
  want (it is already in the destination folder too).
- **open folder** opens the destination folder.
- **File name:** `Artist - Title.mp3`, no ID in the name. Duplicate control lives
  in the index `~/.config/ytflac/downloaded.json`
  (Spotify track ID / YouTube video ID → file path).
- If the track/video already exists in any subfolder of the destination, the job
  finishes instantly (no re-download) — you can re-submit a whole playlist and it
  only completes what is missing. To force a re-download, send
  `{"url": "...", "force": true}` to the API.

## Settings (popup → Settings)

| Field | Purpose |
|---|---|
| Destination folder | where the files are saved |
| cookies.txt | optional, for videos requiring login (and Premium for 256 kbps) |
| Output format | **MP3** (default), **M4A/AAC**, **Opus**, **OGG Vorbis**, **FLAC** (re-encoded, not true lossless), **ALAC**, **WAV**, **Original** (no conversion) |
| Normalize volume | applies loudnorm (EBU R128) — avoids "quiet" tracks |
| Minimum quality (kbps) | 0 = accept everything. YouTube free delivers at most ~160 kbps. |

The `cookies.txt` can be exported with the "Get cookies.txt LOCALLY" extension.

> **Note on FLAC:** the FLAC option re-encodes the YouTube stream (Opus ~160 kbps)
> into FLAC. The file gets large (~45 MB) but does **not** recover the quality
> YouTube discarded — it is not truly lossless. For real FLAC, download from a
> lossless source (Qobuz, Tidal, Bandcamp, CD) manually.

## API (for debugging)

```bash
curl 127.0.0.1:8765/api/health
curl '127.0.0.1:8765/api/resolve?url=https://open.spotify.com/track/...'
curl -X POST 127.0.0.1:8765/api/jobs -H 'Content-Type: application/json' \
     -d '{"url":"https://www.youtube.com/watch?v=..."}'
curl -X POST 127.0.0.1:8765/api/jobs -H 'Content-Type: application/json' \
     -d '{"url":"https://open.spotify.com/album/..."}'
curl '127.0.0.1:8765/api/jobs?id=<id>'
```

## Maintenance

```bash
# update yt-dlp (YouTube changes frequently)
~/.local/share/ytflac/venv/bin/pip install -U "yt-dlp[default]"

# service logs
journalctl --user -u ytflac -n 50 -f

# restart / stop
systemctl --user restart ytflac
systemctl --user stop ytflac
```

`yt-dlp` runs in its own venv (`~/.local/share/ytflac/venv`) because it brings
`mutagen` and the extras used to embed title/artist/cover in the MP3.

**JS runtime:** yt-dlp uses `deno` or `node` to solve YouTube's challenges. The
service detects it automatically (`deno` → `node` → `bun`) and passes
`--js-runtimes`. Without any of them extraction still works, but with fewer
available formats and a warning in the log.

## Security

- The service listens **only** on `127.0.0.1` (not exposed on the network).
- It only accepts URLs from YouTube domains.
- It only serves `.mp3` (and legacy `.flac`) files inside the configured
  destination folder.

## Structure

```
youtube-flac/
├── install.sh
├── README.md
├── server/
│   └── ytflac_server.py
└── extension/
    ├── manifest.json
    ├── background.js
    ├── popup.html
    ├── popup.js
    ├── popup.css
    └── icons/
```
EOF
echo "written"