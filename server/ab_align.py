"""A/B do ALIGN v2 fase B: whisper (atual) × MMS_FA/CTC, no MESMO texto-base.

Não escreve nada na biblioteca — só mede. Régua: mediana do erro início-da-linha
× onset de frase real (audit), com a máscara de fala quando disponível.

Uso:  .venv\\Scripts\\python.exe server\\ab_align.py [id-ou-nome ...]
Sem argumentos, roda os 7 casos de referência. Grava data/ab_align.json.
"""
import json
import os
import statistics
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
import audit  # noqa: E402
import main  # noqa: E402
from measure_align import CASES, resolve  # noqa: E402

OUT = BASE / "data" / "ab_align.json"


def med(xs):
    return round(statistics.median(xs) * 1000) if xs else None


def avaliar(sid: str, lines: list) -> dict:
    """Mede um conjunto de linhas contra os onsets reais (com máscara de fala)."""
    vocals = main.STEMS / sid / "vocals.mp3"
    active, hop = audit.energy_envelope(vocals)
    masked = main.sung_active(sid, list(active), hop)
    usa = masked if masked is not None else active
    onsets = audit.phrase_onsets(usa, hop)
    errs = audit.timing_errors(lines, onsets)
    return {"mediana_ms": med(errs), "verificaveis": len(errs), "linhas": len(lines),
            "mascara": masked is not None}


def rodar(alvos: list[str]) -> None:
    lib = main._load_lib()
    resultado = {}
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
        linha = {"titulo": titulo}
        for engine, fn in (("whisper", main.whisper_align_lines),
                           ("mms", main.mms_align_lines)):
            t0 = time.time()
            try:
                lines = fn(sid, texts)
            except Exception as ex:
                print(f"   {engine}: falhou — {type(ex).__name__}: {str(ex)[:60]}")
                continue
            if not lines:
                linha[engine] = {"erro": "sem alinhamento"}
                continue
            linha[engine] = {**avaliar(sid, lines), "seg": round(time.time() - t0)}
        w, m = linha.get("whisper") or {}, linha.get("mms") or {}
        veredito = "?"
        if w.get("mediana_ms") is not None and m.get("mediana_ms") is not None:
            veredito = ("MMS" if m["mediana_ms"] < w["mediana_ms"] * 0.9 else
                        "whisper" if w["mediana_ms"] < m["mediana_ms"] * 0.9 else "empate")
        linha["veredito"] = veredito
        resultado[sid] = linha
        print(f"{titulo[:44]:46}")
        print(f"   whisper: {json.dumps(w, ensure_ascii=False)}")
        print(f"   mms    : {json.dumps(m, ensure_ascii=False)}")
        print(f"   -> {veredito}")
    OUT.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    vitorias = {}
    for v in resultado.values():
        vitorias[v.get("veredito")] = vitorias.get(v.get("veredito"), 0) + 1
    print(f"\nplacar: {vitorias}  (gravado em data/ab_align.json)")


if __name__ == "__main__":
    lib = main._load_lib()
    alvos = sys.argv[1:] or [resolve(s, n, lib) for s, n, _c in CASES]
    rodar([a for a in alvos if a])
