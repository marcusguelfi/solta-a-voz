"""‼️ A ÚNICA régua que não é circular: nosso alinhamento × LRC marcado por HUMANO.

As duas réguas que a gente usava têm ponto cego por construção:
  • `alignment_agreement` compara com a NOSSA transcrição — auto-referente pro
    motor global, e cega em trecho onde o ASR não transcreveu nada;
  • `onset_error_median` vem da ENERGIA — vira circular na hora de julgar
    qualquer coisa que USE energia pra posicionar (o skip da v4 b, por exemplo).

O `pristineSynced` guardado na biblioteca é o LRC do LRCLIB, com tempos postos
por PESSOAS. Não passa por nada nosso. É o padrão AAE do MIREX (SOTA <0,2s).

Ressalvas honestas: LRC marca quando a linha APARECE, costuma vir um tiquinho
antes do canto; e tem LRC desleixado. Por isso: mediana (não média), só linhas
de texto ÚNICO nos dois lados (refrão repetido é ambíguo), e sempre reportando
quantas linhas casaram.

Uso:  .venv\\Scripts\\python.exe server\\measure_truth.py [id-ou-nome ...]
Grava data/truth_metrics.json.
"""
import json
import os
import re
import statistics
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

OUT = BASE / "data" / "truth_metrics.json"
TS = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)")


def parse_lrc(txt: str) -> list[tuple[float, str]]:
    out = []
    for linha in (txt or "").splitlines():
        m = TS.match(linha.strip())
        if not m:
            continue
        t = int(m.group(1)) * 60 + float(m.group(2))
        texto = main._norm_txt(m.group(3)).strip()
        if texto:
            out.append((t, texto))
    return out


def verdade(sid: str, lines: list[dict]) -> dict | None:
    """AAE contra o LRC humano. Só casa texto ÚNICO nos dois lados.

    ‼️ CICATRIZ: o LRC do LRCLIB casa por TÍTULO/ARTISTA e muitas vezes é de
    OUTRA gravação. O Whisky a Go-Go tem LRC que acaba em 153s numa música de
    249s e está 4,8s deslocado — usar isso como verdade dava AAE de 75 SEGUNDOS
    e culparia nosso alinhamento por um erro que é da fonte. Verdade fundamental
    também precisa ser validada antes de valer."""
    entry = main._get_entry(sid)
    lyr = entry.get("lyrics") or {}
    lrc = parse_lrc(lyr.get("pristineSynced") or "")
    if len(lrc) < 6 or not lines:
        return None
    dur = entry.get("duration") or 0
    if dur and lrc[-1][0] < 0.75 * dur:
        return {"descartado": "LRC não cobre a música (provável outra gravação)",
                "lrc_ate": round(lrc[-1][0], 1), "duracao": dur}
    nossos = [(ln["t"], main._norm_txt(ln.get("text", "")).strip()) for ln in lines]

    def unicos(pares):
        vistos = {}
        for t, txt in pares:
            vistos.setdefault(txt, []).append(t)
        return {txt: ts[0] for txt, ts in vistos.items() if len(ts) == 1 and len(txt) > 8}

    a, b = unicos(lrc), unicos(nossos)
    difs = [b[txt] - a[txt] for txt in a if txt in b]       # SINALIZADO
    if len(difs) < 4:
        return None
    abs_difs = [abs(d) for d in difs]
    return {"aae_ms": round(statistics.median(abs_difs) * 1000),
            "vies_ms": round(statistics.median(difs) * 1000),   # + = atrasado
            "casadas": len(difs), "linhas_lrc": len(lrc),
            "pct_ate_300ms": round(sum(1 for d in abs_difs if d <= 0.3) / len(difs), 3)}


def rodar(alvos: list[str]) -> None:
    lib = main._load_lib()
    resultado = {}
    for alvo in alvos:
        sid = alvo if alvo in lib else resolve(None, alvo, lib)
        if not sid:
            continue
        entry = main._get_entry(sid)
        titulo = f"{entry.get('artist')} - {entry.get('title')}"
        lines = (entry.get("lyrics") or {}).get("lines")
        if not lines:
            print(f"-- {titulo}: sem linhas alinhadas")
            continue
        v = verdade(sid, lines)
        if not v:
            print(f"-- {titulo}: sem LRC humano utilizável")
            continue
        resultado[sid] = {"titulo": titulo, **v}
        if v.get("descartado"):
            print(f"-- {titulo[:42]:44} {v['descartado']} "
                  f"(LRC até {v['lrc_ate']}s de {v['duracao']}s)")
            continue
        print(f"{titulo[:42]:44} AAE={v['aae_ms']:5}ms  viés={v['vies_ms']:+5}ms  "
              f"≤300ms={v['pct_ate_300ms']:.0%}  ({v['casadas']} linhas)")
    meds = [r["aae_ms"] for r in resultado.values() if "aae_ms" in r]
    if resultado:
        OUT.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    if meds:
        print(f"\nAAE mediano da amostra: {round(statistics.median(meds))}ms "
              f"(referência SOTA do MIREX: <200ms) — data/truth_metrics.json")


if __name__ == "__main__":
    lib = main._load_lib()
    alvos = sys.argv[1:] or [resolve(s, n, lib) for s, n, _c in CASES]
    rodar([a for a in alvos if a])
