"""Solta a Voz — servidor do karaokê caseiro.

Fluxo: upload/link -> biblioteca com metadata -> pipeline de preparo em background
(separa voz por IA, extrai melodia de referência, alinha a letra pela CANTORIA via
forced alignment) -> player com letra sincronizada, pitch lane e pontuação por frase.

A música só fica jogável quando o preparo termina (status "ready" + stems). O
pipeline roda num worker de thread única servido por uma queue.Queue; jobs
interrompidos por restart voltam pra fila no boot. Toda escrita em library.json
passa por _update_entry/_add_entry sob _lock; leituras são unlocked.

Arquivos por música: data/media/{id}.ext (original), data/stems/{id}/vocals.mp3 +
instrumental.mp3 + pitch.json. library.json guarda metadata + cache da letra + status.
"""
import html
import json
import logging
import os
import queue
import re
import unicodedata
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mutagen import File as MutagenFile
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
MEDIA = DATA / "media"
STEMS = DATA / "stems"
MODELS = DATA / "models"
STATIC = BASE / "static"
LIB_FILE = DATA / "library.json"
FFMPEG_BIN = BASE / "tools" / "ffmpeg" / "bin"

# pydub/audio-separator/yt-dlp acham o ffmpeg por aqui
if FFMPEG_BIN.exists():
    os.environ["PATH"] = str(FFMPEG_BIN) + os.pathsep + os.environ.get("PATH", "")

SEPARATION_MODEL = "UVR-MDX-NET-Voc_FT.onnx"

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".webm", ".mp4"}
MIME = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".opus": "audio/ogg", ".webm": "audio/webm", ".mp4": "audio/mp4",
}
LRCLIB_UA = "SoltaAVoz/1.0 (karaoke caseiro; https://github.com/marcusguelfi)"

app = FastAPI(title="Solta a Voz")
_lock = threading.Lock()
_jobs: "queue.Queue[str]" = queue.Queue()


@app.middleware("http")
async def no_stale_static(request: Request, call_next):
    """Frontend evolui rápido — navegador sempre revalida os estáticos."""
    resp = await call_next(request)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------------------------------------------------------------- biblioteca

import contextlib


@contextlib.contextmanager
def _cross_process_lock():
    """Trava ENTRE PROCESSOS — servidor, batch_fix e scripts jamais escrevem
    library.json ao mesmo tempo. Lição de 2026-07-17: duas escritas simultâneas
    zeraram o arquivo inteiro (NTFS alocou sem gravar). msvcrt no Windows,
    fcntl no Linux (Docker). ATENÇÃO: as travas NÃO conversam entre host e
    container — nunca rode dois servidores sobre a MESMA pasta data/."""
    lock_path = DATA / "library.lock"
    f = open(lock_path, "a+b")
    try:
        try:
            import msvcrt

            def _try_lock():
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

            def _unlock():
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except ImportError:
            import fcntl

            def _try_lock():
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def _unlock():
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        for _ in range(200):  # até ~20s
            try:
                _try_lock()
                break
            except OSError:
                time.sleep(0.1)
        try:
            yield
        finally:
            try:
                _unlock()
            except OSError:
                pass
    finally:
        f.close()


def _load_lib() -> dict:
    for path in (LIB_FILE, LIB_FILE.with_suffix(".json.bak")):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data:
                    return data
            except json.JSONDecodeError:
                continue
    return {}


def _save_lib(lib: dict) -> None:
    """Gravação ATÔMICA: tmp + fsync + os.replace, mantendo .bak da versão
    anterior — corrupção parcial nunca mais destrói a biblioteca."""
    tmp = LIB_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    if LIB_FILE.exists():
        try:
            shutil.copy2(LIB_FILE, LIB_FILE.with_suffix(".json.bak"))
        except OSError:
            pass
    os.replace(tmp, LIB_FILE)


def _update_entry(sid: str, **fields) -> dict:
    if "status" in fields:  # base da estimativa de progresso do preparo
        fields.setdefault("stageAt", time.time())
    with _lock, _cross_process_lock():
        lib = _load_lib()
        if sid not in lib:
            raise HTTPException(404, "Música não encontrada")
        lib[sid].update(fields)
        _save_lib(lib)
        return lib[sid]


def _get_entry(sid: str) -> dict:
    entry = _load_lib().get(sid)
    if not entry:
        raise HTTPException(404, "Música não encontrada")
    return entry


def _add_entry(entry: dict) -> None:
    with _lock, _cross_process_lock():
        lib = _load_lib()
        lib[entry["id"]] = entry
        _save_lib(lib)


# ---------------------------------------------------------------- metadata

JUNK_TITLE = re.compile(
    r"[(\[][^)\]]*(official|oficial|video|vídeo|clipe|lyric|letra|audio|áudio|"
    r"visualizer|hd|4k|mv|remaster|ao vivo)[^)\]]*[)\]]",
    re.IGNORECASE,
)
MENTION = re.compile(r"@\S+")  # títulos do YouTube adoram "@CanalConvidado"


