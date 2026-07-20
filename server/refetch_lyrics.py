"""Re-busca a letra das músicas contaminadas por LETRA DE OUTRA VERSÃO.

Contexto: a guarda de duração só valia pra letra SEM sincronia, então um LRC de
313s entrou numa gravação de 260s (Psycho Killer). O `rank` já foi corrigido;
isto varre quem ficou contaminado.

‼️ PROTOCOLO (as duas cicatrizes que custaram caro hoje):
  • gotcha 14 — a RÉGUA muda quando a música ganha `speechmap.json`. Então
    transcreve ANTES de medir o "antes", pra comparar com o mesmo instrumento.
  • só troca se a letra nova for melhor MEDIDA (mais canto coberto + mais acordo
    com a transcrição). Empate mantém o que está — mudar sem prova já nos mordeu.

Uso:  .venv\\Scripts\\python.exe server\\refetch_lyrics.py [--aplicar] [id ...]
Sem --aplicar só mostra o que faria.
"""
import difflib
import os
import re
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

# achadas pela varredura: letra cobre <72% da duração da gravação
ALVOS = ["Love Will Tear Us Apart", "Send Me An Angel", "A Dança",
         "Ghostbusters", "Stayin Alive", "Eu Vou Estar", "Zombie"]


def acordo_com_audio(texto: str, W: list[str]) -> float:
    """Fração das linhas longas que existem no canto transcrito."""
    ok = tot = 0
    for linha in (texto or "").splitlines():
        linha = re.sub(r"^\[[^\]]*\]", "", linha)
        alvo = [w for w in main._norm_txt(linha).split() if w]
        if len(alvo) < 4:
            continue
        tot += 1
        n, m = len(alvo), 0.0
        for k in range(max(0, len(W) - 1)):
            for sp in (n - 1, n, n + 1):
                if sp >= 2 and k + sp <= len(W):
                    m = max(m, difflib.SequenceMatcher(None, alvo, W[k:k + sp]).ratio())
            if m >= 0.9:
                break
        if m >= 0.6:
            ok += 1
    return ok / max(tot, 1)


def cobre(texto: str, dur: float) -> float:
    pares = main.parse_lrc(texto or "")
    return (pares[-1][0] / dur) if (pares and dur) else 0.0


def tratar(sid: str, aplicar: bool) -> None:
    e = main._get_entry(sid)
    titulo = f"{e.get('artist')} - {e.get('title')}"
    dur = e.get("duration") or 0
    lyr = dict(e.get("lyrics") or {})
    if lyr.get("alignMethod") == "manual":
        print(f"-- {titulo}: editada à mão, preservada")
        return
    # transcrição PRIMEIRO: a régua depende dela (gotcha 14)
    if not (main.STEMS / sid / "words.json").exists():
        print(f"   {titulo}: transcrevendo (a régua depende disso)…")
        main.full_transcribe(sid)
        main._speech_cache.clear()
    wt = main.word_transcript(sid) or []
    W = [main._norm_txt(w).strip() for _a, _b, w in wt]
    W = [w for w in W if w]
    if len(W) < 20:
        print(f"-- {titulo}: transcrição insuficiente, não dá pra julgar")
        return

    atual = lyr.get("origSynced") or lyr.get("synced") or ""
    a_ac, a_cb = acordo_com_audio(atual, W), cobre(atual, dur)
    try:
        r = main._lrclib_request("search", {"q": f"{e.get('artist')} {e.get('title')}"}) or []
    except Exception as ex:
        print(f"-- {titulo}: busca falhou ({type(ex).__name__})")
        return
    cands = [c for c in r if c.get("syncedLyrics")
             and abs((c.get("duration") or 0) - dur) <= 15]
    if not cands:
        print(f"-- {titulo}: nenhum candidato com duração compatível (±15s)")
        return
    melhor = max(cands, key=lambda c: acordo_com_audio(c["syncedLyrics"], W))
    novo = melhor["syncedLyrics"]
    n_ac, n_cb = acordo_com_audio(novo, W), cobre(novo, dur)
    # ‼️ duas formas de ser melhor, e a 1ª versão só enxergava uma delas:
    #   (a) casa MAIS com o canto (letra mais certa);
    #   (b) cobre MUITO mais da música com o mesmo acordo — é o caso do
    #       Send Me An Angel (0,64→0,96) e do Eu Vou Estar (0,68→0,96): a letra
    #       atual está truncada, faltam versos do fim. Exigir ganho de acordo
    #       recusava justamente essas.
    ganho = ((n_ac > a_ac + 0.05 and n_cb >= a_cb)
             or (n_cb > a_cb + 0.15 and n_ac >= a_ac - 0.02))
    print(f"{'==' if ganho else '--'} {titulo[:40]:42} "
          f"acordo {a_ac:.2f}->{n_ac:.2f}  cobre {a_cb:.2f}->{n_cb:.2f}  "
          f"dur_cand={melhor.get('duration'):.0f}s de {dur:.0f}s"
          f"{'  TROCA' if ganho else '  mantém'}")
    if ganho and aplicar:
        lyr.update({"synced": novo, "origSynced": novo, "pristineSynced": novo,
                    "lines": None, "extended": None})
        main._update_entry(sid, lyrics=lyr)
        res = main.align_lyrics_to_vocals(sid, engine="auto")
        print(f"      realinhada: {len((res or {}).get('lines') or [])} linhas, "
              f"método {(res or {}).get('alignMethod')}")


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if a != "--aplicar"]
    aplicar = "--aplicar" in sys.argv
    lib = main._load_lib()
    alvos = argv or [
        sid for sid in lib
        for e in [main._get_entry(sid)]
        if any(a.lower() in f"{e.get('artist')} {e.get('title')}".lower() for a in ALVOS)
    ]
    for sid in alvos:
        try:
            tratar(sid, aplicar)
        except Exception as ex:
            print(f"‼️ {sid}: {type(ex).__name__}: {str(ex)[:100]}")
