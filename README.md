# 🎤 Solta a Voz — karaokê caseiro

Suba um arquivo de áudio **ou cole um link** (YouTube etc.) — de uma música **ou
de uma playlist inteira**: o app baixa, mostra a metadata (título, artista,
duração, dificuldade) e **prepara o karaokê sozinho**: separa a voz do
instrumental com IA, busca a letra sincronizada e alinha a letra pela cantoria.
Depois é só entrar e cantar. (Link de playlist importa todas as músicas de uma
vez; um `mix`/rádio automático do YouTube conta como música única.)

## Pipeline de preparo (automático ao adicionar)

1. **Download/upload** → metadata via mutagen ou yt-dlp
2. **Separação por IA** — modelo MDX-Net (UVR-MDX-NET-Voc_FT) via onnxruntime,
   roda em CPU (~1,7× a duração da música num i7-7700); gera
   `vocals.mp3` + `instrumental.mp3`
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

## Qualidade do sync — o app sabe onde ele errou

Alinhar letra com canto erra, e o que mais custa tempo não é o erro: é **caçar**
o erro cantando a música inteira. Então o app mede a si mesmo e leva você direto
ao ponto.

Quatro réguas, porque cada uma é cega para o que as outras veem:

| régua | o que enxerga | ponto cego |
|---|---|---|
| **nota do ouvido** | deslocamento como um humano sente | desastre isolado diluído na média |
| **linhas perdidas** | a linha impossível de cantar (>0,7s fora) | linha que falta |
| **cobertura** | letra faltando ou amontoada | tempo deslocado |
| **duração** | linha que pisca e some, ou trava na tela | posição da linha |

