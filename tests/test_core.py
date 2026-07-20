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


# ------------------------------------------- fase B: motor híbrido whisper+CTC

def test_suspect_line_idx_pega_interpolada_e_esmagada():
    lines = [
        {"t": 1.0, "end": 3.0, "text": "frase normal com varias palavras"},   # ok
        {"t": 4.0, "end": 6.0, "text": "outra frase", "interp": True},        # desistiu
        {"t": 7.0, "end": 7.3, "text": "essa aqui tem seis palavras cantadas"},  # esmagada
    ]
    assert main.suspect_line_idx(lines) == [1, 2]


def test_hibrido_usa_ctc_so_nas_linhas_ruins(monkeypatch):
    """Whisper manda; CTC entra só onde ele não se ancorou — e só se couber
    entre as vizinhas confiáveis."""
    whisper = [
        {"t": 10.0, "end": 12.0, "text": "primeira frase bem ancorada aqui"},
        {"t": 12.5, "end": 12.6, "text": "melisma que ele esmagou totalmente"},  # ruim
        {"t": 20.0, "end": 22.0, "text": "terceira frase bem ancorada aqui"},
        {"t": 23.0, "end": 25.0, "text": "quarta frase", "interp": True},        # ruim
    ]
    ctc = [
        {"t": 10.4, "end": 12.4, "text": "primeira frase bem ancorada aqui"},
        {"t": 13.5, "end": 19.0, "text": "melisma que ele esmagou totalmente"},
        {"t": 20.4, "end": 22.4, "text": "terceira frase bem ancorada aqui"},
        {"t": 23.4, "end": 26.0, "text": "quarta frase"},
    ]
    monkeypatch.setattr(main, "whisper_align_lines",
                        lambda sid, texts: [dict(x) for x in whisper])
    monkeypatch.setattr(main, "mms_align_lines",
                        lambda sid, texts: [dict(x) for x in ctc])
    out = main.hybrid_align_lines("x", ["a"] * 4)
    assert out[0]["t"] == 10.0 and not out[0].get("ctc")   # boa: whisper mantido
    assert out[1]["t"] == 13.5 and out[1]["ctc"]           # ruim: CTC assumiu
    assert out[2]["t"] == 20.0 and not out[2].get("ctc")
    assert out[3]["ctc"]


def test_hibrido_recusa_ctc_fora_do_trilho(monkeypatch):
    """CTC que discorda das âncoras firmes do whisper é ignorado."""
    whisper = [
        {"t": 10.0, "end": 12.0, "text": "primeira frase bem ancorada aqui"},
        {"t": 12.5, "end": 12.6, "text": "melisma que ele esmagou totalmente"},
        {"t": 20.0, "end": 22.0, "text": "terceira frase bem ancorada aqui"},
        {"t": 30.0, "end": 31.0, "text": "quarta frase", "interp": True},
    ]
    ctc = [dict(x) for x in whisper]
    ctc[1] = {"t": 55.0, "end": 60.0, "text": "melisma que ele esmagou totalmente"}
    monkeypatch.setattr(main, "whisper_align_lines",
                        lambda sid, texts: [dict(x) for x in whisper])
    monkeypatch.setattr(main, "mms_align_lines",
                        lambda sid, texts: [dict(x) for x in ctc])
    out = main.hybrid_align_lines("x", ["a"] * 4)
    assert out[1]["t"] == 12.5 and not out[1].get("ctc")   # fora do trilho: recusado


def test_hibrido_nao_chama_ctc_quando_whisper_vai_bem(monkeypatch):
    chamou = []
    monkeypatch.setattr(main, "whisper_align_lines", lambda sid, texts: [
        {"t": i * 3.0, "end": i * 3.0 + 2.0, "text": "frase de teste aqui ok"}
        for i in range(12)])
    monkeypatch.setattr(main, "mms_align_lines",
                        lambda sid, texts: chamou.append(1) or [])
    out = main.hybrid_align_lines("x", ["a"] * 12)
    assert len(out) == 12 and not chamou   # CTC nem foi acionado


def test_anchor_fix_nao_reescreve_a_musica_inteira(monkeypatch):
    """CICATRIZ: refrão repetido fazia toda linha casar em vários lugares e a
    versão antiga movia quase tudo (o controle I Have a Dream foi de 36ms pra
    615ms). Se mais de 25% das linhas quiserem mudar, não mexe em nada."""
    _fake_words(monkeypatch, [(10.0 + i * 4, "eu tenho um sonho lindo") for i in range(10)])
    lines = [{"t": 100.0 + i, "end": 101.0 + i, "text": "Eu tenho um sonho lindo"}
             for i in range(8)]
    antes = [dict(x) for x in lines]
    assert main.anchor_fix_lines("x", lines) == 0
    assert lines == antes


