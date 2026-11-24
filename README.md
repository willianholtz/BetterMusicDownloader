# YouTube / Spotify / Qobuz / Amazon Music → audio (Vivaldi extension)

Click the extension icon on a YouTube (or YouTube Music) video, a Spotify/Qobuz
track/album/playlist, or an **Amazon Music album/track** — and the audio is
downloaded into your music folder with metadata and cover art embedded.

- YouTube/Spotify/Qobuz → **MP3 (VBR ~245–285 kbps, lame -V0)** via yt-dlp + ffmpeg.
- Amazon Music albums/tracks → **true FLAC** (original quality) via `lucidadl`.

```
Vivaldi (extension) --HTTP--> local service 127.0.0.1:8765 --+--> yt-dlp --> ffmpeg --> .mp3
                                                            +--> lucidadl (Amazon) --> .flac
```

## Why a local service is needed

A browser cannot download and convert YouTube audio on its own: the stream
requires deciphering the player signature, and there is no ffmpeg access inside
an extension. So the extension is just the UI; the heavy lifting is done by a
local service (`server/ytflac_server.py`) running yt-dlp + ffmpeg. For Amazon
Music the service calls `lucidadl` (Playwright-based) in a subprocess, which
pulls the original FLAC stream — no DRM cracking happens here.

## Features

- **Multi-source metadata**: Spotify and Qobuz track/album/playlist links. Audio always comes from YouTube (matched by duration); tags come from the source.
- **Amazon Music FLAC**: albums and tracks download as **true FLAC** (original quality) via `lucidadl`. Playlists are not supported by the resolver.
- **Parallel downloads**: configurable 1–14 simultaneous downloads (default 14). Speeds up large albums/playlists.
- **Progress tracking**: per-track progress bar that flows smoothly across all phases (search → download → convert → tag), plus an overall album/playlist progress card (e.g. "Human After All — 3/10 · 30%").
- **Preview / "Buscar info"**: inspect metadata and track list (with "already downloaded" status) before downloading. Click a track to download just that one.
- **Smart folders**: albums/playlists download into a subfolder named after the collection. Single tracks downloaded from a collection can optionally go into the same folder (configurable, default on).
- **Skip existing**: already-downloaded tracks are detected (by filename + ISRC index) and skipped automatically. Force re-download available.
- **Retry with backoff**: 403 errors retry with exponential backoff (1s, 4s, 9s) across up to 3 candidates.
- **Normalization**: pure dB gain to a configurable loudness target (-23 to -6 LUFS, default **-12**). Measures integrated loudness and applies only `volume=(target - I) dB` — no compressor, no limiter, no dynamics change. Legacy modes (constant loudnorm / dynaudnorm / loudnorm / off) remain available.
- **Custom file naming**: `{n} {title} {artist} {album}` placeholders for albums.
- **Multi-language UI**: English, Spanish, Brazilian Portuguese (follows browser locale).
- **Output formats**: MP3, M4A/AAC, Opus, OGG Vorbis, FLAC, ALAC, WAV, Original.
- **Completion chime**: short synthesized "ding" (Web Audio, no audio file) when the last active download finishes successfully, even with the popup closed. Toggle in Settings → "Tocar som quando terminar" (default on, stored in `chrome.storage.local`). Cancellations/errors don't chime.

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

## Qobuz

Track and album links work the same way. Like Spotify, the audio does **not**
come from Qobuz (downloads there are paid/DRM-free purchases): the server reads
the public metadata of the album/track via the Qobuz API (title, artists, album,
date, ISRC, 640×640 cover, duration), finds the matching video on YouTube by
**duration** and writes the MP3 with the same tags. Qobuz track links are of the
form `https://play.qobuz.com/track/<id>` and album links
`https://www.qobuz.com/<locale>/album/<slug>/<id>`.

Albums are downloaded track by track into a subfolder named after the album
(`Artist - Album (Version)`). Albums are always (re)downloaded in full — existing
files are overwritten — so a re-run of an album fetches everything again.
Playlists keep the "skip already downloaded" behaviour.

The addon also handles Qobuz URLs where the last segment is a slug (not the
album ID) by searching Qobuz automatically.

## Amazon Music

Album and track links (`https://music.amazon.com/albums/<id>`,
`https://music.amazon.com/tracks/<id>`, and the `.com.br` variants) download as
**true FLAC** — the original stream, not a re-encode. The local service runs
`lucidadl album <url> -s amazon -F original` in a subprocess; its Playwright
Chromium handles the Cloudflare check when one is required.

- The button reads **"Baixar do Amazon Music (FLAC)"** and only appears on
  `/albums/` and `/tracks/` URLs — **playlists are not supported** by the
  resolver (`Amazon page has no available tracks with a token`).
- Files land in `<destination>/<Artist> - <Album>/`. The download itself goes
  to a staging folder in `.incoming/` first, so the destination is never
  polluted with an `Artists/<Artist>/<Album>/` tree.
- Deduplication uses `~/.config/ytflac/amazon_done.json` (keyed by the
  album/track ID) — a second click on the same album reports "Já baixado
  anteriormente." Use `{"force": true}` in the API to re-download.
- The job title shows `Artist - Album` while downloading; cancel via the popup
  stops the subprocess and clears staging.

## YouTube / YouTube Music

