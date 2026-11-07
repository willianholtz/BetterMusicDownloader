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
VERSION = "1.1.0"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ytflac"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_OUTDIR = Path.home() / "Música" / "YouTube FLAC"

YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
}
SPOTIFY_HOSTS = {"open.spotify.com", "play.spotify.com", "spotify.link", "spotify.app.link"}

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

NAME_TEMPLATE = "%(artist,creator,uploader)s - %(title)s.%(ext)s"
INDEX_FILE = CONFIG_DIR / "downloaded.json"

jobs: dict[str, dict] = {}
jobs_order: list[str] = []
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
            "format": "mp3", "normalize": True, "min_bitrate": 0}
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
        "created": time.time(),
        "finished": 0.0,
    }
    with lock:
        jobs[jid] = job
        jobs_order.append(jid)
        # mantem so os 300 mais recentes na memoria
        while len(jobs_order) > 300:
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
            known = index_read()["tracks"].get(sid, "")
            if known and Path(known).is_file():
                return known
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
        known = index_read()["videos"].get(vid, "")
        if known and Path(known).is_file():
            return known
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
        update(jid, status="done", percent=100.0, file=done_file,
               title=Path(done_file).stem, finished=time.time())
        return

    meta = job.get("meta") or None
    if meta and job.get("source") == "spotify" and not (meta.get("isrc") and meta.get("album")):
        if not meta.get("album"):
            itunes_fill(meta)
        deezer_isrc(meta)
        update(jid, meta=meta)
    # faixas do Spotify: baixa para um nome temporario e depois grava as tags
    template = outdir / NAME_TEMPLATE
    if meta:
        template = outdir / f".ytflac-{jid}.%(ext)s"

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

    if code != 0:
        cur = snapshot(jid) or {}
        if cur.get("status") == "cancelled":
            return
        update(jid, status="error",
               error=cur.get("error") or f"yt-dlp saiu com código {code}. Veja {log_path}.",
               finished=time.time())
        _mirror_log(jid, log_path)
        return

    cur = snapshot(jid) or {}
    final = cur.get("file", "")
    if not final or not Path(final).is_file():
        # fallback: arquivo .flac mais recente na pasta
        try:
            ext = fmt if fmt != "opus" else "opus"
            # opus pode vir como webm/m4a/ogg dependendo da fonte
            pattern = f".ytflac-{jid}*.*" if meta else "*.*"
            flacs = sorted(outdir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
            final = str(flacs[0]) if flacs else ""
        except OSError:
            final = ""
    if not final:
        update(jid, status="error", error="O FLAC não apareceu na pasta de destino.", finished=time.time())
        _mirror_log(jid, log_path)
        return

    if cfg.get("normalize", True) and fmt in ("mp3", "flac", "opus", "m4a", "ogg"):
        update(jid, status="converting", percent=100.0)
        final = normalize_file(final)

    if meta:
        update(jid, status="tagging", percent=100.0)
        final = tag_spotify_file(final, meta, dest, fmt)

    index_record(job, target, final)
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
                "outdir": load_config()["outdir"],
                "format": load_config().get("format", "mp3"),
            })
            return

        if path == "/api/settings":
            cfg = load_config()
            self._json(200, {"outdir": cfg["outdir"], "cookies": cfg.get("cookies", ""),
                            "format": cfg.get("format", "mp3"),
                            "normalize": bool(cfg.get("normalize", True)),
                            "min_bitrate": int(cfg.get("min_bitrate", 0) or 0)})
            return

        if path == "/api/resolve":
            url = (qs.get("url") or [""])[0]
            ref = spotify_ref(url)
            if not ref:
                self._json(400, {"error": "URL do Spotify inválida"})
                return
            try:
                if ref[0] == "track":
                    self._json(200, {"kind": "track", "tracks": [spotify_track_meta(*ref)]})
                else:
                    self._json(200, spotify_collection_meta(*ref))
            except Exception as e:  # noqa: BLE001
                self._json(502, {"error": f"Falha ao ler o Spotify: {e}"})
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
                    listing = [dict(jobs[j]) for j in reversed(jobs_order) if j in jobs]
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
                        pool.submit(run_job, job["id"])
                        self._json(200, {"jobs": [job], "kind": "track"})
                        return
                    coll = spotify_collection_meta(*ref)
                    if not coll["tracks"]:
                        self._json(400, {"error": "Não encontrei faixas nessa coleção."})
                        return
                    made = []
                    group = safe_name(coll["name"]) if len(coll["tracks"]) > 1 else ""
                    for t in coll["tracks"]:
                        meta = dict(t)
                        meta["album"] = meta.get("album") or (coll["name"] if coll["kind"] == "album" else "")
                        job = new_job(meta["source"], source="spotify", meta=meta, force=body.get("force", False),
                                      query=f"{' '.join(meta['artists'])} {meta['title']}".strip(),
                                      want_duration=meta.get("duration") or 0,
                                      group=group,
                                      title=f"{' / '.join(meta['artists'])} - {meta['title']}")
                        made.append(job)
                        pool.submit(run_job, job["id"])
                    self._json(200, {"jobs": made, "kind": coll["kind"],
                                     "name": coll["name"], "count": len(made)})
                except Exception as e:  # noqa: BLE001
                    self._json(502, {"error": f"Falha ao ler o Spotify: {e}"})
                return

            if not is_youtube_url(url):
                self._json(400, {"error": "URL inválida (YouTube ou Spotify)"})
                return
            job = new_job(url, force=body.get("force", False))
            pool.submit(run_job, job["id"])
            self._json(200, {"jobs": [job], "kind": "video"})
            return

        if path == "/api/jobs/clear":
            with lock:
                jobs.clear()
                jobs_order.clear()
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
