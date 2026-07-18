"""Fila prioritária: re-alinha JÁ as músicas reportadas pelo Marcus."""
import json, os, sys
os.environ.setdefault("KARAOKE_NO_WORKER", "1")
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "server"))
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
import main

ALVOS = ["take me out", "toxicity", "lonely day", "mulher de fases", "chop suey"]
lib = json.loads((BASE/"data"/"library.json").read_text(encoding="utf-8"))
for alvo in ALVOS:
    sid = next((s for s, e in lib.items()
                if alvo in (e.get("title", "") or "").lower() and e.get("stems")), None)
    if not sid:
        print(f"— {alvo}: não achado"); continue
    print(f"### {lib[sid]['artist']} - {lib[sid]['title']}", flush=True)
    try:
        main.search_and_store_lyrics(sid)
        main.align_best_candidate(sid)
        r = main.align_lyrics_to_vocals(sid)
        n = main.extend_lyrics_with_transcript(sid)
        if n: main.align_lyrics_to_vocals(sid)
        print(f"   ✅ {'whisper' if r else 'correlação'}; +{n} linhas transcritas", flush=True)
    except Exception as exc:
        print(f"   erro: {exc}", flush=True)
print("FIM", flush=True)
