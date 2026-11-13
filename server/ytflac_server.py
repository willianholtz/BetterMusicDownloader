#!/usr/bin/env python3
"""Servidor local do "YouTube/Spotify -> FLAC".

Escuta apenas em 127.0.0.1. Recebe uma URL do YouTube (ou do Spotify), baixa o
melhor audio com yt-dlp e converte para FLAC (sem perdas) com o ffmpeg. A
extensao do Vivaldi so conversa com esta API.

No Spotify o audio tambem vem do YouTube: o servidor le os metadados publicos da
faixa/album/playlist (embed do Spotify), procura o melhor video correspondente
(casando a duracao) e grava o FLAC com titulo, artistas, album, faixa, data,
ISRC e capa do Spotify embutidos. Nada de DRM e quebrado.

Endpoints (JSON):
  GET  /api/health              -> estado do servico
  GET  /api/settings            -> configuracoes atuais
  POST /api/settings            -> {"outdir": "...", "cookies": "..."}
  GET  /api/resolve?url=...     -> metadados (Spotify) sem baixar
  POST /api/jobs                -> {"url": "https://..."}  devolve {"id": ...}
  GET  /api/jobs?id=<id>        -> um job
  GET  /api/jobs                -> ultimos jobs
  POST /api/jobs/cancel         -> {"id": ...}
  GET  /api/file/<id>           -> baixa o .flac pronto
  POST /api/open-folder         -> abre a pasta de destino
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote, unquote

HOST = "127.0.0.1"
PORT = int(os.environ.get("YTFLAC_PORT", "8765"))
VERSION = "1.2.0"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ytflac"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_OUTDIR = Path.home() / "Música" / "YouTube FLAC"

YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
}
SPOTIFY_HOSTS = {"open.spotify.com", "play.spotify.com", "spotify.link", "spotify.app.link"}
QOBUZ_HOSTS = {"www.qobuz.com", "qobuz.com", "play.qobuz.com"}

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

NAME_TEMPLATE = "%(artist,creator,uploader)s - %(title)s.%(ext)s"
INDEX_FILE = CONFIG_DIR / "downloaded.json"

jobs: dict[str, dict] = {}
jobs_order: list[str] = []
batches: dict[str, dict] = {}
batches_order: list[str] = []
HISTORY_FILE = CONFIG_DIR / "history.json"
lock = threading.Lock()
pool = ThreadPoolExecutor(max_workers=3)
procs: dict[str, subprocess.Popen] = {}


# ---------------------------------------------------------------- utilidades

def find_binary(name: str) -> str | None:
    """Procura o binario no PATH e nos lugares comuns do usuario.

    O yt-dlp tem preferencia pela venv do projeto (~/.local/share/ytflac/venv),
    que traz o mutagen e os extras usados para embutir metadados e capa.
    """
    if name == "yt-dlp":
        venv_dl = Path.home() / ".local/share/ytflac/venv/bin/yt-dlp"
        if venv_dl.is_file() and os.access(venv_dl, os.X_OK):
            return str(venv_dl)
    found = shutil.which(name)
    if found:
        return found
    candidates = [
        Path.home() / ".local/bin" / name,
        Path("/usr/bin") / name,
        Path("/usr/local/bin") / name,
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def venv_python() -> str | None:
    py = Path.home() / ".local/share/ytflac/venv/bin/python"
    if py.is_file() and os.access(py, os.X_OK):
        return str(py)
    return None


def load_config() -> dict:
    cfg = {"outdir": str(DEFAULT_OUTDIR), "cookies": "",
            "format": "mp3", "normalize": True, "min_bitrate": 0,
            "mp3gain": False}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in cfg})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def js_runtime() -> list[str]:
    """Runtimes JS que o yt-dlp usa para resolver os desafios do YouTube.

    Sem um runtime a extracao fica limitada (o proprio yt-dlp avisa). Node e o
    mais comum numa maquina de desenvolvimento; deno e o padrao do yt-dlp.
    """
    for name in ("deno", "node", "bun"):
        if find_binary(name):
            return ["--js-runtimes", name]
    return []


def is_youtube_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    return (p.hostname or "").lower() in YOUTUBE_HOSTS


def youtube_playlist_id(url: str) -> str:
    """ID da playlist quando a URL aponta para a playlist inteira.

    Video dentro de playlist (watch?v=...&list=...) devolve "" de proposito:
    nesse caso o usuario quer so aquele video, como sempre foi.
    """
    try:
        p = urlparse(url)
    except ValueError:
        return ""
    if (p.hostname or "").lower() not in YOUTUBE_HOSTS:
        return ""
    q = parse_qs(p.query)
    lst = (q.get("list") or [""])[0].strip()
    if not lst:
        return ""
    if (q.get("v") or [""])[0].strip():
        return ""
    return lst if "playlist" in p.path.lower() else ""


def playlist_prefix(index: int, total: int, title: str) -> str:
    """Prefixo numerico do arquivo; vazio se o titulo ja vem numerado.

    Evita nome duplicado tipo "01 - Artista - 01 - Musica.mp3".
    """
    if re.match(r"^\D{0,60}?\b\d{1,2}\s*[-–—.)]\s", title or ""):
        return ""
    width = max(2, len(str(total)))
    return f"{index:0{width}d} - "


def youtube_playlist_meta(list_id: str) -> dict:
    """Le a playlist do YouTube sem baixar nada (--flat-playlist, ~1 s)."""
    ytdlp = find_binary("yt-dlp")
    if not ytdlp:
        raise RuntimeError("yt-dlp não encontrado")
    url = f"https://www.youtube.com/playlist?list={list_id}"
    cmd = [ytdlp, "--flat-playlist", "--no-warnings", "-J", url] + js_runtime()
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        err = [l for l in (out.stderr or "").splitlines() if l.strip()]
        raise RuntimeError(err[-1] if err else f"yt-dlp saiu com código {out.returncode}")
    data = json.loads(out.stdout)
    entries = []
    for e in data.get("entries") or []:
        if not e or not e.get("id"):
            continue
        entries.append({
            "id": e["id"],
            "title": e.get("title") or e["id"],
            "duration": e.get("duration") or 0,
        })
    return {
        "name": data.get("title") or "Playlist",
        "uploader": data.get("uploader") or data.get("channel") or "",
        "count": data.get("playlist_count") or len(entries),
        "entries": entries,
    }


def spotify_ref(url: str) -> tuple[str, str] | None:
    """(tipo, id) de uma URL/URI do Spotify: track, album, playlist ou episode."""
    m = re.match(r"^spotify:(track|album|playlist|episode):([A-Za-z0-9]+)", url.strip())
    if m:
        return m.group(1), m.group(2)
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if (p.hostname or "").lower() not in SPOTIFY_HOSTS:
        return None
    parts = [x for x in p.path.split("/") if x]
    if parts and parts[0].startswith("intl-"):
        parts = parts[1:]
    if len(parts) >= 2 and parts[0] in ("track", "album", "playlist", "episode"):
        return parts[0], parts[1]
    return None


def qobuz_ref(url: str) -> tuple[str, str] | None:
    """(kind, id) de uma URL do Qobuz: track ou album."""
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if host not in QOBUZ_HOSTS:
        return None
    parts = [x for x in p.path.split("/") if x]
    if host == "play.qobuz.com":
        if len(parts) >= 2 and parts[0] in ("track", "album"):
            return parts[0], parts[1]
        return None
    # www.qobuz.com/<locale>/album|track/<slug>/<id>
    if "album" in parts:
        idx = parts.index("album")
        if idx + 2 < len(parts) or idx + 1 < len(parts):
            # id is the last segment
            return "album", parts[-1]
    if "track" in parts:
        idx = parts.index("track")
        if idx + 1 < len(parts):
            return "track", parts[-1]
    return None


def http_get(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_json(url: str, timeout: float = 20.0):
    return json.loads(http_get(url, timeout).decode("utf-8", "replace"))


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return " ".join(s.split())


# ---------------------------------------------------------------- metadados

def cover_big(url: str) -> str:
    """O Spotify serve a mesma capa em 640x640 trocando o prefixo do hash."""
    if not url:
        return ""
    return re.sub(r"(ab67616d)[0-9a-f]{8}", r"\g<1>0000b273", url)


def spotify_embed(kind: str, sid: str) -> dict:
    html = http_get(f"https://open.spotify.com/embed/{kind}/{sid}").decode("utf-8", "replace")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("O Spotify não devolveu os dados da página.")
    data = json.loads(m.group(1))
    return data["props"]["pageProps"]["state"]["data"]["entity"]


def spotify_cover(entity: dict, fallback_id: str, kind: str) -> str:
    vis = entity.get("visualIdentity") or {}
    images = vis.get("image") or []
    if images:
        best = max(images, key=lambda i: (i.get("width") or 0) * (i.get("height") or 0))
        if best.get("url"):
            return cover_big(best["url"])
    try:
        o = http_json("https://open.spotify.com/oembed?url=" +
                      quote(f"https://open.spotify.com/{kind}/{fallback_id}"))
        return cover_big(o.get("thumbnail_url") or "")
    except (OSError, ValueError, KeyError):
        return ""


_album_cache: dict[str, dict] = {}


def _deezer_album(album_id) -> dict:
    key = str(album_id)
    if key in _album_cache:
        return _album_cache[key]
    try:
        data = http_json(f"https://api.deezer.com/album/{key}")
    except (OSError, ValueError):
        data = {}
    _album_cache[key] = data
    return data


def deezer_isrc(track: dict) -> None:
    """ISRC (e numero de faixa) pelo Deezer, que expoe o codigo publico."""
    if track.get("isrc"):
        return
    q = f"{' '.join(track.get('artists') or [])} {track.get('title') or ''}".strip()
    if not q:
        return
    try:
        found = http_json("https://api.deezer.com/search?limit=6&q=" + quote(q))
    except (OSError, ValueError):
        return
    want_title = norm(track.get("title", ""))
    want_artist = norm(" ".join(track.get("artists") or []))
    want_album = norm(track.get("album", ""))
    for item in found.get("data", []):
        title = norm(item.get("title", ""))
        if title != want_title and want_title not in title:
            continue
        artist = norm((item.get("artist") or {}).get("name", ""))
        if want_artist and artist and not (artist in want_artist or want_artist in artist):
            continue
        if want_album:
            got = norm((item.get("album") or {}).get("title", ""))
            if got and want_album not in got and got not in want_album:
                continue
        try:
            detail = http_json(f"https://api.deezer.com/track/{item['id']}")
        except (OSError, ValueError, KeyError):
            continue
        track["isrc"] = detail.get("isrc") or ""
        if not track.get("track_number"):
            track["track_number"] = detail.get("track_position")
        if not track.get("disc_number"):
            track["disc_number"] = detail.get("disk_number")
        return


def itunes_fill(track: dict) -> None:
    """Album/faixa/data pelo iTunes, que costuma listar o album original.

    O embed do Spotify de uma faixa isolada nao traz o album; o Deezer devolve
    compilacoes. O iTunes acerta o album original e o numero da faixa, e a
    duracao serve de conferencia.
    """
    q = f"{' '.join(track.get('artists') or [])} {track.get('title') or ''}".strip()
    if not q:
        return
    try:
        found = http_json("https://itunes.apple.com/search?entity=song&limit=10&term=" + quote(q))
    except (OSError, ValueError):
        return

    want_title = norm(track.get("title", ""))
    want_artist = norm(" ".join(track.get("artists") or []))
    want_ms = int(round((track.get("duration") or 0) * 1000))
    try:
        want_year = int((track.get("date") or "")[:4])
    except ValueError:
        want_year = 0

    best, best_key = None, None
    for item in found.get("results", []):
        if norm(item.get("trackName", "")) != want_title:
            continue
        artist = norm(item.get("artistName", ""))
        if want_artist and artist and not (artist in want_artist or want_artist in artist):
            continue
        ms = item.get("trackTimeMillis") or 0
        delta = abs(ms - want_ms) if (ms and want_ms) else 999999
        if delta > 5000:
            continue
        year = int((item.get("releaseDate") or "")[:4] or 0)
        gap = abs(year - want_year) if (year and want_year) else 99
        key = (delta, gap, -int((item.get("trackCount") or 0)))
        if best_key is None or key < best_key:
            best, best_key = item, key

    if not best:
        return
    if not track.get("album"):
        track["album"] = best.get("collectionName") or ""
    if not track.get("date"):
        track["date"] = (best.get("releaseDate") or "")[:10]
    if not track.get("track_number"):
        track["track_number"] = best.get("trackNumber")
    if not track.get("track_total"):
        track["track_total"] = best.get("trackCount")
    if not track.get("disc_number"):
        track["disc_number"] = best.get("discNumber")
    if not track.get("disc_total"):
        track["disc_total"] = best.get("discCount")


def spotify_track_meta(kind: str, sid: str) -> dict:
    """Metadados de uma faixa isolada."""
    entity = spotify_embed(kind, sid)
    artists = [a.get("name", "") for a in entity.get("artists") or [] if a.get("name")]
    date = ((entity.get("releaseDate") or {}).get("isoString") or "")[:10]
    track = {
        "title": entity.get("name") or entity.get("title") or "",
        "artists": artists,
        "album": "",
        "albumartist": artists[0] if len(artists) == 1 else "Various Artists",
        "date": date,
        "duration": round((entity.get("duration") or 0) / 1000, 3),
        "cover": spotify_cover(entity, sid, kind),
        "isrc": "",
        "track_number": None,
        "disc_number": None,
        "spotify_id": sid,
        "source": f"https://open.spotify.com/{kind}/{sid}",
    }
    itunes_fill(track)
    deezer_isrc(track)
    return track


def _itunes_album_date(artist: str, album: str) -> str:
    if not album:
        return ""
    term = f"{artist} {album}".strip()
    try:
        found = http_json("https://itunes.apple.com/search?entity=album&limit=6&term=" + quote(term))
    except (OSError, ValueError):
        return ""
    want = norm(album)
    best = ""
    for item in found.get("results", []):
        name = norm(item.get("collectionName", ""))
        if name == want or (want and want in name):
            best = (item.get("releaseDate") or "")[:10]
            break
    return best


def spotify_collection_meta(kind: str, sid: str) -> dict:
    """Album/playlist: nome da colecao + lista de faixas."""
    entity = spotify_embed(kind, sid)
    name = entity.get("name") or entity.get("title") or "Spotify"
    date = ((entity.get("releaseDate") or {}).get("isoString") or "")[:10]
    cover = spotify_cover(entity, sid, kind)
    if not date:
        date = _itunes_album_date(entity.get("subtitle") or "", name)
    tracks = []
    for item in entity.get("trackList") or []:
        uri = item.get("uri") or ""
        if not uri.startswith("spotify:track:"):
            continue
        if item.get("isPlayable") is False:
            continue
        tid = uri.rsplit(":", 1)[-1]
        artists = [a.strip() for a in (item.get("subtitle") or "").split(",") if a.strip()]
        tracks.append({
            "title": item.get("title") or "",
            "artists": artists,
            "album": name if kind == "album" else "",
            "albumartist": artists[0] if len(artists) == 1 else "Various Artists",
            "date": date,
            "duration": round((item.get("duration") or 0) / 1000, 3),
            "cover": cover,
            "isrc": "",
            "track_number": len(tracks) + 1,
            "disc_number": 1,
            "spotify_id": tid,
            "source": f"https://open.spotify.com/track/{tid}",
        })
    for t in tracks:
        t["track_total"] = len(tracks)
    return {
        "kind": kind,
        "name": name,
        "cover": cover,
        "date": date,
        "tracks": tracks,
        "artist": entity.get("subtitle") or "",
    }


QOBUZ_APP_ID = "172934108"


def _qobuz_get(path: str, params: dict | None = None) -> dict:
    q = {"app_id": QOBUZ_APP_ID}
    if params:
        q.update(params)
    qs = "&".join(f"{k}={quote(str(v))}" for k, v in q.items())
    url = f"https://www.qobuz.com/api.json/0.2/{path}?{qs}"
    return http_json(url, 25.0)


def qobuz_track_meta(qid: str) -> dict:
    """Metadados de uma faixa isolada do Qobuz."""
    data = _qobuz_get("track/get", {"track_id": qid})
    album = data.get("album") or {}
    album_title = album.get("title") or ""
    artists = [data.get("performer", {}).get("name") or ""] if data.get("performer") else []
    if not artists or not artists[0]:
        artists = [data.get("composer", {}).get("name") or "Artista"]
    cover = (album.get("image") or {}).get("large") or (album.get("image") or {}).get("small") or ""
    date = (data.get("release_date_original") or data.get("release_date_stream") or "")[:10]
    track = {
        "title": data.get("title") or "",
        "artists": [a for a in artists if a],
        "album": album_title,
        "albumartist": artists[0] if len(artists) == 1 else "Various Artists",
        "date": date,
        "duration": float(data.get("duration") or 0),
        "cover": cover,
        "isrc": data.get("isrc") or "",
        "track_number": data.get("track_number"),
        "disc_number": data.get("media_number"),
        "spotify_id": str(qid),
        "source": f"https://play.qobuz.com/track/{qid}",
    }
    return track


def qobuz_collection_meta(kind: str, qid: str) -> dict:
    """Album do Qobuz: nome + lista de faixas."""
    data = _qobuz_get("album/get", {"album_id": qid})
    name = data.get("title") or "Qobuz"
    artist_name = (data.get("artist") or {}).get("name") or ""
    cover = (data.get("image") or {}).get("large") or (data.get("image") or {}).get("small") or ""
    date = (data.get("release_date_original") or data.get("release_date_download") or "")[:10]
    items = (data.get("tracks") or {}).get("items") or []
    tracks = []
    for it in items:
        artists = [it.get("performer", {}).get("name") or ""] if it.get("performer") else []
        if not artists or not artists[0]:
            artists = [artist_name or "Artista"]
        tracks.append({
            "title": it.get("title") or "",
            "artists": [a for a in artists if a],
            "album": name,
            "albumartist": artist_name or (artists[0] if len(artists) == 1 else "Various Artists"),
            "date": date,
            "duration": float(it.get("duration") or 0),
            "cover": cover,
            "isrc": it.get("isrc") or "",
            "track_number": it.get("track_number") or (len(tracks) + 1),
            "disc_number": it.get("media_number") or 1,
            "spotify_id": str(it.get("id")),
            "source": f"https://play.qobuz.com/track/{it.get('id')}",
        })
    for t in tracks:
        t["track_total"] = len(tracks)
    return {
        "kind": "album",
        "name": name,
        "cover": cover,
        "date": date,
        "tracks": tracks,
        "artist": artist_name,
    }


# ---------------------------------------------------------------- jobs

def new_job(url: str, **extra) -> dict:
    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid,
        "url": url,
        "title": extra.get("title", ""),
        "status": "queued",   # queued | searching | downloading | converting | tagging | done | error | cancelled
        "percent": 0.0,
        "speed": "",
        "eta": "",
        "file": "",
        "error": "",
        "source": extra.get("source", "youtube"),
        "group": extra.get("group", ""),
        "meta": extra.get("meta"),
        "query": extra.get("query", ""),
        "want_duration": extra.get("want_duration", 0),
        "force": bool(extra.get("force")),
        "batch_id": extra.get("batch_id", ""),
        "name_prefix": extra.get("name_prefix", ""),
        "created": time.time(),
        "finished": 0.0,
    }
    with lock:
        jobs[jid] = job
        jobs_order.append(jid)
        # mantem so os 100 mais recentes na memoria
        while len(jobs_order) > 100:
            old = jobs_order.pop(0)
            jobs.pop(old, None)
    return job


def update(jid: str, **fields) -> None:
    with lock:
        if jid in jobs:
            jobs[jid].update(fields)


def snapshot(jid: str) -> dict | None:
    with lock:
        job = jobs.get(jid)
        return dict(job) if job else None

def _history_read() -> list:
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            for e in data:
                files = e.get("files") or []
                e["exists"] = any(os.path.isfile(f) for f in files) if files else False
                e["first_file"] = files[0] if files else ""
            return data
    except (OSError, ValueError):
        pass
    return []


def _history_write(entries: list) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        HISTORY_FILE.write_text(json.dumps(entries, indent=1, ensure_ascii=False),
                               encoding="utf-8")
    except OSError:
        pass


def batch_new(name: str, total: int, job_ids: list[str], group: str = "") -> str:
    bid = uuid.uuid4().hex[:12]
    b = {
        "id": bid,
        "name": name or "Download",
        "group": group,
        "total": total,
        "done": 0,
        "error": 0,
        "job_ids": list(job_ids),
        "created": time.time(),
        "finished": 0.0,
        "status": "running",  # running | done
    }
    with lock:
        batches[bid] = b
        batches_order.append(bid)
        while len(batches_order) > 100:
            old = batches_order.pop(0)
            batches.pop(old, None)
    return bid


def batch_update(bid: str, ok: bool) -> None:
    if not bid:
        return
    files = []
    entry = None
    with lock:
        b = batches.get(bid)
        if not b:
            return
        if ok:
            b["done"] += 1
        else:
            b["error"] += 1
        if b["done"] + b["error"] >= b["total"]:
            b["status"] = "done"
            b["finished"] = time.time()
            for jid in b["job_ids"]:
                job = jobs.get(jid)
                if job and job.get("file"):
                    files.append(job["file"])
            entry = {
                "id": b["id"],
                "name": b["name"],
                "group": b["group"],
                "total": b["total"],
                "done": b["done"],
                "error": b["error"],
                "finished": b["finished"],
                "ok": b["error"] == 0,
                "files": files,
            }
    if entry is not None:
        entries = _history_read()
        entries.insert(0, entry)
        entries = entries[:200]
        _history_write(entries)


# ---------------------------------------------------------------- download

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PROG_DL = re.compile(r"^\[download\]\s+([\d.]+)%")
PROG_ETA = re.compile(r"ETA\s+(\S+)")
PROG_SPEED = re.compile(r"at\s+(\S+/s)")


def pick_target(query: str, want_duration: float) -> str:
    """Escolhe o video mais provavel para a faixa (duracao + titulo).

    Devolve uma URL direta; se a busca falhar, cai no ytsearch1.
    """
    ytdlp = find_binary("yt-dlp")
    if not ytdlp:
        return f"ytsearch1:{query}"
    cmd = [ytdlp, "--flat-playlist", "--no-warnings", "--no-quiet", "--print",
           "%(id)s\t%(duration)s\t%(title)s\t%(channel)s", f"ytsearch8:{query}"]
    cmd += js_runtime()
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                             encoding="utf-8", errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return f"ytsearch1:{query}"

    want = norm(query)
    best, best_score = None, -1e9
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0].strip():
            continue
        vid, dur, title = parts[0].strip(), parts[1], parts[2]
        try:
            d = float(dur)
        except ValueError:
            d = 0.0
        score = 0.0
        if want_duration and d:
            score -= abs(d - want_duration)
            if abs(d - want_duration) <= 3:
                score += 60
        title_n = norm(title)
        score += 12 * len(set(want.split()) & set(title_n.split())) / max(1, len(want.split()))
        if "topic" in title_n or "official" in title_n:
            score += 4
        if score > best_score:
            best, best_score = vid, score
    if not best:
        return f"ytsearch1:{query}"
    return f"https://www.youtube.com/watch?v={best}"


def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', " ", s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:150] or "faixa"


def _audio_ext(fmt: str | None = None) -> str:
    if not fmt:
        fmt = (load_config().get("format") or "mp3").lower()
    return {
        "mp3": ".mp3", "m4a": ".m4a", "opus": ".opus", "vorbis": ".ogg",
        "flac": ".flac", "wav": ".wav", "aac": ".aac", "alac": ".m4a",
        "original": None,
    }.get(fmt, ".mp3")

def final_name(meta: dict, fmt: str | None = None) -> str:
    artists = " / ".join(meta.get("artists") or []) or meta.get("albumartist", "")
    return f"{safe_name(artists)} - {safe_name(meta.get('title', 'faixa'))}{_audio_ext(fmt)}"


def video_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else ""


def index_read() -> dict:
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("tracks", {})
            data.setdefault("videos", {})
            return data
    except (OSError, ValueError):
        pass
    return {"tracks": {}, "videos": {}}


def _index_write(data: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        INDEX_FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def index_record(job: dict, target: str, final: str) -> None:
    """Anota o que ja foi baixado (o nome do arquivo nao carrega mais o ID)."""
    with lock:
        data = index_read()
        meta = job.get("meta")
        if meta and meta.get("spotify_id"):
            data["tracks"][meta["spotify_id"]] = final
        vid = video_id(target) or video_id(job.get("url", ""))
        if vid:
            data["videos"][vid] = final
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            INDEX_FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass


def already_done(job: dict, dest: Path, outdir: Path) -> str:
    """Arquivo que esse job ja produziu (para nao baixar duas vezes).

    Procura em TODA a pasta de destino (inclusive subpastas de album/playlist),
    porque a mesma faixa pode ter sido baixada por outro link antes.
    """
    meta = job.get("meta")
    if meta:
        # nome final (formato atual e antigos)
        for ext in (".mp3", ".flac", ".opus", ".ogg", ".m4a", ".webm"):
            name = final_name(meta)[:-4] + ext
            try:
                for f in outdir.rglob(name):
                    if f.is_file():
                        return str(f)
            except OSError:
                pass
        # nome mudou em algum momento: confere o indice pelo ID da faixa
        sid = str(meta.get("spotify_id") or "")
        if sid:
            idx = index_read()
            known = idx["tracks"].get(sid, "")
            if known:
                if Path(known).is_file():
                    return known
                # stale: remove
                idx["tracks"].pop(sid, None)
                _index_write(idx)
            # e os arquivos antigos, que tinham o ID no nome
            try:
                for old in outdir.rglob("*.*"):
                    if f"[{sid}]" in old.name and old.suffix.lower() in (".mp3", ".flac", ".opus", ".ogg", ".m4a", ".webm") and old.is_file():
                        return str(old)
            except OSError:
                pass
        return ""
    vid = video_id(job.get("url", ""))
    if vid:
        idx = index_read()
        known = idx["videos"].get(vid, "")
        if known:
            if Path(known).is_file():
                return known
            idx["videos"].pop(vid, None)
            _index_write(idx)
        for ext in (".mp3", ".flac", ".opus", ".ogg", ".m4a", ".webm"):
            try:
                for f in outdir.rglob(f"*{ext}"):
                    if f"[{vid}]" in f.name and f.is_file():
                        return str(f)
            except OSError:
                pass
    return ""


def best_audio_bitrate(url: str) -> float:
    """Bitrate (kbps) do melhor audio disponivel para a URL. 0 se nao souber."""
    ytdlp = find_binary("yt-dlp")
    if not ytdlp:
        return 0.0
    cmd = [ytdlp, "--no-playlist", "-J", url] + js_runtime()
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                             encoding="utf-8", errors="replace")
        data = json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return 0.0
    fmts = data.get("formats") or []
    audio = [f for f in fmts
             if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]
    if not audio:
        # fallback: requested_formats
        rf = data.get("requested_formats") or []
        audio = [f for f in rf if f.get("acodec") not in (None, "none")]
    if not audio:
        return 0.0
    best = max(audio, key=lambda f: (f.get("tbr") or 0))
    return float(best.get("tbr") or 0)


def normalize_file(path: str) -> str:
    """Aplica loudnorm (EBU R128) no arquivo, substituindo-o. Devolve o path."""
    p = Path(path)
    if not p.is_file():
        return path
    tmp = p.with_name(p.stem + ".norm" + p.suffix)
    try:
        subprocess.run([
            find_binary("ffmpeg") or "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(p),
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-map", "0:a", "-map", "0:v?",
            str(tmp),
        ], check=True)
    except (OSError, subprocess.SubprocessError):
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return path
    # preserva metadados/capa copiando via ffmpeg? ffmpeg ja carrega no re-encode
    os.replace(tmp, p)
    return str(p)


def apply_mp3gain(path: str) -> str:
    """Aplica o ganho direto no MP3 com mp3gain (sem re-encodar).

    O mp3gain edita o campo global_gain dos frames: o audio comprimido nao e'
    recomprimido, entao nao ha perda de qualidade. -k evita clipping e -s s
    grava a informacao de desfazer no proprio arquivo (tag APEv2).
    """
    p = Path(path)
    if not p.is_file() or p.suffix.lower() != ".mp3":
        return path
    exe = find_binary("mp3gain")
    if not exe:
        return path
    try:
        subprocess.run([exe, "-q", "-r", "-k", "-p", "-s", "s", str(p)],
                       check=True, capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return path
    return str(p)


def run_job(jid: str) -> None:
    job = snapshot(jid)
    if not job or job.get("status") == "cancelled":
        return

    ytdlp = find_binary("yt-dlp")
    ffmpeg = find_binary("ffmpeg")
    if not ytdlp:
        update(jid, status="error", error="yt-dlp não encontrado. Rode o install.sh.")
        return
    if not ffmpeg:
        update(jid, status="error", error="ffmpeg não encontrado. Instale o pacote ffmpeg.")
        return

    cfg = load_config()
    outdir = Path(cfg["outdir"]).expanduser()
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        update(jid, status="error", error=f"Não consegui criar a pasta de destino: {e}")
        return

    group = job.get("group") or ""
    dest = outdir / group if group else outdir
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError:
        dest = outdir

    target = job["url"]
    if job.get("query"):
        update(jid, status="searching", percent=0.0)
        target = pick_target(job["query"], job.get("want_duration") or 0)
    if (snapshot(jid) or {}).get("status") == "cancelled":
        return

    fmt = (cfg.get("format") or "mp3").lower()
    if fmt not in ("mp3", "m4a", "opus", "vorbis", "flac", "wav", "aac", "alac", "original"):
        fmt = "mp3"

    min_br = int(cfg.get("min_bitrate") or 0)
    if min_br > 0:
        update(jid, status="searching", percent=0.0)
        br = best_audio_bitrate(target)
        if br and br < min_br:
            update(jid, status="error",
                   error=f"Qualidade da fonte ({br:.0f} kbps) abaixo do mínimo configurado ({min_br} kbps).",
                   finished=time.time())
            return

    done_file = "" if job.get("force") else already_done(job, dest, outdir)
    if done_file:
        batch_update(job.get("batch_id", ""), True)
        update(jid, status="done", percent=100.0, file=done_file,
               title=Path(done_file).stem, finished=time.time())
        return

    meta = job.get("meta") or None
    if meta and job.get("source") in ("spotify", "qobuz") and not (meta.get("isrc") and meta.get("album")):
        if not meta.get("album"):
            itunes_fill(meta)
        deezer_isrc(meta)
        update(jid, meta=meta)
    # faixas do Spotify: baixa para um nome temporario e depois grava as tags
    if meta:
        template = outdir / f".ytflac-{jid}.%(ext)s"
    else:
        # playlist do YouTube: nome vem do titulo do video, numerado, na pasta da playlist
        prefix = job.get("name_prefix") or ""
        tpl = (prefix + "%(title)s.%(ext)s") if (prefix or job.get("group")) else NAME_TEMPLATE
        template = dest / tpl

    cmd = [
        ytdlp,
        "--no-playlist",
        "--newline",
        "--no-simulate",
        "--no-part",
        "--no-mtime",
        "-f", "bestaudio/best",
        "--embed-metadata",
        "--print", "after_move:FILEPATH:%(filepath)s",
        "--no-quiet",
        "--progress",
        "-o", str(template),
    ]
    if fmt == "original":
        # mantem o stream original (sem re-codificar)
        pass
    else:
        cmd += ["-x", "--audio-format", fmt, "--audio-quality", "0"]
    if fmt != "original":
        cmd += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
    cmd += js_runtime()
    if cfg.get("cookies") and Path(cfg["cookies"]).expanduser().is_file():
        cmd += ["--cookies", str(Path(cfg["cookies"]).expanduser())]
    cmd.append(target)

    update(jid, status="downloading", percent=0.0)
    log_dir = CONFIG_DIR / "jobs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{jid}.log"

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )
        procs[jid] = proc
        with open(log_path, "w", encoding="utf-8") as log:
            for raw in proc.stdout:
                line = ANSI.sub("", raw).rstrip("\n")
                log.write(line + "\n")
                log.flush()

                if line.startswith("FILEPATH:"):
                    update(jid, file=line[len("FILEPATH:"):].strip())
                    continue

                m = PROG_DL.match(line)
                if m and (snapshot(jid) or {}).get("status") != "converting":
                    try:
                        update(jid, percent=float(m.group(1)))
                    except ValueError:
                        pass
                    s = PROG_SPEED.search(line)
                    e = PROG_ETA.search(line)
                    if s or e:
                        update(jid, speed=s.group(1) if s else "", eta=e.group(1) if e else "")
                    continue

                if line.startswith("[ExtractAudio]") or line.startswith("[Metadata]") or line.startswith("[ThumbnailsConvertor]"):
                    if (snapshot(jid) or {}).get("status") != "cancelled":
                        update(jid, status="converting", percent=100.0)

                if line.startswith("ERROR:"):
                    update(jid, error=line[6:].strip())

        code = proc.wait()
    except Exception as e:  # noqa: BLE001 - qualquer falha vira mensagem para a UI
        update(jid, status="error", error=str(e), finished=time.time())
        procs.pop(jid, None)
        return
    finally:
        procs.pop(jid, None)
        # limpa arquivos temporarios INTERMEDIARIOS deste job (nao o .mp3 principal,
        # que ainda precisa ser tagueado/movido; ele e limpo nos caminhos de erro)
        try:
            for tmp in outdir.glob(f".ytflac-{jid}*"):
                if tmp.suffix.lower() in (".webm", ".jpg", ".jpeg", ".png", ".part") or tmp.name.endswith(".norm.mp3"):
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    if code != 0:
        cur = snapshot(jid) or {}
        if cur.get("status") == "cancelled":
            return
        batch_update(job.get("batch_id", ""), False)
        # limpa temp .mp3 orfao
        try:
            for tmp in outdir.glob(f".ytflac-{jid}*.mp3"):
                tmp.unlink(missing_ok=True)
        except OSError:
            pass
        update(jid, status="error",
               error=cur.get("error") or f"yt-dlp saiu com código {code}. Veja {log_path}.",
               finished=time.time())
        _mirror_log(jid, log_path)
        return

    cur = snapshot(jid) or {}
    final = cur.get("file", "")
    if not final or not Path(final).is_file():
        # fallback: arquivo mais recente na pasta (filtra audio e o prefixo do job)
        try:
            search = outdir if meta else dest
            pattern = f".ytflac-{jid}*.*" if meta else "*.*"
            prefix = job.get("name_prefix") or ""
            cand = [p for p in search.glob(pattern)
                    if p.suffix.lower() in (".mp3", ".flac", ".opus", ".ogg", ".m4a", ".webm")
                    and (not prefix or p.name.startswith(prefix))]
            cand.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            final = str(cand[0]) if cand else ""
        except OSError:
            final = ""
    if not final:
        try:
            for tmp in outdir.glob(f".ytflac-{jid}*.mp3"):
                tmp.unlink(missing_ok=True)
        except OSError:
            pass
        update(jid, status="error", error="O arquivo não apareceu na pasta de destino.", finished=time.time())
        _mirror_log(jid, log_path)
        return

    if cfg.get("normalize", True) and fmt in ("mp3", "flac", "opus", "m4a", "ogg"):
        update(jid, status="converting", percent=100.0)
        final = normalize_file(final)

    if cfg.get("mp3gain") and fmt == "mp3":
        update(jid, status="converting", percent=100.0)
        final = apply_mp3gain(final)

    if meta:
        update(jid, status="tagging", percent=100.0)
        final = tag_spotify_file(final, meta, dest, fmt)

    index_record(job, target, final)
    batch_update(job.get("batch_id", ""), True)
    update(jid, status="done", percent=100.0, file=final,
           title=Path(final).stem, finished=time.time())
    _mirror_log(jid, log_path)


def _mirror_log(jid: str, log_path: Path) -> None:
    """last.log = log do job mais recente que terminou (para depurar)."""
    try:
        shutil.copyfile(log_path, CONFIG_DIR / "last.log")
    except OSError:
        pass


def tag_spotify_file(path: str, meta: dict, dest: Path, fmt: str | None = None) -> str:
    """Grava os metadados/capa do Spotify no FLAC e renomeia para a pasta final."""
    py = venv_python()
    src = Path(path)
    if fmt == "original":
        ext = src.suffix or ".mp3"
        out = dest / (final_name(meta, "mp3")[:-4] + ext)
    else:
        out = dest / final_name(meta, fmt)
    if out.exists():
        out = dest / f"{out.stem} ({int(time.time())}){out.suffix}"

    if not py:
        try:
            src.replace(out)
        except OSError:
            pass
        return str(out)

    payload = dict(meta)
    cover = meta.get("cover") or ""
    cover_file = ""
    if cover:
        try:
            ext = ".jpg" if ".jpg" in cover.lower() or "jpeg" in cover.lower() else ".jpg"
            cover_file = str(CONFIG_DIR / f"cover-{meta.get('spotify_id','x')}{ext}")
            (CONFIG_DIR).mkdir(parents=True, exist_ok=True)
            Path(cover_file).write_bytes(http_get(cover, 25))
        except (OSError, urllib.error.URLError):
            cover_file = ""

    tagger = Path(__file__).with_name("tagfile.py")
    cmd = [py, str(tagger), str(src), str(out), json.dumps(payload, ensure_ascii=False)]
    if cover_file:
        cmd.append(cover_file)
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                       encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        pass
    if cover_file:
        try:
            os.unlink(cover_file)
        except OSError:
            pass
    if not out.is_file():
        return str(src)
    return str(out)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"ytflac/{VERSION}"

    def log_message(self, fmt, *args):  # silencia o log padrao
        pass

    # -- helpers ---------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str = "application/json",
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- rotas -----------------------------------------------------------
    def do_OPTIONS(self):  # noqa: N802
        self._send(204, b"")

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/health":
            self._json(200, {
                "ok": True,
                "version": VERSION,
                "ytdlp": find_binary("yt-dlp"),
                "ffmpeg": find_binary("ffmpeg"),
                "mp3gain": find_binary("mp3gain") or "",
                "outdir": load_config()["outdir"],
                "format": load_config().get("format", "mp3"),
            })
            return

        if path == "/api/settings":
            cfg = load_config()
            self._json(200, {"outdir": cfg["outdir"], "cookies": cfg.get("cookies", ""),
                            "format": cfg.get("format", "mp3"),
                            "normalize": bool(cfg.get("normalize", True)),
                            "mp3gain": bool(cfg.get("mp3gain", False)),
                            "mp3gain_available": bool(find_binary("mp3gain")),
                            "min_bitrate": int(cfg.get("min_bitrate", 0) or 0)})
            return

        if path == "/api/history":
            self._json(200, {"history": _history_read()})
            return

        if path == "/api/resolve":
            url = (qs.get("url") or [""])[0]
            ref = spotify_ref(url)
            qref = qobuz_ref(url) if not ref else None
            if not ref and not qref:
                self._json(400, {"error": "URL inválida (Spotify ou Qobuz)"})
                return
            try:
                if qref:
                    if qref[0] == "track":
                        t = qobuz_track_meta(qref[1])
                        t["exists"] = bool(already_done({"meta": t, "url": t.get("source", url), "force": False}, Path(load_config()["outdir"]).expanduser(), Path(load_config()["outdir"]).expanduser()))
                        self._json(200, {"kind": "track", "tracks": [t]})
                    else:
                        coll = qobuz_collection_meta(qref[0], qref[1])
                        outdir = Path(load_config()["outdir"]).expanduser()
                        group = safe_name(coll["name"]) if len(coll["tracks"]) > 1 else ""
                        dest = outdir / group if group else outdir
                        for t in coll["tracks"]:
                            t["exists"] = bool(already_done({"meta": t, "url": t.get("source", ""), "force": False}, dest, outdir))
                        coll["existing"] = sum(1 for t in coll["tracks"] if t.get("exists"))
                        self._json(200, coll)
                elif ref[0] == "track":
                    t = spotify_track_meta(*ref)
                    t["exists"] = bool(already_done({"meta": t, "url": t.get("source", url), "force": False}, Path(load_config()["outdir"]).expanduser(), Path(load_config()["outdir"]).expanduser()))
                    self._json(200, {"kind": "track", "tracks": [t]})
                else:
                    coll = spotify_collection_meta(*ref)
                    outdir = Path(load_config()["outdir"]).expanduser()
                    group = safe_name(coll["name"]) if len(coll["tracks"]) > 1 else ""
                    dest = outdir / group if group else outdir
                    for t in coll["tracks"]:
                        t["exists"] = bool(already_done({"meta": t, "url": t.get("source", ""), "force": False}, dest, outdir))
                    coll["existing"] = sum(1 for t in coll["tracks"] if t.get("exists"))
                    self._json(200, coll)
            except Exception as e:  # noqa: BLE001
                self._json(502, {"error": f"Falha ao ler a fonte: {e}"})
            return

        if path == "/api/exists":
            url = (qs.get("url") or [""])[0].strip()
            vid = video_id(url)
            outdir = Path(load_config()["outdir"]).expanduser()
            if vid:
                f = already_done({"url": url, "force": False}, None, outdir)
                self._json(200, {"exists": bool(f), "file": f or ""})
                return
            # spotify/qobuz single
            ref = spotify_ref(url)
            qref = qobuz_ref(url) if not ref else None
            if ref and ref[0] == "track":
                t = spotify_track_meta(*ref)
                t["exists"] = bool(already_done({"meta": t, "url": t.get("source", url), "force": False}, outdir, outdir))
                self._json(200, {"exists": t["exists"], "title": t["title"]})
                return
            if qref and qref[0] == "track":
                t = qobuz_track_meta(qref[1])
                t["exists"] = bool(already_done({"meta": t, "url": t.get("source", url), "force": False}, outdir, outdir))
                self._json(200, {"exists": t["exists"], "title": t["title"]})
                return
            self._json(400, {"error": "URL inválida"})
            return

        if path == "/api/jobs":
            jid = (qs.get("id") or [""])[0]
            if jid:
                job = snapshot(jid)
                if not job:
                    self._json(404, {"error": "job não encontrado"})
                else:
                    self._json(200, job)
            else:
                with lock:
                    listing = [dict(jobs[j]) for j in reversed(jobs_order) if j in jobs][:50]
                self._json(200, {"jobs": listing})
            return

        if path.startswith("/api/file/"):
            jid = path[len("/api/file/"):]
            job = snapshot(jid)
            if not job or job.get("status") != "done" or not job.get("file"):
                self._json(404, {"error": "arquivo não disponível"})
                return
            outdir = Path(load_config()["outdir"]).expanduser().resolve()
            f = Path(job["file"]).resolve()
            if outdir not in f.parents or f.suffix.lower() not in (".mp3", ".flac", ".opus", ".ogg", ".m4a", ".webm") or not f.is_file():
                self._json(403, {"error": "caminho inválido"})
                return
            data = f.read_bytes()
            self._send(200, data, "audio/flac",
                       {"Content-Disposition": f'attachment; filename="{f.name}"'})
            return

        self._json(404, {"error": "rota desconhecida"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path.lower()
        body = self._read_json()

        if path == "/api/exists":
            url = (qs.get("url") or [""])[0].strip()
            vid = video_id(url)
            outdir = Path(load_config()["outdir"]).expanduser()
            if vid:
                f = already_done({"url": url, "force": False}, None, outdir)
                self._json(200, {"exists": bool(f), "file": f or ""})
                return
            # spotify/qobuz single
            ref = spotify_ref(url)
            qref = qobuz_ref(url) if not ref else None
            if ref and ref[0] == "track":
                t = spotify_track_meta(*ref)
                t["exists"] = bool(already_done({"meta": t, "url": t.get("source", url), "force": False}, outdir, outdir))
                self._json(200, {"exists": t["exists"], "title": t["title"]})
                return
            if qref and qref[0] == "track":
                t = qobuz_track_meta(qref[1])
                t["exists"] = bool(already_done({"meta": t, "url": t.get("source", url), "force": False}, outdir, outdir))
                self._json(200, {"exists": t["exists"], "title": t["title"]})
                return
            self._json(400, {"error": "URL inválida"})
            return

        if path == "/api/jobs":
            url = (body.get("url") or "").strip()
            ref = spotify_ref(url)
            if ref:
                try:
                    if ref[0] == "track":
                        meta = spotify_track_meta(*ref)
                        job = new_job(url, source="spotify", meta=meta, force=body.get("force", False),
                                      query=f"{' '.join(meta['artists'])} {meta['title']}".strip(),
                                      want_duration=meta.get("duration") or 0,
                                      title=f"{' / '.join(meta['artists'])} - {meta['title']}")
                        bid = batch_new(job["title"], 1, [job["id"]], "")
                        with lock:
                            jobs[job["id"]]["batch_id"] = bid
                        job["batch_id"] = bid
                        pool.submit(run_job, job["id"])
                        self._json(200, {"jobs": [job], "kind": "track"})
                        return
                    coll = spotify_collection_meta(*ref)
                    if not coll["tracks"]:
                        self._json(400, {"error": "Não encontrei faixas nessa coleção."})
                        return
                    made = []
                    group = safe_name(coll["name"]) if len(coll["tracks"]) > 1 else ""
                    _cfg = load_config()
                    _outdir = Path(_cfg["outdir"]).expanduser()
                    _dest = _outdir / group if group else _outdir
                    skipped = 0
                    for t in coll["tracks"]:
                        meta = dict(t)
                        meta["album"] = meta.get("album") or (coll["name"] if coll["kind"] == "album" else "")
                        # conta quantas ja existem (para avisar o usuario)
                        probe = new_job(meta["source"], source="spotify", meta=meta, force=False,
                                        query=f"{' '.join(meta['artists'])} {meta['title']}".strip(),
                                        want_duration=meta.get("duration") or 0,
                                        group=group,
                                        title=f"{' / '.join(meta['artists'])} - {meta['title']}")
                        if already_done(probe, _dest, _outdir):
                            skipped += 1
                        jobs.pop(probe["id"], None)  # remove o probe (nao baixa)
                        try:
                            jobs_order.remove(probe["id"])
                        except ValueError:
                            pass
                        job = new_job(meta["source"], source="spotify", meta=meta, force=body.get("force", False),
                                      query=f"{' '.join(meta['artists'])} {meta['title']}".strip(),
                                      want_duration=meta.get("duration") or 0,
                                      group=group,
                                      title=f"{' / '.join(meta['artists'])} - {meta['title']}")
                        made.append(job)
                    bid = batch_new(coll["name"], len(made), [j["id"] for j in made], group)
                    for j in made:
                        with lock:
                            jobs[j["id"]]["batch_id"] = bid
                        j["batch_id"] = bid
                        pool.submit(run_job, j["id"])
                    self._json(200, {"jobs": made, "kind": coll["kind"],
                                     "name": coll["name"], "count": len(made),
                                     "skipped": skipped})
                except Exception as e:  # noqa: BLE001
                    self._json(502, {"error": f"Falha ao ler o Spotify: {e}"})
                return

            qref = qobuz_ref(url)
            if qref:
                try:
                    if qref[0] == "track":
                        meta = qobuz_track_meta(qref[1])
                        job = new_job(url, source="qobuz", meta=meta, force=body.get("force", False),
                                      query=f"{' '.join(meta['artists'])} {meta['title']}".strip(),
                                      want_duration=meta.get("duration") or 0,
                                      title=f"{' / '.join(meta['artists'])} - {meta['title']}")
                        bid = batch_new(job["title"], 1, [job["id"]], "")
                        with lock:
                            jobs[job["id"]]["batch_id"] = bid
                        job["batch_id"] = bid
                        pool.submit(run_job, job["id"])
                        self._json(200, {"jobs": [job], "kind": "track"})
                        return
                    coll = qobuz_collection_meta(qref[0], qref[1])
                    if not coll["tracks"]:
                        self._json(400, {"error": "Não encontrei faixas nessa coleção."})
                        return
                    made = []
                    group = safe_name(coll["name"]) if len(coll["tracks"]) > 1 else ""
                    _cfg = load_config()
                    _outdir = Path(_cfg["outdir"]).expanduser()
                    _dest = _outdir / group if group else _outdir
                    skipped = 0
                    for t in coll["tracks"]:
                        meta = dict(t)
                        meta["album"] = meta.get("album") or (coll["name"] if coll["kind"] == "album" else "")
                        probe = new_job(meta["source"], source="qobuz", meta=meta, force=False,
                                        query=f"{' '.join(meta['artists'])} {meta['title']}".strip(),
                                        want_duration=meta.get("duration") or 0,
                                        group=group,
                                        title=f"{' / '.join(meta['artists'])} - {meta['title']}")
                        if already_done(probe, _dest, _outdir):
                            skipped += 1
                        jobs.pop(probe["id"], None)
                        try:
                            jobs_order.remove(probe["id"])
                        except ValueError:
                            pass
                        job = new_job(meta["source"], source="qobuz", meta=meta, force=body.get("force", False),
                                      query=f"{' '.join(meta['artists'])} {meta['title']}".strip(),
                                      want_duration=meta.get("duration") or 0,
                                      group=group,
                                      title=f"{' / '.join(meta['artists'])} - {meta['title']}")
                        made.append(job)
                    bid = batch_new(coll["name"], len(made), [j["id"] for j in made], group)
                    for j in made:
                        with lock:
                            jobs[j["id"]]["batch_id"] = bid
                        j["batch_id"] = bid
                        pool.submit(run_job, j["id"])
                    self._json(200, {"jobs": made, "kind": coll["kind"],
                                     "name": coll["name"], "count": len(made),
                                     "skipped": skipped})
                except Exception as e:  # noqa: BLE001
                    self._json(502, {"error": f"Falha ao ler o Qobuz: {e}"})
                return

            pl = youtube_playlist_id(url)
            if pl:
                try:
                    coll = youtube_playlist_meta(pl)
                except Exception as e:  # noqa: BLE001
                    self._json(502, {"error": f"Falha ao ler a playlist: {e}"})
                    return
                if not coll["entries"]:
                    self._json(400, {"error": "Playlist vazia, privada ou indisponível."})
                    return
                force = bool(body.get("force", False))
                group = safe_name(coll["name"])
                _outdir = Path(load_config()["outdir"]).expanduser()
                _dest = _outdir / group
                total = len(coll["entries"])
                made, skipped = [], 0
                for i, e in enumerate(coll["entries"], 1):
                    vurl = f"https://www.youtube.com/watch?v={e['id']}"
                    if not force and already_done({"url": vurl, "force": False}, _dest, _outdir):
                        skipped += 1
                        continue
                    made.append(new_job(
                        vurl, group=group, force=force, title=e["title"],
                        name_prefix=playlist_prefix(i, total, e["title"])))
                if not made:
                    self._json(200, {"jobs": [], "kind": "playlist", "name": coll["name"],
                                     "count": 0, "skipped": skipped})
                    return
                bid = batch_new(coll["name"], len(made), [j["id"] for j in made], group)
                for j in made:
                    with lock:
                        jobs[j["id"]]["batch_id"] = bid
                    j["batch_id"] = bid
                    pool.submit(run_job, j["id"])
                self._json(200, {"jobs": made, "kind": "playlist", "name": coll["name"],
                                 "count": len(made), "skipped": skipped})
                return

            if not is_youtube_url(url):
                self._json(400, {"error": "URL inválida (YouTube ou Spotify)"})
                return
            job = new_job(url, force=body.get("force", False))
            bid = batch_new(job["title"] or "YouTube", 1, [job["id"]], "")
            with lock:
                jobs[job["id"]]["batch_id"] = bid
            job["batch_id"] = bid
            pool.submit(run_job, job["id"])
            self._json(200, {"jobs": [job], "kind": "video"})
            return

        if path == "/api/play":
            jid = (body.get("id") or "").strip()
            fpath = (body.get("file") or "").strip()
            if jid:
                job = snapshot(jid)
                if job and job.get("file"):
                    fpath = job["file"]
            if not fpath:
                self._json(404, {"error": "arquivo não disponível"})
                return
            outdir = Path(load_config()["outdir"]).expanduser().resolve()
            f = Path(fpath).expanduser().resolve()
            if outdir not in f.parents or f.suffix.lower() not in (".mp3", ".flac", ".opus", ".ogg", ".m4a", ".webm") or not f.is_file():
                self._json(403, {"error": "caminho inválido"})
                return
            try:
                subprocess.Popen(["xdg-open", str(f)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._json(200, {"ok": True, "file": str(f)})
            except OSError as e:
                self._json(500, {"error": str(e)})
            return

        if path == "/api/jobs/clear":
            with lock:
                jobs.clear()
                jobs_order.clear()
            self._json(200, {"ok": True})
            return

        if path == "/api/history/clear":
            _history_write([])
            self._json(200, {"ok": True})
            return

        if path == "/api/jobs/cancel":
            jid = (body.get("id") or "").strip()
            proc = procs.get(jid)
            update(jid, status="cancelled", finished=time.time())
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self._json(200, {"ok": True})
            return

        if path == "/api/settings":
            cfg = load_config()
            outdir = (body.get("outdir") or "").strip()
            if outdir:
                cfg["outdir"] = str(Path(outdir).expanduser())
            if "cookies" in body:
                cfg["cookies"] = (body.get("cookies") or "").strip()
            if "format" in body:
                fmt = (body.get("format") or "").strip().lower()
                if fmt in ("mp3", "m4a", "opus", "vorbis", "flac", "wav", "aac", "alac", "original"):
                    cfg["format"] = fmt
            if "normalize" in body:
                cfg["normalize"] = bool(body.get("normalize"))
            if "mp3gain" in body:
                cfg["mp3gain"] = bool(body.get("mp3gain"))
            if "min_bitrate" in body:
                try:
                    cfg["min_bitrate"] = max(0, int(body.get("min_bitrate") or 0))
                except (TypeError, ValueError):
                    pass
            save_config(cfg)
            try:
                Path(cfg["outdir"]).expanduser().mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self._json(400, {"error": str(e)})
                return
            self._json(200, cfg)
            return

        if path == "/api/open-folder":
            outdir = Path(load_config()["outdir"]).expanduser()
            try:
                outdir.mkdir(parents=True, exist_ok=True)
                subprocess.Popen(["xdg-open", str(outdir)],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                self._json(200, {"ok": True})
            except OSError as e:
                self._json(500, {"error": str(e)})
            return

        self._json(404, {"error": "rota desconhecida"})


def main() -> int:
    if not find_binary("yt-dlp"):
        print("AVISO: yt-dlp não encontrado no PATH (~/.local/bin).", file=sys.stderr)
    if not find_binary("ffmpeg"):
        print("AVISO: ffmpeg não encontrado. Instale com: sudo pacman -S ffmpeg", file=sys.stderr)

    cfg = load_config()
    try:
        Path(cfg["outdir"]).expanduser().mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"YouTube/Spotify->FLAC service v{VERSION} em http://{HOST}:{PORT}", flush=True)
    print(f"Pasta de destino: {cfg['outdir']}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
