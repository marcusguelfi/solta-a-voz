"""A/B do ALIGN v4 (a): casador SEM custo (difflib) × LOCAL com custo (Smith-Waterman).

Mede o mesmo motor global com KARAOKE_ALIGN_SW=0 e =1, no MESMO texto-base. Não
escreve nada na biblioteca.

Três números por música:
  onset_ms  — régua INDEPENDENTE (energia do áudio). É ela que decide.
  acordo    — concordância nas linhas ancoráveis (v4 c).
  ancoras   — quantas palavras da letra casaram de verdade com o canto.

Uso:  .venv\\Scripts\\python.exe server\\ab_sw.py [id-ou-nome ...]
Grava data/ab_sw.json.
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

OUT = BASE / "data" / "ab_sw.json"


def contar_ancoras(sid: str, texts: list[str]) -> dict:
    """Compara os dois casadores no mesmo par (letra, transcrição)."""
    import difflib

    wt = main.word_transcript(sid)
    if not wt:
        return {}
    tw = [main._norm_txt(w).strip() for _a, _b, w in wt]
    T = [w for w in tw if w]
    L = [w for t in texts for w in main._norm_txt(t).split() if w]
    if len(T) < 20 or len(L) < 8:
        return {}
    bl = difflib.SequenceMatcher(None, L, T, autojunk=False).get_matching_blocks()
    velho = sum(tam for _a, _b, tam in bl)
    novo = len(main.local_align_words(L, T))
    return {"palavras_letra": len(L), "palavras_canto": len(T),
            "ancoras_difflib": velho, "ancoras_sw": novo}


def erro_pareado(sid: str, a: list[dict], b: list[dict]) -> tuple:
    """‼️ Compara os dois motores NAS MESMAS linhas — as que a energia consegue
    julgar nas DUAS versões. Medir cada lado com `onset_error_median` solto dá
    veredito falso: cada chamada reseleciona as linhas verificáveis, e no Take
    Me Out isso fez o SW (que estava 3x melhor) parecer 5x pior."""
    import statistics

    ia, onsets = main.linhas_verificaveis(sid, a)
    ib, _ = main.linhas_verificaveis(sid, b)
    comum = sorted(set(ia) & set(ib))
    if len(comum) < 3 or not onsets:
        return None, None, len(comum)

    def med(ln):
        return round(statistics.median(
            [min(abs(ln[i]["t"] - o) for o in onsets) for i in comum]), 3)

    return med(a), med(b), len(comum)


def alinhar(sid: str, texts: list[str], sw: bool) -> list[dict] | None:
    os.environ["KARAOKE_ALIGN_SW"] = "1" if sw else "0"
    return main.global_align_lines(sid, texts)


def avaliar(sid: str, lines: list[dict] | None) -> dict:
    if not lines:
        return {"erro": "sem alinhamento"}
    onset = main.onset_error_median(sid, lines)
    qual = main.alignment_quality(sid, lines) or {}
    idx, _ons = main.linhas_verificaveis(sid, lines)
    return {"onset_ms": round(onset * 1000) if onset is not None else None,
            "verificaveis": f"{len(idx)}/{len(lines)}",   # régua cega = veredito fraco
            "acordo": qual.get("acordo"), "cobertura": qual.get("cobertura")}


def rodar(alvos: list[str]) -> None:
    lib = main._load_lib()
    resultado, placar = {}, {"SW": 0, "difflib": 0, "empate": 0}
    for alvo in alvos:
        sid = alvo if alvo in lib else resolve(None, alvo, lib)
        if not sid:
            print(f"?? não achei: {alvo}")
            continue
        entry = main._get_entry(sid)
        titulo = f"{entry.get('artist')} - {entry.get('title')}"
        base = main.base_texts_for(entry)
        if not base:
            print(f"-- {titulo}: sem texto-base")
            continue
        texts = base[2]
        linha = {"titulo": titulo, **contar_ancoras(sid, texts)}
        lv, ls = alinhar(sid, texts, sw=False), alinhar(sid, texts, sw=True)
        linha["difflib"], linha["sw"] = avaliar(sid, lv), avaliar(sid, ls)
        a, b, n = erro_pareado(sid, lv, ls) if lv and ls else (None, None, 0)
        linha["pareado"] = {"difflib_ms": round(a * 1000) if a is not None else None,
                            "sw_ms": round(b * 1000) if b is not None else None,
                            "linhas_comuns": n}
        if a is not None and b is not None:
            linha["veredito"] = ("SW" if b < a * 0.9 else
                                 "difflib" if a < b * 0.9 else "empate")
        else:
            linha["veredito"] = "?"
        placar[linha["veredito"]] = placar.get(linha["veredito"], 0) + 1
        resultado[sid] = linha
        print(f"{titulo[:46]:48}")
        print(f"   âncoras: difflib={linha.get('ancoras_difflib')} "
              f"sw={linha.get('ancoras_sw')} de {linha.get('palavras_letra')} palavras")
        print(f"   difflib: {json.dumps(linha['difflib'], ensure_ascii=False)}")
        print(f"   sw     : {json.dumps(linha['sw'], ensure_ascii=False)}")
        print(f"   PAREADO ({n} linhas comuns): difflib={linha['pareado']['difflib_ms']}ms "
              f"sw={linha['pareado']['sw_ms']}ms  -> {linha['veredito']}")
    OUT.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nplacar (régua independente): {placar}  (gravado em data/ab_sw.json)")


if __name__ == "__main__":
    lib = main._load_lib()
    alvos = sys.argv[1:] or [resolve(s, n, lib) for s, n, _c in CASES]
    rodar([a for a in alvos if a])
