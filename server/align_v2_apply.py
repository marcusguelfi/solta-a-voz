"""Aplica o regime ALIGN v2 completo nos casos de referência (ou nos que você
passar): transcrição completa (máscara de fala + palavras) → realinhamento com
âncoras por linha → extensão validada. Mede antes e depois com a régua de fala.

Uso:  .venv\\Scripts\\python.exe server\\align_v2_apply.py [--engine whisper|mms] [id|nome ...]
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
from measure_align import CASES, measure, resolve  # noqa: E402

LOG = BASE / "data" / "align_v2_log.txt"
FRESH = False


def log(m: str) -> None:
    print(m, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(m + "\n")


def aplicar(sid: str, engine: str) -> None:
    entry = main._get_entry(sid)
    titulo = f"{entry.get('artist')} - {entry.get('title')}"
    if (entry.get("lyrics") or {}).get("alignMethod") == "manual":
        log(f"-- {titulo}: editada à mão, preservada")
        return
    antes = measure(sid)
    t0 = time.time()
    # 0) trilho limpo: extensões antigas viram origSynced e envenenam o align
    if FRESH:
        main.reset_to_pristine(sid)
    # 1) transcrição completa: máscara de fala (A) + palavras pras âncoras (C)
    if not (main.STEMS / sid / "words.json").exists():
        main.full_transcribe(sid)
    main._speech_cache.clear()
    # 2) realinhamento com o regime novo
    res = main.align_lyrics_to_vocals(sid, engine=engine)
    if not res:
        log(f"‼️ {titulo}: align falhou — letra anterior mantida")
        return
    # 3) extensão validada contra a fonte canônica
    try:
        n = main.extend_lyrics_with_transcript(sid)
        if n:
            main.align_lyrics_to_vocals(sid, engine=engine)
    except Exception as ex:
        log(f"   extensão falhou: {str(ex)[:60]}")
        n = 0
    depois = measure(sid)
    rec = (main._get_entry(sid).get("lyrics") or {}).get("reconciled") or {}
    log(f"== {titulo}  ({time.time() - t0:.0f}s, motor {engine})")
    log(f"   antes : {json.dumps({k: antes.get(k) for k in ('mediana_fala_ms', 'verificaveis_fala', 'linhas')}, ensure_ascii=False)}")
    log(f"   depois: {json.dumps({k: depois.get(k) for k in ('mediana_fala_ms', 'verificaveis_fala', 'linhas')}, ensure_ascii=False)}")
    log(f"   ancoradas={rec.get('anchored', 0)} fantasmas={rec.get('droppedGhost', 0)} "
        f"tails={rec.get('trimmedTails', 0)} extensao=+{n}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    engine = "whisper"
    FRESH = "--fresh" in argv
    if FRESH:
        argv.remove("--fresh")
    if "--engine" in argv:
        i = argv.index("--engine")
        engine = argv[i + 1]
        del argv[i:i + 2]
    lib = main._load_lib()
    alvos = [a if a in lib else resolve(None, a, lib) for a in argv] or \
            [resolve(s, n, lib) for s, n, _c in CASES]
    for sid in [a for a in alvos if a]:
        try:
            aplicar(sid, engine)
        except Exception as ex:
            log(f"‼️ {sid}: {type(ex).__name__}: {str(ex)[:120]}")
    log("FIM align_v2_apply")
