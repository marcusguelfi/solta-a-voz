"""Re-conserta a letra de músicas suspeitas: re-busca no LRCLIB e escolhe o
candidato que melhor bate com a TRANSCRIÇÃO do canto (não só timing), depois
re-alinha pela cantoria. Reusa a transcrição cacheada (rápido).

Uso:  .venv\\Scripts\\python.exe server\\refix.py <id> [<id> ...]
"""
import json
import os
import sys
from pathlib import Path

os.environ["KARAOKE_NO_WORKER"] = "1"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "server"))
import main  # noqa: E402


def refix(sid: str) -> None:
    lib = json.loads((BASE / "data" / "library.json").read_text(encoding="utf-8"))
    e = lib.get(sid)
    if not e:
        print(f"{sid}: não existe")
        return
    title = f"{e.get('artist')} - {e.get('title')}"
    before = (e.get("lyrics") or {}).get("lyricMatch")
    print(f"\n== {title}")
    print(f"  antes: lyricMatch={before}  matched={(e.get('lyrics') or {}).get('matched')}")

    # invalida o cache da letra pra forçar re-busca de candidatos frescos
    e["lyrics"] = None
    (BASE / "data" / "library.json").write_text(
        json.dumps(lib, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        main.search_and_store_lyrics(sid)          # semente
        res = main.align_best_candidate(sid)       # escolhe por similaridade
    except Exception as exc:
        print(f"  ERRO na re-busca: {exc}")
        return
    if not res:
        print("  sem candidatos utilizáveis")
        return
    sim = res.get("lyricMatch")
    print(f"  depois: lyricMatch={sim}  matched={res.get('matched')}")
    if sim is not None and sim < 0.35:
        print("  ⚠️  ainda suspeita — LRCLIB pode não ter a letra certa (fix manual no app)")
        return
    try:
        main.align_lyrics_to_vocals(sid)           # re-alinha pela cantoria
        print("  ✅ re-alinhada")
    except Exception as exc:
        print(f"  alinhamento falhou: {exc}")


if __name__ == "__main__":
    for sid in sys.argv[1:]:
        refix(sid)
