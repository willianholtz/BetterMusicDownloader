#!/usr/bin/env python3
"""Grava metadados/capa do Spotify num arquivo de audio e move para o nome final.

Roda com o python da venv (~/.local/share/ytflac/venv), que tem o mutagen.
Detecta o formato pela extensao do arquivo (mp3, flac, opus/ogg).
Uso: tagfile.py <entrada> <saida> '<json>' [capa.jpg]
"""

import base64
import json
import os
import sys

COVER_MIME_DEFAULT = "image/jpeg"


def _cover_bytes(path):
    if not path or not os.path.isfile(path):
        return None, None
    with open(path, "rb") as fh:
        data = fh.read()
    mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
    return data, mime


def tag_mp3(audio, meta, cover_path):
    from mutagen.mp3 import MP3
    from mutagen.id3 import TIT2, TPE1, TPE2, TALB, TDRC, TRCK, TPOS, TSRC, COMM, APIC

    try:
        audio.delete()
    except Exception:
        pass
    try:
        audio.add_tags()
    except Exception:
        pass
    t = audio.tags
    if t is None:
        audio.add_tags()
        t = audio.tags

    for key in list(t.keys()):
        if key.startswith("APIC"):
            del t[key]

    def put(Frame, value):
        if value in (None, "", 0):
            return
        t.add(Frame(encoding=3, text=str(value)))

    put(TIT2, meta.get("title"))
    artists = meta.get("artists") or []
    if isinstance(artists, str):
        artists = [artists]
    artists = [a for a in artists if a]
    if artists:
        t.add(TPE1(encoding=3, text=" / ".join(artists)))
    put(TPE2, meta.get("albumartist"))
    put(TALB, meta.get("album"))
    put(TDRC, (meta.get("date") or "")[:10])
    put(TSRC, meta.get("isrc"))

    tn = meta.get("track_number")
    total = meta.get("track_total") or meta.get("tracktotal")
    if tn:
        txt = f"{tn}/{total}" if total else str(tn)
        t.add(TRCK(encoding=3, text=txt))
    dn = meta.get("disc_number")
    dt = meta.get("disc_total") or meta.get("disctotal")
    if dn:
        txt = f"{dn}/{dt}" if dt else str(dn)
        t.add(TPOS(encoding=3, text=txt))

    src_url = meta.get("source")
    if src_url:
        t.add(COMM(encoding=3, lang="eng", desc="comment", text=src_url))
        t.add(COMM(encoding=3, lang="eng", desc="description", text=src_url))

    data, mime = _cover_bytes(cover_path)
    if data:
        t.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
    audio.save()


def tag_flac(audio, meta, cover_path):
    from mutagen.flac import Picture

    audio.delete()
    audio.clear_pictures()

    def put(key, value):
        if value in (None, "", 0):
            return
        audio[key] = str(value)

    put("title", meta.get("title"))
    artists = meta.get("artists") or []
    if isinstance(artists, str):
        artists = [artists]
    artists = [a for a in artists if a]
    if artists:
        audio["artist"] = " / ".join(artists)
    put("albumartist", meta.get("albumartist"))
    put("album", meta.get("album"))
    put("date", (meta.get("date") or "")[:10])
    put("tracknumber", meta.get("track_number"))
    put("discnumber", meta.get("disc_number"))
    put("isrc", meta.get("isrc"))
    put("comment", meta.get("source"))
    put("description", meta.get("source"))
    put("encoder", "ytflac")

    total = meta.get("track_total") or meta.get("tracktotal")
    if total:
        put("tracktotal", total)
    dt = meta.get("disc_total") or meta.get("disctotal")
    if dt:
        put("disctotal", dt)

    data, mime = _cover_bytes(cover_path)
    if data:
        pic = Picture()
        pic.type = 3
        pic.mime = mime
        pic.desc = "Cover"
        pic.data = data
        audio.add_picture(pic)
    audio.save()


def tag_opus(audio, meta, cover_path):
    # Vorbis comments
    audio.delete()
    audio["title"] = str(meta.get("title") or "")
    artists = meta.get("artists") or []
    if isinstance(artists, str):
        artists = [artists]
    artists = [a for a in artists if a]
    if artists:
        audio["artist"] = " / ".join(artists)
    if meta.get("albumartist"):
        audio["albumartist"] = str(meta.get("albumartist"))
    if meta.get("album"):
        audio["album"] = str(meta.get("album"))
    if meta.get("date"):
        audio["date"] = str(meta.get("date"))[:10]
    if meta.get("track_number"):
        tn = meta.get("track_number")
        total = meta.get("track_total") or meta.get("tracktotal")
        audio["tracknumber"] = f"{tn}/{total}" if total else str(tn)
    if meta.get("disc_number"):
        dn = meta.get("disc_number")
        dt = meta.get("disc_total") or meta.get("disctotal")
        audio["discnumber"] = f"{dn}/{dt}" if dt else str(dn)
    if meta.get("isrc"):
        audio["isrc"] = str(meta.get("isrc"))
    if meta.get("source"):
        audio["comment"] = str(meta.get("source"))
        audio["description"] = str(meta.get("source"))
    audio["encoder"] = "ytflac"

    data, mime = _cover_bytes(cover_path)
    if data:
        audio.add_picture(data, mime, 3, "Cover")
    audio.save()


def main() -> int:
    if len(sys.argv) < 4:
        print("uso: tagfile.py <entrada> <saida> <json> [capa]", file=sys.stderr)
        return 2
    src, out, raw = sys.argv[1], sys.argv[2], sys.argv[3]
    cover = sys.argv[4] if len(sys.argv) > 4 else ""
    meta = json.loads(raw)

    ext = os.path.splitext(src)[1].lower()
    if ext == ".flac":
        from mutagen.flac import FLAC
        audio = FLAC(src)
        tag_flac(audio, meta, cover)
    elif ext in (".opus", ".ogg"):
        from mutagen.oggopus import OggOpus
        audio = OggOpus(src)
        tag_opus(audio, meta, cover)
    else:
        from mutagen.mp3 import MP3
        audio = MP3(src)
        tag_mp3(audio, meta, cover)

    os.replace(src, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
