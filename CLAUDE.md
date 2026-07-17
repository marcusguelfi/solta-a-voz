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
      `autoOffset = 0`. Confiança: ≥50% das linhas (mín. 4) com end>start>0, senão
      descarta; as fracas são interpoladas entre as âncoras boas (rap denso).
   b. **Correlação** — testa até 8 versões de letra do LRCLIB (a letra pode ser
      de OUTRA versão da música!) contra a máscara de canto do pyin via
      cross-correlation (±35s); escolhe a de maior cobertura → `autoOffset` global.
   c. **Onset** — primeiro trecho com energia vocal vs 1ª linha do LRC.
   Pós-processamento das lines: `reconcile_with_lrc` (refrão repetido),
   descarte de frases além do fim do áudio, e **`clamp_ends_to_voice`** — corta
   o instrumental preso no FIM de cada frase (termina no fim do 1º trecho
   contínuo de canto, tolera respiro de 2s). Sem isso a frase fica acesa durante
   o interlúdio (o "fora do tempo" do Depeche Mode).
4. WAVs viram mp3 192k (ffmpeg) e são apagados.

### Player (app.js)

- **Modo stems** (música ready): dois `AudioBufferSource` (vocals + instrumental)
  em sync de sample, gains independentes. Voz padrão **0%**. Limiter
  (DynamicsCompressor) na saída.
- **Modo center-cut** (FALLBACK apenas): mid/side ao vivo no MediaElementSource —
  mid highpass 140Hz = "voz" atenuável, graves preservados. Só entra se os stems
  falharem no load; a música normal fica bloqueada até `ready` (guard `isReady`).
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
| POST | /api/link | {url} → yt-dlp (m4a) → fila. **Playlist** (`is_playlist_url`): baixa todas em background, retorna `{playlist, count}`. `list=RD*` = rádio/mix = single |
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
- **Pente fino**: `.venv\Scripts\python.exe server\audit.py [id] [--web]` audita
  o alinhamento por frase. O sinal de saúde é ENERGIA vocal, não duração (o pyin
  capta o synth do instrumental, então `canto%` engana). Flags: GHOST (sem voz),
  FROUXO (instrumental preso na frase = problema), LONGA (janela grande mas cheia
  de voz = legítima), CURTA (>9 palavras/s = cram), FORA, OVERLAP (cosmético),
  DESCOBERTO (canto sem frase na letra). `--web` cruza com lyrics.ovh. Reporta
  também o que o pipeline fez (reconcile/tails/dropped/1ª frase vs onset).
- Whisper se perde em refrão repetido → `reconcile_with_lrc` usa o LRC humano
  (origSynced) como trilho; letra de versão mais longa que o áudio (ao vivo) tem
  as frases além do fim descartadas (droppedBeyondAudio).
- Pitch lane decide POR FRASE: ≥25% de frames afinados = notas (melody); menos
  = blocos de energia na linha central (rhythm, rap falado).
- yt-dlp avisa "No supported JavaScript runtime" — funciona mesmo assim; se o
  YouTube quebrar formatos, instalar deno ou atualizar yt-dlp.
- **Playlist vs rádio**: `watch?v=X&list=RD...&start_radio=1` é mix infinito
  auto-gerado (tratar como single, senão importaria sem parar). `is_playlist_url`
  só considera playlist real (`/playlist`, `/sets/`, ou `list=` não-RD). Import
  em thread daemon separada, teto `MAX_PLAYLIST=50`; download reusa `_download_one`.

## Roadmap (atualizado 2026-07-13 — prioridades do Marcus, ordem sugerida)

### Prioridade 1 — Multiplayer
- ✅ (2026-07-14) **Dueto & Duelo local** (frontend puro). Botão "👥 dueto &
  duelo" na biblioteca → modal de setup (modo + 2 jogadores nome/emoji,
  salvos em localStorage `mp:players`) → sessão "armada" → clicar numa música
  pronta abre em modo mp. As frases se revezam por verso (`assignOwners`,
  gap-based: troca no silêncio > 2,5s ou a cada 6 frases), cada frase pontua
  pro dono via `finalizeLine` (roteia pra `mp.totals[owner]`). Dueto = placar
  combinado + nota; Duelo = dois placares + vencedor (`showMpResults`). Estado
  no objeto `mp`; um mic só (passa entre os dois). Linhas com tint por dono
  (`data-owner` + `--p0`/`--p1`), chip do turno destacado, indicador "🎤 nome".
