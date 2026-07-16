"""Pente fino do alinhamento letra-cantoria.

Uso:  .venv\\Scripts\\python.exe server\\audit.py [id-da-musica] [--web]
Sem id, audita a biblioteca inteira. Com --web, cruza a letra com uma fonte
externa independente (lyrics.ovh) e aponta frases que faltam/sobram.

Pra cada frase alinhada mede:
- canto%   — frames com voz AFINADA (pyin) dentro da janela (bom pra melodia)
- energia% — frames com ENERGIA vocal dentro da janela (pega rap falado)

Flags (cada um é um gotcha que já nos mordeu neste projeto). O sinal de saúde é
ENERGIA, não duração — nota longa e cheia de voz é legítima:
- GHOST      frase sem energia nenhuma — não existe nessa gravação (letra de
             outra versão) → some com align_best_candidate/reconcile.
- FROUXO     janela > 2,5s com pouca energia (instrumental preso na frase) — é o
             "letra atrasada no fim" → some com clamp_ends_to_voice. PROBLEMA.
- LONGA      janela > 12s mas cheia de voz — nota sustentada legítima. Só info.
- CURTA      palavras rápidas demais pra caber (cram/mistimed). PROBLEMA.
- FORA       frase começa além do fim do áudio (versão ao vivo mais longa)
             → some com droppedBeyondAudio.
- OVERLAP    o fim invade o início da próxima frase. Cosmético.
- DESCOBERTO canto real fora de qualquer frase da letra (adlib/refrão repetido
             que o LRC não lista) — aparece "sem marcação" no gráfico de tom.
"""
import difflib
import json
import re
import statistics
import sys
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

# console do Windows (cp850/cp1252) engasga em ×, é, etc. — força UTF-8 na saída
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
STEMS = BASE / "data" / "stems"

MAX_WINDOW = 12.0      # janela > isso e cheia de voz = LONGA (nota sustentada)
ENERGY_MIN = 0.15      # fração da janela com energia pra não ser fantasma (GHOST)
LOOSE_WINDOW = 2.5     # acima disso, exige energia decente (senão FROUXO)
LOOSE_ENERGY = 0.45    # energia mínima aceitável numa janela longa


def energy_envelope(vocals_path: Path):
    import librosa
    import numpy as np

    y, sr = librosa.load(str(vocals_path), sr=16000, mono=True)
    hop = 512
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]
    thr = float(np.percentile(rms, 95)) * 0.15
    return (rms > thr).astype(float), hop / sr


def phrase_onsets(active, e_hop: float, min_silence: float = 0.4) -> list[float]:
    """Instantes (s) onde a voz COMEÇA depois de >= min_silence de silêncio —
    ou seja, começos de frase. Ground-truth pra medir se o início da linha bate."""
    gap = max(1, int(min_silence / e_hop))
    onsets, silence = [], gap + 1  # começa "em silêncio" pra pegar o 1º onset
    for k in range(len(active)):
        if active[k]:
            if silence >= gap:
                onsets.append(round(k * e_hop, 3))
            silence = 0
        else:
            silence += 1
    return onsets


def timing_errors(lines: list, onsets: list[float], max_match: float = 3.0) -> list[float]:
    """Erro (s) do início da linha ao onset de frase mais próximo — SÓ pra linhas
    que iniciam frase (silêncio antes) e têm onset plausível perto (<max_match).

    Por que o filtro: em canto contínuo/rap há poucos silêncios detectáveis, então
    linhas de fluxo casariam com onsets distantes = erro-fantasma de dezenas de
    segundos. Medimos só o subconjunto VERIFICÁVEL (começos de frase), que é onde
    'a letra tem que bater com o canto'. Retorna [] se nada é verificável."""
    if not onsets:
        return []
    errs = []
    for i, ln in enumerate(lines):
        prev_end = lines[i - 1].get("end") or lines[i - 1]["t"] if i else -9
        if i and (ln["t"] - prev_end) <= 0.8:  # fluxo contínuo: não dá pra verificar
            continue
        d = min(abs(ln["t"] - o) for o in onsets)
        if d <= max_match:
            errs.append(d)
    return errs


def _pctl(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * p))]


def _norm_lines(text: str) -> list[str]:
    out = []
    for ln in (text or "").splitlines():
        t = unicodedata.normalize("NFD", ln.lower())
        t = "".join(c for c in t if unicodedata.category(c) != "Mn")
        t = re.sub(r"[^a-z0-9 ]", "", t).strip()
        if t:
            out.append(t)
    return out


