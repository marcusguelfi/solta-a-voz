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
import json
import logging
import os
import queue
import re
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

def _load_lib() -> dict:
    if LIB_FILE.exists():
        try:
            return json.loads(LIB_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_lib(lib: dict) -> None:
    LIB_FILE.write_text(json.dumps(lib, ensure_ascii=False, indent=2), encoding="utf-8")


def _update_entry(sid: str, **fields) -> dict:
    if "status" in fields:  # base da estimativa de progresso do preparo
        fields.setdefault("stageAt", time.time())
    with _lock:
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
    with _lock:
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
    meta = {"title": "", "artist": "", "album": "", "duration": 0, "bitrate": 0}
    try:
        f = MutagenFile(path, easy=True)
        if f is None:
            return meta
        if f.tags:
            meta["title"] = (f.tags.get("title") or [""])[0]
            meta["artist"] = (f.tags.get("artist") or [""])[0]
            meta["album"] = (f.tags.get("album") or [""])[0]
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
        return (0 if r.get("syncedLyrics") else 1, dur_diff)

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
    best = None
    for cand in candidates:
        got = correlation_align(pitch, cand["syncedLyrics"])
        if not got:
            continue
        offset, coverage = got
        # letra de OUTRA versão da música (ao vivo estendida etc.) perde pontos
        cand_dur = cand.get("duration") or 0
        score = coverage - (0.15 if cand_dur and duration and abs(cand_dur - duration) > 25 else 0.0)
        if best is None or score > best[3]:
            best = (cand, offset, coverage, score)
    if not best:
        return None
    cand, offset, coverage, _score = best
    result = {
        "found": True,
        "synced": cand["syncedLyrics"],
        "plain": cand.get("plainLyrics"),
        "difficulty": compute_difficulty(cand["syncedLyrics"], entry.get("duration") or 0),
        "matched": {"artist": cand.get("artistName"), "title": cand.get("trackName")},
        "alignScore": coverage,
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


def guess_language(text: str) -> str:
    t = f" {re.sub(r'[^a-zà-úç ]', ' ', text.lower())} "
    pt = sum(t.count(w) for w in (" que ", " não ", " nao ", " você ", " voce ",
                                  " meu ", " minha ", " pra ", " mais ", " eu ", " são "))
    en = sum(t.count(w) for w in (" the ", " you ", " and ", " love ", " that ",
                                  " this ", " it ", " of ", " my "))
    return "pt" if pt >= en else "en"


def _regroup_words_to_lines(result, line_texts: list[str]) -> list[tuple[float, float]] | None:
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
        out.append((float(chunk[0].start), float(chunk[-1].end)))
    return out


def _interpolate_bad_lines(lines: list[dict], good: list[int]) -> None:
    """Linhas que o Whisper não cravou são distribuídas entre as âncoras boas
    (rap denso: melhor estimar do que jogar fora o alinhamento inteiro)."""
    for i, ln in enumerate(lines):
        if ln.pop("_ok"):
            continue
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
    lang = guess_language(text)

    # rap/fluxo denso às vezes falha com a supressão de silêncio — tenta sem ela
    for attempt in ({}, {"suppress_silence": False}):
        try:
            result = model.align(str(vocals), text, language=lang,
                                 original_split=True, **attempt)
        except Exception:
            continue
        segs = list(result.segments)
        if len(segs) == len(line_texts):
            spans = [(float(s.start), float(s.end)) for s in segs]
        else:
            spans = _regroup_words_to_lines(result, line_texts)
            if spans is None:
                continue
        lines, good = [], []
        for i, ((start, end), txt) in enumerate(zip(spans, line_texts)):
            valid = end > start > 0
            if valid:
                good.append(i)
            lines.append({"t": round(start, 2), "end": round(end, 2),
                          "text": txt, "_ok": valid})
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


def reconcile_with_lrc(lines: list[dict], lrc: list[tuple[float, str]]) -> dict:
    """O Whisper é preciso localmente, mas se perde em REFRÕES REPETIDOS (atribui
    a frase à repetição errada e estica a janela). O LRC humano tem offset global,
    porém estrutura relativa confiável — serve de trilho: linha do Whisper que
    fugir do trilho volta pro tempo do LRC deslocado; nenhuma frase invade a próxima."""
    import statistics

    n = min(len(lines), len(lrc))
    offset = statistics.median(lines[i]["t"] - lrc[i][0] for i in range(n))
    tol = 3.5
    fixed = 0
    for i in range(n):
        expected = lrc[i][0] + offset
        if abs(lines[i]["t"] - expected) > tol:
            nxt = (lrc[i + 1][0] + offset) if i + 1 < n else expected + 5
            lines[i]["t"] = round(expected, 2)
            lines[i]["end"] = round(max(min(expected + 8, nxt - 0.05), expected + 0.6), 2)
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
    energy = (pitch or {}).get("energy")
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


def align_lyrics_to_vocals(sid: str) -> dict | None:
    """Re-cronometra a letra pela cantoria real. Funciona até com letra sem sync."""
    entry = _get_entry(sid)
    lyr = entry.get("lyrics") or {}
    # preserva o LRC original (fonte humana) — alinhamentos futuros validam contra ele
    orig_synced = lyr.get("origSynced") or lyr.get("synced")
    if orig_synced:
        base_lines = parse_lrc(orig_synced)
        texts = [t for _t, t in base_lines]
    elif lyr.get("plain"):
        base_lines = None
        texts = [ln.strip() for ln in lyr["plain"].splitlines() if ln.strip()]
    else:
        return None
    if len(texts) < 4:
        return None
    lines = whisper_align_lines(sid, texts)
    if not lines:
        return None
    reconciled = None
    if base_lines and len(base_lines) == len(lines):
        reconciled = reconcile_with_lrc(lines, base_lines)
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
    # corta o instrumental preso no fim das frases (frase segue a cantoria)
    tails = clamp_ends_to_voice(sid, lines)
    if tails:
        reconciled = {**(reconciled or {}), "trimmedTails": tails}
    new_synced = "\n".join(
        f"[{int(ln['t'] // 60):02d}:{ln['t'] % 60:05.2f}] {ln['text']}" for ln in lines)
    result = {**lyr, "found": True, "synced": new_synced, "lines": lines,
              "origSynced": orig_synced,
              "difficulty": compute_difficulty(new_synced, entry.get("duration") or 0),
              "alignMethod": "whisper", "reconciled": reconciled}
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

    # 5) forced alignment: re-cronometra cada linha pela CANTORIA (método definitivo;
    # os passos 3 servem de fallback se o Whisper não confiar no resultado)
    try:
        if align_lyrics_to_vocals(sid):
            auto_offset = 0.0
    except Exception:
        logging.exception("forced alignment falhou no pipeline pra %s", sid)

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
    with _lock:
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
        "thumb": None, "url": None, "lyrics": None, "addedAt": int(time.time()),
        "status": "none", "stems": False, "autoOffset": 0,
    }
    _add_entry(entry)
    enqueue(sid)
    return _get_entry(sid)


class LinkBody(BaseModel):
    url: str


@app.post("/api/link")
def add_from_link(body: LinkBody):
    import yt_dlp

    url = body.url.strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "Link inválido — cole uma URL http(s)")
    sid = uuid.uuid4().hex[:12]
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best",
        "outtmpl": str(MEDIA / f"{sid}.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        raise HTTPException(502, f"Falha ao baixar: {exc}") from exc
    if info.get("entries"):
        info = info["entries"][0]
    files = list(MEDIA.glob(f"{sid}.*"))
    if not files:
        raise HTTPException(502, "Download terminou sem arquivo de áudio")
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
        "url": url, "lyrics": None, "addedAt": int(time.time()),
        "status": "none", "stems": False, "autoOffset": 0,
    }
    _add_entry(entry)
    enqueue(sid)
    return _get_entry(sid)


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


@app.patch("/api/songs/{sid}")
def patch_song(sid: str, body: SongPatch):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    fields["lyrics"] = None  # metadata mudou -> invalida cache da letra
    return _update_entry(sid, **fields)


@app.delete("/api/songs/{sid}")
def delete_song(sid: str):
    with _lock:
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
    uvicorn.run(app, host="127.0.0.1", port=8777)
