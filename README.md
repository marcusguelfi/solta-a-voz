# 🎤 Solta a Voz — karaokê caseiro

Suba um arquivo de áudio **ou cole um link** (YouTube etc.): o app baixa a música,
mostra a metadata (título, artista, duração, dificuldade) e **prepara o karaokê
sozinho**: separa a voz do instrumental com IA, busca a letra sincronizada e
alinha a letra pelo início real do canto. Depois é só entrar e cantar.

## Pipeline de preparo (automático ao adicionar)

1. **Download/upload** → metadata via mutagen ou yt-dlp
2. **Separação por IA** — modelo MDX-Net (UVR-MDX-NET-Voc_FT) via onnxruntime,
   roda em CPU (~2-5 min por música); gera `vocals.mp3` + `instrumental.mp3`
3. **Letra sincronizada** — API gratuita do [LRCLIB](https://lrclib.net/docs)
4. **Melodia de referência** — pitch do cantor original extraído do stem de voz
   (librosa/pyin) → base da pontuação
5. **Alinhamento pela cantoria** (forced alignment) — o stable-ts/Whisper acha
   onde cada linha da letra é de fato **cantada** no stem de voz e re-cronometra
   linha a linha (início *e fim*). A letra segue o cantor, não o relógio da
   música — pausas instrumentais mostram contagem regressiva, não letra escorrendo.
   Fallbacks em cascata: correlação com a melodia (escolhe a versão certa da
   letra entre as do LRCLIB) → onset de energia da voz.
6. **Dificuldade** — heurística: palavras/min cantado + pico de velocidade (p90)

## Player

- **Modo IA** (música preparada): voz e instrumental são faixas separadas
  tocadas em sync perfeito — slider **Voz** em 0% = karaokê de verdade;
  sobe se quiser voz-guia. Slider **Instrumental** até 130% com limiter.
- **Modo rápido** (música recém-adicionada, ainda processando): center-cut
  (cancelamento do canal central) em tempo real — dá pra cantar enquanto a IA trabalha.
- Letra com destaque progressivo, contagem regressiva (● ● ●) antes da primeira
  linha e em pausas longas, ajuste fino ±0,5s se precisar (salvo por música).

## Pontuação 🎤 (igual karaokê de verdade)

Botão **🎤 pontuar** no player liga o microfone (cancelamento de eco ativo — mas
fones dão pontuação mais precisa). A cada frase da letra:

- o pitch da sua voz (autocorrelação, ~15x/s) é comparado com a melodia do
  cantor original, com **tolerância de oitava** (homem cantando música de
  mulher pontua normal) e janela de ±350ms pra latência do mic;
- a frase fecha com nota 0-100 e um pop na tela (PERFEITO! / Mandou bem! /
  Boa! / Quase… / Ops…); frases sem canto = "Cadê a voz? 👀";
- medidor de **tom** ao vivo mostra se você está afinado (verde), quase (âmbar)
  ou fora (rosa);
- no fim: nota geral S/A/B/C/D/E, total de pontos e recorde por música
  (salvo no navegador).

Pontuação exige música preparada (modo IA) — é a melodia extraída da voz
original que serve de gabarito.

## Rodando

**Roda 100% local e sozinho — não depende de Claude, de API paga nem de nuvem.**
Internet só é usada pra baixar música por link (yt-dlp) e buscar letra (LRCLIB).

```bat
:: em qualquer computador novo (só precisa do Python 3.13 instalado):
setup.bat        :: cria o venv, instala tudo e baixa o ffmpeg portátil

:: sempre:
start.bat        (ou: .venv\Scripts\python.exe server\main.py)
```

Abra <http://localhost:8777>. Os modelos de IA (~50MB de separação + ~460MB do
Whisper) baixam sozinhos no primeiro preparo e ficam em `data\models`.

> 💡 **Prefira áudio de estúdio** — versão ao vivo tem plateia e reverb que
> atrapalham a separação de voz e o alinhamento da letra.

## Estrutura

```
server/main.py    — API + worker de preparo (fila em thread)
static/           — frontend (HTML/CSS/JS puro, Web Audio API)
data/library.json — biblioteca (metadata + cache de letra + status)
data/media/       — arquivos originais
data/stems/{id}/  — vocals.mp3 + instrumental.mp3 por música
tools/ffmpeg/     — ffmpeg portátil
```

## Atalhos no player

- **Espaço** play/pause · **Esc** volta ao repertório · **← →** pula 5s
