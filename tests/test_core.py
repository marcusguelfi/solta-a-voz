"""Testes das funções puras do pipeline (sem modelos pesados).

Rodar:  .venv\\Scripts\\python.exe -m pytest tests -q
"""
import os
import sys
from pathlib import Path

os.environ["KARAOKE_NO_WORKER"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import main  # noqa: E402


# ---------------------------------------------------------------- metadata

def test_parse_video_title_com_mencao():
    artist, title = main.parse_video_title("Péricles - Até Que Durou @BrawJordan", None)
    assert artist == "Péricles"
    assert title == "Até Que Durou"


def test_parse_video_title_com_sujeira():
    artist, title = main.parse_video_title(
        "Chitãozinho & Xororó - Evidências (Clipe Oficial) [HD]", "Canal X")
    assert artist == "Chitãozinho & Xororó"
    assert "Evidências" in title
    assert "Oficial" not in title


def test_parse_video_title_sem_separador_usa_uploader():
    artist, title = main.parse_video_title("Evidências", "Chitãozinho & Xororó - Topic")
    assert artist == "Chitãozinho & Xororó"
    assert title == "Evidências"


def test_clean_search_title():
    assert main.clean_search_title("Raplord (part. Jonas Bento) @Haikaiss") == "Raplord"
    assert main.clean_search_title("Yellow feat. Alguém") == "Yellow"


def test_first_artist():
    assert main.first_artist("Haikaiss, Rafael Spinardi, Jonas Bento") == "Haikaiss"
    assert main.first_artist("Chico Buarque; Nara Leão") == "Chico Buarque"
    assert main.first_artist("") == ""


def test_guess_language():
    assert main.guess_language("eu não sei o que você quer pra mim") == "pt"
    assert main.guess_language("I love you and the things that you do") == "en"


# ---------------------------------------------------------------- LRC

def test_parse_lrc_ordena_e_expande_multi_timestamp():
    lrc = "[00:30.00]refrão\n[00:10.00]primeira\n[00:20.00][00:40.00]dupla"
    lines = main.parse_lrc(lrc)
    assert [t for t, _ in lines] == [10.0, 20.0, 30.0, 40.0]
    assert lines[1][1] == "dupla" and lines[3][1] == "dupla"


def test_parse_lrc_ignora_linha_vazia():
    lines = main.parse_lrc("[00:05.00]\n[00:06.00]texto")
    assert len(lines) == 1


# ---------------------------------------------------------------- dificuldade

def _synced(step, words_per_line, n=10):
    out = []
    for i in range(n):
        t = i * step
        out.append(f"[{int(t // 60):02d}:{t % 60:05.2f}] " + " ".join(["la"] * words_per_line))
    return "\n".join(out)


def test_dificuldade_facil_vs_expert():
    facil = main.compute_difficulty(_synced(step=6, words_per_line=3), duration=70)
    expert = main.compute_difficulty(_synced(step=3, words_per_line=12), duration=40)
    assert facil["label"] == "Fácil"
    assert expert["label"] == "Expert"
    assert expert["wpm"] > facil["wpm"]


def test_dificuldade_requer_minimo_de_linhas():
    assert main.compute_difficulty("[00:01.00] oi", duration=60) is None


# ---------------------------------------------------------------- alinhamento

def _pitch_com_canto_em(sing_spans, total_s=100.0, hop=0.032):
    n = int(total_s / hop)
    midi = [None] * n
    for a, b in sing_spans:
        for k in range(int(a / hop), min(n, int(b / hop))):
            midi[k] = 60.0
    return {"hop": hop, "midi": midi}


def test_correlation_align_recupera_offset():
    # letra diz que o canto é em 10,15,...,35 (janelas de 5s); o áudio canta
    # tudo 2s DEPOIS, com 5s de canto por frase → offset único recuperável
    lrc_times = [10, 15, 20, 25, 30, 35]
    synced = "\n".join(f"[{0:02d}:{t:05.2f}] linha de teste aqui" for t in lrc_times)
    pitch = _pitch_com_canto_em([(t + 2.0, t + 2.0 + 5.0) for t in lrc_times])
    offset, coverage = main.correlation_align(pitch, synced)
    assert abs(offset - 2.0) < 0.15
    assert coverage > 0.9


def test_reconcile_corrige_linha_fugitiva():
    lrc = [(10.0, "a"), (20.0, "b"), (30.0, "c"), (40.0, "d"), (50.0, "e")]
    lines = [{"t": t + 1.0, "end": t + 4.0, "text": txt} for t, txt in lrc]
    lines[2]["t"] = 45.0  # whisper se perdeu nessa (deveria ser ~31)
    lines[2]["end"] = 49.0
    info = main.reconcile_with_lrc(lines, lrc)
    assert info["fixed"] == 1
    assert abs(info["offset"] - 1.0) < 0.01
    assert abs(lines[2]["t"] - 31.0) < 0.1
    for i in range(len(lines) - 1):  # nunca invade a próxima
        assert lines[i]["end"] <= lines[i + 1]["t"]


def test_clamp_ends_to_voice_corta_instrumental(monkeypatch):
    # frase cantada em 0-3s, 12s de instrumental, voz de OUTRA parte volta em 15s
    hop = 0.032
    n = int(20 / hop)
    energy = [0] * n
    for k in range(0, int(3 / hop)):
        energy[k] = 1
    for k in range(int(15 / hop), int(16 / hop)):
        energy[k] = 1
    monkeypatch.setattr(main, "load_pitch", lambda sid: {"hop": hop, "energy": energy})

    lines = [{"t": 0.0, "end": 16.0, "text": "frase esticada"},
             {"t": 16.5, "end": 18.0, "text": "proxima"}]
    trimmed = main.clamp_ends_to_voice("x", lines)
    assert trimmed == 1
    assert 2.8 < lines[0]["end"] < 3.8   # termina no fim do 1º trecho cantado
    assert lines[1]["end"] == 18.0       # não mexe nas outras


def test_clamp_ends_to_voice_preserva_nota_longa(monkeypatch):
    # janela longa mas cheia de voz (nota sustentada) — não corta nada
    hop = 0.032
    n = int(14 / hop)
    monkeypatch.setattr(main, "load_pitch", lambda sid: {"hop": hop, "energy": [1] * n})
    lines = [{"t": 0.0, "end": 13.0, "text": "laaaaa"}]
    assert main.clamp_ends_to_voice("x", lines) == 0
    assert lines[0]["end"] == 13.0


def test_lyric_similarity_separa_certa_de_errada():
    lyrics = "Agora eu era o herói e o meu cavalo só falava inglês guardava bodoque"
    # transcrição imperfeita mas com as mesmas palavras de conteúdo
    bom = "agora eu era heroi e meu cavalo so falava ingles ali guardava bodoque sim"
    ruim = "yesterday all my troubles seemed so far away now it looks as though"
    assert main.lyric_similarity(lyrics, bom) > 0.7
    assert main.lyric_similarity(lyrics, ruim) < 0.2
    assert main.lyric_similarity("", "qualquer coisa") == 0.0


def test_norm_words():
    assert main._norm_words("Coração, é VOCÊ!") == ["coracao", "e", "voce"]


def test_transcript_is_reliable_pega_lixo():
    assert main.transcript_is_reliable("it doesnt hurt me do you want to feel how it feels")
    assert not main.transcript_is_reliable("සිවිවිවිවිවිවිවිවිවිවිවිවිවි")  # lixo não-latino
    assert not main.transcript_is_reliable("")
    assert not main.transcript_is_reliable("uma duas tres")  # curto demais
    # alucinação em loop do Whisper (A Thousand Miles no intro de piano)
    assert not main.transcript_is_reliable("a little bit of " * 12)
    # salada de scripts (Rammstein alemão): latina na maioria mas com cirílico/CJK
    assert not main.transcript_is_reliable("eu tenho um nehe ja surgiu скоро kendim かな clinic")


def test_vocal_start_pula_intro():
    hop = 0.032
    # 8s de intro instrumental (silêncio no stem de voz) + canto a partir de 8s
    energy = [0] * int(8 / hop) + [1] * int(4 / hop)
    start = main.vocal_start_from_energy(energy, hop)
    assert 6.0 < start < 8.0        # começa ~1,5s antes do onset (pula o intro)
    assert main.vocal_start_from_energy([], hop) == 0.0


def test_guess_language_pt_en_es():
    assert main.guess_language("una noche con los amigos por la calle muy bonita pero") == "es"
    assert main.guess_language("you and the love that you do with my heart") == "en"
    assert main.guess_language("você não sabe o que eu sinto pra mim mais uma vez") == "pt"
    # alemão (fora de PT/EN/ES) -> None: Whisper auto-detecta em vez de forçar errado
    assert main.guess_language("Ich bin in Hitze schon seit Tagen so werd ich mir") is None


def test_lyric_similarity_transcricao_parcial_nao_pune_letra_longa():
    # transcrição cobre só o começo; a letra é longa. Precisão (não recall) segura.
    letra_longa = " ".join(f"palavra{i} conteudo{i}" for i in range(40))
    transcript = "palavra0 conteudo0 palavra1 conteudo1 palavra2 conteudo2"
    # todas as palavras ouvidas estão na letra -> alta, mesmo cobrindo 15% da letra
    assert main.lyric_similarity(letra_longa, transcript) > 0.9


# ---- medição de PRECISÃO DE TIMING (início da linha vs onset real da frase) ----

def _load_audit():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
    import audit
    return audit


def test_phrase_onsets_detecta_inicios():
    audit = _load_audit()
    hop = 0.032
    n = int(12 / hop)
    active = [0] * n
    for a, b in [(1.0, 2.0), (5.0, 6.0), (10.0, 11.0)]:  # 3 frases com silêncio entre
        for k in range(int(a / hop), int(b / hop)):
            active[k] = 1
    onsets = audit.phrase_onsets(active, hop)
    assert len(onsets) == 3
    assert abs(onsets[0] - 1.0) < 0.05
    assert abs(onsets[1] - 5.0) < 0.05
    assert abs(onsets[2] - 10.0) < 0.05


def test_timing_errors_mede_desvio():
    audit = _load_audit()
    onsets = [1.0, 5.0, 10.0]
    # linha 0 quase no ponto, linha 1 no ponto, linha 2 atrasada 1s
    errs = audit.timing_errors([{"t": 1.05}, {"t": 5.0}, {"t": 9.0}], onsets)
    assert errs[0] < 0.1
    assert errs[1] < 0.05
    assert abs(errs[2] - 1.0) < 0.05


def test_is_playlist_url():
    assert main.is_playlist_url("https://www.youtube.com/playlist?list=PLabc")
    assert main.is_playlist_url("https://www.youtube.com/watch?v=X&list=PLabc")
    assert main.is_playlist_url("https://soundcloud.com/user/sets/minha-lista")
    # rádio/mix auto-gerado a partir de um vídeo = single, não playlist
    assert not main.is_playlist_url("https://www.youtube.com/watch?v=X&list=RDX&start_radio=1")
    assert not main.is_playlist_url("https://www.youtube.com/watch?v=X")


def test_drop_ghost_lines_remove_linha_sem_canto(monkeypatch):
    hop = 0.032
    n = int(60 / hop)
    energy = [0] * n
    for a, b in [(5, 10), (20, 25), (40, 45)]:  # 3 blocos cantados
        for k in range(int(a / hop), int(b / hop)):
            energy[k] = 1
    monkeypatch.setattr(main, "load_pitch", lambda sid: {"hop": hop, "energy": energy})
    lines = ([{"t": 5.0 + i * 0.5, "end": 5.4 + i * 0.5, "text": f"c{i}"} for i in range(8)]  # cantadas
             + [{"t": 30.0, "end": 33.0, "text": "banter de show sobre silencio"}])       # fantasma
    kept, dropped = main.drop_ghost_lines("x", sorted(lines, key=lambda l: l["t"]))
    assert dropped == 1
    assert all("banter" not in ln["text"] for ln in kept)


def test_clamp_sem_energia_nao_faz_nada(monkeypatch):
    monkeypatch.setattr(main, "load_pitch", lambda sid: None)
    lines = [{"t": 0.0, "end": 30.0, "text": "x"}]
    assert main.clamp_ends_to_voice("x", lines) == 0
    assert lines[0]["end"] == 30.0


def test_detect_vocal_onset(tmp_path):
    import numpy as np
    import soundfile as sf

    sr = 22050
    silence = np.zeros(sr)                      # 1s de silêncio
    t = np.linspace(0, 2, 2 * sr, endpoint=False)
    tone = 0.5 * np.sin(2 * np.pi * 220 * t)    # 2s de "voz"
    wav = tmp_path / "voz.wav"
    sf.write(wav, np.column_stack([np.r_[silence, tone]] * 2), sr)
    onset = main.detect_vocal_onset(wav)
    assert onset is not None
    assert 0.8 <= onset <= 1.3


# ---------------------------------------------------------------- progresso / helpers

def test_with_progress_estima_por_estagio():
    import time as _t
    entry = {"status": "separating", "stageAt": _t.time() - 10, "duration": 100}
    out = main._with_progress(entry)
    lo, hi, _ = main.STAGE_PROGRESS["separating"]
    assert lo <= out["progress"] < hi


def test_with_progress_ready_sem_barra():
    out = main._with_progress({"status": "ready", "stems": True})
    assert "progress" not in out


def test_dificuldade_medio():
    lines = "\n".join(
        f"[00:{i * 4:05.2f}] uma duas tres quatro cinco seis" for i in range(10))
    d = main.compute_difficulty(lines, duration=45)
    assert d["label"] == "Médio"


def test_clean_search_title_colchetes_e_feat():
    assert main.clean_search_title("Song [Official Video] (feat. X)") == "Song"
    assert main.clean_search_title("Título ft. Alguém") == "Título"


# ---------------------------------------------------------------- alinhamento (helpers)

class _Word:
    def __init__(self, start, end):
        self.start, self.end = start, end


class _Seg:
    def __init__(self, words):
        self.words = words


class _Res:
    def __init__(self, segments):
        self.segments = segments


def test_regroup_words_to_lines():
    words = [_Word(0, 1), _Word(1, 2), _Word(3, 4), _Word(4, 5)]
    res = _Res([_Seg(words)])
    spans = main._regroup_words_to_lines(res, ["oi mundo", "tudo bem"])
    # agora cada span carrega o chunk de palavras junto (pro word-level fill)
    assert [(s[0], s[1]) for s in spans] == [(0.0, 2.0), (3.0, 5.0)]
    assert [len(s[2]) for s in spans] == [2, 2]


def test_regroup_falha_se_contagem_nao_bate():
    res = _Res([_Seg([_Word(0, 1)])])
    assert main._regroup_words_to_lines(res, ["duas palavras"]) is None


def test_interpolate_bad_lines_estima_entre_ancoras():
    lines = [
        {"t": 10.0, "end": 12.0, "text": "a", "_ok": True},
        {"t": 0.0, "end": 0.0, "text": "b", "_ok": False},
        {"t": 30.0, "end": 32.0, "text": "c", "_ok": True},
    ]
    main._interpolate_bad_lines(lines, good=[0, 2])
    assert 12.0 < lines[1]["t"] < 30.0
    assert all("_ok" not in ln for ln in lines)  # a flag interna é consumida


def test_line_words_relative():
    """Palavras viram offsets relativos ao início da linha (padrão UltraStar)."""
    class W:
        def __init__(s, a, b, w):
            s.start, s.end, s.word = a, b, w
    words = main._line_words([W(10.0, 10.5, " Agora"), W(10.5, 11.2, "eu")], 10.0)
    assert words == [[0.0, 0.5, "Agora"], [0.5, 1.2, "eu"]]
    # menos de 2 palavras válidas -> None (fill cai pra interpolação linear)
    assert main._line_words([W(10.0, 10.0, "x"), W(10.5, 11.0, "y")], 10.0) is None
    assert main._line_words([], 0.0) is None


def test_canon_valida_extensao_contra_fonte():
    """Anti-alucinação: transcrição só entra se existir na letra oficial."""
    src = ["Fazendo fogueira, sem eira nem beira", "Deixa eu viver no meu mundo"]
    # quase igual -> entra com o texto OFICIAL
    assert main._canon_or_none("fazendo fogueira sem eira nem beira", src) == src[0]
    # alucinação curta ("fazendo foque") -> fora
    assert main._canon_or_none("fazendo foque", src) is None
    # alucinação total -> fora
    assert main._canon_or_none("blablabla nada a ver com nada", src) is None


def test_reconcile_recusa_offset_absurdo():
    """Caso Another Brick: offset -52s teria puxado a letra pra tempo NEGATIVO."""
    lines = [{"t": 5.0, "end": 8.0, "text": "a"}, {"t": 10.0, "end": 13.0, "text": "b"},
             {"t": 15.0, "end": 18.0, "text": "c"}]
    lrc = [(60.0, "a"), (65.0, "b"), (70.0, "c")]
    antes = [dict(l) for l in lines]
    rec = main.reconcile_with_lrc(lines, lrc)
    assert rec.get("skipped") == "offset absurdo"
    assert lines == antes  # não tocou em nada


def test_reconcile_nunca_cria_tempo_negativo():
    lines = [{"t": 30.0, "end": 33.0, "text": "a"}, {"t": 4.0, "end": 6.0, "text": "b"},
             {"t": 12.0, "end": 14.0, "text": "c"}]
    lrc = [(0.5, "a"), (6.0, "b"), (12.0, "c")]
    main.reconcile_with_lrc(lines, lrc)  # offset ~0: linha 'a' fugiu do trilho
    assert all(l["t"] >= 0 for l in lines)


def test_source_similarity_certa_vs_errada():
    fonte = " ".join(["devia ter amado mais ter chorado mais ter visto o sol nascer",
                      "ter arriscado mais e ate errado mais ter feito o que eu queria fazer"])
    certa = "Devia ter amado mais, ter chorado mais! Ter visto o sol nascer, ter arriscado"
    errada = "yesterday all my troubles seemed so far away now i need a place to hide"
    assert main.source_similarity(certa, fonte) > 0.5
    assert main.source_similarity(errada, fonte) < 0.2
    assert main.source_similarity(certa, None) is None
    assert main.source_similarity("curta", fonte) is None  # curto demais: não conclui


def test_is_live_title():
    assert main._is_live_title("Flor de Tangerina (Ao Vivo)")
    assert main._is_live_title("Song (Live at Wembley)")
    assert main._is_live_title("MTV Unplugged - Hoje")
    assert not main._is_live_title("Flor de Tangerina")
    assert not main._is_live_title(None)


# ------------------------------------------- fase A: máscara de fala cantada

def _fake_map(monkeypatch, segments, covered):
    monkeypatch.setattr(main, "speech_map",
                        lambda sid, build=False, force=False:
                        {"segments": segments, "covered": covered})
    main._speech_cache.clear()


def test_sung_active_apaga_instrumento_vazado(monkeypatch):
    """Gaita/solo vazado no stem: energia existe, fala não → não conta como canto."""
    hop = 0.1
    active = [1] * 100  # 10s de "energia" contínua no stem de voz
    # só 0-3s é fala cantada; 5-8s é um segmento de gaita (no_speech alto)
    _fake_map(monkeypatch, [[0.0, 3.0, 0.05], [5.0, 8.0, 0.92]], [0.0, 10.0])
    out = main.sung_active("x", active, hop)
    assert out is not None
    assert sum(out[0:30]) == 30          # canto preservado
    assert sum(out[52:78]) == 0          # gaita zerada (fora da margem de 0,3s)


def test_sung_active_nao_julga_fora_da_cobertura(monkeypatch):
    hop = 0.1
    active = [1] * 100
    _fake_map(monkeypatch, [[0.0, 2.0, 0.05]], [0.0, 5.0])  # só inspecionou 5s
    out = main.sung_active("x", active, hop)
    assert sum(out[55:100]) == 45        # depois de 5s mantém tudo (sem evidência)
    assert sum(out[25:48]) == 0          # dentro da cobertura, sem fala → zera


def test_sung_active_descarta_mascara_catastrofica(monkeypatch):
    """Transcrição furada apagaria a música toda: remédio pior que a doença."""
    hop = 0.1
    active = [1] * 100
    _fake_map(monkeypatch, [[0.0, 0.2, 0.99]], [0.0, 10.0])  # nada é fala
    assert main.sung_active("x", active, hop) is None


def test_sung_energy_cai_pra_crua_sem_mapa(monkeypatch):
    monkeypatch.setattr(main, "speech_map", lambda sid, build=False, force=False: None)
    main._speech_cache.clear()
    pitch = {"hop": 0.1, "energy": [1, 0, 1, 1]}
    assert main.sung_energy("x", pitch) == [1, 0, 1, 1]


# ------------------------------------------- fase C: anchor-matching por linha

def _fake_words(monkeypatch, pares):
    """pares = [(t, "texto da frase")] -> vira transcrição palavra a palavra."""
    words, dur = [], 0.45
    for t, frase in pares:
        for i, w in enumerate(frase.split()):
            words.append([round(t + i * dur, 2), round(t + (i + 1) * dur - 0.05, 2), w])
    monkeypatch.setattr(main, "word_transcript", lambda sid, build=False: words)


def test_anchor_fix_corrige_off_by_one(monkeypatch):
    """Caso Epitáfio: a 1ª frase sumiu e todas herdaram o texto da vizinha."""
    _fake_words(monkeypatch, [
        (10.0, "devia ter amado mais"),
        (14.0, "ter chorado mais ainda"),
        (18.0, "ter visto o sol nascer"),
        (24.0, "devia ter arriscado mais e ate errado mais"),
    ])
    lines = [
        {"t": 10.0, "end": 13.0, "text": "Ter chorado mais ainda"},      # certo: 14
        {"t": 14.0, "end": 17.0, "text": "Ter visto o sol nascer"},      # certo: 18
        {"t": 18.0, "end": 23.0, "text": "Devia ter arriscado mais e até errado mais"},
    ]
    fixed = main.anchor_fix_lines("x", lines)
    assert fixed == 3
    assert abs(lines[0]["t"] - 14.0) < 0.4
    assert abs(lines[1]["t"] - 18.0) < 0.4
    assert abs(lines[2]["t"] - 24.0) < 0.4
    assert all(lines[i]["t"] < lines[i + 1]["t"] for i in range(len(lines) - 1))


def test_anchor_fix_nao_mexe_no_que_esta_certo(monkeypatch):
    _fake_words(monkeypatch, [(10.0, "agora eu era o heroi"),
                             (16.0, "e o meu cavalo so falava ingles"),
                             (22.0, "a noiva do cowboy era voce")])
    lines = [{"t": 10.05, "end": 12.0, "text": "Agora eu era o herói"},
             {"t": 16.02, "end": 19.0, "text": "E o meu cavalo só falava inglês"}]
    antes = [dict(x) for x in lines]
    assert main.anchor_fix_lines("x", lines) == 0
    assert lines == antes


def test_anchor_fix_ignora_linha_curta(monkeypatch):
    """Menos de 3 palavras = âncora fraca; não arrisca mover."""
    _fake_words(monkeypatch, [(10.0, "vai"), (20.0, "vai vai vai vai")])
    lines = [{"t": 30.0, "end": 31.0, "text": "Vai"}]
    assert main.anchor_fix_lines("x", lines) == 0
    assert lines[0]["t"] == 30.0


def test_anchor_fix_sem_transcricao(monkeypatch):
    monkeypatch.setattr(main, "word_transcript", lambda sid, build=False: None)
    lines = [{"t": 1.0, "end": 2.0, "text": "qualquer coisa aqui"}]
    assert main.anchor_fix_lines("x", lines) == 0