def test_anchor_fix_respeita_lugar_que_ja_bate(monkeypatch):
    """Linha cujo lugar atual concorda com o canto não é movida, mesmo que
    exista casamento igual mais adiante (repetição)."""
    _fake_words(monkeypatch, [(10.0, "eu tenho um sonho lindo"),
                              (40.0, "eu tenho um sonho lindo"),
                              (70.0, "outra frase qualquer aqui agora")])
    lines = [{"t": 10.1, "end": 12.0, "text": "Eu tenho um sonho lindo"},
             {"t": 70.1, "end": 72.0, "text": "Outra frase qualquer aqui agora"}]
    assert main.anchor_fix_lines("x", lines) == 0


def test_drop_ghost_tem_teto_proporcional(monkeypatch):
    """Derrubar 1/4 da letra é sintoma de premissa errada, não de fantasma."""
    hop = 0.032
    n = int(200 / hop)
    monkeypatch.setattr(main, "load_pitch", lambda sid: {"hop": hop, "energy": [0] * n})
    monkeypatch.setattr(main, "sung_energy", lambda sid, pitch=None, build=False: [0] * n)
    lines = [{"t": i * 4.0, "end": i * 4.0 + 2.0, "text": f"linha {i}"} for i in range(40)]
    _keep, dropped = main.drop_ghost_lines("x", lines)
    assert dropped <= 10   # teto de 25%, não os 40 que a energia zerada pediria


def test_drop_ghost_usa_energia_crua_nao_a_mascarada(monkeypatch):
    """CICATRIZ: apagar letra usa o sinal conservador. A máscara de fala é
    para POSICIONAR (clamp/extensão), nunca para deletar — com ela, Vamos
    Fugir perdeu 25 das 61 linhas."""
    hop = 0.032
    n = int(120 / hop)
    energia_crua = [1] * n                       # há canto o tempo todo
    monkeypatch.setattr(main, "load_pitch", lambda sid: {"hop": hop, "energy": energia_crua})
    # máscara diria que nada é fala (transcrição incompleta): não pode apagar
    monkeypatch.setattr(main, "sung_energy",
                        lambda sid, pitch=None, build=False: [0] * n)
    lines = [{"t": i * 4.0, "end": i * 4.0 + 3.0, "text": f"linha {i} cantada"}
             for i in range(25)]
    keep, dropped = main.drop_ghost_lines("x", lines)
    assert dropped == 0 and len(keep) == 25


# ------------------------------------- ALIGN v3: alinhamento global de sequências

def _wt(monkeypatch, pares, passo=0.4):
    """pares = [(t, "frase cantada")] -> transcrição palavra a palavra."""
    words = []
    for t, frase in pares:
        for i, w in enumerate(frase.split()):
            words.append([round(t + i * passo, 2), round(t + (i + 1) * passo - 0.05, 2), w])
    monkeypatch.setattr(main, "word_transcript", lambda sid, build=False: words)
    return words


def test_global_align_usa_o_tempo_do_canto(monkeypatch):
    _wt(monkeypatch, [(10.0, "agora eu era o heroi e o meu cavalo"),
                      (20.0, "so falava ingles a noiva do cowboy"),
                      (30.0, "era voce alem de outras tres")])
    linhas = main.global_align_lines("x", ["Agora eu era o herói e o meu cavalo",
                                           "Só falava inglês a noiva do cowboy",
                                           "Era você além de outras três"])
    assert linhas is not None
    assert abs(linhas[0]["t"] - 10.0) < 0.2
    assert abs(linhas[1]["t"] - 20.0) < 0.2
    assert abs(linhas[2]["t"] - 30.0) < 0.2
    assert linhas[0]["words"] and len(linhas[0]["words"]) == 9   # tempo por palavra


