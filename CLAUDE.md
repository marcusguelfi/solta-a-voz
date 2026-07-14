# CLAUDE.md — Solta a Voz 🎤

Guia pra qualquer sessão do Claude (ou humano) trabalhar neste projeto sem re-descobrir decisões.

## O que é

Karaokê caseiro self-hosted do Marcus. Fluxo do usuário: **cola um link ou sobe um
arquivo → o app prepara tudo sozinho → entra e canta com pontuação**. Roda 100%
local (Windows 10, i7-7700, GTX 1060, 16GB), servidor na porta **8777**.

## Como rodar

```bat
:: primeira vez — OBRIGATÓRIO Python 3.13 (3.14 quebra o stack de IA: diffq sem wheel)
py -3.13 -m venv .venv
.venv\Scripts\pip install -r requirements.txt "audio-separator[cpu]" stable-ts soundfile numpy

:: sempre
start.bat          :: abre o navegador e sobe o servidor
```

- ffmpeg **portátil** em `tools\ffmpeg\bin` (build essentials do gyan.dev) — o
  servidor injeta no PATH em `server/main.py`. Não dependa de ffmpeg global.
- Modelos de IA baixam sozinhos no primeiro uso pra `data\models` (~50MB MDX,
  ~460MB Whisper small, ~610MB BS-Roformer se usado).
- Dev com preview do Claude Code: config `karaoke` no `.claude/launch.json` do
  projeto pc-control-client (histórico: a sessão nasceu lá).

## Arquitetura

```
server/main.py     FastAPI + worker de pipeline (thread única + queue.Queue)
static/index.html  UI única (biblioteca + player fullscreen)
static/app.js      motor de áudio (Web Audio), letra, pontuação — vanilla JS
static/style.css   tema "palco escuro" (magenta #ff2d78 / âmbar #ffb347)
data/library.json  banco de dados (dict por id, escrita sob threading.Lock)
data/media/        áudio original ({id}.ext)
data/stems/{id}/   vocals.mp3, instrumental.mp3, pitch.json
tools/ffmpeg/      ffmpeg portátil (fora do git)
```

### Pipeline de preparo (worker, 1 música por vez)

Status: `queued → separating → analyzing → aligning → ready` (ou `error`; jobs
interrompidos por restart voltam pra fila no boot).

1. **separating** — audio-separator, modelo `UVR-MDX-NET-Voc_FT.onnx`
   (onnxruntime CPU). ~1,7× a duração da música neste i7. Gera WAVs.
2. **analyzing** — melodia de referência do stem de voz: `librosa.pyin`
   (fmin 65, fmax 1000, sr 16k, hop 512) → `pitch.json {hop, midi[], energy[]}`
   (midi null = sem canto afinado; energy 0/1 = presença de voz, pega rap
   FALADO). Gabarito da pontuação + máscara do alinhamento + modo do pitch lane.
3. **aligning** — cadeia de fallbacks, do melhor pro pior:
   a. **Forced alignment (stable-ts/Whisper "small", CPU)** — `model.align()`
      com `original_split=True` acha início E fim de cada linha CANTADA.
      Resultado em `lyrics.lines = [{t, end, text}]`, `alignMethod: "whisper"`,
      `autoOffset = 0`. Confiança: ≥70% das linhas com end>start>0, senão descarta.
   b. **Correlação** — testa até 8 versões de letra do LRCLIB (a letra pode ser
      de OUTRA versão da música!) contra a máscara de canto do pyin via
      cross-correlation (±35s); escolhe a de maior cobertura → `autoOffset` global.
   c. **Onset** — primeiro trecho com energia vocal vs 1ª linha do LRC.
4. WAVs viram mp3 192k (ffmpeg) e são apagados.

### Player (app.js)

- **Modo stems** (música ready): dois `AudioBufferSource` (vocals + instrumental)
  em sync de sample, gains independentes. Voz padrão **0%**. Limiter
  (DynamicsCompressor) na saída.
- **Modo center-cut** (música ainda processando): mid/side ao vivo no
  MediaElementSource — mid highpass 140Hz = "voz" atenuável, graves preservados.
  Dá pra cantar enquanto a IA trabalha.