def clean_search_title(title: str) -> str:
    """Versão agressiva pra BUSCA de letra: sem @menções, parênteses e feat."""
    t = MENTION.sub("", title or "")
    t = re.sub(r"[(\[][^)\]]*[)\]]", "", t)
    t = re.sub(r"\b(feat|ft|part)\.?\s.*$", "", t, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", t).strip(" -–—|.")


def first_artist(artist: str) -> str:
    return re.split(r"\s*[,;/&]\s*", artist or "")[0].strip()


def parse_video_title(title: str, uploader: str | None) -> tuple[str, str]:
    """Extrai (artista, faixa) de um título tipo 'Artista - Música (Clipe Oficial)'."""
    clean = MENTION.sub("", JUNK_TITLE.sub("", title or ""))
    clean = re.sub(r"\s{2,}", " ", clean).strip(" -–—|")
    parts = re.split(r"\s*[-–—|]\s*", clean, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0].strip(), parts[1].strip()
    artist = (uploader or "").replace(" - Topic", "").strip()
    return artist, clean or (title or "Sem título")


def read_tags(path: Path) -> dict:
    meta = {"title": "", "artist": "", "album": "", "genre": "", "duration": 0, "bitrate": 0}
    try:
        f = MutagenFile(path, easy=True)
        if f is None:
            return meta
        if f.tags:
            meta["title"] = (f.tags.get("title") or [""])[0]
            meta["artist"] = (f.tags.get("artist") or [""])[0]
            meta["album"] = (f.tags.get("album") or [""])[0]
            meta["genre"] = (f.tags.get("genre") or [""])[0]
        if f.info:
            meta["duration"] = round(getattr(f.info, "length", 0) or 0)
            meta["bitrate"] = int(getattr(f.info, "bitrate", 0) or 0)
    except Exception:
        pass
    return meta


def read_cover(path: Path) -> tuple[bytes, str] | None:
    try:
        f = MutagenFile(path)
        if f is None:
            return None
        if hasattr(f, "pictures") and f.pictures:  # FLAC
            pic = f.pictures[0]
            return pic.data, pic.mime or "image/jpeg"
        if f.tags:
            covr = f.tags.get("covr")  # MP4/M4A
            if covr:
                data = bytes(covr[0])
                mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
                return data, mime
            for key in f.tags.keys():  # ID3 APIC
                if key.startswith("APIC"):
                    apic = f.tags[key]
                    return apic.data, apic.mime or "image/jpeg"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- dificuldade

LRC_TIME = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def parse_lrc(synced: str) -> list[tuple[float, str]]:
    """LRC -> lista ordenada de (segundos, texto). Uma linha pode ter vários
    timestamps (refrão) — cada um vira uma entrada."""
    lines = []
    for raw in (synced or "").splitlines():
        times = LRC_TIME.findall(raw)
        text = LRC_TIME.sub("", raw).strip()
        if not times or not text:
            continue
        for mm, ss in times:
            lines.append((int(mm) * 60 + float(ss), text))
    lines.sort(key=lambda x: x[0])
    return lines


def compute_difficulty(synced: str, duration: float) -> dict | None:
    """Heurística: palavras por minuto cantado + pico de velocidade (p90)."""
    lines = parse_lrc(synced)
    if len(lines) < 4:
        return None
    total_words = 0
    sing_time = 0.0
    rates = []
    for i, (t, text) in enumerate(lines):
        words = len(text.split())
        nxt = lines[i + 1][0] if i + 1 < len(lines) else min(t + 5, max(duration, t + 2))
        dur = max(0.8, min(nxt - t, 8.0))
        total_words += words
        sing_time += dur
        rates.append(words / dur)
    if sing_time < 20:
        return None
    wpm = total_words / (sing_time / 60)
    rates.sort()
    peak_wps = rates[min(len(rates) - 1, int(len(rates) * 0.9))]
    score = wpm * 0.6 + peak_wps * 60 * 0.4
    if score < 80:
        label = "Fácil"
    elif score < 120:
        label = "Médio"
    elif score < 165:
        label = "Difícil"
    else:
        label = "Expert"
    return {"label": label, "score": round(score), "wpm": round(wpm),
            "words": total_words, "lines": len(lines)}


# ---------------------------------------------------------------- LRCLIB

def _lrclib_request(path: str, params: dict):
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v})
    req = urllib.request.Request(
        f"https://lrclib.net/api/{path}?{query}", headers={"User-Agent": LRCLIB_UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_lyrics(artist: str, title: str, album: str, duration: float) -> dict | None:
    clean_t = clean_search_title(title)
    lead = first_artist(artist)

    # 1) busca exata (artista + faixa + duração), com e sem limpeza
    seen_get = set()
    for a, t in ((artist, title), (lead, clean_t)):
        if not (a and t and duration) or (a, t) in seen_get:
            continue
        seen_get.add((a, t))
        try:
            hit = _lrclib_request("get", {
                "artist_name": a, "track_name": t,
                "album_name": album, "duration": int(duration)})
            if hit and (hit.get("syncedLyrics") or hit.get("plainLyrics")):
                return hit
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            pass

    def rank(r):
        dur_diff = abs((r.get("duration") or 0) - duration) if duration else 999
        # regra do estúdio: letra de versão ao vivo só em último caso
        live = 1 if _is_live_title(r.get("trackName")) and not _is_live_title(title) else 0
        return (live, 0 if r.get("syncedLyrics") else 1, dur_diff)

    # 2) busca livre em escada: completa -> artista principal + título limpo -> só título
    seen_q = set()
    for q in (f"{artist} {title}", f"{lead} {clean_t}", clean_t):
        q = q.strip()
        if not q or q in seen_q:
            continue
        seen_q.add(q)
        try:
            results = _lrclib_request("search", {"q": q})
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            continue
        if not results:
            continue
        best = sorted(results, key=rank)[0]
        if duration and abs((best.get("duration") or 0) - duration) > 20 and not best.get("syncedLyrics"):
            continue
        return best
    return None


def _norm_txt(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", " ", s)


def fetch_letras_mus(artist: str, title: str) -> str | None:
    """letras.mus.br: busca (solr do próprio site) + página da letra. Forte em
    MPB/BR, que é onde o LRCLIB mais falha. Volta texto puro (sem timestamps)."""
    q = urllib.parse.quote(f"{first_artist(artist)} {clean_search_title(title)}".strip())
    req = urllib.request.Request(f"https://solr.sscdn.co/letras/m1/?q={q}",
                                 headers={"User-Agent": LRCLIB_UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode("utf-8", "replace")
    data = json.loads(raw[raw.index("(") + 1:raw.rindex(")")])  # JSONP -> JSON
    want = set(_norm_txt(f"{artist} {title}").split())
    doc = None
    for d in data.get("response", {}).get("docs", []):
        if not d.get("url"):
            continue  # docs de álbum/artista não têm página de letra
        got = set(_norm_txt(f"{d.get('art', '')} {d.get('txt', '')}").split())
        if len(want & got) >= max(2, len(want) // 3):
            doc = d
            break
    if not doc:
        return None
    req = urllib.request.Request(
        f"https://www.letras.mus.br/{doc['dns']}/{doc['url']}/",
        headers={"User-Agent": LRCLIB_UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        page = r.read().decode("utf-8", "replace")
    m = re.search(r'<div class="lyric-original">(.*?)</div>', page, re.S)
    if not m:
        return None
    lines = []
    for verse in re.findall(r"<p>(.*?)</p>", m.group(1), re.S):
        for piece in re.split(r"<br\s*/?>", verse):
            txt = html.unescape(re.sub(r"<[^>]+>", "", piece)).strip()
            if txt:
                lines.append(txt)
        lines.append("")  # linha em branco separa estrofes, como no LRCLIB
    text = "\n".join(lines).strip()
    return text if len(text) > 80 else None


def fetch_lyrics_ovh(artist: str, title: str) -> str | None:
    url = ("https://api.lyrics.ovh/v1/"
           + urllib.parse.quote(first_artist(artist) or artist)
           + "/" + urllib.parse.quote(clean_search_title(title) or title))
    req = urllib.request.Request(url, headers={"User-Agent": LRCLIB_UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        text = (json.loads(r.read().decode()).get("lyrics") or "").replace("\r\n", "\n").strip()
    return text if len(text) > 80 else None


LIVE_TITLE = re.compile(r"\b(ao vivo|live|ac[uú]stico|unplugged|acoustic)\b", re.IGNORECASE)


def _is_live_title(title: str | None) -> bool:
    return bool(LIVE_TITLE.search(title or ""))


def source_similarity(cand_text: str, source_text: str | None) -> float | None:
    """Concordância entre o texto de um candidato de letra e a fonte canônica
    (letras.mus.br): média harmônica da sobreposição de palavras nas duas
    direções, normalizadas. Música certa ≥ ~0.6; letra de OUTRA música < ~0.3."""
    if not source_text:
        return None
    a = set(_norm_txt(cand_text or "").split())
    b = set(_norm_txt(source_text).split())
    if len(a) < 10 or len(b) < 10:
        return None
    inter = len(a & b)
    p, r = inter / len(a), inter / len(b)
    return round(2 * p * r / max(p + r, 1e-9), 3)


def fetch_plain_fallback(artist: str, title: str) -> str | None:
    """Rede de segurança quando o LRCLIB não tem a letra. Texto puro basta:
    o forced alignment (whisper) cria o sync do zero a partir dele — esperar
    o preparo inteiro e ficar sem cantar é o pior resultado possível."""
    for fn in (fetch_letras_mus, fetch_lyrics_ovh):
        try:
            text = fn(artist, title)
            if text:
                logging.info("letra via fallback %s: %s - %s", fn.__name__, artist, title)
                return text
        except Exception:
            continue
    return None


def search_and_store_lyrics(sid: str, artist: str | None = None,
                            title: str | None = None) -> dict:
    """Busca letra no LRCLIB, calcula dificuldade e salva no cache da entrada."""
    entry = _get_entry(sid)
    override = bool(artist or title)
    s_artist = artist or entry.get("artist") or ""
    s_title = title or entry.get("title") or ""
    hit = fetch_lyrics(s_artist, s_title, entry.get("album") or "",
                       entry.get("duration") or 0)
    if not hit:
        plain = fetch_plain_fallback(s_artist, s_title)
        if plain:
            hit = {"plainLyrics": plain, "syncedLyrics": None,
                   "artistName": s_artist, "trackName": s_title}
    if not hit:
        result = {"found": False, "synced": None, "plain": None, "difficulty": None}
    else:
        synced = hit.get("syncedLyrics")
        result = {
            "found": True,
            "synced": synced,
            "plain": hit.get("plainLyrics"),
            "difficulty": compute_difficulty(synced, entry.get("duration") or 0) if synced else None,
            "matched": {"artist": hit.get("artistName"), "title": hit.get("trackName")},
        }
    fields = {"lyrics": result}
    if override and result["found"]:
        fields.update({"artist": s_artist or entry.get("artist"),
                       "title": s_title or entry.get("title")})
    # letra mudou e a música já foi separada: realinha essa letra com o áudio
    if result.get("synced"):
        pitch = load_pitch(sid)
        got = correlation_align(pitch, result["synced"]) if pitch else None
        if got:
            fields["autoOffset"] = got[0]
            result["alignScore"] = got[1]
            result["autoOffset"] = got[0]
        elif entry.get("vocalOnset") is not None:
            lrc_lines = parse_lrc(result["synced"])
            if lrc_lines:
                diff = entry["vocalOnset"] - lrc_lines[0][0]
                if abs(diff) <= 30:
                    fields["autoOffset"] = round(diff, 2)
                    result["autoOffset"] = fields["autoOffset"]
    _update_entry(sid, **fields)
    return result


# ---------------------------------------------------------------- pipeline de preparo
# separa voz/instrumental por IA e alinha a letra pelo início real do canto

def detect_vocal_onset(wav_path: Path) -> float | None:
    """Primeiro trecho sustentado (200ms) com energia vocal no stem de voz."""
    import numpy as np
    import soundfile as sf

    try:
        data, sr = sf.read(wav_path, always_2d=True, dtype="float32")
    except Exception:
        return None
    mono = np.abs(data).mean(axis=1)
    win = max(1, int(sr * 0.05))
    n = len(mono) // win
    if n < 8:
        return None
    rms = np.sqrt((mono[: n * win].reshape(n, win) ** 2).mean(axis=1))
    peak = float(rms.max())
    if peak <= 1e-6:
        return None
    active = rms > peak * 0.12
    for i in range(n - 4):
        if active[i:i + 4].all():
            return round(i * 0.05, 2)
    return None


def load_pitch(sid: str) -> dict | None:
    p = STEMS / sid / "pitch.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def correlation_align(pitch: dict, synced: str) -> tuple[float, float] | None:
    """Melhor offset da letra vs canto real: correlaciona os frames com voz cantada
    (pyin, imune a plateia/ruído) com a máscara de onde o LRC espera canto.
    Retorna (offset_segundos, cobertura 0-1)."""
    import numpy as np

    lines = parse_lrc(synced)
    if len(lines) < 4:
        return None
    hop = pitch["hop"]
    voiced = np.array([0.0 if m is None else 1.0 for m in pitch["midi"]], dtype=np.float32)
    n = len(voiced)
    if n < 100 or voiced.sum() < 50:
        return None
    mask = np.zeros(n, dtype=np.float32)
    for i, (t, _text) in enumerate(lines):
        nxt = lines[i + 1][0] if i + 1 < len(lines) else t + 5
        a = int(t / hop)
        b = int(min(nxt, t + 8) / hop)
        if a < n:
            mask[max(0, a):max(0, min(b, n))] = 1.0
    denom = float(min(voiced.sum(), mask.sum()))
    if denom < 50:
        return None

    def overlap(shift: int) -> float:
        if shift >= 0:
            return float(np.dot(voiced[shift:], mask[:n - shift])) if shift < n else 0.0
        return float(np.dot(voiced[:n + shift], mask[-shift:])) if -shift < n else 0.0

    max_shift = int(35 / hop)
    best_s, best_ov = 0, -1.0
    for s in range(-max_shift, max_shift + 1, 2):  # passo grosso: 64ms
        ov = overlap(s)
        if ov > best_ov:
            best_ov, best_s = ov, s
    for s in range(best_s - 2, best_s + 3):  # refino: 32ms
        ov = overlap(s)
        if ov > best_ov:
            best_ov, best_s = ov, s
    return round(best_s * hop, 2), round(best_ov / denom, 3)


def align_best_candidate(sid: str, pitch: dict | None = None) -> dict | None:
    """Escolhe, entre as versões de letra do LRCLIB, a que melhor casa com o áudio
    (a letra pode ser de outra versão da música — estúdio vs ao vivo), e o offset."""
    entry = _get_entry(sid)
    pitch = pitch or load_pitch(sid)
    if not pitch:
        return None

    candidates = []
    cached = entry.get("lyrics") or {}
    if cached.get("synced"):
        matched = cached.get("matched") or {}
        candidates.append({"syncedLyrics": cached["synced"], "plainLyrics": cached.get("plain"),
                           "artistName": matched.get("artist"), "trackName": matched.get("title")})
    e_artist, e_title = entry.get("artist") or "", entry.get("title") or ""
    seen = {c["syncedLyrics"] for c in candidates}
    for q in (f"{e_artist} {e_title}".strip(),
              f"{first_artist(e_artist)} {clean_search_title(e_title)}".strip()):
        if not q or len(candidates) >= 8:
            continue
        try:
            results = _lrclib_request("search", {"q": q})
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            continue
        for r in results or []:
            s = r.get("syncedLyrics")
            if s and s not in seen:
                seen.add(s)
                candidates.append(r)
            if len(candidates) >= 8:
                break

    duration = entry.get("duration") or 0
    # fonte canônica (letras.mus.br) valida a ESCOLHA da letra — pedido do
    # Marcus após LRCLIB entregar "outra música" e versão "(Ao Vivo)":
    # "precisamos de validação do letras.com, não só confiar no que a IA ouviu"
    letras_text = None
    try:
        letras_text = fetch_letras_mus(e_artist, e_title)
    except Exception:
        pass
    # transcrição do canto real: o sinal que diz se a letra é da MÚSICA CERTA
    # (correlação só mede timing; letra errada com canto denso ainda correlaciona).
    # Dica de idioma pelo texto dos candidatos evita transcrição-lixo (idioma errado).
    transcript = None
    hint = guess_language(" ".join(c.get("plainLyrics") or c["syncedLyrics"]
                                   for c in candidates[:3])) if candidates else None
    try:
        transcript = transcribe_vocals(sid, language=hint)
        if not transcript_is_reliable(transcript):
            transcript = None  # transcrição-lixo: cai pra correlação, não enviesa
    except Exception:
        logging.exception("transcrição falhou pra %s", sid)
    best = None
    for cand in candidates:
        got = correlation_align(pitch, cand["syncedLyrics"])
        if not got:
            continue
        offset, coverage = got
        # letra de OUTRA versão da música (ao vivo estendida etc.) perde pontos
        cand_dur = cand.get("duration") or 0
        dur_penalty = 0.15 if cand_dur and duration and abs(cand_dur - duration) > 25 else 0.0
        sim = lyric_similarity(cand.get("plainLyrics") or cand["syncedLyrics"], transcript) \
            if transcript else 0.0
        # concordância com a fonte canônica + castigo pra versão ao vivo
        # quando a NOSSA faixa não é ao vivo (regra do estúdio)
        src_sim = source_similarity(cand.get("plainLyrics") or cand["syncedLyrics"],
                                    letras_text)
        live_pen = 0.25 if _is_live_title(cand.get("trackName")) \
            and not _is_live_title(e_title) else 0.0
        # similaridade com o canto real domina; fonte canônica pesa junto;
        # correlação/duração/ao-vivo desempatam
        score = sim * 2.0 + (src_sim or 0.0) * 1.2 + coverage - dur_penalty - live_pen
        if best is None or score > best[4]:
            best = (cand, offset, coverage, score, sim, src_sim)
    if not best:
        return None
    cand, offset, coverage, _score, sim, src_sim = best
    # venceu uma versão AO VIVO e a nossa faixa é de estúdio: mesmo com o
    # vocabulário parecido, a ORDEM/texto dos versos difere ("Ontem" × "Hoje",
    # Flor de Tangerina) — o texto de ESTÚDIO do letras.mus.br assume
    if letras_text and _is_live_title(cand.get("trackName")) \
            and not _is_live_title(e_title):
        result = {"found": True, "synced": None, "plain": letras_text,
                  "difficulty": None, "source": "letras.mus.br",
                  "matched": {"artist": e_artist, "title": e_title},
                  "srcMatch": src_sim, "reason": "candidato era ao vivo"}
        _update_entry(sid, lyrics=result, autoOffset=0.0)
        return result
    # nem a melhor candidata bate com a fonte canônica: o LRCLIB não tem essa
    # música direito — o texto do letras.mus.br VIRA a letra (whisper cria o
    # sync do zero a partir do texto puro)
    if letras_text and src_sim is not None and src_sim < 0.45:
        result = {"found": True, "synced": None, "plain": letras_text,
                  "difficulty": None, "source": "letras.mus.br",
                  "matched": {"artist": e_artist, "title": e_title},
                  "srcMatch": src_sim}
        _update_entry(sid, lyrics=result, autoOffset=0.0)
        return result
    result = {
        "found": True,
        "synced": cand["syncedLyrics"],
        "plain": cand.get("plainLyrics"),
        "difficulty": compute_difficulty(cand["syncedLyrics"], entry.get("duration") or 0),
        "matched": {"artist": cand.get("artistName"), "title": cand.get("trackName")},
        "alignScore": coverage,
        "lyricMatch": sim if transcript else None,  # confiança de ser a letra CERTA
        "srcMatch": src_sim,  # concordância com letras.mus.br (None = fonte indisponível)
    }
    _update_entry(sid, lyrics=result, autoOffset=offset)
    return result


# ------------------------------------------------ alinhamento pela cantoria
# Forced alignment (stable-ts/Whisper): acha onde cada linha da letra é CANTADA
# no stem de voz — a letra segue o cantor, não o relógio da música.

_whisper_model = None
_whisper_lock = threading.Lock()


def _get_whisper():
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            import stable_whisper
            _whisper_model = stable_whisper.load_model("small", device="cpu")
        return _whisper_model


_fast_model = None
_fast_lock = threading.Lock()


def _get_whisper_fast():
    """Modelo rápido ("base") só pra TRANSCRIÇÃO de verificação de identidade —
    não precisa da precisão do "small" (só das palavras de conteúdo). ~2-3× mais
    rápido. O alinhamento (que precisa de precisão) continua no "small"."""
    global _fast_model
    with _fast_lock:
        if _fast_model is None:
            import stable_whisper
            _fast_model = stable_whisper.load_model("base", device="cpu")
        return _fast_model


def _norm_words(text: str) -> list[str]:
    import unicodedata

    t = unicodedata.normalize("NFD", (text or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return t.split()


def _latin_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if ord(c) < 0x250) / len(letters)


def transcript_is_reliable(transcript: str | None) -> bool:
    """Transcrição confiável = alfabeto latino, conteúdo suficiente e DIVERSA. O
    Whisper falha de dois jeitos que dariam falsa 'letra suspeita':
    1) idioma errado -> lixo não-latino (ex.: cingalês);
    2) ALUCINAÇÃO em loop em intro/silêncio -> 'a little bit of a little bit of...'
       (poucas palavras repetidas). Nesses casos NÃO verificamos, não acusamos."""
    if not transcript:
        return False
    # scripts estrangeiros (cirílico/grego/CJK) = Whisper confuso com o idioma,
    # mesmo que a maioria seja latina (caso Rammstein: alemão vira salada com скоро/かな)
    if any(ord(c) >= 0x370 for c in transcript):
        return False
    words = _norm_words(transcript)
    if len(words) < 8 or _latin_ratio(transcript) <= 0.6:
        return False
    return len(set(words)) / len(words) >= 0.30  # diversidade: loop = baixa


def vocal_start_from_energy(energy: list, hop: float, sustain: float = 0.5) -> float:
    """Início do canto: 1º trecho com voz sustentada (>= sustain segundos). Pra
    transcrição começar DEPOIS do intro instrumental, onde o Whisper alucina."""
    if not energy:
        return 0.0
    need, run = max(1, int(sustain / hop)), 0
    for k, v in enumerate(energy):
        run = run + 1 if v else 0
        if run >= need:
            return max(0.0, (k - need) * hop - 1.5)  # 1,5s de folga antes
    return 0.0


def transcribe_vocals(sid: str, force: bool = False, max_seconds: int = 110,
                      language: str | None = None) -> str | None:
    """Transcreve o stem de voz (Whisper transcribe, NÃO align) — o que está
    REALMENTE sendo cantado. Cacheado em stems/{id}/transcript.json. É a base da
    verificação: forced alignment encaixa qualquer texto, transcrição não mente.

    Otimizado pra IDENTIDADE (não display): sem timestamps de palavra e só os
    primeiros ~110s — o bastante pra pegar letra totalmente trocada, ~4× mais rápido."""
    cache = STEMS / sid / "transcript.json"
    if cache.exists() and not force:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            txt, prev_hint = data.get("text"), data.get("hint", "__old__")
            # reusa se confiável, ou se já tentamos EXATAMENTE esta dica (não readianta)
            if txt and (transcript_is_reliable(txt) or prev_hint == language):
                return txt
        except json.JSONDecodeError:
            pass
    vocals = STEMS / sid / "vocals.mp3"
    if not vocals.exists():
        return None
    # clipa max_seconds A PARTIR DO ONSET DO CANTO — pular o intro instrumental
    # (piano/silêncio) mata a alucinação do Whisper na raiz, além de acelerar
    pitch = load_pitch(sid)
    start = vocal_start_from_energy((pitch or {}).get("energy"), (pitch or {}).get("hop", 0.032))
    clip = STEMS / sid / "_vclip.mp3"
    src = vocals
    ffmpeg = FFMPEG_BIN / "ffmpeg.exe"
    try:
        subprocess.run([str(ffmpeg) if ffmpeg.exists() else "ffmpeg", "-y",
                        "-ss", str(round(start, 2)), "-t", str(max_seconds),
                        "-i", str(vocals), str(clip)],
                       capture_output=True, check=True)
        src = clip
    except Exception:
        pass
    model = _get_whisper_fast()
    try:
        # dica de idioma evita o Whisper "viajar" pra um idioma exótico e cuspir lixo
        result = model.transcribe(str(src), language=language,
                                  word_timestamps=False, suppress_silence=False)
        text = result.text or ""
        lang = language or getattr(result, "language", None)
    except Exception:
        logging.exception("transcrição falhou pra %s", sid)
        return None
    finally:
        clip.unlink(missing_ok=True)
    cache.write_text(json.dumps({"text": text, "language": lang, "hint": language}),
                     encoding="utf-8")
    return text


# ------------------------------------------------ máscara de fala cantada
# ALIGN v2 fase A. Instrumento de pitch contínuo vazado no stem de voz (a GAITA
# do Samurai é o caso-escola; a literatura de singing voice detection aponta
# esses instrumentos como O falso positivo clássico) engana TODAS as nossas
# regras de energia: fantasma, clamp, extensão e onset passam a tratar solo como
# canto. A discriminação que funciona é LINGUÍSTICA: o Whisper devolve
# no_speech_prob por segmento — gaita não tem fonemas. Aqui isso vira uma máscara
# sobre a energia. Cobre a MÚSICA INTEIRA (transcribe_vocals só vê ~110s).

SPEECH_NSP_MAX = 0.5      # acima disso o segmento não é fala cantada
SPEECH_MARGIN = 0.3       # folga (s) nas bordas do segmento
_speech_cache: dict[str, dict | None] = {}


def full_transcribe(sid: str, force: bool = False) -> dict | None:
    """UMA passada de transcrição na música inteira que alimenta as duas frentes
    do ALIGN v2: os segmentos com no_speech_prob (máscara de fala, fase A) e as
    palavras com tempo (âncoras por linha, fase C). Modelo "small" — a precisão
    de palavra é o que sustenta o anchor-matching. Escreve speechmap.json e
    words.json; ~2-4min de CPU por música, uma vez só."""
    vocals = STEMS / sid / "vocals.mp3"
    if not vocals.exists():
        return None
    try:
        model = _get_whisper()
        result = model.transcribe(str(vocals), language=detected_language(sid),
                                  word_timestamps=True, suppress_silence=False)
    except Exception:
        logging.exception("transcrição completa falhou pra %s", sid)
        return None
    segments = [[round(float(s.start), 2), round(float(s.end), 2),
                 round(float(getattr(s, "no_speech_prob", 0.0) or 0.0), 3)]
                for s in result.segments]
    words = []
    for seg in result.segments:
        for w in (seg.words or []):
            txt = (getattr(w, "word", "") or "").strip()
            if txt and float(w.end) > float(w.start):
                words.append([round(float(w.start), 2), round(float(w.end), 2), txt])
    end = max((s[1] for s in segments), default=0.0)
    data = {"segments": segments, "covered": [0.0, round(end, 2)]}
    try:
        (STEMS / sid / "speechmap.json").write_text(json.dumps(data), encoding="utf-8")
        (STEMS / sid / "words.json").write_text(json.dumps({"words": words}),
                                                encoding="utf-8")
    except OSError:
        pass
    _speech_cache.clear()
    return data


def speech_map(sid: str, build: bool = True, force: bool = False) -> dict | None:
    """Segmentos de fala cantada da música inteira: {"segments": [[a,b,nsp]...],
    "covered": [0, fim]}. Cacheado em stems/{id}/speechmap.json."""
    cache = STEMS / sid / "speechmap.json"
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return full_transcribe(sid, force=force) if build else None


def word_transcript(sid: str, build: bool = False) -> list | None:
    """Palavras cantadas com tempo [[a, b, palavra]] — base das âncoras (fase C)."""
    cache = STEMS / sid / "words.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8")).get("words") or None
        except json.JSONDecodeError:
            pass
    if build and full_transcribe(sid):
        return word_transcript(sid, build=False)
    return None


def _speech_mask(sid: str, n: int, hop: float, build: bool = False) -> list[bool] | None:
    """True = há fala cantada naquele frame. None quando não há evidência."""
    key = f"{sid}:{n}:{hop}"
    if key in _speech_cache:
        return _speech_cache[key]
    data = speech_map(sid, build=build)
    mask = None
    if data and data.get("segments"):
        c0, c1 = data.get("covered") or [0.0, 0.0]
        mask = [False] * n
        for k in range(n):
            t = k * hop
            if not (c0 <= t <= c1):
                mask[k] = True  # fora do que inspecionamos: não julga
        for a, b, nsp in data["segments"]:
            if nsp > SPEECH_NSP_MAX:
                continue
            for k in range(max(0, int((a - SPEECH_MARGIN) / hop)),
                           min(n, int((b + SPEECH_MARGIN) / hop) + 1)):
                mask[k] = True
    _speech_cache[key] = mask
    return mask


def sung_active(sid: str, active, hop: float, build: bool = False):
    """Aplica a máscara num vetor de atividade vocal (0/1 ou bool). Devolve None
    se não há máscara — ou se ela apagaria quase tudo (transcrição furada:
    nunca deixar o remédio ser pior que a doença)."""
    n = len(active)
    mask = _speech_mask(sid, n, hop, build=build)
    if mask is None:
        return None
    out = [(1 if (active[k] and mask[k]) else 0) for k in range(n)]
    antes = sum(1 for v in active if v)
    if antes and sum(out) / antes < 0.08:
        logging.warning("máscara de fala descartada em %s (apagaria %.0f%%)",
                        sid, 100 * (1 - sum(out) / antes))
        return None
    return out


def sung_energy(sid: str, pitch: dict | None = None, build: bool = False):
    """Energia do stem já limpa de instrumento vazado (ou a crua, se não há
    evidência). Usada por ghost/clamp/extensão — o CORE do sync."""
    pitch = pitch or load_pitch(sid)
    energy = (pitch or {}).get("energy")
    if not energy:
        return None
    masked = sung_active(sid, energy, pitch["hop"], build=build)
    return masked if masked is not None else energy


def detected_language(sid: str) -> str | None:
    """Idioma detectado pelo Whisper na transcrição (cacheado) — mais confiável
    que o heurístico guess_language pra alinhar (biblioteca tem PT/EN/ES)."""
    cache = STEMS / sid / "transcript.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8")).get("language")
        except json.JSONDecodeError:
            pass
    return None


def lyric_similarity(lyrics_text: str, transcript: str) -> float:
    """0..1: quanto o CANTO REAL (transcrição) confere com a letra. É a PRECISÃO
    das palavras de conteúdo (len>=4) da transcrição que aparecem na letra —
    'do que eu ouvi cantar, quanto está na letra?'. Escolhido de propósito em vez
    de recall: a transcrição cobre só um trecho (primeiros ~110s), então recall
    da letra inteira puniria música longa certa; precisão não. Palavrinhas comuns
    (len<4) fora pra não inflar. Letra certa ~0.6-0.9; música errada ~<0.3."""
    lset = {w for w in _norm_words(lyrics_text) if len(w) >= 4}
    tw = [w for w in _norm_words(transcript) if len(w) >= 4]
    if len(tw) < 5 or not lset:
        return 0.0
    hit = sum(1 for w in tw if w in lset)
    return round(hit / len(tw), 3)


def guess_language(text: str) -> str | None:
    """PT/EN/ES por palavras-marca. Retorna None se INCERTO (ex.: alemão) — aí o
    Whisper auto-detecta, em vez de forçar um idioma errado e gerar transcrição-lixo."""
    t = f" {re.sub(r'[^a-zà-úç ]', ' ', text.lower())} "
    scores = {
        "pt": sum(t.count(w) for w in (" não ", " nao ", " você ", " voce ", " meu ",
                                       " minha ", " pra ", " mais ", " eu ", " são ", " uma ")),
        "en": sum(t.count(w) for w in (" the ", " you ", " and ", " love ", " that ",
                                       " this ", " it ", " of ", " my ", " to ", " with ")),
        "es": sum(t.count(w) for w in (" los ", " las ", " una ", " con ", " por ", " muy ",
                                       " pero ", " estoy ", " corazón ", " mi ", " tú ", " esta ")),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] >= 2 else None  # incerto -> deixa o Whisper decidir


def _regroup_words_to_lines(result, line_texts: list[str]) -> list[tuple] | None:
    """Se os segmentos não baterem com as linhas, remapeia pelas palavras
    (o align preserva a ordem exata do texto fornecido)."""
    words = [w for seg in result.segments for w in seg.words]
    counts = [len(t.split()) for t in line_texts]
    if sum(counts) != len(words):
        return None
    out, i = [], 0
    for c in counts:
        chunk = words[i:i + c]
        i += c
        out.append((float(chunk[0].start), float(chunk[-1].end), chunk))
    return out


def _line_words(chunk, start: float) -> list | None:
    """Timestamps POR PALAVRA, relativos ao início da linha (padrão UltraStar:
    sílaba a sílaba; aqui palavra a palavra). Relativos = sobrevivem de graça a
    qualquer deslocamento da linha (reconcile, offset, editor humano)."""
    if not chunk:
        return None
    words = []
    for w in chunk:
        try:
            ws, we = float(w.start), float(w.end)
        except (TypeError, ValueError):
            return None
        text = (getattr(w, "word", "") or "").strip()
        if we > ws >= 0 and text:
            words.append([round(ws - start, 2), round(we - start, 2), text])
    return words if len(words) >= 2 else None


def _interpolate_bad_lines(lines: list[dict], good: list[int]) -> None:
    """Linhas que o Whisper não cravou são distribuídas entre as âncoras boas
    (rap denso: melhor estimar do que jogar fora o alinhamento inteiro)."""
    for i, ln in enumerate(lines):
        if ln.pop("_ok"):
            continue
        ln["interp"] = True  # não foi ancorada no áudio: candidata ao CTC (fase B)
        prev_g = max((g for g in good if g < i), default=None)
        next_g = min((g for g in good if g > i), default=None)
        if prev_g is None and next_g is None:
            return
        if prev_g is None:
            step = (lines[next_g]["t"]) / max(next_g, 1)
            ln["t"] = round(max(0.0, lines[next_g]["t"] - (next_g - i) * step), 2)
            ln["end"] = round(ln["t"] + max(step - 0.2, 1.0), 2)
        elif next_g is None:
            ln["t"] = round(lines[prev_g]["end"] + (i - prev_g - 1) * 2.5 + 0.3, 2)
            ln["end"] = round(ln["t"] + 2.0, 2)
        else:
            a, b = lines[prev_g]["end"], lines[next_g]["t"]
            count = next_g - prev_g - 1
            slot = max((b - a) / max(count, 1), 0.5)
            k = i - prev_g - 1
            ln["t"] = round(a + slot * k + 0.05, 2)
            ln["end"] = round(min(a + slot * (k + 1) - 0.05, b), 2)


def whisper_align_lines(sid: str, line_texts: list[str]) -> list[dict] | None:
    vocals = STEMS / sid / "vocals.mp3"
    if not vocals.exists():
        return None
    text = "\n".join(line_texts)
    model = _get_whisper()
    # idioma detectado pelo Whisper na transcrição > heurístico (PT/EN/ES na lib)
    lang = detected_language(sid) or guess_language(text)

    # rap/fluxo denso às vezes falha com a supressão de silêncio — tenta sem ela
    for attempt in ({}, {"suppress_silence": False}):
        try:
            result = model.align(str(vocals), text, language=lang,
                                 original_split=True, **attempt)
        except Exception:
            continue
        segs = list(result.segments)
        if len(segs) == len(line_texts):
            spans = [(float(s.start), float(s.end), list(s.words or [])) for s in segs]
        else:
            spans = _regroup_words_to_lines(result, line_texts)
            if spans is None:
                continue
        lines, good = [], []
        for i, ((start, end, chunk), txt) in enumerate(zip(spans, line_texts)):
            valid = end > start > 0
            if valid:
                good.append(i)
            ln = {"t": round(start, 2), "end": round(end, 2),
                  "text": txt, "_ok": valid}
            if valid:
                words = _line_words(chunk, start)
                if words:
                    ln["words"] = words
            lines.append(ln)
        if len(good) < max(4, len(lines) * 0.5):
            continue  # fraco demais mesmo pra salvar — tenta próxima variante
        _interpolate_bad_lines(lines, good)
        for i in range(1, len(lines)):  # tempos sempre crescentes
            if lines[i]["t"] <= lines[i - 1]["t"]:
                lines[i]["t"] = round(lines[i - 1]["t"] + 0.05, 2)
            if lines[i]["end"] < lines[i]["t"]:
                lines[i]["end"] = round(lines[i]["t"] + 1.5, 2)
        return lines
    return None


# ------------------------------------------------ alinhador CTC (ALIGN v2 fase B)
# O whisper alinha por PALAVRAS FALADAS: em melisma (vogal sustentada, "Quaaaando"
# do Samba Morrer, "Saaaai" do Samurai) ele desiste e PULA trechos. O CTC tem o
# token "blank", que ABSORVE duração — nota esticada alinha por construção.
# MMS_FA = wav2vec2 multilíngue (1100+ idiomas, PT incluso), treinado em fala mas
# com transferência comprovada pra canto. Emissões em blocos: a música inteira de
# uma vez estoura a atenção quadrática.

_mms = {}
_mms_lock = threading.Lock()


def _get_mms():
    with _mms_lock:
        if not _mms:
            from torchaudio.pipelines import MMS_FA

            _mms["bundle"] = MMS_FA
            _mms["model"] = MMS_FA.get_model()
            _mms["tokenizer"] = MMS_FA.get_tokenizer()
            _mms["aligner"] = MMS_FA.get_aligner()
        return _mms


def _mms_words(text: str) -> list[str]:
    """Palavras no alfabeto que o MMS entende (sem acento/pontuação)."""
    return [w for w in _norm_txt(text).split() if w]


def mms_align_lines(sid: str, line_texts: list[str],
                    chunk_s: float = 20.0) -> list[dict] | None:
    """Mesma assinatura de whisper_align_lines: [{t, end, text, words}], com
    words RELATIVAS ao início da linha. None se não deu pra alinhar."""
    import torch

    vocals = STEMS / sid / "vocals.mp3"
    if not vocals.exists():
        return None
    # cada linha vira uma lista de palavras normalizadas; guarda quantas por linha
    per_line = [_mms_words(t) for t in line_texts]
    flat = [w for ws in per_line for w in ws]
    if len(flat) < 8:
        return None
    try:
        import librosa

        y, sr = librosa.load(str(vocals), sr=16000, mono=True)
        wave = torch.from_numpy(y).unsqueeze(0)
        m = _get_mms()
        step = int(chunk_s * sr)
        with torch.inference_mode():
            parts = [m["model"](wave[:, i:i + step])[0] for i in range(0, wave.size(1), step)]
            emission = torch.cat(parts, dim=1)
            token_spans = m["aligner"](emission[0], m["tokenizer"](flat))
    except Exception:
        logging.exception("alinhamento MMS falhou pra %s", sid)
        return None
    if len(token_spans) != len(flat):
        return None
    ratio = wave.size(1) / emission.size(1) / sr  # frames -> segundos
    times = [(spans[0].start * ratio, spans[-1].end * ratio) for spans in token_spans]

    lines, i = [], 0
    for text, ws in zip(line_texts, per_line):
        if not ws:
            lines.append(None)
            continue
        chunk = times[i:i + len(ws)]
        i += len(ws)
        start, end = chunk[0][0], chunk[-1][1]
        ln = {"t": round(start, 2), "end": round(max(end, start + 0.3), 2), "text": text}
        words = [[round(a - start, 2), round(b - start, 2), w]
                 for (a, b), w in zip(chunk, ws)]
        if len(words) >= 2:
            ln["words"] = words
        lines.append(ln)
    # linha sem palavra utilizável: interpola entre as vizinhas (raro)
    for k, ln in enumerate(lines):
        if ln is not None:
            continue
        prev = next((lines[j] for j in range(k - 1, -1, -1) if lines[j]), None)
        nxt = next((lines[j] for j in range(k + 1, len(lines)) if lines[j]), None)
        t = (prev["end"] + 0.1) if prev else (nxt["t"] - 1.0 if nxt else 0.0)
        lines[k] = {"t": round(max(0.0, t), 2), "end": round(max(0.0, t) + 1.0, 2),
                    "text": line_texts[k]}
    for k in range(1, len(lines)):  # tempos sempre crescentes
        if lines[k]["t"] <= lines[k - 1]["t"]:
            lines[k]["t"] = round(lines[k - 1]["t"] + 0.05, 2)
        if lines[k]["end"] < lines[k]["t"]:
            lines[k]["end"] = round(lines[k]["t"] + 1.0, 2)
    return lines


def suspect_line_idx(lines: list[dict]) -> list[int]:
    """Linhas em que o whisper NÃO se ancorou: interpoladas (ele desistiu) ou
    esmagadas (duração impossível para o nº de palavras — sintoma clássico de
    melisma, quando ele pula a vogal esticada e comprime a frase)."""
    out = []
    for i, ln in enumerate(lines):
        n = max(1, len(ln.get("text", "").split()))
        dur = (ln.get("end") or ln["t"]) - ln["t"]
        if ln.get("interp") or dur < 0.18 * n:
            out.append(i)
    return out


def hybrid_align_lines(sid: str, line_texts: list[str]) -> list[dict] | None:
    """MOTOR PADRÃO do ALIGN v2 (decidido pelo A/B de 2026-07-19, 7 casos):
    whisper é melhor quando se ancora (controle 34ms × 112ms do CTC), mas
    DESISTE em melisma/andamento variável — e aí o CTC ganha feio (Samurai
    48→22ms, Take Me Out 788→492ms). Então: whisper manda, e só as linhas onde
    ele não se ancorou recebem o tempo do CTC — aceitas apenas se couberem
    entre as vizinhas confiáveis (mesma lógica de trilho do reconcile)."""
    lines = whisper_align_lines(sid, line_texts)
    if not lines:
        return mms_align_lines(sid, line_texts)
    bad = suspect_line_idx(lines)
    if len(bad) < max(2, len(lines) * 0.08):
        return lines  # whisper se virou bem: não paga o custo do CTC
    alt = mms_align_lines(sid, line_texts)
    if not alt or len(alt) != len(lines):
        return lines
    trocadas = 0
    for i in bad:
        novo = alt[i]
        piso = lines[i - 1]["end"] if i > 0 and i - 1 not in bad else 0.0
        teto = lines[i + 1]["t"] if i + 1 < len(lines) and i + 1 not in bad else 1e9
        if not (piso - 0.05 <= novo["t"] < teto - 0.05):
            continue  # CTC discorda das âncoras firmes do whisper: mantém
        lines[i] = {**novo, "end": round(min(novo["end"], max(teto - 0.05, novo["t"] + 0.4)), 2),
                    "ctc": True}
        trocadas += 1
    if trocadas:
        logging.info("híbrido: %d linha(s) do CTC em %s", trocadas, sid)
    return lines


def uncovered_sung_regions(sid: str, lines: list[dict], min_len: float = 6.0) -> list[tuple[float, float]]:
    """Regiões com canto (energia) FORA de qualquer janela de frase — refrões
    repetidos/outros que o LRC não lista. É a causa do 'final sem letra' e do
    alinhador espremer frases (Péricles, Mulher de Fases)."""
    pitch = load_pitch(sid)
    energy = sung_energy(sid, pitch)  # gaita/solo não conta como canto (fase A)
    if not energy or not lines:
        return []
    hop = pitch["hop"]
    n = len(energy)
    covered = [False] * n
    for i, ln in enumerate(lines):
        end = ln.get("end") or (lines[i + 1]["t"] if i + 1 < len(lines) else ln["t"] + 5)
        for k in range(max(0, int((ln["t"] - 0.4) / hop)), min(n, int((end + 0.4) / hop))):
            covered[k] = True
    regions, gap_lim = [], max(1, int(1.2 / hop))
    k = 0
    while k < n:
        if energy[k] and not covered[k]:
            j, gap = k, 0
            while j < n and gap <= gap_lim:
                gap = 0 if (energy[j] and not covered[j]) else gap + 1
                j += 1
            j -= gap
            if (j - k) * hop >= min_len:
                regions.append((round(k * hop, 2), round(j * hop, 2)))
            k = j + 1
        else:
            k += 1
    return regions


def transcribe_region_lines(sid: str, a: float, b: float) -> list[dict]:
    """Transcreve [a,b] do stem de voz com timestamps de palavra e agrupa em
    frases (gap > 0.8s ou ~9 palavras). Usa o modelo de ALINHAMENTO (small) —
    essas linhas entram na letra, precisam de qualidade."""
    vocals = STEMS / sid / "vocals.mp3"
    if not vocals.exists():
        return []
    clip = STEMS / sid / "_region.mp3"
    ffmpeg = FFMPEG_BIN / "ffmpeg.exe"
    start = max(0.0, a - 0.4)
    try:
        subprocess.run([str(ffmpeg) if ffmpeg.exists() else "ffmpeg", "-y",
                        "-ss", str(round(start, 2)), "-t", str(round(b - start + 0.8, 2)),
                        "-i", str(vocals), str(clip)], capture_output=True, check=True)
    except Exception:
        return []
    try:
        model = _get_whisper()
        result = model.transcribe(str(clip), language=detected_language(sid),
                                  word_timestamps=True)
        words = [w for seg in result.segments for w in seg.words]
    except Exception:
        logging.exception("transcrição de região falhou pra %s", sid)
        return []
    finally:
        clip.unlink(missing_ok=True)
    if not words:
        return []
    full_text = " ".join(w.word.strip() for w in words)
    if not transcript_is_reliable(full_text) and len(_norm_words(full_text)) >= 8:
        return []  # lixo/alucinação: melhor sem letra do que letra errada
    lines, cur = [], []
    for w in words:
        if cur and (float(w.start) + start) - (float(cur[-1].end) + start) > 0.8 or len(cur) >= 9:
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    out = []
    for ws in lines:
        text = " ".join(w.word.strip() for w in ws).strip()
        if len(re.sub(r"[^A-Za-zÀ-ú0-9]", "", text)) < 4:
            continue  # fragmento-lixo ("ini")
        out.append({"t": round(float(ws[0].start) + start, 2),
                    "end": round(float(ws[-1].end) + start, 2),
                    "text": text, "auto": True})
    return out


def _canon_or_none(text: str, source_lines: list[str]) -> str | None:
    """Valida uma linha TRANSCRITA contra a letra OFICIAL: se casa bem com uma
    linha da fonte, devolve o texto oficial (canônico); senão None. É o freio
    de alucinação do whisper — caso real: ele ouviu "fazendo foque..." onde a
    letra diz "Fazendo fogueira, sem eira nem beira" (Pisando Descalço)."""
    import difflib

    cand = " ".join(_norm_txt(text).split())
    if not cand:
        return None
    best, best_r = None, 0.0
    for src in source_lines:
        s = " ".join(_norm_txt(src).split())
        if not s:
            continue
        r = difflib.SequenceMatcher(None, cand, s).ratio()
        if r > best_r:
            best, best_r = src, r
    return best if best_r >= 0.55 else None


def extend_lyrics_with_transcript(sid: str) -> int:
    """Preenche os buracos: transcreve as regiões cantadas sem frase e insere as
    linhas (marcadas auto=True) na letra. Retorna quantas linhas entraram."""
    entry = _get_entry(sid)
    lyr = entry.get("lyrics") or {}
    lines = lyr.get("lines")
    if not lines:
        return 0
    added = []
    for a, b in uncovered_sung_regions(sid, lines):
        added.extend(transcribe_region_lines(sid, a, b))
    if not added:
        return 0
    # VALIDAÇÃO ANTI-ALUCINAÇÃO (2026-07-18): linha transcrita só entra se
    # EXISTE na letra oficial — e entra com o texto OFICIAL, não o do ouvido
    # da IA. Sem fonte plain salva, busca no letras.mus.br/lyrics.ovh agora.
    source = lyr.get("plain")
    if not source:
        try:
            source = fetch_plain_fallback(entry.get("artist") or "",
                                          entry.get("title") or "")
            if source:
                lyr = {**lyr, "plain": source}
        except Exception:
            source = None
    if source:
        src_lines = [l.strip() for l in source.splitlines() if l.strip()]
        validated, dropped = [], 0
        for ln in added:
            canon = _canon_or_none(ln["text"], src_lines)
            if canon:
                validated.append({**ln, "text": canon})
            else:
                dropped += 1
        if dropped:
            logging.info("extensão: %d linha(s) alucinada(s) descartada(s) em %s",
                         dropped, sid)
        added = validated
        if not added:
            return 0
    # dedup: não inserir texto idêntico colado numa linha que JÁ existe
    # (região "descoberta" encostada numa frase igual gera eco visual)
    def _dup(ln):
        n = _norm_txt(ln["text"]).strip()
        return any(_norm_txt(ex["text"]).strip() == n
                   and abs(ex["t"] - ln["t"]) < 6.0 for ex in lines)
    added = [ln for ln in added if not _dup(ln)]
    if not added:
        return 0
    merged = sorted(lines + added, key=lambda ln: ln["t"])
    for i in range(len(merged) - 1):  # sem invadir a próxima
        if merged[i]["end"] > merged[i + 1]["t"] - 0.02:
            merged[i]["end"] = round(max(merged[i]["t"] + 0.4, merged[i + 1]["t"] - 0.05), 2)
    new_synced = "\n".join(
        f"[{int(ln['t'] // 60):02d}:{ln['t'] % 60:05.2f}] {ln['text']}" for ln in merged)
    result = {**lyr, "lines": merged, "synced": new_synced,
              # o texto completo vira o trilho do próximo realinhamento — senão o
              # align voltaria pro LRC furado e desfaria a extensão
              "origSynced": new_synced,
              # ‼️ o trilho vira o texto ESTENDIDO (senão o próximo align desfaz
              # a extensão), então uma rodada ruim envenenaria a base pra sempre.
              # pristineSynced guarda a fonte humana original, gravada UMA vez.
              "pristineSynced": lyr.get("pristineSynced") or lyr.get("origSynced")
              or lyr.get("synced"),
              "difficulty": compute_difficulty(new_synced, entry.get("duration") or 0),
              "extended": len(added)}
    _update_entry(sid, lyrics=result)
    return len(added)


def drop_ghost_lines(sid: str, lines: list[dict]) -> tuple[list[dict], int]:
    """A REGRA DE OURO do sync: linha sem canto real embaixo NÃO aparece.
    Remove linhas cuja janela tem energia vocal ~0 — é o que acontece quando o
    texto é de outra versão (banter de show sobre intro de estúdio, In The End)
    ou quando a interpolação espalhou linhas por cima de silêncio (a letra
    'passando devagarinho do nada'). O countdown assume esses intervalos."""
    pitch = load_pitch(sid)
    # ‼️ CICATRIZ (2026-07-19): apagar letra é a ação mais destrutiva do
    # pipeline, então ela usa o sinal CONSERVADOR (energia CRUA = "tem algo
    # aqui?"). Com a energia mascarada a regra comeu letra de verdade — Vamos
    # Fugir foi de 61 pra 36 linhas, Whisky a Go-Go de 46 pra 27. A máscara
    # serve pra POSICIONAR (clamp/extensão/onset), não pra deletar.
    energy = (pitch or {}).get("energy")
    if not energy or len(lines) < 8:
        return lines, 0
    hop = pitch["hop"]
    n = len(energy)
    # ‼️ CICATRIZ (2026-07-19): com a máscara de fala a energia ficou mais
    # esparsa e a regra de ouro passou a derrubar letra demais (Vamos Fugir
    # perdeu 20 de 61 linhas). Derrubar 1/4 da música não é "tirar fantasma" —
    # é sintoma de que a premissa está errada. Teto proporcional:
    max_drop = max(3, int(len(lines) * 0.25))
    keep, dropped = [], 0
    for i, ln in enumerate(lines):
        end = ln.get("end") or (lines[i + 1]["t"] if i + 1 < len(lines) else ln["t"] + 5)
        a, b = max(0, int(ln["t"] / hop)), min(n, int(end / hop))
        seg = energy[a:b] or [0]
        cov = sum(seg) / len(seg)
        if cov < 0.12 and len(lines) - dropped > 6 and dropped < max_drop:
            dropped += 1
            continue
        keep.append(ln)
    return keep, dropped


def anchor_fix_lines(sid: str, lines: list[dict], radius: float = 12.0,
                     min_score: float = 0.75, bad_score: float = 0.6,
                     bad_score_word: float = 0.6, tol: float = 0.8,
                     max_frac: float = 0.25) -> int:
    """ALIGN v2 fase C — ANCHOR-MATCHING POR LINHA (versão conservadora).

    Compara o TEXTO de cada linha com o que foi REALMENTE cantado (transcrição
    com tempo de palavra) e reancora SÓ as linhas que estão no lugar errado.
    Remédio do off-by-one (Epitáfio: a 1ª frase virou fantasma e as outras
    herdaram o texto da vizinha).

    ‼️ CICATRIZ (2026-07-19): a 1ª versão movia a linha sempre que achasse um
    casamento melhor em ±25s, e isso DESTRUIU o controle (I Have a Dream: 36ms
    → 615ms, 29 → 19 linhas) porque em refrão repetido toda linha casa em
    vários lugares e ela escolhia a repetição errada. As 4 travas de agora:
      1. só mexe se o lugar ATUAL discorda do canto (score < bad_score);
      2. entre os candidatos bons, escolhe o MAIS PRÓXIMO, não o de maior nota;
      3. exige nota alta (min_score) e raio curto;
      4. se mais de max_frac das linhas quiserem mudar, a transcrição não casa
         com a estrutura da letra — não mexe em NADA."""
    import difflib

    wt = word_transcript(sid)
    if not wt or len(wt) < 20 or not lines:
        return 0
    words = [(a, b, _norm_txt(w).strip()) for a, b, w in wt]
    words = [(a, b, w) for a, b, w in words if w]
    if len(words) < 20:
        return 0
    txt = [w for _a, _b, w in words]

    # comparação por PALAVRA, não por caractere: "ter chorado mais" × "ter amado
    # mais" dá ~0,7 em caractere (inflado por 'ter'/'mais' em comum) e 0,5 em
    # palavra — foi essa inflação que quase matou a detecção do off-by-one.
    def nota(alvo: list[str], k: int, span: int) -> float:
        return difflib.SequenceMatcher(None, alvo, txt[k:k + span]).ratio()

    def varrer(alvo: list[str], centro: float, raio: float, corte: float):
        """(melhor nota, [candidatos acima do corte]) dentro do raio."""
        n, best, cands = len(alvo), 0.0, []
        for k in range(len(words) - 2):
            if abs(words[k][0] - centro) > raio:
                continue
            for span in (n - 1, n, n + 1):
                if span < 2 or k + span > len(words):
                    continue
                r = nota(alvo, k, span)
                best = max(best, r)
                if r >= corte:
                    cands.append((words[k][0], words[k + span - 1][1], r))
        return best, cands

    def nota_aqui(alvo: list[str], t: float) -> float:
        """O que é cantado COMEÇANDO neste instante bate com a linha? Tem que
        ancorar no início: uma janela solta de ±1,5s vaza pra frase seguinte e
        conclui 'está tudo bem' justo no caso deslocado."""
        n = len(alvo)
        k0 = next((k for k in range(len(words)) if words[k][0] >= t - 0.3), None)
        if k0 is None:
            return 0.0
        best = 0.0
        for k in (k0, k0 + 1):
            for span in (n - 1, n, n + 1):
                if k < len(words) and span >= 2 and k + span <= len(words):
                    best = max(best, nota(alvo, k, span))
        return best

    propostas = []
    for i, ln in enumerate(lines):
        alvo = [w for w in _norm_txt(ln["text"]).split() if w]
        if len(alvo) < 3:
            continue  # âncora fraca: não arrisca
        # (1) o lugar ATUAL já bate com o canto? então não é problema nosso.
        # ‼️ CICATRIZ: limiar ABSOLUTO não serve — "ter chorado mais" sobre o
        # canto "deve ter amado mais" dá 0,67 só pelas palavrinhas em comum e
        # a linha errada passava por certa. O critério é RELATIVO: só move se
        # existir um lugar MUITO melhor que este.
        aqui = nota_aqui(alvo, ln["t"])
        if aqui >= 0.92:
            continue  # praticamente exato: não encosta
        # (2)+(3) candidatos bons na vizinhança: fica com o MAIS PRÓXIMO
        _b, cands = varrer(alvo, ln["t"], radius, min_score)
        if not cands:
            continue
        if max(c[2] for c in cands) - aqui < 0.25:
            continue  # o outro lugar não é convincentemente melhor
        # entre os de nota MÁXIMA (janelas quase iguais empatam), o mais próximo:
        # a nota separa o casamento certo do "quase certo" (janela deslocada uma
        # palavra); a proximidade desempata refrão repetido.
        topo = max(c[2] for c in cands)
        t0, t1, _r = min((c for c in cands if c[2] >= topo - 0.05),
                         key=lambda c: abs(c[0] - ln["t"]))
        if abs(t0 - ln["t"]) > tol:
            propostas.append((i, t0, t1))
    # (4) mexer em muita linha só é aceitável se o conserto for COERENTE:
    # deslocamento no mesmo sentido e destinos em ordem crescente (assinatura
    # do off-by-one). Destinos embaralhados/repetidos = a transcrição não casa
    # com a estrutura da letra — não mexe em NADA.
    if len(propostas) > max(2, len(lines) * max_frac):
        import statistics as _st

        alvos = [t0 for _i, t0, _t1 in propostas]
        desloc = [t0 - lines[i]["t"] for i, t0, _t1 in propostas]
        mediana = _st.median(desloc)
        crescente = all(alvos[k] < alvos[k + 1] - 0.05 for k in range(len(alvos) - 1))
        mesmo_sentido = sum(1 for d in desloc
                            if (d > 0) == (mediana > 0)) >= 0.8 * len(desloc)
        if not (crescente and mesmo_sentido):
            logging.warning("âncoras descartadas em %s: %d de %d linhas mudariam "
                            "sem coerência", sid, len(propostas), len(lines))
            return 0
    # a ordem é checada contra as posições FINAIS das vizinhas: num deslocamento
    # global cada linha cai onde a seguinte está hoje, e comparar com a posição
    # antiga faria todas se bloquearem mutuamente
    destino = {i: t0 for i, t0, _t1 in propostas}
    pos = lambda j: destino.get(j, lines[j]["t"])  # noqa: E731
    fixed = 0
    for i, t0, t1 in propostas:
        piso = pos(i - 1) + 0.15 if i > 0 else 0.0
        teto = pos(i + 1) - 0.15 if i + 1 < len(lines) else 1e9
        if not (piso <= t0 <= teto):
            continue  # sairia da ordem da letra: não força
        ln = lines[i]
        dur = max(ln.get("end", ln["t"] + 2) - ln["t"], 0.5)
        ln["t"] = round(max(0.0, t0), 2)
        ln["end"] = round(min(max(t1, ln["t"] + min(dur, 2.0)), teto + 0.1), 2)
        ln.pop("words", None)  # tempo veio da âncora, não do alinhador
        ln["anchored"] = True
        fixed += 1
    # ‼️ CICATRIZ: sem isto uma linha ancorada pulava por cima da vizinha e a
    # letra ficava fora de ordem no player (Epitáfio: "E até errado mais" em
    # 21,19s aparecendo DEPOIS de uma linha em 22,20s).
    if fixed:
        for i in range(1, len(lines)):
            if lines[i]["t"] <= lines[i - 1]["t"]:
                lines[i]["t"] = round(lines[i - 1]["t"] + 0.05, 2)
            if lines[i]["end"] < lines[i]["t"] + 0.3:
                lines[i]["end"] = round(lines[i]["t"] + 0.8, 2)
            if lines[i - 1]["end"] > lines[i]["t"] - 0.02:
                lines[i - 1]["end"] = round(
                    max(lines[i - 1]["t"] + 0.4, lines[i]["t"] - 0.05), 2)
    return fixed


def reconcile_with_lrc(lines: list[dict], lrc: list[tuple[float, str]]) -> dict:
    """O Whisper é preciso localmente, mas se perde em REFRÕES REPETIDOS (atribui
    a frase à repetição errada e estica a janela). O LRC humano tem offset global,
    porém estrutura relativa confiável — serve de trilho: linha do Whisper que
    fugir do trilho volta pro tempo do LRC deslocado; nenhuma frase invade a próxima."""
    import statistics

    n = min(len(lines), len(lrc))
    offset = statistics.median(lines[i]["t"] - lrc[i][0] for i in range(n))
    # offset absurdo = o próprio trilho está errado (whisper se perdeu em massa,
    # típico de intro instrumental longa — caso Another Brick: -52s puxou tudo
    # pra tempo NEGATIVO). Não aplica: melhor whisper cru + "revisar sync".
    if abs(offset) > 20:
        return {"fixed": 0, "offset": round(offset, 2), "skipped": "offset absurdo"}
    tol = 3.5
    fixed = 0
    for i in range(n):
        expected = lrc[i][0] + offset
        if expected < 0:
            continue  # nunca cria linha antes do início do áudio
        if abs(lines[i]["t"] - expected) > tol:
            nxt = (lrc[i + 1][0] + offset) if i + 1 < n else expected + 5
            lines[i]["t"] = round(expected, 2)
            lines[i]["end"] = round(max(min(expected + 8, nxt - 0.05), expected + 0.6), 2)
            lines[i].pop("words", None)  # tempo veio do trilho, não do canto
            fixed += 1
    for i in range(len(lines) - 1):  # fim nunca passa do início da próxima
        if lines[i]["end"] > lines[i + 1]["t"] - 0.02:
            lines[i]["end"] = round(max(lines[i]["t"] + 0.6, lines[i + 1]["t"] - 0.05), 2)
    return {"fixed": fixed, "offset": round(offset, 2)}


def clamp_ends_to_voice(sid: str, lines: list[dict]) -> int:
    """Corta silêncio/instrumental no FIM de cada frase. O Whisper às vezes
    estica o 'end' de uma linha até a próxima entrada de voz (típico na última
    frase de um verso, antes de um interlúdio) — a linha fica acesa durante o
    instrumental inteiro. Usa a energia vocal (pitch.json) pra terminar a frase
    onde a voz de fato para. Só encurta; nunca mexe no início nem estica."""
    pitch = load_pitch(sid)
    energy = sung_energy(sid, pitch)  # solo instrumental não segura a frase (fase A)
    if not energy:
        return 0
    hop = pitch["hop"]
    n = len(energy)
    gap_limit = max(1, int(2.0 / hop))  # silêncio > 2s = a frase acabou
    trimmed = 0
    for i, ln in enumerate(lines):
        nxt = lines[i + 1]["t"] if i + 1 < len(lines) else ln["end"] + 5
        lo = max(0, int(ln["t"] / hop))
        hi = min(n, int(min(ln["end"], nxt) / hop))
        # primeira voz na janela; se a janela é toda instrumental, não mexe
        k = lo
        while k < hi and not energy[k]:
            k += 1
        if k >= hi:
            continue
        # fim do PRIMEIRO trecho contínuo de canto (tolera respiros curtos);
        # voz que volta depois de um silêncio longo já é outra parte
        last_voiced, silence = k, 0
        k += 1
        while k < hi:
            if energy[k]:
                last_voiced, silence = k, 0
            elif (silence := silence + 1) > gap_limit:
                break
            k += 1
        new_end = round(last_voiced * hop + 0.3, 2)
        if new_end < ln["end"] - 0.5 and new_end > ln["t"] + 0.6:
            ln["end"] = new_end
            trimmed += 1
    return trimmed


def alignment_agreement(sid: str, lines: list[dict]) -> float | None:
    """Quanto o alinhamento CONCORDA com o que foi realmente cantado: média do
    casamento texto-da-linha × palavras transcritas naquele instante. É a régua
    interna que deixa o pipeline escolher entre duas versões do alinhamento em
    vez de confiar cegamente numa delas."""
    import difflib

    wt = word_transcript(sid)
    if not wt or not lines:
        return None
    words = [(a, b, _norm_txt(w).strip()) for a, b, w in wt]
    words = [(a, b, w) for a, b, w in words if w]
    if len(words) < 20:
        return None
    txt = [w for _a, _b, w in words]
    notas = []
    for ln in lines:
        alvo = [w for w in _norm_txt(ln.get("text", "")).split() if w]
        if len(alvo) < 3:
            continue
        n = len(alvo)
        k0 = next((k for k in range(len(words)) if words[k][0] >= ln["t"] - 0.3), None)
        if k0 is None:
            notas.append(0.0)
            continue
        melhor = 0.0
        for k in (k0, k0 + 1):
            for span in (n - 1, n, n + 1):
                if k < len(words) and span >= 2 and k + span <= len(words):
                    melhor = max(melhor, difflib.SequenceMatcher(
                        None, alvo, txt[k:k + span]).ratio())
        notas.append(melhor)
    return round(sum(notas) / len(notas), 4) if notas else None


def agreement_ceiling(sid: str, lines: list[dict]) -> float | None:
    """ALIGN v3 fase 0 — o TETO da concordância nesta música.

    Mesma conta da concordância, mas procurando o melhor casamento em QUALQUER
    posição, não só onde a linha está. Separa as duas fontes de erro que a
    concordância soma:
      teto baixo   -> a TRANSCRIÇÃO não entendeu o canto (melisma, vazamento);
                      nem alinhamento perfeito pontuaria alto. Nada a fazer no
                      alinhador — sobe modelo de ASR/separação.
      teto alto e concordância baixa -> é ALINHAMENTO. Isso a gente conserta.
    """
    import difflib

    wt = word_transcript(sid)
    if not wt or not lines:
        return None
    words = [_norm_txt(w).strip() for _a, _b, w in wt]
    words = [w for w in words if w]
    if len(words) < 20:
        return None
    notas = []
    for ln in lines:
        alvo = [w for w in _norm_txt(ln.get("text", "")).split() if w]
        if len(alvo) < 3:
            continue
        n = len(alvo)
        melhor = 0.0
        for k in range(len(words) - 1):
            for span in (n - 1, n, n + 1):
                if span >= 2 and k + span <= len(words):
                    melhor = max(melhor, difflib.SequenceMatcher(
                        None, alvo, words[k:k + span]).ratio())
            if melhor >= 0.999:
                break
        notas.append(melhor)
    return round(sum(notas) / len(notas), 4) if notas else None


def global_align_lines(sid: str, line_texts: list[str],
                       min_cobertura: float = 0.35) -> list[dict] | None:
    """ALIGN v3 fase 3 — ALINHAMENTO GLOBAL DE SEQUÊNCIAS (âncoras + gaps).

    A formulação que a literatura e os sistemas de produção usam, no lugar das
    heurísticas por linha: alinha a sequência INTEIRA de palavras da letra
    contra a sequência INTEIRA de palavras transcritas do áudio. Os blocos que
    casam viram ÂNCORAS (tempo vindo do canto real); os buracos entre elas são
    interpolados.

    Por que é melhor que tudo que tentamos antes:
      • sem premissa de offset global -> imune a mudança de andamento;
      • refrão repetido casa na ORDEM certa (é alinhamento de sequência, não
        busca do "mais parecido");
      • melisma/palavra mal transcrita vira gap interpolado entre âncoras
        firmes, em vez de arrastar a frase;
      • dá tempo por PALAVRA de graça (realce palavra a palavra).
    """
    import difflib

    wt = word_transcript(sid)
    if not wt:
        return None
    tw = [(a, b, _norm_txt(w).strip()) for a, b, w in wt]
    tw = [(a, b, w) for a, b, w in tw if w]
    if len(tw) < 20:
        return None
    T = [w for _a, _b, w in tw]

    L, dono = [], []          # palavras da letra + de qual linha cada uma é
    for i, texto in enumerate(line_texts):
        for w in _norm_txt(texto).split():
            if w:
                L.append(w)
                dono.append(i)
    if len(L) < 8:
        return None

    # autojunk=False: com letra longa o difflib trataria palavras frequentes
    # ("que", "eu") como lixo e jogaria fora âncoras boas
    blocos = difflib.SequenceMatcher(None, L, T, autojunk=False).get_matching_blocks()
    ini: list[float | None] = [None] * len(L)
    fim: list[float | None] = [None] * len(L)
    ancoradas = 0
    for a, b, tam in blocos:
        for k in range(tam):
            ini[a + k], fim[a + k] = tw[b + k][0], tw[b + k][1]
            ancoradas += 1
    cobertura = ancoradas / len(L)
    if cobertura < min_cobertura:
        logging.info("alinhamento global recusado em %s: só %.0f%% das palavras "
                     "casaram com o canto", sid, 100 * cobertura)
        return None

    # gaps: distribui o tempo entre as âncoras que cercam o buraco
    PASSO = 0.35  # s por palavra quando não há âncora dos dois lados
    k = 0
    while k < len(L):
        if ini[k] is not None:
            k += 1
            continue
        j = k
        while j < len(L) and ini[j] is None:
            j += 1
        antes = fim[k - 1] if k > 0 else None
        depois = ini[j] if j < len(L) else None
        n = j - k
        if antes is not None and depois is not None:
            passo = max((depois - antes) / (n + 1), 0.05)
            for m in range(n):
                ini[k + m] = round(antes + passo * (m + 1), 2)
                fim[k + m] = round(ini[k + m] + passo * 0.9, 2)
        elif antes is not None:                      # cauda sem âncora
            for m in range(n):
                ini[k + m] = round(antes + PASSO * (m + 1), 2)
                fim[k + m] = round(ini[k + m] + PASSO * 0.9, 2)
        elif depois is not None:                     # início sem âncora
            for m in range(n):
                ini[k + m] = round(max(0.0, depois - PASSO * (n - m)), 2)
                fim[k + m] = round(ini[k + m] + PASSO * 0.9, 2)
        else:
            return None                              # nada casou: desiste
        k = j

    lines: list[dict] = []
    for i, texto in enumerate(line_texts):
        idx = [k for k in range(len(L)) if dono[k] == i]
        if not idx:
            lines.append(None)
            continue
        t0, t1 = ini[idx[0]], fim[idx[-1]]
        ln = {"t": round(t0, 2), "end": round(max(t1, t0 + 0.3), 2), "text": texto}
        palavras = [[round(ini[k] - t0, 2), round(fim[k] - t0, 2), L[k]] for k in idx]
        if len(palavras) >= 2:
            ln["words"] = palavras
        lines.append(ln)
    # linha sem palavra utilizável (só pontuação): encaixa entre as vizinhas
    for i, ln in enumerate(lines):
        if ln is not None:
            continue
        ant = next((lines[j] for j in range(i - 1, -1, -1) if lines[j]), None)
        prox = next((lines[j] for j in range(i + 1, len(lines)) if lines[j]), None)
        t = (ant["end"] + 0.1) if ant else ((prox["t"] - 1.0) if prox else 0.0)
        lines[i] = {"t": round(max(0.0, t), 2), "end": round(max(0.0, t) + 1.0, 2),
                    "text": line_texts[i]}
    for i in range(1, len(lines)):
        if lines[i]["t"] <= lines[i - 1]["t"]:
            lines[i]["t"] = round(lines[i - 1]["t"] + 0.05, 2)
        if lines[i]["end"] < lines[i]["t"] + 0.3:
            lines[i]["end"] = round(lines[i]["t"] + 0.8, 2)
    logging.info("alinhamento global em %s: %.0f%% das palavras ancoradas",
                 sid, 100 * cobertura)
    return lines


def reset_to_pristine(sid: str) -> bool:
    """Desfaz extensões acumuladas: volta o trilho pra fonte humana original.
    Sem pristineSynced (letras antigas), rebusca a letra na fonte."""
    entry = _get_entry(sid)
    lyr = entry.get("lyrics") or {}
    pristine = lyr.get("pristineSynced")
    if pristine:
        _update_entry(sid, lyrics={**lyr, "origSynced": pristine, "synced": pristine,
                                   "lines": None, "extended": None})
        return True
    return bool(align_best_candidate(sid))


def _melhor_alinhamento(sid: str, texts: list[str], folga: float = 0.06):
    """Escolhe o motor POR MÚSICA, medindo — nunca por preferência.

    Ordem pensada pra ser barata: o alinhamento GLOBAL não roda modelo nenhum
    (só usa a transcrição que já existe), então tenta primeiro. Se ele já está
    no TETO do que a transcrição permite, não faz sentido pagar o alinhador
    pesado — aceita e pronto (fica mais rápido E melhor). Senão, roda o híbrido
    e fica com o que concordar mais com o canto real.

    Medido em 2026-07-19 nos 7 casos: global venceu em 5 (Epitáfio 0,898→0,989
    com teto 0,996; controle 0,885→0,946) e perdeu em 2 (Whisky a Go-Go,
    Take Me Out) — daí a escolha ser por música, não global."""
    candidatos = []
    gl = global_align_lines(sid, texts)
    if gl:
        nota = alignment_agreement(sid, gl)
        teto = agreement_ceiling(sid, gl)
        if nota is not None and teto is not None and nota >= teto - folga:
            logging.info("global aceito de cara em %s (%.3f de teto %.3f)",
                         sid, nota, teto)
            return gl, "global"
        candidatos.append((nota or 0.0, "global", gl))
    hb = hybrid_align_lines(sid, texts)
    if hb:
        candidatos.append((alignment_agreement(sid, hb) or 0.0, "hibrido", hb))
    if not candidatos:
        return None, None
    nota, nome, lines = max(candidatos, key=lambda c: c[0])
    logging.info("motor escolhido em %s: %s (%.3f)", sid, nome, nota)
    return lines, nome


def base_texts_for(entry: dict):
    """Texto-base do alinhamento: trilho do LRC original (fonte humana) ou o
    texto puro. Compartilhado pelo pipeline e pelos experimentos A/B."""
    lyr = entry.get("lyrics") or {}
    orig_synced = lyr.get("origSynced") or lyr.get("synced")
    if orig_synced:
        base_lines = parse_lrc(orig_synced)
        texts = [t for _t, t in base_lines if len(re.sub(r"[^A-Za-zÀ-ú0-9]", "", t)) >= 4]
    elif lyr.get("plain"):
        base_lines = None
        texts = [ln.strip() for ln in lyr["plain"].splitlines() if ln.strip()]
    else:
        return None
    return (orig_synced, base_lines, texts) if len(texts) >= 4 else None


def align_lyrics_to_vocals(sid: str, engine: str = "auto") -> dict | None:
    """Re-cronometra a letra pela cantoria real. Funciona até com letra sem sync.
    engine: "hibrido" (padrão: whisper + CTC nas linhas que ele não ancorou),
    "whisper" ou "mms"."""
    entry = _get_entry(sid)
    lyr = entry.get("lyrics") or {}
    base = base_texts_for(entry)
    if not base:
        return None
    orig_synced, base_lines, texts = base
    if engine == "auto":
        lines, escolhido = _melhor_alinhamento(sid, texts)
        engine = escolhido or "auto"
    else:
        lines = (mms_align_lines(sid, texts) if engine == "mms"
                 else whisper_align_lines(sid, texts) if engine == "whisper"
                 else global_align_lines(sid, texts) if engine == "global"
                 else hybrid_align_lines(sid, texts))
    if not lines:
        return None
    reconciled = None
    if base_lines and len(base_lines) == len(lines):
        # ‼️ CICATRIZ (2026-07-19, caso Epitáfio): o reconcile foi criado quando
        # o alinhador era fraco e não havia transcrição. Hoje ele às vezes
        # DESTRÓI um alinhamento bom pra encaixar num LRC de outro master — no
        # Epitáfio arrastou tudo 4,4s pra trás (o alinhador tinha acertado com
        # 0,2s) e a 1ª frase caiu como fantasma em cima do silêncio: era esse o
        # "ficou sem o começo". Agora o trilho não manda por decreto: aplica-se
        # o reconcile numa CÓPIA e só se aceita se ele CONCORDAR MAIS com o que
        # foi cantado de verdade.
        antes = alignment_agreement(sid, lines)
        copia = [dict(ln) for ln in lines]
        info = reconcile_with_lrc(copia, base_lines)
        depois = alignment_agreement(sid, copia)
        if antes is None or depois is None or depois >= antes - 0.005:
            lines[:] = copia
            reconciled = info
        else:
            reconciled = {"skipped": "reconcile piorava a concordância",
                          "agreement": [antes, depois]}
            logging.info("reconcile recusado em %s: concordância %.3f -> %.3f",
                         sid, antes, depois)
    # letra de versão mais longa (ao vivo): frases além do fim do áudio não
    # existem nesta gravação — descarta em vez de espremer no finalzinho
    duration = entry.get("duration") or 0
    if duration:
        kept = [ln for ln in lines if ln["t"] < duration - 2]
        if len(kept) >= 4 and len(kept) < len(lines):
            if reconciled is None:
                reconciled = {}
            reconciled["droppedBeyondAudio"] = len(lines) - len(kept)
            lines = kept
    # fase C: linha cujo TEXTO foi cantado em outro lugar volta pro lugar certo
    # (antes do ghost: melhor reancorar do que deixar cair como fantasma)
    anchored = anchor_fix_lines(sid, lines)
    if anchored:
        reconciled = {**(reconciled or {}), "anchored": anchored}
    # corta o instrumental preso no fim das frases (frase segue a cantoria)
    tails = clamp_ends_to_voice(sid, lines)
    if tails:
        reconciled = {**(reconciled or {}), "trimmedTails": tails}
    # REGRA DE OURO: linha sem canto embaixo não aparece (deixa o countdown agir)
    lines, ghosts = drop_ghost_lines(sid, lines)
    if ghosts:
        reconciled = {**(reconciled or {}), "droppedGhost": ghosts}
    # invariante duro: tempo negativo não existe (caso Another Brick, offset -52s)
    neg = [ln for ln in lines if ln["t"] < 0]
    if neg:
        lines = [ln for ln in lines if ln["t"] >= 0]
        reconciled = {**(reconciled or {}), "droppedNegative": len(neg)}
    if len(lines) < 4:
        return None  # alinhamento colapsou — melhor manter a letra anterior
    new_synced = "\n".join(
        f"[{int(ln['t'] // 60):02d}:{ln['t'] % 60:05.2f}] {ln['text']}" for ln in lines)
    # trilho do LRC recusado (offset absurdo) = whisper sozinho numa faixa que
    # já se mostrou traiçoeira — rebaixa a confiança pro card avisar "revisar"
    # concordância com o canto real: vira o selo de qualidade da música. Abaixo
    # de 0,65 o app avisa "⚠ revisar sync" sozinho — o Marcus não precisa mais
    # descobrir cantando (Samurai deu 0,48 e a régua de onsets dizia "98ms ok").
    acordo = alignment_agreement(sid, lines)
    method = engine + ("-suspeito" if (reconciled or {}).get("skipped")
                       or (acordo is not None and acordo < 0.65) else "")
    result = {**lyr, "found": True, "synced": new_synced, "lines": lines,
              "origSynced": orig_synced,
              "difficulty": compute_difficulty(new_synced, entry.get("duration") or 0),
              "alignMethod": method, "reconciled": reconciled, "agreement": acordo}
    _update_entry(sid, lyrics=result, autoOffset=0)
    return result


def extract_pitch(wav_path: Path) -> dict | None:
    """Melodia de referência do stem de voz (pyin) — base da pontuação do jogador."""
    try:
        import librosa
        import numpy as np

        y, sr = librosa.load(str(wav_path), sr=16000, mono=True)
        hop = 512
        f0, voiced, _prob = librosa.pyin(
            y, fmin=65, fmax=1000, sr=sr, frame_length=2048, hop_length=hop)
        midi = []
        for f, v in zip(f0, voiced):
            if v and f and not np.isnan(f):
                midi.append(round(float(69 + 12 * np.log2(f / 440.0)), 2))
            else:
                midi.append(None)
        if not any(m is not None for m in midi):
            return None
        # energia vocal (pega rap FALADO, que o pyin não vê) — base do modo ritmo
        rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]
        thr = float(np.percentile(rms, 95)) * 0.15
        energy = [1 if float(v) > thr else 0 for v in rms[:len(midi)]]
        energy += [0] * (len(midi) - len(energy))
        return {"hop": hop / sr, "midi": midi, "energy": energy}
    except Exception:
        return None


def _run_ffmpeg_mp3(src: Path, dst: Path) -> None:
    ffmpeg = FFMPEG_BIN / "ffmpeg.exe"
    cmd = [str(ffmpeg) if ffmpeg.exists() else "ffmpeg",
           "-y", "-i", str(src), "-b:a", "192k", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou: {proc.stderr[-300:]}")


def process_song(sid: str) -> None:
    """Pipeline completo de uma música (roda no worker). Etapas, com o status
    que cada uma publica pra barra de progresso:
      separating -> separa voz/instrumental (MDX-Net)
      analyzing  -> melodia + energia de referência (pitch.json)
      aligning   -> letra do LRCLIB (melhor versão por correlação) e depois
                    forced alignment pela cantoria; center-cut/onset são fallback
      ready      -> converte stems pra mp3, limpa WAVs, libera a música
    """
    entry = _get_entry(sid)
    src = MEDIA / entry["file"]
    if not src.exists():
        raise RuntimeError("arquivo de áudio não existe mais")
    stem_dir = STEMS / sid
    stem_dir.mkdir(parents=True, exist_ok=True)

    # 1) separação IA (MDX-Net via onnxruntime, CPU)
    _update_entry(sid, status="separating")
    from audio_separator.separator import Separator

    sep = Separator(log_level=logging.WARNING, output_dir=str(stem_dir),
                    model_file_dir=str(MODELS), output_format="WAV")
    sep.load_model(model_filename=SEPARATION_MODEL)
    outputs = sep.separate(str(src))

    vocals_wav = instr_wav = None
    for name in outputs:
        p = Path(name)
        if not p.is_absolute():
            p = stem_dir / p.name
        if "(Vocals)" in p.name:
            vocals_wav = p
        elif "(Instrumental)" in p.name:
            instr_wav = p
    if not vocals_wav or not instr_wav or not vocals_wav.exists() or not instr_wav.exists():
        raise RuntimeError(f"separação não gerou os stems esperados: {outputs}")

    # 2) melodia de referência pra pontuação (tom do cantor original)
    _update_entry(sid, status="analyzing")
    pitch = extract_pitch(vocals_wav)
    if pitch:
        (stem_dir / "pitch.json").write_text(json.dumps(pitch), encoding="utf-8")

    # 3) alinhamento: melhor versão de letra do LRCLIB + offset por correlação
    _update_entry(sid, status="aligning")
    entry = _get_entry(sid)
    if not entry.get("lyrics"):
        try:
            search_and_store_lyrics(sid)
        except Exception:
            pass
    onset = detect_vocal_onset(vocals_wav)
    aligned = None
    try:
        aligned = align_best_candidate(sid, pitch)
    except Exception:
        pass
    if aligned:
        auto_offset = _get_entry(sid).get("autoOffset") or 0.0
    else:
        # fallback sem pitch/candidatos: início de energia da voz vs 1ª linha
        auto_offset = 0.0
        synced = (_get_entry(sid).get("lyrics") or {}).get("synced")
        if synced and onset is not None:
            lrc_lines = parse_lrc(synced)
            if lrc_lines:
                diff = onset - lrc_lines[0][0]
                if abs(diff) <= 30:
                    auto_offset = round(diff, 2)

    # 4) mp3 pra servir + limpeza dos WAV gigantes
    _run_ffmpeg_mp3(vocals_wav, stem_dir / "vocals.mp3")
    _run_ffmpeg_mp3(instr_wav, stem_dir / "instrumental.mp3")
    vocals_wav.unlink(missing_ok=True)
    instr_wav.unlink(missing_ok=True)

    # 4.5) UMA transcrição da música inteira alimenta as duas frentes do ALIGN v2:
    # máscara de fala (gaita/sax vazado no stem não é canto) e palavras com tempo
    # (âncoras por linha). Tem que rodar antes de qualquer regra de energia.
    try:
        word_transcript(sid, build=True)
    except Exception:
        logging.exception("transcrição completa falhou pra %s", sid)

    # 5) forced alignment: re-cronometra cada linha pela CANTORIA (método definitivo;
    # os passos 3 servem de fallback se o Whisper não confiar no resultado)
    try:
        if align_lyrics_to_vocals(sid):
            auto_offset = 0.0
    except Exception:
        logging.exception("forced alignment falhou no pipeline pra %s", sid)

    # 6) auto-cura: canto sem frase (refrão repetido que o LRC não lista) vira
    # letra transcrita + realinha com o texto completo — sem isso o alinhador
    # espreme frases e o final fica sem letra (padrão Péricles/Mulher de Fases)
    try:
        if extend_lyrics_with_transcript(sid):
            align_lyrics_to_vocals(sid)
    except Exception:
        logging.exception("extensão de letra falhou pra %s", sid)

    _update_entry(sid, status="ready", stems=True, autoOffset=auto_offset,
                  vocalOnset=onset, errorMsg=None)


def _worker() -> None:
    while True:
        sid = _jobs.get()
        try:
            process_song(sid)
        except HTTPException:
            pass  # música removida enquanto estava na fila
        except Exception as exc:
            try:
                _update_entry(sid, status="error", errorMsg=str(exc)[:300])
            except HTTPException:
                pass
        finally:
            _jobs.task_done()


def enqueue(sid: str) -> None:
    _update_entry(sid, status="queued", errorMsg=None)
    _jobs.put(sid)


# KARAOKE_NO_WORKER=1 permite importar este módulo em testes sem efeitos colaterais
if os.environ.get("KARAOKE_NO_WORKER") != "1":
    threading.Thread(target=_worker, daemon=True, name="karaoke-pipeline").start()

    # jobs interrompidos por restart voltam pra fila
    with _lock, _cross_process_lock():
        _boot_lib = _load_lib()
        _stuck = [e["id"] for e in _boot_lib.values()
                  if e.get("status") in ("queued", "separating", "analyzing", "aligning")]
        for _sid in _stuck:
            _boot_lib[_sid]["status"] = "queued"
        if _stuck:
            _save_lib(_boot_lib)
    for _sid in _stuck:
        _jobs.put(_sid)


# ---------------------------------------------------------------- rotas API

@app.post("/api/upload")
async def upload_song(file: UploadFile):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in AUDIO_EXTS:
        raise HTTPException(400, f"Formato não suportado: {ext or 'sem extensão'}")
    sid = uuid.uuid4().hex[:12]
    dest = MEDIA / f"{sid}{ext}"
    with open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
    tags = read_tags(dest)
    fallback = Path(file.filename or "").stem
    artist, title = "", tags["title"] or fallback
    if tags["artist"]:
        artist = tags["artist"]
    elif " - " in fallback and not tags["title"]:
        artist, title = parse_video_title(fallback, None)
    entry = {
        "id": sid, "source": "upload", "file": dest.name,
        "title": title, "artist": artist, "album": tags["album"],
        "duration": tags["duration"], "bitrate": tags["bitrate"],
        "hasCover": read_cover(dest) is not None,
        "genre": tags.get("genre") or None,
        "thumb": None, "url": None, "lyrics": None, "addedAt": int(time.time()),
        "status": "none", "stems": False, "autoOffset": 0,
    }
    _add_entry(entry)
    enqueue(sid)
    return _get_entry(sid)


class LinkBody(BaseModel):
    url: str


MAX_PLAYLIST = 50  # teto de segurança pra não importar uma rádio infinita


def _download_one(url: str) -> dict:
    """Baixa UM áudio por link e cria a entrada (sem enfileirar). Retorna a entrada."""
    import yt_dlp

    sid = uuid.uuid4().hex[:12]
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best",
        "outtmpl": str(MEDIA / f"{sid}.%(ext)s"),
        "noplaylist": True,  # um vídeo por vez, mesmo se a URL trouxer &list=
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if info.get("entries"):
        info = info["entries"][0]
    files = list(MEDIA.glob(f"{sid}.*"))
    if not files:
        raise RuntimeError("download terminou sem arquivo de áudio")
    dest = files[0]
    # yt-dlp às vezes lista TODOS os compositores — 2 nomes bastam pra exibir
    raw_artist = info.get("artist") or ", ".join(info.get("artists") or []) or ""
    artist = ", ".join(re.split(r"\s*[,;]\s*", raw_artist)[:2]) or None
    title = info.get("track")
    if not (artist and title):
        p_artist, p_title = parse_video_title(info.get("title", ""), info.get("uploader"))
        artist = artist or p_artist
        title = title or p_title
    entry = {
        "id": sid, "source": "link", "file": dest.name,
        "title": title, "artist": artist, "album": info.get("album") or "",
        "duration": round(info.get("duration") or 0), "bitrate": 0,
        "hasCover": False, "thumb": info.get("thumbnail"),
        "genre": (info.get("genre") or "").strip() or None,
        "url": url, "lyrics": None, "addedAt": int(time.time()),
        "status": "none", "stems": False, "autoOffset": 0,
    }
    _add_entry(entry)
    return entry


def is_playlist_url(url: str) -> bool:
    """Playlist de verdade (/playlist ou list= PL/OL/FL/UU/LL...); list=RD* é
    rádio/mix auto-gerado a partir de UM vídeo — trata como single."""
    if "/playlist" in url or "/sets/" in url:  # youtube playlist / soundcloud set
        return True
    m = re.search(r"[?&]list=([^&]+)", url)
    return bool(m and not m.group(1).startswith("RD"))


def playlist_entry_urls(url: str) -> list[str]:
    """URLs dos vídeos de uma playlist (extração rápida, sem baixar)."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "noplaylist": False, "playlistend": MAX_PLAYLIST}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    out = []
    for e in info.get("entries") or []:
        u = e.get("url") or e.get("id") or ""
        if u and not u.startswith("http"):
            u = f"https://www.youtube.com/watch?v={u}"
        if u:
            out.append(u)
    return out


def _import_playlist(urls: list[str]) -> None:
    """Baixa cada item da playlist em sequência e enfileira o preparo (background)."""
    for u in urls:
        try:
            entry = _download_one(u)
            enqueue(entry["id"])
        except Exception:
            logging.exception("falha ao importar item da playlist: %s", u)


@app.post("/api/link")
def add_from_link(body: LinkBody):
    url = body.url.strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "Link inválido — cole uma URL http(s)")

    if is_playlist_url(url):
        try:
            urls = playlist_entry_urls(url)
        except Exception as exc:
            raise HTTPException(502, f"Falha ao ler a playlist: {exc}") from exc
        if not urls:
            raise HTTPException(502, "Playlist vazia ou inacessível")
        if len(urls) == 1:  # "playlist" de 1 item -> trata como single
            url = urls[0]
        else:
            threading.Thread(target=_import_playlist, args=(urls,),
                             daemon=True, name="playlist-import").start()
            return {"playlist": True, "count": len(urls)}

    try:
        entry = _download_one(url)
    except Exception as exc:
        raise HTTPException(502, f"Falha ao baixar: {exc}") from exc
    enqueue(entry["id"])
    return _get_entry(entry["id"])


# faixa de % por estágio + custo esperado (medido neste i7-7700, CPU)
STAGE_PROGRESS = {
    "queued": (2, 8, 30.0),        # (início%, fim%, segundos esperados fixos)
    "separating": (8, 72, None),   # None = proporcional à duração da música
    "analyzing": (72, 88, None),
    "aligning": (88, 98, None),
}
STAGE_FACTOR = {"separating": 1.8, "analyzing": 0.35, "aligning": 0.6}


def _with_progress(entry: dict) -> dict:
    stage = STAGE_PROGRESS.get(entry.get("status"))
    if not stage:
        return entry
    lo, hi, fixed = stage
    expected = fixed or max(30.0, (entry.get("duration") or 240) * STAGE_FACTOR[entry["status"]])
    elapsed = time.time() - (entry.get("stageAt") or time.time())
    frac = min(0.97, max(0.0, elapsed / expected))
    return {**entry, "progress": round(lo + (hi - lo) * frac)}


@app.get("/api/songs")
def list_songs():
    lib = _load_lib()
    return [_with_progress(e) for e in sorted(lib.values(), key=lambda e: -e.get("addedAt", 0))]


class SongPatch(BaseModel):
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    genre: str | None = None


@app.patch("/api/songs/{sid}")
def patch_song(sid: str, body: SongPatch):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "title" in fields or "artist" in fields:
        fields["lyrics"] = None  # identidade mudou -> re-busca letra
    # editar só gênero/álbum NÃO pode apagar o alinhamento
    return _update_entry(sid, **fields)


@app.delete("/api/songs/{sid}")
def delete_song(sid: str):
    with _lock, _cross_process_lock():
        lib = _load_lib()
        entry = lib.pop(sid, None)
        if not entry:
            raise HTTPException(404, "Música não encontrada")
        _save_lib(lib)
    try:
        (MEDIA / entry["file"]).unlink(missing_ok=True)
        shutil.rmtree(STEMS / sid, ignore_errors=True)
    except OSError:
        pass
    return {"ok": True}


@app.post("/api/process/{sid}")
def trigger_process(sid: str):
    entry = _get_entry(sid)
    if entry.get("status") in ("queued", "separating", "analyzing", "aligning"):
        return entry
    enqueue(sid)
    return _get_entry(sid)


@app.post("/api/realign/{sid}")
def realign(sid: str):
    """Realinha a letra com a cantoria (Whisper) sem refazer a separação."""
    entry = _get_entry(sid)
    if not (entry.get("lyrics") or {}).get("synced"):
        align_best_candidate(sid)  # garante uma letra base na versão certa
    try:
        result = align_lyrics_to_vocals(sid)
        method = "whisper"
    except Exception:
        logging.exception("forced alignment falhou pra %s", sid)
        result = None
        method = None
    if not result:
        result = align_best_candidate(sid)
        method = "correlation"
    if not result:
        raise HTTPException(409, "Sem stems/letra pra alinhar — prepare a música primeiro")
    entry = _get_entry(sid)
    return {"method": method, "autoOffset": entry.get("autoOffset"),
            "alignScore": result.get("alignScore"), "lines": len(result.get("lines") or []),
            "reconciled": result.get("reconciled"), "matched": result.get("matched")}


class LinesBody(BaseModel):
    lines: list[dict]


@app.put("/api/lines/{sid}")
def put_lines(sid: str, body: LinesBody):
    """Editor humano de linhas: recebe a letra editada (t/end/text) e salva.
    É o último recurso definitivo — o que a IA não separa, a pessoa marca."""
    entry = _get_entry(sid)
    # palavras são relativas ao início da linha: sobrevivem à edição de tempo —
    # basta reanexar por texto (some só se o texto da linha mudou)
    old_words: dict[str, list] = {}
    for old in ((entry.get("lyrics") or {}).get("lines") or []):
        if old.get("words") and old.get("text") not in old_words:
            old_words[old["text"]] = old["words"]
    lines = []
    for ln in body.lines:
        text = str(ln.get("text", "")).strip()
        if not text:
            continue
        t = round(float(ln["t"]), 2)
        end = round(float(ln.get("end") or t + 2), 2)
        new = {"t": t, "end": max(end, t + 0.3), "text": text}
        if text in old_words:
            new["words"] = old_words[text]
        lines.append(new)
    if len(lines) < 1:
        raise HTTPException(400, "letra vazia")
    lines.sort(key=lambda l: l["t"])
    new_synced = "\n".join(
        f"[{int(l['t'] // 60):02d}:{l['t'] % 60:05.2f}] {l['text']}" for l in lines)
    lyr = entry.get("lyrics") or {}
    result = {**lyr, "found": True, "lines": lines, "synced": new_synced,
              "difficulty": compute_difficulty(new_synced, entry.get("duration") or 0),
              "alignMethod": "manual"}
    extra = {}
    # 1ª edição manual guarda a versão AUTOMÁTICA — botão "voltar pro automático"
    # (pedido após o Marcus apagar a letra inteira editando Pisando Descalço)
    if lyr.get("alignMethod") != "manual" and lyr.get("lines"):
        extra["lyricsBackup"] = {"lyrics": lyr,
                                 "autoOffset": entry.get("autoOffset") or 0}
    _update_entry(sid, lyrics=result, autoOffset=0, **extra)
    return result


@app.post("/api/lines/{sid}/restore")
def restore_lines(sid: str):
    """Desfaz TODAS as edições manuais: volta pra última versão automática."""
    entry = _get_entry(sid)
    backup = entry.get("lyricsBackup")
    if not backup or not backup.get("lyrics"):
        raise HTTPException(404, "não há versão automática guardada desta letra")
    _update_entry(sid, lyrics=backup["lyrics"],
                  autoOffset=backup.get("autoOffset") or 0)
    return backup


@app.get("/api/pitch/{sid}")
def get_pitch(sid: str):
    _get_entry(sid)
    p = STEMS / sid / "pitch.json"
    if not p.exists():
        raise HTTPException(404, "Sem análise de melodia — prepare a música de novo")
    return FileResponse(p, media_type="application/json")


@app.get("/api/lyrics/{sid}")
def get_lyrics(sid: str, artist: str | None = None, title: str | None = None):
    entry = _get_entry(sid)
    override = bool(artist or title)
    if entry.get("lyrics") and not override:
        return entry["lyrics"]
    return search_and_store_lyrics(sid, artist, title)


@app.get("/api/cover/{sid}")
def get_cover(sid: str):
    entry = _get_entry(sid)
    cover = read_cover(MEDIA / entry["file"])
    if cover:
        data, mime = cover
        return Response(content=data, media_type=mime,
                        headers={"Cache-Control": "max-age=86400"})
    if entry.get("thumb"):
        return RedirectResponse(entry["thumb"])
    raise HTTPException(404, "Sem capa")


def range_response(path: Path, request: Request):
    if not path.exists():
        raise HTTPException(404, "Arquivo de áudio sumiu do disco")
    size = path.stat().st_size
    mime = MIME.get(path.suffix.lower(), "application/octet-stream")
    range_header = request.headers.get("range")
    if range_header:
        m = re.match(r"bytes=(\d*)-(\d*)", range_header)
        start = int(m.group(1)) if m and m.group(1) else 0
        end = int(m.group(2)) if m and m.group(2) else size - 1
        end = min(end, size - 1)
        if start > end:
            raise HTTPException(416, "Range inválido")
        length = end - start + 1

        def stream():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(stream(), status_code=206, media_type=mime, headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        })
    return FileResponse(path, media_type=mime, headers={"Accept-Ranges": "bytes"})


@app.get("/api/audio/{sid}")
def get_audio(sid: str, request: Request):
    entry = _get_entry(sid)
    return range_response(MEDIA / entry["file"], request)


@app.get("/api/stems/{sid}/{which}")
def get_stem(sid: str, which: str, request: Request):
    if which not in ("vocals", "instrumental"):
        raise HTTPException(404, "Stem inválido")
    _get_entry(sid)
    resp = range_response(STEMS / sid / f"{which}.mp3", request)
    resp.headers["Cache-Control"] = "no-store"  # stems podem ser regravados por modelo melhor
    return resp


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    MEDIA.mkdir(parents=True, exist_ok=True)
    STEMS.mkdir(parents=True, exist_ok=True)
    MODELS.mkdir(parents=True, exist_ok=True)
    # KARAOKE_HOST=0.0.0.0 para Docker/servidor doméstico; padrão só local
    uvicorn.run(app, host=os.environ.get("KARAOKE_HOST", "127.0.0.1"),
                port=int(os.environ.get("KARAOKE_PORT", "8777")))