A **nota do ouvido** não é invenção nossa: são os parâmetros de
[Lizé Masclef, Vaglio & Moussallam (ISMIR 2021)](https://archives.ismir.net/ismir2021/paper/000052.pdf),
calibrados com pessoas julgando sincronia num setup de karaokê — hoje na
`mir_eval` como `karaoke_perceptual_metric`. Dela vêm três coisas que a
intuição erra:

- o ótimo **não é zero**: a letra deve aparecer ~67ms **antes** do canto;
- atrasar dói **muito** mais que adiantar (faixa boa: −170ms a +40ms);
- por isso medir com valor absoluto é cego justo pro que mais incomoda.

### Editor guiado — "confirmar 2 linhas", não "editar a música"

O card mostra **⚠ 4 linhas** em vez de um genérico "revisar sync". Clicando, o
editor **abre já na primeira linha errada**, com o áudio 2s antes dela, e o
botão `⚠ linha 2/4 · próxima` circula pelas outras. As suspeitas ficam marcadas
em âmbar (só no modo edição).

```bat
:: recalcula as réguas de toda a biblioteca (só mede, não realinha)
.venv\Scripts\python.exe server\rescore.py [--dry]
```

## Player

- Voz e instrumental são faixas separadas tocadas em sync perfeito — slider
  **Voz** em 0% = karaokê de verdade; sobe se quiser voz-guia. Slider
  **Instrumental** até 130% com limiter anti-estouro. (A música só abre quando
  o preparo termina; center-cut do original existe apenas como fallback se as
  faixas separadas falharem no carregamento.)
- Letra com destaque progressivo que **segue a cantoria**: a linha acende e
  apaga conforme a frase é cantada, com contagem regressiva (● ● ●) na intro e
  em pausas instrumentais. Ajuste fino ±0,5s no menu ☰ (salvo por música).
- **Pitch lane** — gráfico horizontal abaixo dos controles mostrando as notas do
  cantor original rolando (frase cantada = notas na altura certa; rap falado =
  blocos de ritmo). Com o mic ligado, sua voz vira um rastro colorido por cima
  (verde afinado, âmbar quase, rosa fora).

## Pontuação 🎤 (igual karaokê de verdade)

Botão **🎤 pontuar** no player liga o microfone (cancelamento de eco ativo — mas
fones dão pontuação mais precisa). A cada frase da letra:

- o pitch da sua voz (autocorrelação, ~15x/s) é comparado com a melodia do
  cantor original, com **tolerância de oitava** (homem cantando música de
  mulher pontua normal) e janela de ±350ms pra latência do mic;
- a frase fecha com nota 0-100 e um pop na tela (PERFEITO! / Mandou bem! /
  Boa! / Quase… / Ops…); frases sem canto = "Cadê a voz? 👀";
- no fim: nota geral S/A/B/C/D/E, total de pontos e recorde por música
  (salvo no navegador).

Pontuação usa a melodia extraída da voz original (`pitch.json`) como gabarito.

## Dueto & Duelo (2 jogadores, local) 👥

Botão **👥 dueto & duelo** na biblioteca abre o setup: escolha o modo e dê
nome + emoji pros dois jogadores. Aí é só escolher a música — as frases da letra
se **revezam por verso** entre os dois (cada linha ganha a cor do seu dono), e
você passa o microfone na vez de cada um.

- **⚔️ Duelo** — competição: cada frase pontua pro seu dono, dois placares na
  tela, e no fim sai o **vencedor** (ou empate).
- **🎶 Dueto** — cooperativo: mesma divisão de frases, mas o placar é
  **combinado** — a nota final é de vocês dois juntos.

Um microfone só (passa de mão em mão). Precisa de música preparada (a pontuação
usa a melodia de referência).

## Fila da festa 🎶

No hover do card, o botão **➕** joga a música numa fila. A barra de fila no topo
da biblioteca deixa reordenar/remover e "▶ tocar fila". Quando uma música acaba,
a próxima entra sozinha; com o mic ligado, a tela de resultado ganha um botão
**⏭ próxima da fila**. A fila fica salva no navegador.

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

### Docker (servidor doméstico: Coolify, Portainer, compose)

```bash
docker compose up -d          # ou: docker build -t solta-a-voz . && docker run ...
```

A imagem é CPU-only (torch do índice `cpu`, bem menor que o padrão com CUDA) e
os modelos baixam no primeiro preparo, ficando no volume `/app/data` — então
rebuild não obriga a baixar tudo de novo.

> ⚠️ **NUNCA rode dois servidores sobre a mesma pasta `data/`** — um no host e
> outro no container, por exemplo. O lock de arquivo **não atravessa** a
> fronteira host/container, e as duas escritas concorrentes corrompem o
> `library.json`. Derrube um antes de subir o outro.

## Estrutura

```
server/main.py    — API + worker de preparo (fila em thread única)
server/audit.py   — pente fino do alinhamento (ver abaixo)
static/           — frontend (HTML/CSS/JS puro, Web Audio API)
tests/test_core.py— testes das funções puras do pipeline
data/library.json — biblioteca (metadata + cache de letra + status)
data/media/       — arquivos originais ({id}.ext)
data/stems/{id}/  — vocals.mp3, instrumental.mp3, pitch.json por música
tools/ffmpeg/     — ffmpeg portátil (fora do git)
```

Ferramentas de qualidade (não fazem parte do app; só medem e reprocessam):

```
server/rescore.py        — recalcula as 4 réguas na biblioteca (só mede)
server/align_v2_apply.py — reprocessa músicas com o pipeline atual
server/refetch_lyrics.py — troca letra por outra que o ÁUDIO confirma
server/measure_truth.py  — AAE contra LRC marcado por humano
server/ab_sw.py          — A/B do casador (Smith-Waterman × difflib)
server/ab_skip.py        — A/B do skip de instrumental
```

## Atalhos no player

- **Espaço** play/pause · **Esc** volta ao repertório · **← →** pula 5s

## Manutenção

```bat
:: testes das funções puras (metadata, LRC, dificuldade, alinhamento)
.venv\Scripts\python.exe -m pytest tests -q

:: pente fino do alinhamento de uma música (ou toda a biblioteca sem id)
.venv\Scripts\python.exe server\audit.py [id] [--web]

:: recalcula as réguas de qualidade (só mede, seguro repetir)
.venv\Scripts\python.exe server\rescore.py [--dry]
```

> ⚠️ Scripts do `server/` mexem na biblioteca. Rode sempre com
> `KARAOKE_NO_WORKER=1`, com o python do venv, e **faça backup do
> `data/library.json`** antes de qualquer coisa que escreva.

O `audit.py` mede, frase a frase, quanto do canto real cai dentro de cada janela
da letra e sinaliza problemas (frase esticada, além do fim do áudio, canto sem
frase na letra). Com `--web` cruza a letra com uma fonte externa (lyrics.ovh)
pra apontar versos faltando.

> **Requisito**: Python **3.11 a 3.13**. O 3.14 quebra (o `diffq`, dependência
> do audio-separator, ainda não tem wheel). A imagem Docker usa 3.12.

## Uso responsável

Projeto pessoal, feito pra cantar em casa com músicas que você já tem ou tem
direito de usar. Ele **baixa áudio** (via yt-dlp) e **busca letras** (LRCLIB) que
podem ser material protegido por direitos autorais — a responsabilidade pelo que
você baixa e por respeitar os termos das fontes é sua. Nada é redistribuído: tudo
fica local na sua máquina. Não use para fins comerciais nem para redistribuir
conteúdo de terceiros.

Créditos das ferramentas open source que tornam isso possível:
[yt-dlp](https://github.com/yt-dlp/yt-dlp) ·
[audio-separator](https://github.com/nomadkaraoke/python-audio-separator) (MDX-Net) ·
[stable-ts](https://github.com/jianfch/stable-ts) / OpenAI Whisper ·
[librosa](https://librosa.org/) · [LRCLIB](https://lrclib.net/) ·
[FastAPI](https://fastapi.tiangolo.com/) · [FFmpeg](https://ffmpeg.org/).
