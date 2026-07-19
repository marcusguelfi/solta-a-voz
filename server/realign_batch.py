"""Realinha uma lista de músicas com o regime COMPLETO de validação atual:
re-escolhe a letra (LRCLIB × letras.mus.br, castigo ao vivo, takeover), realinha
pela cantoria e estende com validação anti-alucinação. Uso:

  .venv\\Scripts\\python.exe server\\realign_batch.py <trecho-do-nome> [...]

Cada argumento casa (case-insensitive) com "artista título". Escreve no
data/realign_log.txt além do stdout. Seguro com o servidor rodando (travas)."""
import os
import sys
from pathlib import Path

os.environ.setdefault("KARAOKE_NO_WORKER", "1")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "server"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import main  # noqa: E402

LOG = BASE / "data" / "realign_log.txt"


def log(msg: str) -> None:
    print(msg, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def run(queries: list[str]) -> None:
    lib = main._load_lib()
    alvos = []
    for q in queries:
        ql = q.lower()
        hit = next(((sid, e) for sid, e in lib.items()
                    if ql in f"{e.get('artist', '')} {e.get('title', '')}".lower()), None)
        if hit:
            alvos.append(hit)
        else:
            log(f"?? não achei: {q}")
    for sid, e in alvos:
        nome = f"{e.get('artist')} - {e.get('title')}"
        log(f"== {nome}")
        try:
            sel = main.align_best_candidate(sid)
            if sel:
                log(f"   letra: source={sel.get('source')} srcMatch={sel.get('srcMatch')} "
                    f"lyricMatch={sel.get('lyricMatch')} matched={sel.get('matched')}")
            res = main.align_lyrics_to_vocals(sid)
            if not res:
                log("   align falhou — letra anterior mantida")
                continue
            n = 0
            try:
                n = main.extend_lyrics_with_transcript(sid)
                if n:
                    main.align_lyrics_to_vocals(sid)
            except Exception as ex:
                log(f"   extensão falhou: {str(ex)[:60]}")
            log(f"   ✅ {len(res['lines'])} linhas, método {res.get('alignMethod')}"
                + (f", +{n} da extensão" if n else ""))
        except Exception as ex:
            log(f"   ‼️ falhou: {type(ex).__name__}: {str(ex)[:100]}")
    log("FIM realign_batch")


if __name__ == "__main__":
    run(sys.argv[1:] or [])