- Próximo: festa LAN (celular como controle) → duelo online (relay WebSocket).
  Ver detalhes lá embaixo.

### Prioridade 2 — PRECISÃO DAS LETRAS (plano 2026-07-14, pedido do Marcus)

**Problema real** (irmãs/tia baixaram ~70 músicas; 42 com letra): algumas letras
"trocaram totalmente" = o LRCLIB devolveu a letra de OUTRA música. A causa raiz:
`model.align(vocals, texto)` faz *forced alignment* — **encaixa QUALQUER texto** no
áudio, mesmo o errado, gerando timing confiante porém lixo. O audit atual é
energy-driven: mede se há VOZ em cada janela, mas NÃO se as PALAVRAS estão certas
(letra errada forçada sobre canto real passa no audit). Por isso "ficou fora do
tempo" E "letra trocada" convivem com status ready.

**Solução: transcrever-então-verificar** (padrão de sistemas de lyric sync tipo
AudioShake; confirmar fontes quando a web voltar — busca estava 529 nesta sessão):
1. **Transcrever** o stem de voz com Whisper (`model.transcribe`, não `align`) →
   o que está REALMENTE sendo cantado. Cache em `stems/{id}/transcript.json`.
2. Buscar VÁRIOS candidatos de letra (LRCLIB search ≥8 + lyrics.ovh).
3. Pontuar cada candidato por **similaridade fuzzy com a transcrição** (word-recall:
   fração das palavras da letra que aparecem na transcrição — robusto a erros do
   Whisper e reordenação; combinar com sequence ratio pra ordem).
4. Escolher o melhor se score ≥ ~0.45; senão **flag "letra suspeita"** (não força
   letra errada). Wrong song ~<0.25; certa 0.5-0.85 (transcrição de canto é imperfeita).
5. Só então `align` na letra verificada.

**Custo**: +1 passada de transcribe (~2min/música CPU) além do align. Cache deixa
re-run grátis. Transcrever 1×, comparar todos os candidatos é barato (texto).

**Rollout**:
- Músicas novas: pipeline faz transcribe → verify → align.
- 42 existentes: modo batch `audit.py --verify` transcreve cada (cacheia),
  re-pontua a letra atual, flag as suspeitas. Re-buscar+re-alinhar só as flagadas.

**Audit v4**: novo flag **LETRA SUSPEITA** (similaridade transcrição×letra <
limiar) — o sinal de CORREÇÃO que faltava (o energy só via timing). Sinal barato
secundário (sem Whisper extra): capturar a probabilidade média de palavra do
`align` → `lyrics.alignConfidence`; baixa = suspeita.

**Não-só-Whisper**: ranquear candidatos pela similaridade-ASR (não pela duração do
LRCLIB); lyrics.ovh como 2ª fonte (futuro: letras.mus.br/Genius); a própria
transcrição vira letra de último recurso se nenhum candidato bater.

**Pesquisa + MEDIÇÃO de precisão de timing (2026-07-14, "isso é o CORE"):**
- Pesquisa: MFA e WhisperX (wav2vec2 phoneme align) são mais precisos que
  alinhamento por Whisper — sub-100ms vs ~1s de drift no pior caso. torchaudio
  tem `functional.forced_align()` + bundle multilíngue `MMS_FA` (mas depreca em
  2.8+). Fontes: arxiv 2406.19363, whisperX, docs.pytorch.org/audio.
- **MEDIÇÃO (audit timing)**: nosso `stable-ts` no nível de LINHA dá **~20-24ms
  de erro mediano** (João e Maria 22ms, Creep 20ms, Evidências 24ms) quando a
  letra é a CERTA. Ou seja: **não precisamos de wav2vec2/MFA** — a precisão de
  timing já é ótima pra karaokê. O "fora do tempo" era 99% letra ERRADA
  (forced-align de texto errado) + finais esticados (clamp). O gargalo é
  CORREÇÃO da letra, não precisão do alinhamento. Confirmado por medição, não achismo.
