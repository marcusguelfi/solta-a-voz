"""Bancada de verdade: Epitáfio (CONTROLE) + as 10 piores, medidas contra LRC humano.

Pedido do Marcus (2026-07-19): em vez da biblioteca inteira, um grupo pequeno com
CONTROLE. O controle existe pra provar que a mudança não estraga o que já está
bom — foi assim que a gente pegou o anchor v1 destruindo o I Have a Dream.

Passo 1 (este arquivo):  baixa o LRC humano do LRCLIB SÓ COMO VERDADE e mede o
estado ATUAL. O LRC vai pra data/truth/<sid>.lrc — arquivo separado, FORA do
library.json, pra que o pipeline não tenha como usá-lo por acidente (se virar
trilho, a métrica passa a se auto-referenciar e perdemos a régua honesta).

Uso:  .venv\\Scripts\\python.exe server\\bench_truth.py [--fetch]
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
import main  # noqa: E402
import measure_truth as mt  # noqa: E402

TRUTH = BASE / "data" / "truth"
OUT = BASE / "data" / "bench_truth.json"

# (sid, rótulo). Epitáfio primeiro: é o CONTROLE, não pode piorar.
ALVOS = [
    ("66011072d2ae", "Epitáfio  [CONTROLE]"),
    ("e9c7ce8ceec1", "The Final Countdown"),
    ("e8d1ce233e54", "September"),
    ("736e6ae52b47", "Samurai"),
    ("524f5ca1de82", "Stayin' Alive"),
    ("48cc1966dcee", "Psycho Killer"),
    ("6a753d679b39", "Bad Boys"),
    ("d69d8f8e7c97", "Whisky a Go-Go"),
    ("496798166b42", "Vamos Fugir"),
    ("570128cf3402", "Take Me Out"),
    ("8a44ede95182", "Não Deixe o Samba Morrer"),
]


def baixar(sid: str) -> str | None:
    """LRC sincronizado do LRCLIB. Guarda de versão: LRC casa por título/artista
    e MUITAS VEZES é de outra gravação (Whisky: LRC de 153s numa música de 249s,
    4,8s deslocado). Verdade fundamental também se valida antes de valer."""
    e = main._get_entry(sid)
    dur = e.get("duration") or 0
    syn = None
    try:
        hit = main._lrclib_request("get", {
            "artist_name": e.get("artist") or "", "track_name": e.get("title") or "",
            "album_name": e.get("album") or "", "duration": int(dur)})
        syn = (hit or {}).get("syncedLyrics")
    except Exception:
        pass
    if not syn:
        try:
            r = main._lrclib_request("search",
                                     {"q": f"{e.get('artist')} {e.get('title')}"}) or []
            cand = [x for x in r if x.get("syncedLyrics")
                    and abs((x.get("duration") or 0) - dur) <= 8]
            syn = cand[0]["syncedLyrics"] if cand else None
        except Exception:
            pass
    if not syn:
        return None
    lrc = mt.parse_lrc(syn)
    if not lrc or (dur and lrc[-1][0] < 0.75 * dur):
        return None                      # outra gravação: não serve de verdade
    TRUTH.mkdir(parents=True, exist_ok=True)
    (TRUTH / f"{sid}.lrc").write_text(syn, encoding="utf-8")
    return syn


def verdade_de(sid: str) -> str | None:
    f = TRUTH / f"{sid}.lrc"
    if f.exists():
        return f.read_text(encoding="utf-8")
    return ((main._get_entry(sid).get("lyrics") or {}).get("pristineSynced")) or None


def aae(sid: str, lines: list[dict]) -> dict | None:
    """AAE contra o LRC humano, casando só linha de texto ÚNICO nos dois lados
    (refrão repetido é ambíguo)."""
    txt = verdade_de(sid)
    lrc = mt.parse_lrc(txt or "")
    if len(lrc) < 6 or not lines:
        return None
    nossos = [(ln["t"], main._norm_txt(ln.get("text", "")).strip()) for ln in lines]

    def unicos(pares):
        v = {}
        for t, s in pares:
            v.setdefault(s, []).append(t)
        return {s: ts[0] for s, ts in v.items() if len(ts) == 1 and len(s) > 8}

    a, b = unicos(lrc), unicos(nossos)
    difs = [b[s] - a[s] for s in a if s in b]
    if len(difs) < 4:
        return None
    ab = [abs(d) for d in difs]
    return {"aae_ms": round(statistics.median(ab) * 1000),
            "vies_ms": round(statistics.median(difs) * 1000),
            "casadas": len(difs),
            "pct_300ms": round(sum(1 for d in ab if d <= 0.3) / len(difs), 3)}


def main_(fetch: bool) -> None:
    lib = main._load_lib()
    tabela = {}
    for sid, rot in ALVOS:
        if sid not in lib:
            print(f"?? {rot}: não está na biblioteca")
            continue
        e = main._get_entry(sid)
        lyr = e.get("lyrics") or {}
        if fetch and not (TRUTH / f"{sid}.lrc").exists():
            baixar(sid)
        v = aae(sid, lyr.get("lines") or [])
        tem_trans = (main.STEMS / sid / "words.json").exists()
        tabela[sid] = {"rotulo": rot, "alignMethod": lyr.get("alignMethod"),
                       "transcricao": tem_trans, "antes": v}
        estado = (f"AAE={v['aae_ms']:5}ms viés={v['vies_ms']:+5}ms "
                  f"≤300ms={v['pct_300ms']:.0%} (n={v['casadas']})"
                  if v else "SEM VERDADE UTILIZÁVEL")
        print(f"{rot[:26]:28} {str(lyr.get('alignMethod'))[:16]:18} "
              f"trans={'sim' if tem_trans else 'NÃO'}  {estado}")
    OUT.write_text(json.dumps(tabela, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\ngravado em data/bench_truth.json ({len(tabela)} músicas)")


if __name__ == "__main__":
    main_("--fetch" in sys.argv)
