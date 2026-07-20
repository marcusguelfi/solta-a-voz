"""A/B do ALIGN v4 (b): reparte UNIFORME × reparte NO CANTO (skip do instrumental).

‼️ MÉTODO: o skip usa a ENERGIA pra posicionar a linha, então medir o resultado
com `onset_error_median` (que também vem da energia) seria circular — melhoraria
por construção, sem provar nada. Quem valida aqui é a CONCORDÂNCIA (texto ×
transcrição), que não passa pela energia. As duas fontes são independentes entre
si, e é isso que dá valor ao teste.

`em_silencio` entra como sanidade, não como prova: por construção o skip tende a
melhorá-la.

Uso:  .venv\\Scripts\\python.exe server\\ab_skip.py [id-ou-nome ...]
Grava data/ab_skip.json.
"""
import json
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
from measure_align import CASES, resolve  # noqa: E402

OUT = BASE / "data" / "ab_skip.json"


def em_silencio(sid: str, lines: list[dict], folga: float = 0.5) -> float | None:
    """Fração das linhas que COMEÇAM onde não há canto nenhum por perto."""
    pitch = main.load_pitch(sid)
    energy = main.sung_energy(sid, pitch)
    if not energy or not lines:
        return None
    hop = pitch["hop"]
    fora = 0
    for ln in lines:
        a = max(0, int((ln["t"] - folga) / hop))
        b = min(len(energy), int((ln["t"] + folga) / hop))
        if not any(energy[a:b]):
            fora += 1
    return round(fora / len(lines), 3)


def avaliar(sid: str, texts: list[str], skip: bool) -> dict:
    os.environ["KARAOKE_ALIGN_SKIP"] = "1" if skip else "0"
    lines = main.global_align_lines(sid, texts)
    if not lines:
        return {"erro": "sem alinhamento"}
    qual = main.alignment_quality(sid, lines) or {}
    onset = main.onset_error_median(sid, lines)
    return {"acordo": qual.get("acordo"),          # <- a régua que decide aqui
            "em_silencio": em_silencio(sid, lines),
            "onset_ms": round(onset * 1000) if onset is not None else None}


def rodar(alvos: list[str]) -> None:
    lib = main._load_lib()
    resultado, placar = {}, {}
    for alvo in alvos:
        sid = alvo if alvo in lib else resolve(None, alvo, lib)
        if not sid:
            continue
        entry = main._get_entry(sid)
        titulo = f"{entry.get('artist')} - {entry.get('title')}"
        base = main.base_texts_for(entry)
        if not base:
            print(f"-- {titulo}: sem texto-base")
            continue
        texts = base[2]
        linha = {"titulo": titulo,
                 "uniforme": avaliar(sid, texts, skip=False),
                 "skip": avaliar(sid, texts, skip=True)}
        a = linha["uniforme"].get("acordo")
        b = linha["skip"].get("acordo")
        if a is not None and b is not None:
            linha["veredito"] = ("skip" if b > a + 0.01 else
                                 "uniforme" if a > b + 0.01 else "empate")
        else:
            linha["veredito"] = "?"
        placar[linha["veredito"]] = placar.get(linha["veredito"], 0) + 1
        resultado[sid] = linha
        print(f"{titulo[:46]:48}")
        print(f"   uniforme: {json.dumps(linha['uniforme'], ensure_ascii=False)}")
        print(f"   skip    : {json.dumps(linha['skip'], ensure_ascii=False)}")
        print(f"   -> {linha['veredito']}  (decidido pela CONCORDÂNCIA, não pela energia)")
    OUT.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nplacar: {placar}  (gravado em data/ab_skip.json)")


if __name__ == "__main__":
    lib = main._load_lib()
    alvos = sys.argv[1:] or [resolve(s, n, lib) for s, n, _c in CASES]
    rodar([a for a in alvos if a])
