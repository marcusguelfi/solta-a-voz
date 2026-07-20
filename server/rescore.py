"""Recalcula e GRAVA as três réguas em toda a biblioteca (sem realinhar nada).

Por que existe: `coverage` e `perceptual` só eram escritos quando o alinhamento
rodava, então as 111 músicas alinhadas pelo motor antigo ficavam sem nota. Isso
aqui só MEDE e grava — não roda modelo, não mexe em tempo de linha nenhum.
É seguro rodar quantas vezes quiser.

O que grava em `lyrics`:
  coverage    — {cobertura, orfao_s, sobra}  (letra faltando/amontoada)
  perceptual  — {nota, perdidas, onde, ...}  (o ouvido; `onde` alimenta o editor)
  alignMethod — recebe/perde o sufixo `-suspeito` conforme as três réguas

Uso:  .venv\\Scripts\\python.exe server\\rescore.py [--dry]
"""
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


def selo(acordo, cob, percept) -> bool:
    """Mesma regra do pipeline — cada régua cobre o ponto cego das outras."""
    return bool((acordo is not None and acordo < 0.65)
                or ((cob or {}).get("cobertura") is not None and cob["cobertura"] < 0.7)
                or ((percept or {}).get("nota") is not None and percept["nota"] < 0.55)
                or ((percept or {}).get("perdidas") or 0) >= 2)


def rodar(dry: bool) -> None:
    lib = main._load_lib()
    mudou = marcadas = perdidas_total = 0
    sem_medida = []
    for sid in list(lib):
        entry = main._get_entry(sid)
        lyr = entry.get("lyrics") or {}
        lines = lyr.get("lines")
        if not lines:
            continue
        try:
            cob = main.display_coverage(sid, lines)
            percept = main.perceptual_score(sid, lines)
        except Exception as ex:
            print(f"!! {sid}: {type(ex).__name__}: {str(ex)[:60]}")
            continue
        if cob is None and percept is None:
            sem_medida.append(sid)
            continue
        acordo = (lyr.get("quality") or {}).get("acordo")
        if acordo is None:
            acordo = lyr.get("agreement")
        ruim = selo(acordo, cob, percept)
        base = (lyr.get("alignMethod") or "").replace("-suspeito", "") or "desconhecido"
        method = base + ("-suspeito" if ruim else "")
        perdidas_total += (percept or {}).get("perdidas") or 0
        marcadas += 1 if ruim else 0
        novo = {**lyr, "coverage": cob, "perceptual": percept, "alignMethod": method}
        if novo != lyr:
            mudou += 1
            if not dry:
                main._update_entry(sid, lyrics=novo)
    print(f"{'(dry) ' if dry else ''}atualizadas: {mudou}   marcadas '-suspeito': {marcadas}")
    print(f"linhas impossíveis de cantar na biblioteca: {perdidas_total}")
    if sem_medida:
        print(f"sem régua nenhuma (pitch/energia ausente): {len(sem_medida)}")


if __name__ == "__main__":
    rodar("--dry" in sys.argv)