def cross_check_web(entry: dict) -> None:
    """Compara nossa letra com uma fonte independente (lyrics.ovh)."""
    artist = re.split(r"\s*[,;/&]\s*", entry.get("artist") or "")[0]
    title = re.sub(r"[(\[][^)\]]*[)\]]", "", entry.get("title") or "").strip()
    url = (f"https://api.lyrics.ovh/v1/"
           f"{urllib.parse.quote(artist)}/{urllib.parse.quote(title)}")
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            web_text = json.loads(r.read().decode("utf-8")).get("lyrics") or ""
    except Exception:
        print("  [web] fonte externa não tem essa música (ou está fora do ar)")
        return
    ours = _norm_lines("\n".join(
        ln["text"] for ln in (entry.get("lyrics") or {}).get("lines") or []))
    web = _norm_lines(web_text)
    if not ours or not web:
        print("  [web] sem texto suficiente pra comparar")
        return
    ratio = difflib.SequenceMatcher(None, " ".join(ours), " ".join(web)).ratio()
    # fontes quebram linhas diferente: compara também com pares de linhas juntas
    targets = ours + [f"{ours[i]} {ours[i + 1]}" for i in range(len(ours) - 1)]
    missing = [w for w in dict.fromkeys(web)
               if not any(w in o or difflib.SequenceMatcher(None, w, o).ratio() > 0.8
                          for o in targets)]
    print(f"  [web] similaridade com fonte externa: {ratio*100:.0f}%"
          f" | frases de lá que faltam aqui: {len(missing)}")
    for m in missing[:5]:
        print(f"        falta? «{m}»")


def audit_song(sid: str, entry: dict) -> dict | None:
    lines = (entry.get("lyrics") or {}).get("lines")
    duration = entry.get("duration") or 0
    title = f"{entry.get('artist')} - {entry.get('title')}"
    if not lines:
        print(f"\n== {title}: sem alinhamento frase a frase (método "
              f"{(entry.get('lyrics') or {}).get('alignMethod') or 'offset global'}) — pulando")
        return None

    pitch_file = STEMS / sid / "pitch.json"
    vocals = STEMS / sid / "vocals.mp3"
    if not pitch_file.exists() or not vocals.exists():
        print(f"\n== {title}: sem stems/pitch — pulando")
        return None
    pitch = json.loads(pitch_file.read_text(encoding="utf-8"))
    p_hop, midi = pitch["hop"], pitch["midi"]
    active, e_hop = energy_envelope(vocals)

    def frac(arr, hop, a, b):
        i0, i1 = max(0, int(a / hop)), min(len(arr), int(b / hop))
        if i1 <= i0:
            return 0.0
        seg = arr[i0:i1]
        if isinstance(seg, list):
            return sum(1 for v in seg if v is not None) / len(seg)
        return float(seg.mean())

    # o que o pipeline fez com essa letra (rastro dos gotchas resolvidos)
    lyr = entry.get("lyrics") or {}
    rec = lyr.get("reconciled") or {}
    bits = [f"método={lyr.get('alignMethod') or 'offset'}"]
    if lyr.get("alignScore") is not None:
        bits.append(f"cobertura={lyr['alignScore']}")
    if rec.get("fixed"):
        bits.append(f"reconcile={rec['fixed']}@{rec.get('offset')}s")
    if rec.get("trimmedTails"):
        bits.append(f"tails cortadas={rec['trimmedTails']}")
    if rec.get("droppedBeyondAudio"):
        bits.append(f"frases fantasma removidas={rec['droppedBeyondAudio']}")
    onset = entry.get("vocalOnset")
    if onset is not None and lines:
        bits.append(f"1ª frase {lines[0]['t']:.1f}s vs onset {onset:.1f}s")

    print(f"\n== {title}  ({len(lines)} frases, áudio {duration}s)")
    print(f"  pipeline: {', '.join(bits)}")
    flags_count = {"GHOST": 0, "FROUXO": 0, "LONGA": 0, "CURTA": 0,
                   "FORA": 0, "OVERLAP": 0, "DESCOBERTO": 0}
    healthy_flags = {"LONGA", "OVERLAP"}  # informativos, não contam contra a saúde
    windows, unhealthy = [], [False] * len(lines)
    for i, ln in enumerate(lines):
        t, end = ln["t"], ln.get("end") or (lines[i + 1]["t"] if i + 1 < len(lines) else ln["t"] + 5)
        win = end - t
        words = len(ln["text"].split())
        sung = frac(midi, p_hop, t, end)
        energ = frac(active, e_hop, t, end)
        windows.append(win)
        flags = []
        if duration and t >= duration - 1:
            flags.append("FORA")
        elif energ < ENERGY_MIN:
            flags.append("GHOST")
        elif win > LOOSE_WINDOW and energ < LOOSE_ENERGY:
            flags.append("FROUXO")            # instrumental preso (curta ou longa)
        elif win > MAX_WINDOW:
            flags.append("LONGA")             # longa mas cheia de voz — legítima
        # cram: mais rápido que ~9 palavras/s é fisicamente implausível
        if words >= 2 and win / words < 0.11:
            flags.append("CURTA")
        if i + 1 < len(lines) and end > lines[i + 1]["t"] + 0.05:
            flags.append("OVERLAP")
        if any(f not in healthy_flags for f in flags):
            unhealthy[i] = True
        for f in flags:
            flags_count[f] += 1
        if flags:
            print(f"  [{' '.join(flags):>18}] {t:7.2f}->{end:7.2f} "
                  f"canto {sung*100:3.0f}% energia {energ*100:3.0f}%  {ln['text'][:44]}")
    # canto DESCOBERTO: energia vocal fora de qualquer janela de frase — é o
    # que aparece "sem marcação" no gráfico (adlib, vocalize, letra incompleta)
    covered = [False] * len(active)
    for i, ln in enumerate(lines):
        t = ln["t"]
        end = ln.get("end") or (lines[i + 1]["t"] if i + 1 < len(lines) else t + 5)
        for k in range(max(0, int((t - 0.4) / e_hop)),
                       min(len(active), int((end + 0.4) / e_hop))):
            covered[k] = True
    gap_frames = max(1, int(0.5 / e_hop))
    k, n = 0, len(active)
    while k < n:
        if active[k] and not covered[k]:
            j, gap = k, 0
            while j < n and gap <= gap_frames:
                gap = 0 if (active[j] and not covered[j]) else gap + 1
                j += 1
            j -= gap
            if (j - k) * e_hop >= 2.0:
                flags_count["DESCOBERTO"] += 1
                print(f"  [  DESCOBERTO] {k * e_hop:7.1f}->{j * e_hop:7.1f}  "
                      f"canto sem frase na letra ({(j - k) * e_hop:.1f}s)")
            k = j + 1
        else:
            k += 1

    # PRECISÃO DE TIMING: início da linha vs onset real da frase (energia)
    terrs = timing_errors(lines, phrase_onsets(active, e_hop))
    tinfo = ""
    if len(terrs) >= 3:
        med, p90 = statistics.median(terrs), _pctl(terrs, 0.9)
        mistimed = sum(1 for e in terrs if e > 0.6)
        tinfo = (f" | timing (início de {len(terrs)} frases): mediana {med * 1000:.0f}ms, "
                 f"p90 {p90 * 1000:.0f}ms{f', {mistimed} > 600ms' if mistimed else ''}")

    ok = len(lines) - sum(unhealthy)
    print(f"  -> {ok}/{len(lines)} frases saudáveis | "
          f"janela mediana {statistics.median(windows):.1f}s "
          f"| flags: {', '.join(f'{k}={v}' for k, v in flags_count.items() if v) or 'nenhuma'}{tinfo}")
    return {"sid": sid, "ok": ok, "total": len(lines), "flags": flags_count,
            "timing_med": statistics.median(terrs) if terrs else None}