- Sliders com memória por modo no localStorage (`mix:vocal:stems` etc.).
- **Letra**: usa `lyrics.lines` (com fim de frase!) quando existe; senão parse LRC.
  `lyricTime() = getTime() - autoOffset + manualOffset + LYRIC_LEAD(0.45s)` —
  o lead acende a linha um pouco ANTES do canto, como karaokê comercial.
  Preenchimento da linha vai até `line.end` (não até a próxima linha!) — em pausa
  instrumental a letra ESPERA com contagem regressiva (● ● ●).
- Ajuste manual ±0,5s por música (localStorage `lyroff:{id}`).

### Pontuação (estilo SingStar)

- Mic: getUserMedia com echoCancellation → AnalyserNode (NÃO conecta na saída).
- Pitch por autocorrelação (~15x/s, gate RMS 0.012, confiança 0.3) em app.js.
- Compara com `pitch.json` com **tolerância de oitava** (fold mod 12 pra ±6) e
  janela de ±350ms (latência do mic). Frase fecha nota 0-100 (finalize 650ms
  depois da virada, pra colher as últimas amostras).
- Nota final S(93%)/A(82%)/B(68%)/C(50%)/D(30%)/E + recorde em localStorage.
- Pontuação SÓ no modo stems (precisa do gabarito de melodia).

## API (resumo)

| Método | Rota | Função |
|---|---|---|
| POST | /api/upload | multipart de áudio → entra na fila |
| POST | /api/link | {url} → yt-dlp (m4a, sem ffmpeg) → fila |
| GET | /api/songs | biblioteca ordenada por addedAt desc |
| PATCH/DELETE | /api/songs/{id} | edita metadata / remove tudo |
| POST | /api/process/{id} | re-enfileira preparo |
| POST | /api/realign/{id} | só realinha letra (Whisper→correlação) |
| GET | /api/lyrics/{id}?artist=&title= | busca LRCLIB (override re-alinha) |
| GET | /api/audio/{id}, /api/stems/{id}/{vocals\|instrumental} | áudio com Range |
| GET | /api/pitch/{id} | melodia de referência |
| GET | /api/cover/{id} | capa embutida ou thumb do YouTube |

## Regras e lições aprendidas (NÃO re-descobrir)

- **Só música de ESTÚDIO** — regra do Marcus. Ao vivo tem plateia/reverb que
  estragam separação e alinhamento. Ao buscar por link, preferir áudio oficial
  de álbum / canal Topic / "Remastered". Há dica disso na UI.
- **Offset global de letra NÃO basta** — o LRCLIB pode devolver letra de outra
  versão; a única solução robusta é forced alignment (foi a maior reclamação
  do Marcus: "a letra tem que seguir a cantoria, não a música").
- **Python 3.14 não serve** (diffq/audio-separator sem wheels). Ficar no 3.13.
- **Estáticos com cache**: middleware manda `Cache-Control: no-cache` em tudo
  que não é /api. Clientes antigos podem precisar de UM Ctrl+F5.
- Stems servem com `Cache-Control: no-store` (podem ser regravados por modelo melhor).
- **BS-Roformer** (`model_bs_roformer_ep_317_sdr_12.9755.ckpt`): instrumental
  muito melhor que MDX em produção densa, mas ~15× a duração da música em CPU —
  só faz sentido com GPU ou paciência. torch já está no venv.
- library.json: TODA escrita via `_update_entry`/`_add_entry` (lock). Leituras
  são unlocked (transientes toleráveis).
- Screenshot/aba do preview pode travar com blur pesado — os holofotes usam
  radial-gradient, não `filter: blur()`. Manter assim.
- **Pente fino**: `.venv\Scripts\python.exe server\audit.py [id]` audita o
  alinhamento por frase (canto%, energia%, GHOST/STRETCH/FORA/OVERLAP). Rodar
  depois de mexer no alinhamento.
- Whisper se perde em refrão repetido → `reconcile_with_lrc` usa o LRC humano
  (origSynced) como trilho; letra de versão mais longa que o áudio (ao vivo) tem
  as frases além do fim descartadas (droppedBeyondAudio).
- Pitch lane decide POR FRASE: ≥25% de frames afinados = notas (melody); menos
  = blocos de energia na linha central (rhythm, rap falado).
- yt-dlp avisa "No supported JavaScript runtime" — funciona mesmo assim; se o
  YouTube quebrar formatos, instalar deno ou atualizar yt-dlp.

## Roadmap (atualizado 2026-07-13 — prioridades do Marcus, ordem sugerida)

