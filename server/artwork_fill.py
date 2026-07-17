"""Repõe as capas perdidas na corrupção: busca a arte do álbum no iTunes
(600x600) e grava em thumb. Escrita direta com trava cross-process (segura)."""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

os.environ.setdefault("KARAOKE_NO_WORKER", "1")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "server"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import main  # noqa: E402


def artwork(artist: str, title: str) -> str | None:
    term = urllib.parse.quote(f"{artist} {title}".strip())
    try:
        with urllib.request.urlopen(
                f"https://itunes.apple.com/search?term={term}&entity=song&limit=1",
                timeout=15) as r:
            res = (json.loads(r.read().decode()).get("results") or [])
        art = res[0].get("artworkUrl100") if res else None
        return art.replace("100x100", "600x600") if art else None
    except Exception:
        return None


lib = json.loads((BASE / "data" / "library.json").read_text(encoding="utf-8"))
todo = [(s, e) for s, e in lib.items() if not e.get("thumb") and not e.get("hasCover")]
print(f"sem capa: {len(todo)}", flush=True)
ok = 0
for sid, e in todo:
    url = artwork(e.get("artist") or "", e.get("title") or "")
    if url:
        main._update_entry(sid, thumb=url)
        ok += 1
        print(f"  🖼 {e.get('artist','')[:22]} - {e.get('title','')[:32]}", flush=True)
    time.sleep(3.2)
print(f"FIM: {ok}/{len(todo)} capas repostas", flush=True)