SUSPECT_SIM = 0.35  # similaridade letra×canto abaixo disso = letra provavelmente errada


def verify_lyrics(sid: str, entry: dict) -> float | None:
    """Transcreve o canto real (Whisper) e mede se a letra bate — pega letra de
    OUTRA música que o forced alignment encaixaria mesmo assim."""
    import os
    os.environ["KARAOKE_NO_WORKER"] = "1"
    sys.path.insert(0, str(BASE / "server"))
    import main  # carrega Whisper só quando --verify é usado

    lyr = entry.get("lyrics") or {}
    text = "\n".join(l["text"] for l in lyr.get("lines") or []) or lyr.get("plain") or ""
    if not text.strip():
        print("  [verify] sem letra pra verificar")
        return None
    transcript = main.transcribe_vocals(sid)
    if not transcript:
        print("  [verify] sem transcrição (stems ausentes?)")
        return None
    sim = main.lyric_similarity(text, transcript)
    tag = "✅ letra confere" if sim >= SUSPECT_SIM else "⚠️  LETRA SUSPEITA — provável música errada"
    print(f"  [verify] similaridade letra×canto: {sim:.2f}  {tag}")
    return sim


def main():
    lib = json.loads((BASE / "data" / "library.json").read_text(encoding="utf-8"))
    flags = {"--web", "--verify"}
    args = [a for a in sys.argv[1:] if a not in flags]
    with_web = "--web" in sys.argv
    with_verify = "--verify" in sys.argv
    targets = args if args else list(lib.keys())
    results, suspects = [], []
    for sid in targets:
        if sid not in lib:
            print(f"id {sid} não existe na biblioteca")
            continue
        r = audit_song(sid, lib[sid])
        if r and with_web:
            cross_check_web(lib[sid])
        if r and with_verify:
            sim = verify_lyrics(sid, lib[sid])
            if sim is not None and sim < SUSPECT_SIM:
                suspects.append((sid, sim))
        if r:
            results.append(r)
    if len(results) > 1:
        print("\n===== RESUMO =====")
        for r in results:
            e = lib[r["sid"]]
            pct = round(100 * r["ok"] / r["total"])
            print(f"  {pct:3d}% saudável  {e.get('artist')} - {e.get('title')}")
    if with_verify and suspects:
        print("\n===== LETRAS SUSPEITAS (re-buscar/re-alinhar) =====")
        for sid, sim in sorted(suspects, key=lambda x: x[1]):
            e = lib[sid]
            print(f"  sim {sim:.2f}  {sid}  {e.get('artist')} - {e.get('title')}")


if __name__ == "__main__":
    main()