def test_global_align_interpola_palavra_nao_reconhecida(monkeypatch):
    """Melisma/palavra mal transcrita vira gap interpolado entre âncoras, sem
    arrastar a frase inteira."""
    _wt(monkeypatch, [(10.0, "quando eu XXXX pisar mais na avenida"),
                      (20.0, "vou deixar a saudade ficar por aqui"),
                      (30.0, "nao deixe o samba morrer nao deixe o samba acabar")])
    linhas = main.global_align_lines("x", ["Quando eu não puder pisar mais na avenida",
                                           "Vou deixar a saudade ficar por aqui",
                                           "Não deixe o samba morrer não deixe o samba acabar"])
    assert linhas is not None
    assert abs(linhas[0]["t"] - 10.0) < 0.3      # âncora do início segura
    assert abs(linhas[1]["t"] - 20.0) < 0.3      # a linha seguinte não escorregou


def test_global_align_imune_a_mudanca_de_andamento(monkeypatch):
    """Sem premissa de offset global: cada trecho ancora onde foi cantado."""
    _wt(monkeypatch, [(10.0, "primeira frase da musica bem devagar aqui"),
                      (60.0, "segunda frase depois de uma pausa enorme mesmo"),
                      (65.0, "terceira frase agora bem rapida de verdade"),
                      (72.0, "quarta frase pra fechar a contagem direito")])
    linhas = main.global_align_lines("x", ["Primeira frase da música bem devagar aqui",
                                           "Segunda frase depois de uma pausa enorme mesmo",
                                           "Terceira frase agora bem rápida de verdade",
                                           "Quarta frase pra fechar a contagem direito"])
    assert abs(linhas[1]["t"] - 60.0) < 0.3
    assert abs(linhas[2]["t"] - 65.0) < 0.3


def test_global_align_recusa_quando_nada_casa(monkeypatch):
    """Transcrição de outra música: não force alinhamento, devolve None."""
    _wt(monkeypatch, [(10.0, "yesterday all my troubles seemed so far away now"),
                      (20.0, "suddenly i am not half the man i used to be")])
    linhas = main.global_align_lines("x", ["Agora eu era o herói e o meu cavalo",
                                           "Só falava inglês a noiva do cowboy",
                                           "Era você além de outras três"])
    assert linhas is None


def test_agreement_ceiling_separa_transcricao_de_alinhamento(monkeypatch):
    _wt(monkeypatch, [(10.0, "agora eu era o heroi"),
                      (20.0, "e o meu cavalo so falava ingles"),
                      (30.0, "a noiva do cowboy era voce alem de outras tres")])
    # linha no lugar ERRADO, mas o texto existe no canto -> teto alto
    fora = [{"t": 50.0, "end": 52.0, "text": "Agora eu era o herói"}]
    assert main.agreement_ceiling("x", fora) > 0.9
    assert main.alignment_agreement("x", fora) < 0.4
    # texto que NÃO foi cantado -> teto baixo (problema é transcrição/letra)
    inexistente = [{"t": 10.0, "end": 12.0, "text": "Frase que ninguém cantou aqui"}]
    assert main.agreement_ceiling("x", inexistente) < 0.6


def test_onset_error_median_e_independente_da_transcricao(monkeypatch):
    """CICATRIZ: a concordância é auto-referente pro motor global (ele põe a
    linha no tempo da transcrição e é medido contra a própria transcrição).
    Quem decide o motor é esta régua, que vem da ENERGIA do áudio."""
    hop = 0.032
    n = int(60 / hop)
    energy = [0] * n
    for a in (10.0, 20.0, 30.0, 40.0, 50.0):          # cinco frases cantadas
        for k in range(int(a / hop), int((a + 2.0) / hop)):
            energy[k] = 1
    monkeypatch.setattr(main, "load_pitch", lambda sid: {"hop": hop, "energy": energy})
    monkeypatch.setattr(main, "sung_energy", lambda sid, pitch=None, build=False: energy)
    marcos = (10.0, 20.0, 30.0, 40.0, 50.0)
    certo = [{"t": a, "end": a + 1.5, "text": f"frase {i}"} for i, a in enumerate(marcos)]
    atrasado = [{"t": a + 0.4, "end": a + 1.9, "text": f"frase {i}"}
                for i, a in enumerate(marcos)]
    e_certo, e_atras = main.onset_error_median("x", certo), main.onset_error_median("x", atrasado)
    assert e_certo is not None and e_atras is not None
    assert e_certo < 0.1 and e_atras > 0.3           # enxerga o viés de 400ms


# ------------------------------------- ALIGN v4 (a): alinhamento local com custo