Any YouTube (or YouTube Music) video or playlist link works exactly like the
Spotify/Qobuz flows: the audio comes straight from YouTube via yt-dlp, the service
converts it to **MP3 VBR lame -V0** and embeds metadata.

- Single videos (`youtube.com/watch?v=…`, `youtu.be/…`, `music.youtube.com/watch?v=…`)
  resolve metadata via yt-dlp and download directly.
- Playlists (`youtube.com/playlist?list=…`, including `music.youtube.com/playlist?list=…`)
  download track by track into a folder named after the playlist.

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
cd ~/Documentos/GitHub/BetterMusicDownloader
bash install.sh          # terminal mode (default)
bash install.sh --gui    # try zenity/kdialog/dialog if available
```

The script:
1. checks `python3` and `ffmpeg` (installs `ffmpeg` automatically if missing,
   detecting `pacman`/`apt`/`dnf`);
2. creates the `yt-dlp` venv in `~/.local/share/ytflac/venv` (with `mutagen`);
3. creates `server/venv`, installs `lucidadl` and Playwright Chromium (for
   Amazon Music FLAC);
4. installs and enables the `ytflac.service` (systemd `--user`);
5. creates `~/Música/YouTube FLAC`;
6. opens Vivaldi on `vivaldi://extensions` automatically (if `vivaldi` is in PATH).

### Optional GUI tools

`zenity`, `kdialog` or `dialog` are **optional**. If one of them is installed and you
run `bash install.sh --gui`, the script shows progress in graphical windows. Without
any of them, everything runs in the terminal — no functionality is lost.

```bash
# Arch
sudo pacman -S zenity        # or kdialog, dialog
# Debian/Ubuntu
sudo apt install zenity
```

### Load in Vivaldi

1. `vivaldi://extensions`
2. enable **Developer mode**
3. **Load unpacked** → pick `~/Documentos/GitHub/BetterMusicDownloader/extension`

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
- **Seek info before downloading**: click **"Buscar info"** to preview metadata and
  track list (with "already downloaded" status). Click a track to download just
  that one. The list stays open and updates as downloads complete.

### YouTube playlists

A pure playlist URL (`youtube.com/playlist?list=…`) downloads **the whole
playlist**: one job per video, a folder named after the playlist, files numbered
(`01 - Title.mp3`, skipped when the title already carries its own number) and
already-downloaded items are left alone. A video *inside* a playlist
(`watch?v=…&list=…`) still downloads only that video — open the playlist URL
itself when you want everything.

## Settings (popup → Settings)

| Field | Purpose |
|---|---|
| Destination folder | where the files are saved |
| cookies.txt | optional, for videos requiring login (and Premium for 256 kbps) |
| Output format | **MP3** (default), **M4A/AAC**, **Opus**, **OGG Vorbis**, **FLAC** (re-encoded, not true lossless), **ALAC**, **WAV**, **Original** (no conversion) |
| Normalize volume | applies loudnorm (EBU R128) — avoids "quiet" tracks |
| Normalization mode | Constant (linear, preserves dynamics), Dynaudnorm (smooth), Loudnorm (dynamic), Off |
| Loudness target (LUFS) | -23 to -6 (default -8). Less negative = louder. |
| Apply gain to the file (mp3gain) | bakes the level into the MP3, **without re-encoding**, so any player respects it. Needs `mp3gain` installed (AUR); option stays disabled otherwise. Works in 1.5 dB steps and saves undo data in the file. |
| Minimum quality (kbps) | 0 = accept everything. YouTube free delivers at most ~160 kbps. |
| Parallel downloads | 1–14 simultaneous (default 14). More = faster albums, more CPU/network. |
| Use collection folder for single tracks | when downloading a single track from an album/playlist, save it in the same folder (default on) |
| File name format (albums) | customizable: `{n} {title} {artist} {album}` placeholders |

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
curl -X POST 127.0.0.1:8765/api/jobs -H 'Content-Type: application/json' \
     -d '{"url":"https://open.spotify.com/track/...","group":"My Folder"}'
curl -X POST 127.0.0.1:8765/api/jobs -H 'Content-Type: application/json' \
     -d '{"url":"https://music.amazon.com/albums/<id>","title":"Artist - Album"}'
curl '127.0.0.1:8765/api/jobs?id=<id>'
curl -X POST 127.0.0.1:8765/api/jobs/cancel -H 'Content-Type: application/json' \
     -d '{"id":"<job id>"}'
```

## Maintenance

```bash
# update yt-dlp (YouTube changes frequently)
~/.local/share/ytflac/venv/bin/pip install -U "yt-dlp[default]"

# update lucidadl (Amazon Music)
cd ~/Documentos/GitHub/BetterMusicDownloader/server && venv/bin/pip install -U lucidadl

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
- It only accepts URLs from YouTube, Spotify, Qobuz and Amazon Music domains.
- It only serves `.mp3` and `.flac` files inside the configured destination folder.

## Structure

```
BetterMusicDownloader/
├── install.sh
├── README.md
├── server/
│   ├── ytflac_server.py
│   └── venv/              # lucidadl + Playwright (Amazon Music FLAC)
└── extension/
    ├── manifest.json
    ├── background.js
    ├── popup.html
    ├── popup.js
    ├── popup.css
    └── icons/
```

