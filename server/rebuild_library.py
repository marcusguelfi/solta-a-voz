"""Reconstrói library.json após a corrupção de 2026-07-17.

Fontes: genre_fill_log.txt (164 nomes+gêneros na ordem do índice = addedAt desc),
mtime dos arquivos em data/media (mesma ordem), tags mutagen (quando existem),
scan_log.txt (nomes completos pra ~47), stems/ no disco (status ready).
Valida com âncoras id↔nome conhecidas da sessão antes de gravar.
"""
import json
import os
import re
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
import main  # noqa: E402  (usa o _save_lib atômico + trava)

ANCHORS = {  # id -> trecho do título (confirmados nesta sessão)
    "5156ca99b4a8": "Joao e maria", "df3d443fa0c3": "Evidências",
    "a9f26d0b36c1": "Mulher de Fases", "7f9337d24ce1": "Até Que Durou",
    "db81fd044651": "Raplord", "eed882061943": "Creep",
    "a998ebf186c9": "Enjoy the Silence", "d78dcf6a1cab": "Somebody",
    "cdfd4474a1e2": "Chop Suey", "a62995198cad": "In The End",
    "5b80cb69d0f9": "A Thousand Miles", "2f84382debf3": "Waidmanns",
}


def parse_genre_log() -> list[dict]:
    out = []
    for line in (BASE / "data" / "genre_fill_log.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("  ✅ "):
            genre = line[5:27].strip()
            rest = line[28:]
        elif line.startswith("  —"):
            genre = None
            rest = line[26:]
        else:
            continue
        artist, _, title = rest.partition(" - ")
        out.append({"artist": artist.strip(), "title": title.strip(), "genre": genre})
    return out


def full_names() -> list[str]:
    """Nomes completos do scan_log (UTF-16 do Tee-Object) + logs de fix."""
    names = []
    for fname, enc in [("scan_log.txt", "utf-16"), ("batch_fix_log.txt", "utf-8")]:
        p = BASE / "data" / fname
        if not p.exists():
            continue
        try:
            txt = p.read_text(encoding=enc, errors="ignore")
        except Exception:
            continue
        names += re.findall(r"==\s+(.+?)\s+\(\d+ frases", txt)
        names += re.findall(r"problemas\]\s+(.+)$", txt, re.M)
    return list(dict.fromkeys(names))


def main_run(write: bool) -> None:
    order = parse_genre_log()
    media = sorted((BASE / "data" / "media").iterdir(),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    print(f"log de gêneros: {len(order)} | arquivos de mídia: {len(media)}")
    n = min(len(order), len(media))
    completos = full_names()

    lib = {}
    hits = 0
    for i in range(n):
        f = media[i]
        sid = f.stem
        meta = order[i]
        artist, title, genre = meta["artist"], meta["title"], meta["genre"]
        # nome completo (o log trunca em 24/34 chars) se acharmos correspondência
        for full in completos:
            fa, _, ft = full.partition(" - ")
            if ft.strip().startswith(title[:20]) and fa.strip().startswith(artist[:15]):
                artist, title = fa.strip(), ft.strip()
                break
        tags = main.read_tags(f)
        if tags["artist"] and tags["title"]:
            artist, title = tags["artist"], tags["title"]
        if tags.get("genre"):
            genre = tags["genre"]
        stems = (BASE / "data" / "stems" / sid / "instrumental.mp3").exists()
        if sid in ANCHORS:
            ok = ANCHORS[sid].lower()[:12] in title.lower()
            hits += ok
            print(f"  âncora {sid}: esperado~'{ANCHORS[sid]}' obtido='{title[:40]}' "
                  f"{'✅' if ok else '❌ DESALINHADO'}")
        lib[sid] = {
            "id": sid, "source": "link", "file": f.name,
            "title": title or f.stem, "artist": artist, "album": tags.get("album") or "",
            "genre": genre, "duration": tags["duration"], "bitrate": tags["bitrate"],
            "hasCover": False, "thumb": None, "url": None,
            "lyrics": None, "addedAt": int(f.stat().st_mtime),
            "status": "ready" if stems else "none", "stems": stems, "autoOffset": 0,
            "rebuilt": True,  # marca da reconstrução pós-corrupção
        }
    anchors_found = sum(1 for a in ANCHORS if a in lib)
    print(f"\nâncoras: {hits}/{anchors_found} alinhadas | "
          f"prontas(stems): {sum(1 for e in lib.values() if e['stems'])} | total: {len(lib)}")
    if write:
        if hits < anchors_found * 0.7:
            print("‼️  alinhamento fraco — NÃO gravando. Rode sem --write e inspecione.")
            return
        with main._lock, main._cross_process_lock():
            main._save_lib(lib)
        print("✅ library.json reconstruída (atômica, com .bak a partir de agora)")


if __name__ == "__main__":
    main_run("--write" in sys.argv)
