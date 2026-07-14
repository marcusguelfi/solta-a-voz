"""Pente fino do alinhamento letra-cantoria.

Uso:  .venv\\Scripts\\python.exe server\\audit.py [id-da-musica] [--web]
Sem id, audita a biblioteca inteira. Com --web, cruza a letra com uma fonte
externa independente (lyrics.ovh) e aponta frases que faltam/sobram.

Pra cada frase alinhada mede:
- canto%   — frames com voz AFINADA (pyin) dentro da janela (bom pra melodia)
- energia% — frames com ENERGIA vocal dentro da janela (pega rap falado)
e aponta: GHOST (frase sem energia = não existe nessa gravação), STRETCH
(janela esticada demais), FORA (além do fim do áudio), OVERLAP (invade a próxima).
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

BASE = Path(__file__).resolve().parent.parent
STEMS = BASE / "data" / "stems"

MAX_WINDOW = 12.0      # frase cantável raramente passa disso
ENERGY_MIN = 0.15      # fração da janela com energia pra não ser fantasma


def energy_envelope(vocals_path: Path):
    import librosa
    import numpy as np

    y, sr = librosa.load(str(vocals_path), sr=16000, mono=True)
    hop = 512
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]
    thr = float(np.percentile(rms, 95)) * 0.15
    return (rms > thr).astype(float), hop / sr


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

    print(f"\n== {title}  ({len(lines)} frases, áudio {duration}s)")
    flags_count = {"GHOST": 0, "STRETCH": 0, "FORA": 0, "OVERLAP": 0, "DESCOBERTO": 0}
    windows, energies = [], []
    for i, ln in enumerate(lines):
        t, end = ln["t"], ln.get("end") or (lines[i + 1]["t"] if i + 1 < len(lines) else ln["t"] + 5)
        win = end - t
        sung = frac(midi, p_hop, t, end)
        energ = frac(active, e_hop, t, end)
        windows.append(win)
        energies.append(energ)
        flags = []
        if duration and t >= duration - 1:
            flags.append("FORA")
        elif energ < ENERGY_MIN:
            flags.append("GHOST")
        if win > MAX_WINDOW:
            flags.append("STRETCH")
        if i + 1 < len(lines) and end > lines[i + 1]["t"] + 0.05:
            flags.append("OVERLAP")
        for f in flags:
            flags_count[f] += 1
        if flags:
            print(f"  [{' '.join(flags):>12}] {t:7.2f}->{end:7.2f} "
                  f"canto {sung*100:3.0f}% energia {energ*100:3.0f}%  {ln['text'][:46]}")
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

    ok = len(lines) - sum(1 for i in range(len(lines))
                          if energies[i] < ENERGY_MIN or windows[i] > MAX_WINDOW)
    print(f"  -> {ok}/{len(lines)} frases saudáveis | energia mediana "
          f"{statistics.median(energies)*100:.0f}% | janela mediana {statistics.median(windows):.1f}s "
          f"| flags: {', '.join(f'{k}={v}' for k, v in flags_count.items() if v) or 'nenhuma'}")
    return {"sid": sid, "ok": ok, "total": len(lines), "flags": flags_count}


def main():
    lib = json.loads((BASE / "data" / "library.json").read_text(encoding="utf-8"))
    args = [a for a in sys.argv[1:] if a != "--web"]
    with_web = "--web" in sys.argv
    targets = args if args else list(lib.keys())
    results = []
    for sid in targets:
        if sid not in lib:
            print(f"id {sid} não existe na biblioteca")
            continue
        r = audit_song(sid, lib[sid])
        if r and with_web:
            cross_check_web(lib[sid])
        if r:
            results.append(r)
    if len(results) > 1:
        print("\n===== RESUMO =====")
        for r in results:
            e = lib[r["sid"]]
            pct = round(100 * r["ok"] / r["total"])
            print(f"  {pct:3d}% saudável  {e.get('artist')} - {e.get('title')}")


if __name__ == "__main__":
    main()