- **Metodologia do audit timing**: mede erro início-da-linha × onset-de-frase real
  (energia) SÓ pra linhas que iniciam frase (silêncio antes) e têm onset perto
  (<3s). Sem esse filtro, canto contínuo/rap dá erro-fantasma de dezenas de
  segundos (poucos silêncios → casa com onset distante). Lição: métrica ingênua
  MENTE; medir só o subconjunto verificável. `audit.py` reporta "timing (início
  de N frases): mediana Xms".
- **Verificação pegou caso real**: Placebo "Running Up That Hill (Cover)" =
  similaridade 0.00 = letra totalmente errada (LRCLIB devolveu outra música).
- Refino do review: `lyric_similarity` virou PRECISÃO (fração do CANTO que está na
  letra), não recall — recall puniria música longa certa cuja transcrição cobre
  só os 1ºs 110s. `detected_language` (idioma que o Whisper detecta na transcrição)
  substitui o heurístico guess_language no align (lib tem PT/EN/ES).

**Falsos positivos da verificação (SEMPRE inspecionar suspeita antes de refixar):**
A transcrição do Whisper falha de 4 jeitos que dariam falsa "letra suspeita" —
`transcript_is_reliable` guarda os 4. Numa varredura de 50: das 4 "suspeitas", 2
eram Whisper falhando (falso), 2 reais. **Suspeita 0.00 quase sempre é falso.**
1. **Idioma errado** → lixo não-latino (cingalês). Guard: `_latin_ratio > 0.6`.
   Caso: Placebo "Running Up That Hill" (0.00 → 0.89 com dica de idioma).
2. **Alucinação em loop** em intro/silêncio → "a little bit of a little bit of..."
   Guard: diversidade `únicos/total >= 0.30`. Caso: Vanessa Carlton "A Thousand
   Miles" (intro de piano) → resolvido de vez pelo **onset-clip** (transcrição
   começa no canto, `vocal_start_from_energy`, pula o intro).
3. **Idioma fora de PT/EN/ES** (alemão) → o `base` misdetecta pra "pt" mesmo sem
   hint e cospe salada latina com cirílico/CJK (скоро/かな). Guard: qualquer char
   `>= 0x370` = não confiável. `guess_language` retorna None se incerto (auto-detect).
   Caso: Rammstein "Waidmanns Heil".
4. **Sem stems/transcrição** → não verificável.
   → Sempre dar dica de idioma e checar reliability. Suspeita REAL: música com
   título vago ("Tema de Abertura") que o LRCLIB casa por texto do título com
   outra (Jota Quest "Minha Estrela"). Fix: `refix.py`, ou buscar pela transcrição.
- Verificação usa modelo **"base"** (rápido, identidade não precisa de precisão);
  alinhamento continua "small". `server/refix.py` re-conserta suspeitas reais.
- Log da varredura: `data/scan_log.txt`. audit.py `--verify` transcreve tudo
  (lento no CPU, ~1-2min/música com base).

**Auto-cura de letra incompleta (2026-07-15, "padrão Péricles/Mulher de Fases"):**
Quando o LRC não lista refrões repetidos do final, o align espreme frases e o
final fica sem letra. Pipeline passo 6: `uncovered_sung_regions` (energia fora
de frase, ≥6s) → `transcribe_region_lines` (small + word timestamps, guards) →
`extend_lyrics_with_transcript` insere linhas `auto: True` e o texto completo
vira o trilho (`origSynced`) → realinha. Resultados: Mulher de Fases ganhou o
final (19s cobertos, timing 48ms), Péricles 47/47.
- **Limitação conhecida (próxima sessão)**: linhas VELHAS espremidas/GHOST na
  região onde a transcrição inseriu novas não são removidas — falta dedup
  (remover linha original com energia~0/janela<0.15s/palavra quando há linha
  auto cobrindo o mesmo trecho). Caso: Mulher de Fases 172-173.6s (4 OVERLAPs).
- Rodar manualmente: scratchpad run_extend.py ou pipeline cuida das novas.