def test_local_align_acha_ilha_e_nao_forca_o_resto():
    """v4(a): o trecho que existe nos dois casa; o verso que o ASR não ouviu
    NÃO ganha âncora inventada — fica de fora pra virar gap interpolado."""
    a = "quando eu nao puder pisar mais na avenida".split()
    fantasma = "esse verso aqui ninguem cantou nunca jamais".split()
    b = "vou deixar a saudade ficar por aqui comigo".split()
    letra, canto = a + fantasma + b, a + b
    pares = main.local_align_words(letra, canto)
    assert pares == sorted(pares)                              # monotônico
    ancorados = {i for i, _ in pares}
    assert all(i in ancorados for i in range(len(a)))          # 1ª frase inteira
    ini_f, ini_b = len(a), len(a) + len(fantasma)
    assert not (ancorados & set(range(ini_f, ini_b)))          # o fantasma, nenhuma
    assert all(i in ancorados for i in range(ini_b, len(letra)))  # a ilha DEPOIS também


def test_local_align_nao_ancora_palavra_divergente_dentro_da_ilha():
    """Palavra trocada no meio da frase é gap, não âncora: o tempo dela é
    interpolado entre as vizinhas confiáveis."""
    letra = "agora eu era o heroi e o meu cavalo branco".split()
    canto = "agora eu era o XXXXX e o meu cavalo branco".split()
    pares = main.local_align_words(letra, canto)
    assert (4, 4) not in pares                         # 'heroi' x 'XXXXX'
    assert (0, 0) in pares and (9, 9) in pares          # cerca dos dois lados


def test_local_align_recusa_texto_de_outra_musica():
    letra = "agora eu era o heroi e o meu cavalo".split()
    canto = "yesterday all my troubles seemed so far away".split()
    assert main.local_align_words(letra, canto) == []


def test_local_align_nao_casa_palavra_curta_por_acaso():
    """CICATRIZ da família das que já nos morderam: 'de'/'da'/'eu' casam em
    qualquer lugar e produzem âncora falsa que arrasta a frase."""
    assert main._sim_palavra("de", "da") == 0.0
    assert main._sim_palavra("eu", "eu") == 1.0        # idêntica ainda vale
    assert main._sim_palavra("coracao", "coracoes") >= 0.8    # flexão: ainda ancora
    assert main._sim_palavra("cantar", "cantei") == 0.0       # raiz igual, palavra outra


def test_global_align_com_sw_mantem_o_tempo_do_canto(monkeypatch):
    """v4(a) trocou o casador por baixo do motor global: o contrato de fora
    (linha no tempo em que foi cantada) tem que continuar valendo."""
    _wt(monkeypatch, [(10.0, "agora eu era o heroi e o meu cavalo"),
                      (20.0, "so falava ingles a noiva do cowboy"),
                      (30.0, "era voce alem de outras tres bem ali")])
    linhas = main.global_align_lines("x", ["Agora eu era o herói e o meu cavalo",
                                           "Só falava inglês a noiva do cowboy",
                                           "Era você além de outras três bem ali"])
    assert linhas is not None
    assert abs(linhas[0]["t"] - 10.0) < 0.2
    assert abs(linhas[2]["t"] - 30.0) < 0.2


def test_skip_pula_instrumental_em_vez_de_espalhar_letra(monkeypatch):
    """v4(b): o buraco entre duas âncoras tem 30s, mas só os 6s finais têm
    canto. Repartir no relógio joga letra dentro do instrumental (foi assim que
    o Take Me Out cravou uma linha em 60,55s, região sem uma palavra sequer)."""
    hop = 0.032
    n = int(60 / hop)
    energy = [0] * n
    for k in range(int(50 / hop), int(56 / hop)):     # canto só de 50s a 56s
        energy[k] = 1
    monkeypatch.setattr(main, "load_pitch", lambda sid: {"hop": hop, "energy": energy})
    monkeypatch.setattr(main, "sung_energy", lambda sid, pitch=None, build=False: energy)
    marcos = main._repartir_no_canto("x", 20.0, 57.0, 4)
    assert marcos is not None
    assert all(50.0 <= t <= 56.5 for t in marcos)     # nada no instrumental
    assert marcos == sorted(marcos)


