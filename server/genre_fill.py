"""Preenche o gênero das músicas que ficaram sem, via iTunes Search API
(gratuita, sem chave). Grava via PATCH no servidor (localhost:8777) pra
serializar com as outras escritas. Respeita o rate-limit (~20 req/min).

Uso:  .venv\\Scripts\\python.exe server\\genre_fill.py
"""
import json
import sys
import time
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API = "http://127.0.0.1:8777"


def get(url: str):
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def patch_genre(sid: str, genre: str) -> None:
    body = json.dumps({"genre": genre}).encode("utf-8")
    req = urllib.request.Request(f"{API}/api/songs/{sid}", data=body, method="PATCH",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15).read()


def itunes_genre(artist: str, title: str) -> str | None:
    term = urllib.parse.quote(f"{artist} {title}".strip())
    try:
        data = get(f"https://itunes.apple.com/search?term={term}&entity=song&limit=1")
        results = data.get("results") or []
        return (results[0].get("primaryGenreName") or "").strip() or None
    except Exception:
        return None


def main() -> None:
    songs = get(f"{API}/api/songs")
    pend = [s for s in songs if not s.get("genre")]
    print(f"sem gênero: {len(pend)} de {len(songs)}", flush=True)
    ok = 0
    for s in pend:
        g = itunes_genre(s.get("artist") or "", s.get("title") or "")
        if g:
            patch_genre(s["id"], g)
            ok += 1
            print(f"  ✅ {g:22s} {s.get('artist','')[:24]} - {s.get('title','')[:34]}", flush=True)
        else:
            print(f"  —  (não achou)          {s.get('artist','')[:24]} - {s.get('title','')[:34]}", flush=True)
        time.sleep(3.2)  # rate-limit do iTunes
    print(f"FIM: {ok}/{len(pend)} gêneros preenchidos", flush=True)


if __name__ == "__main__":
    main()