### Prioridade 1 — Multiplayer (ver detalhes abaixo)
Começar pelo **duelo local por revezamento** (~1 sessão): perfis nome+emoji,
A canta → B canta → placar comparativo; depois frases alternadas (mic passa-passa
usando as janelas do forced alignment). Recordes saem do localStorage pro
library.json. Depois: festa LAN → duelo online.

### Prioridade 2 — Audit/alinhamento ainda mais robusto
- ✅ (2026-07-13) detectar CANTO DESCOBERTO: energia vocal fora de qualquer
  janela de frase (adlibs, vocalize, tail de versão diferente) — audit reporta.
- Próximo: pro trecho descoberto, TRANSCREVER com Whisper e sugerir linhas
  extras de letra (aprovação manual) — fecha os buracos do gráfico de tom.
- Comparar com múltiplas fontes web (lyrics.ovh ✅, letras.mus.br/Vagalume) e
  escolher o texto mais completo antes de alinhar.

### Prioridade 3 — UI do player
- ✅ (2026-07-13) título realmente centralizado (grid 1fr/auto/1fr).
- ✅ (2026-07-13) ajuste ⏱ da letra saiu da barra pro menu ☰; lane maior.
- ✅ (2026-07-13) música indisponível até o preparo completo (sem modo rápido).
- ✅ (2026-07-14) sliders Voz/Instrumental na linha do transporte (sem caixa
  própria) — lane com ainda mais espaço.
- ✅ (2026-07-14) **fila da festa**: ➕ no card, barra de fila na biblioteca
  (localStorage), auto-avanço no fim da música (sem mic) e botão "⏭ próxima
  da fila" na tela de resultado (com mic).
- Próximo: modo telão/fullscreen (F11 + fonte maior), nome das notas no lane.

### Recomendações do Claude (próximos passos que valem a pena)
1. **Pontuação de ritmo pra rap** — hoje frases faladas não pontuam (gate de
   melodia); comparar onsets de energia do mic vs referência na janela da frase
   → Raplord vira jogo de flow. Esforço médio, ganho alto.
2. **GPU** — onnxruntime-directml na GTX 1060: preparo de ~8min pra ~1-2min.
   Testar `pip install onnxruntime-directml` + provider em audio-separator.
3. **Editor fino por frase** — no player, segurar numa linha abre mini-editor
   de início/fim (arrastar no lane); salva em lyrics.lines. Mata qualquer
   resíduo de dessincronia sem depender de IA.
4. **Fila da festa** — lista "próximas músicas" tocável em sequência; base
   pro modo festa LAN.
5. **Backup/restore** — exportar/importar data/ zipado (a biblioteca é
   trabalho de horas de CPU; merece backup fácil).
6. ✅ (2026-07-14) **Testes** — `tests/test_core.py` cobre as funções puras
   (metadata, LRC, dificuldade, correlação, reconcile, onset). Rodar:
   `.venv\Scripts\python.exe -m pytest tests -q` (main.py importa limpo com
   env KARAOKE_NO_WORKER=1). Falta: teste e2e do pipeline com WAV sintético.

### Multiplayer (planejado em 2026-07-12, ordem de ataque 1→2→3)

1. **Duelo local (revezamento)** — perfis de jogador (nome+emoji), modos:
   (a) um canta a música inteira de cada vez, placar comparativo no final;
   (b) "mic passa-passa": frases alternadas com dono por frase (usar as janelas
   do forced alignment). Mover recordes do localStorage pro library.json.
   Sem infra nova. ~1 sessão.
2. **Modo festa LAN** — celular como CONTROLE via QR code (fila de músicas,
   votação, placar da noite); canto continua no mic do PC. Requer: uvicorn em
   0.0.0.0 + regra de firewall + WebSocket do FastAPI pra fila/placar.
   ⚠️ mic no celular NÃO funciona em HTTP de rede local (getUserMedia exige
   secure context; localhost é exceção, IP LAN não) — precisaria mkcert.
   ~2-3 sessões.
3. **Duelo online** — cada jogador roda o app local; mini-relay WebSocket na
   nuvem (~100 linhas, host grátis) sincroniza sala: código de convite, os dois
   adicionam a mesma música por link (só JSON de placar trafega, nunca áudio),
   ready-check + contagem, placar ao vivo por frase. Latência irrelevante
   (áudio local). ~3-4 sessões.

## Convenções

- UI e comentários em **PT-BR**; sem frameworks JS; sem build step.
- Commits SEM assinatura do Claude (pedido do Marcus).
- Testar mudança de player no preview antes de entregar (ver launch.json).