def test_skip_desiste_quando_a_energia_nao_ajuda(monkeypatch):
    """Buraco quase todo cantado (ou quase todo mudo): a energia não tem o que
    dizer ali e o reparte uniforme volta a mandar."""
    hop = 0.032
    n = int(60 / hop)
    cheio = [1] * n
    monkeypatch.setattr(main, "load_pitch", lambda sid: {"hop": hop, "energy": cheio})
    monkeypatch.setattr(main, "sung_energy", lambda sid, pitch=None, build=False: cheio)
    assert main._repartir_no_canto("x", 10.0, 40.0, 4) is None
    vazio = [0] * n
    monkeypatch.setattr(main, "sung_energy", lambda sid, pitch=None, build=False: vazio)
    assert main._repartir_no_canto("x", 10.0, 40.0, 4) is None


def _energia(monkeypatch, marcos, hop=0.032, dur=2.0, total=200.0):
    n = int(total / hop)
    energy = [0] * n
    for a in marcos:
        for k in range(int(a / hop), min(n, int((a + dur) / hop))):
            energy[k] = 1
    monkeypatch.setattr(main, "load_pitch", lambda sid: {"hop": hop, "energy": energy})
    monkeypatch.setattr(main, "sung_energy", lambda sid, pitch=None, build=False: energy)
    return energy


def test_erro_pareado_compara_as_mesmas_linhas(monkeypatch):
    """CICATRIZ (Take Me Out, v4 a): comparar `onset_error_median` dos dois lados
    dá veredito FALSO — cada chamada reseleciona quais linhas são verificáveis.
    O deslocamento certo derrubava o erro de 958ms pra 58ms e era RECUSADO
    porque uma linha saiu da janela e o n caiu abaixo do mínimo."""
    marcos = (10.0, 30.0, 50.0, 70.0)
    _energia(monkeypatch, marcos)
    base = [{"t": a - 0.9, "end": a + 1.0, "text": f"linha longa numero {i}"}
            for i, a in enumerate(marcos)]
    # a última fica longe demais e some da janela quando corrigimos o viés
    base.append({"t": 120.0, "end": 121.0, "text": "linha fora de qualquer onset"})
    corrigido = [{**ln, "t": ln["t"] + 0.9, "end": ln["end"] + 0.9} for ln in base]
    a, b, n = main._erro_pareado("x", base, corrigido)
    assert n >= 3
    assert b < a                      # o pareado ENXERGA a melhora
    assert a > 0.5 and b < 0.2


def test_vies_so_e_aplicado_se_a_energia_confirmar(monkeypatch):
    """CICATRIZ: a correção de viés era um palpite aplicado SEM conferência —
    chegou a arrastar 33 linhas de uma música que já estava a 178ms."""
    marcos = (10.0, 30.0, 50.0, 70.0)
    _energia(monkeypatch, marcos)
    certo = [{"t": a, "end": a + 1.5, "text": f"linha longa numero {i}"}
             for i, a in enumerate(marcos)]
    piorado = [{**ln, "t": ln["t"] + 0.6, "end": ln["end"] + 0.6} for ln in certo]
    a, b, _n = main._erro_pareado("x", certo, piorado)
    assert b > a                      # deslocar quem já está certo PIORA
    # e quem está certo não gera candidato nenhum (viés abaixo do limiar)
    assert main._vies_candidatos("x", certo) == []


def test_alignment_quality_separa_ancoravel_de_curta(monkeypatch):
    """v4(c): linha curta demais não é erro de alinhamento — é característica
    da letra (Samurai: 45% das linhas com <5 palavras). A métrica tem que
    dizer o que SABE (acordo) e o quanto sabe (cobertura), sem punir por isso."""
    _wt(monkeypatch, [(10.0, "agora eu era o heroi e o meu cavalo"),
                      (20.0, "so falava ingles a noiva do cowboy era voce"),
                      (30.0, "alem de outras tres eu enfrentava os batalhoes")])
    lines = [
        {"t": 10.0, "end": 13.0, "text": "Agora eu era o herói e o meu cavalo"},   # ancorável, certa
        {"t": 20.0, "end": 23.0, "text": "Só falava inglês a noiva do cowboy era você"},
        {"t": 26.0, "end": 27.0, "text": "Ai, quanto querer"},      # 3 palavras: ambígua
        {"t": 27.0, "end": 28.0, "text": "Vai, sem dizer"},          # 3 palavras: ambígua
    ]
    q = main.alignment_quality("x", lines)
    assert q["ancoraveis"] == 2 and q["curtas"] == 2
    assert q["cobertura"] == 0.5
    assert q["acordo"] > 0.9        # o que dá pra verificar está ÓTIMO
    # a métrica antiga misturava tudo e daria uma nota bem menor
    assert main.alignment_agreement("x", lines) < q["acordo"]