Feitos relacionados:
- ✅ (2026-07-13) CANTO DESCOBERTO no audit (energia fora de frase).
- ✅ (2026-07-14) hover mostra recorde por música no card.
- ✅ (2026-07-14) audit mede timing (início×onset) e correção (--verify).
- ✅ (2026-07-14) 3 guards de transcrição + modelo rápido + refix.
- ✅ (2026-07-15) auto-cura (extensão por transcrição) no pipeline.
- ✅ (2026-07-15) biblioteca: busca, ordenação, chips de gênero (campo novo
  `genre` via yt-dlp/tags; PATCH não apaga letra em edição de gênero/álbum).
- ✅ (2026-07-15) prévia no hover (3s) — gotcha: autoplay exige gesto; o 1º
  clique em qualquer lugar destrava o elemento (wav silencioso no pointerdown).

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
4. ✅ (2026-07-14) **Fila da festa** — ➕ no card, barra na biblioteca, auto-avanço.
   Base pro modo festa LAN.
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

## Gotchas de desenvolvimento (workflow — economizam tempo)

- **Screenshot do preview trava** quando o player está tocando (loop
  requestAnimationFrame + canvas do pitch lane). Não insista no screenshot —
  valide via `javascript_tool` (ler estado: `engine.playing`, `lyrLines`,
  `score.ref`, contar pixels do canvas) e `read_console_messages`. Isso é
  autoritativo. Se precisar de screenshot, `enginePause()` + `closePlayer()` antes.
- **PowerShell 5.1 + `git commit -m`**: here-string com parênteses/aspas quebra
  (vira pathspec). Escreva a mensagem num arquivo e use `git commit -F arquivo`.
- **Body JSON via curl no PowerShell**: `Out-File` grava UTF-8 **com BOM** e o
  FastAPI rejeita. Use `[System.IO.File]::WriteAllText(path, json)` (sem BOM) e
  `curl --data-binary "@arquivo"`. Ou PATCH/POST direto de outro jeito.
- **Preview cai entre sessões**: `preview_start` de novo; a `library.json` é lida
  fresca a cada request, então editar o arquivo direto (scripts) reflete no ato.
- **Aplicar fix retroativo sem re-rodar Whisper**: muita coisa opera só sobre
  `lyrics.lines` + `pitch.json` (ex.: clamp de tails). Um script que carrega a
  lib, chama a função e reescreve `synced`/`lines` conserta a biblioteca toda em
  segundos, em vez de ~2,5min de Whisper por música.
- **`javascript_tool` mantém escopo entre chamadas** — não redeclare `const out`
  duas vezes (dá "already declared"); use nomes diferentes ou `var`.
- **Mic é bloqueado no preview pane** (getUserMedia negado). Pra validar
  pontuação/multiplayer sem mic: injete `score.samples` na mão (`{t, midi}`),
  setando `score.enabled=true` e um `score.ref` sintético, e chame `finalizeLine`
  / `showMpResults` direto. A lógica é validável assim; o mic real só funciona
  na máquina do usuário.

## Validação (2026-07-14) — baseline verde

- `pytest tests -q`: **23 testes** (metadata, LRC, dificuldade Fácil/Médio/Expert,
  correlação, reconcile, clamp com/sem energia/nota longa, onset, progresso,
  regroup, interpolate). Rodar antes de entregar mudança no servidor.
- `audit.py` (sem id): 10 músicas, todas ~100% saudáveis (98% quando têm avisos
  legítimos: crams de rap, nota final sumindo).
- API smoke (servidor local): /pitch /stems /audio /lyrics = 200; /cover = 307
  (redirect thumb); Range = 206 + `no-store`; sid/stem inválidos = 404.
- Browser E2E: detectPitch (220Hz→220,3; silêncio→null), tolerância de oitava
  (mesma/oitava=0, 1 semitom=1), fila (chips+auto-avanço), player (stems tocando,
  pitch ref, 30 linhas, lane desenhando ~1550px), zero erros no console.

## Convenções

- UI e comentários em **PT-BR**; sem frameworks JS; sem build step.
- Commits SEM assinatura do Claude (pedido do Marcus).
- Testar mudança de player no preview antes de entregar (ver launch.json).
- Ao adicionar função pura ao pipeline, **adicione um teste** em
  `tests/test_core.py` (importa com `KARAOKE_NO_WORKER=1`, sem modelos pesados).
