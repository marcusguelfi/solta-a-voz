"""Pós-corrupção: repõe letra+alinhamento das músicas prontas (lyrics=None)."""
import json, os, sys, time
os.environ.setdefault("KARAOKE_NO_WORKER", "1")
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "server"))
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
import main

lib = json.loads((BASE/"data"/"library.json").read_text(encoding="utf-8"))
todo = [s for s,e in lib.items() if e.get("stems") and not e.get("lyrics")]
print(f"repondo letra de {len(todo)} músicas…", flush=True)
for i, sid in enumerate(todo, 1):
    e = json.loads((BASE/"data"/"library.json").read_text(encoding="utf-8"))[sid]
    nome = f"{e.get('artist')} - {e.get('title')}"
    print(f"[{i}/{len(todo)}] {nome}", flush=True)
    try:
        main.search_and_store_lyrics(sid)
        main.align_best_candidate(sid)
        r = main.align_lyrics_to_vocals(sid)
        print(f"   {'✅ whisper' if r else '≈ correlação'} ", flush=True)
    except Exception as exc:
        print(f"   erro: {exc}", flush=True)
print("FIM", flush=True)
