"""Métrica oficial do ALIGN v2 — mede a sincronia dos casos de referência.

Para cada música: mediana do erro (início da linha × onset de frase real) em ms,
medida com DOIS réguas:
  bruta  — onsets da energia do stem (jeito histórico; conta gaita/solo vazado)
  fala   — onsets só onde há FALA CANTADA (máscara no_speech_prob) = a verdade

Uso:  .venv\\Scripts\\python.exe server\\measure_align.py [--tag antes-fase-a]
Grava data/align_metrics.json (acumulativo) além do stdout.
"""
import json
import os
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
import audit  # noqa: E402
import main  # noqa: E402

# os 7 casos de referência (ALIGN_V2_HANDOFF.md)
CASES = [
    ("736e6ae52b47", "Samurai", "gaita+melisma"),
    ("8a44ede95182", "Nao Deixe o Samba Morrer", "melisma+pulos"),
    ("66011072d2ae", "Epitafio", "off-by-one"),
    ("d69d8f8e7c97", "Whisky a Go-Go", "atrasos"),
    ("496798166b42", "Vamos Fugir", "atrasos"),
    (None, "take me out", "tempo change"),
    (None, "i have a dream", "CONTROLE"),
]
METRICS = BASE / "data" / "align_metrics.json"


def resolve(sid, name, lib):
    if sid and sid in lib:
        return sid
    hit = next((s for s, e in lib.items()
                if name.lower() in f"{e.get('artist', '')} {e.get('title', '')}".lower()), None)
    return hit


def med(xs):
    return round(statistics.median(xs) * 1000) if xs else None


def measure(sid: str) -> dict:
    """Mediana do erro por régua + cobertura. Não escreve nada na biblioteca."""
    entry = main._get_entry(sid)
    lines = (entry.get("lyrics") or {}).get("lines") or []
    vocals = main.STEMS / sid / "vocals.mp3"
    if not lines or not vocals.exists():
        return {"erro": "sem linhas ou sem stem"}
    active, hop = audit.energy_envelope(vocals)
    raw_on = audit.phrase_onsets(active, hop)
    raw_err = audit.timing_errors(lines, raw_on)
    out = {"linhas": len(lines), "onsets_bruto": len(raw_on),
           "verificaveis_bruto": len(raw_err), "mediana_bruta_ms": med(raw_err)}
    masked = main.sung_active(sid, active, hop) if hasattr(main, "sung_active") else None
    if masked is not None:
        sp_on = audit.phrase_onsets(masked, hop)
        sp_err = audit.timing_errors(lines, sp_on)
        out.update({"onsets_fala": len(sp_on), "verificaveis_fala": len(sp_err),
                    "mediana_fala_ms": med(sp_err),
                    "frames_mascarados_pct": round(
                        100 * (1 - (sum(masked) / max(sum(active), 1))), 1)})
    return out


def main_run(tag: str) -> None:
    lib = main._load_lib()
    resultado = {}
    for sid, name, classe in CASES:
        real = resolve(sid, name, lib)
        if not real:
            print(f"?? nao achei: {name}")
            continue
        e = lib[real]
        titulo = f"{e.get('artist')} - {e.get('title')}"
        m = measure(real)
        resultado[real] = {"titulo": titulo, "classe": classe, **m}
        print(f"{titulo[:44]:46} [{classe}]")
        print(f"   {json.dumps(m, ensure_ascii=False)}")
    todas = json.loads(METRICS.read_text(encoding="utf-8")) if METRICS.exists() else {}
    todas[tag] = resultado
    METRICS.write_text(json.dumps(todas, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\ngravado em data/align_metrics.json sob a tag '{tag}'")


if __name__ == "__main__":
    tag = "sem-tag"
    if "--tag" in sys.argv:
        tag = sys.argv[sys.argv.index("--tag") + 1]
    main_run(tag)
