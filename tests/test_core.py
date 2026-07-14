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
