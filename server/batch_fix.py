"""Consertador em lote: audita a biblioteca, ranqueia as músicas com problema
de sync (piores primeiro) e realinha cada uma com o pipeline atual (regra de
ouro + clamp + extensão). Pula as saudáveis; é retomável (re-rodar continua).

Uso:  .venv\\Scripts\\python.exe server\\batch_fix.py [max_musicas]
Log:  data/batch_fix_log.txt
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("KARAOKE_NO_WORKER", "1")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "server"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import main  # noqa: E402

LOG = BASE / "data" / "batch_fix_log.txt"


def log(msg: str) -> None:
    print(msg, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def health_problems(sid: str, entry: dict) -> int:
    """Conta problemas de sync (fantasmas + frases espremidas + janelas frouxas).
    0 = saudável, não mexe. Espelha os flags-problema do audit."""
    lines = (entry.get("lyrics") or {}).get("lines")
    pitch = main.load_pitch(sid)
    energy = (pitch or {}).get("energy")
    if not lines or not energy:
        return 0
    hop, n = pitch["hop"], len(energy)
    problems = 0
    for i, ln in enumerate(lines):
        end = ln.get("end") or (lines[i + 1]["t"] if i + 1 < len(lines) else ln["t"] + 5)
        a, b = max(0, int(ln["t"] / hop)), min(n, int(end / hop))
        seg = energy[a:b] or [0]
        cov = sum(seg) / len(seg)
        win = end - ln["t"]
        words = len(ln["text"].split())
        if cov < 0.12:
            problems += 1                       # fantasma
        elif win > 2.5 and cov < 0.45:
            problems += 1                       # frouxa (instrumental preso)
        if words >= 2 and win / words < 0.11:
            problems += 1                       # espremida
    return problems


def run(limit: int) -> None:
    lib = json.loads((BASE / "data" / "library.json").read_text(encoding="utf-8"))
    ranked = []
    for sid, e in lib.items():
        if e.get("status") == "ready" and e.get("stems") and (e.get("lyrics") or {}).get("lines"):
            p = health_problems(sid, e)
            if p > 0:
                ranked.append((p, sid, f"{e.get('artist')} - {e.get('title')}"))
    ranked.sort(reverse=True)
    log(f"=== batch_fix: {len(ranked)} músicas com problemas de sync "
        f"(consertando até {limit}) ===")
    for p, sid, nome in ranked[:limit]:
        log(f"[{p:2d} problemas] {nome}")
        try:
            r = main.align_lyrics_to_vocals(sid)
            if not r:
                log("   align não aplicou (sem letra/stems?)")
                continue
            n = main.extend_lyrics_with_transcript(sid)
            if n:
                log(f"   extensão: +{n} linhas, realinhando…")
                main.align_lyrics_to_vocals(sid)
            lib2 = json.loads((BASE / "data" / "library.json").read_text(encoding="utf-8"))
            rec = (lib2[sid].get("lyrics") or {}).get("reconciled") or {}
            depois = health_problems(sid, lib2[sid])
            log(f"   ✅ {p} -> {depois} problemas | {rec}")
        except Exception as exc:
            log(f"   ERRO: {exc}")
    log("=== batch_fix: fim ===")


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 999)
