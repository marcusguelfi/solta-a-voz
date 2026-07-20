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

## ROADMAP (reorganizado 2026-07-18 — fases definidas pelo Marcus)

### FASE 1 — O CORE: música entra, sync sai PERFEITO (prioridade absoluta)
O produto é: colocar música → auto-ajuste da letra do jeito mais eficiente
possível. "2% de erro em 500 músicas já é muita coisa — frustra, e isso eu não
quero pro app."
1. **Pesquisa AMPLA de sincronização** — 1ª rodada feita 2026-07-18:
   - **UltraStar .txt** (padrão da cena SingStar caseira): sílaba-a-sílaba com
     pitch POR NOTA, tempo em BEATS numa grade de BPM (×4, resolução) + GAP em
     ms até o beat 0. Autoria MANUAL é o padrão da comunidade (usdb.eu tem
     milhares de arquivos feitos à mão). Técnica: grade de beats quantiza o
     timing — soa "no ritmo" mesmo com pequenos erros.
   - **UltraSinger** (rakuri255, projeto irmão): whisper + pitch → UltraStar
     txt automático. Técnica-chave: **quantização do pitch à TONALIDADE
     detectada da música** — remove slides/transições vocais e corrige erros
     do detector. Direto aplicável ao nosso lane/pontuação.
   - **UltrastarCreatorTool** (retotito): separação + WhisperX + pitch + EDITOR
     piano-roll completo como etapa final. 3ª confirmação independente:
     auto-pipeline + editor humano É o padrão da indústria/cena.
   - Derivações pro nosso pipeline:
     a) ✅ (2026-07-18) **Realce palavra-a-palavra**: whisper_align_lines
        guarda `words: [[dt, dEnd, palavra], ...]` POR LINHA, **relativos ao
        início da linha** — sobrevivem de graça a reconcile/offset/editor
        (o put_lines reanexa por texto). reconcile_with_lrc REMOVE words de
        linha que voltou pro trilho (tempo não veio do canto). No front,
        fillPercent() anda pelo comprimento em caracteres de cada palavra;
        sem words (letra antiga/interpolada/auto) cai pra linear. Medido em
        João e Maria: 34% vs 15% linear em 0,2s — segue a cadência real.
        Só músicas (re)alinhadas daqui pra frente ganham words.
     b) ✅ (2026-07-18) **Quantização à la UltraSinger no lane** (parcial):
        a nota EXIBIDA de cada segmento é Math.round(média) — semitom mais
        próximo, lane limpo sem vibrato serrilhando. SÓ desenho; pontuação
        segue no midi cru. Falta a versão completa: detectar TONALIDADE
        (librosa) e snap à escala, aí sim vale levar pra pontuação.
     c) ❌ **Snap à grade de beats — REPROVADO EMPIRICAMENTE (2026-07-18)**:
        experimento a pedido do Marcus (scratchpad beat_snap_test.py): snap do
        início de linha à grade de MEIO-beat (librosa beat_track, tol 120ms)
        vs onset real de frase. Resultado unânime — PIOROU nas 3: João e Maria
        22→72ms, Creep 20→54ms, Chop Suey 40→77ms. Canto real não começa na
        grade (anacruse/síncope); o whisper cru é 2-3× melhor. CASO ENCERRADO
        com números — não redescobrir essa ideia.
   - Falta pesquisar: KaraFun/CDG (formatos comerciais), Musixmatch sync
     (crowdsourcing por tap), apps mobile (Smule) — 2ª rodada.
2. **Melhor fonte + junção de letras**: multi-fonte com ranking pela
   transcrição (LRCLIB ✅, letras.mus.br ✅ 2026-07-18, lyrics.ovh ✅; falta
   Genius/Musixmatch e MESCLAR fontes — pegar estrofe que falta numa da outra).
3. **ALIGN v2 (especificado 2026-07-19 pelos casos reais — PRÓXIMA SESSÃO):**
   três frentes, cada uma mata uma classe de erro comprovada:
   a) **Anchor-matching por linha** (nomadkaraoke): casar transcrição×letra
      por linha e reancorar só a errada. Mata: off-by-one (Epitáfio), refrão
      repetido escorregando (plain-only sem trilho).
   b) **Alinhador CTC treinado pra CANTO** no lugar/apoio do whisper nos
      casos difíceis: torchaudio `forced_align` + bundle MMS_FA (wav2vec2),
      e referência acadêmica NUS AutoLyrixAlign (Gupta et al. — alinhamento
      de LETRA A CANTO, treinado em canto, DALI dataset). CTC tem token
      "blank" que ABSORVE duração → melisma ("Quaaaando" do Samba Morrer,
      "Saaaai" do Samurai) alinha nativamente, onde o modelo de palavras do
      whisper desiste e PULA TRECHOS. Whisper fica pra transcrição/identidade.
   c) **Máscara de FALA no stem de voz** contra instrumento vazado: a GAITA
      do Stevie (Samurai) cai no stem de voz e engana TODAS as regras de
      energia (ghost/clamp/extensão/onset). Usar no_speech_prob dos segmentos
      do whisper (já transcrevemos!) pra zerar energia em regiões sem fala
      cantada — gaita/solo deixam de contar como canto.
   Casos de teste: Samurai (gaita+melisma), Não Deixe o Samba Morrer
   (melisma+pulos), Epitáfio (off-by-one), Take Me Out (tempo change).
   I Have a Dream = controle (ficou perfeita — não regredir!).

   **PESQUISA VALIDADA (2026-07-19, antes de codar — pedido do Marcus):**
   - **AutoLyrixAlign (NUS/Gupta)** — campeão do MIREX 2019 em alinhamento
     letra↔áudio POLIFÔNICO (github chitralekha18/AutoLyrixAlign). Kaldi +
     Singularity, pesado, treinado em inglês → serve de BENCHMARK (rodável
     no servidor doméstico via Docker), não de motor pro acervo BR.
   - **lyrics-aligner (schufo)** — PyTorch/MIT, alinha+separa junto, MAS
     fonemas ARPAbet = SÓ INGLÊS. Descartado como principal.
   - **torchaudio forced_align + MMS_FA** — CTC multilíngue (1100+ línguas,
     PT incluso), API estável, pacote pronto (MahmoudAshraf97/
     ctc-forced-aligner). Blank do CTC absorve duração → melisma alinha por
     construção. Treinado em FALA; literatura mostra wav2vec2 transferindo
     bem pra canto (Ou et al. 2022, transfer learning p/ lyric transcription).
     → O CANDIDATO. Regra: A/B contra o whisper com o audit (timing_errors
     × onsets) nos 6 casos + controle ANTES de adotar.
   - **Vazamento de instrumento** — classe conhecida na literatura de
     singing voice detection: "instrumentos de pitch contínuo geram falsos
     positivos de voz" (Lehner et al. ICASSP 2014). Nossa versão barata e
     LINGUÍSTICA (não só energia): no_speech_prob dos segmentos do whisper
     (a transcrição já existe!) vira máscara de fala-cantada sobre energy.
   **ORDEM DE EXECUÇÃO**: (A) máscara de fala + suíte de regressão dos 7
   casos (mais barato, mata a classe Samurai); (B) MMS_FA lado a lado com
   whisper, adoção por métrica (fallback nas linhas que o whisper pulou/
   esmagou, ou motor titular se vencer geral); (C) anchor-matching por
   linha usando a transcrição pra resgatar SÓ as linhas discordantes.
4. **Gráfico de tom preciso pra CANTORIA DO USUÁRIO**: revisar pipeline do mic
   (autocorrelação atual) — captar com precisão o que a pessoa canta; latência,
   oitava, vibrato. O lane é o feedback central do jogo.
5. **Dedup pós-extensão** (Mulher de Fases 172s: OVERLAPs de linha velha
   espremida onde a transcrição inseriu novas).

### FASE 2 — UX/Front (destravada em 2026-07-18)
- ✅ (2026-07-18) Card: ações na base da capa — ▶ centro, ➕ ✕ lados.
- ✅ (2026-07-18) Modal de exclusão estilizado (fim do confirm() nativo).
- ✅ (2026-07-18) Gavetas de gênero estilo Steam (prateleiras horizontais).
- ✅ (2026-07-18) **Arrasto com o mouse nas gavetas** (makeDragScroll) + botão
  "▦ todos juntos"/"🗂 por gênero" (libFilter.view, localStorage cfg:libView;
  default = "todos juntos" desde 2026-07-18, pedido do Marcus).
  Gotchas do drag: (1) clique pós-arrasto engolido em CAPTURE senão abre
  música sem querer; (2) scroll-snap desligado durante o arrasto (classe
  .dragging) senão pula; (3) -webkit-user-drag:none nas capas senão o drag
  nativo de <img> rouba o gesto; (4) setPointerCapture pra seguir fora da row.
- ✅ (2026-07-18) **Editor humano de linhas** no player (☰ → editar tempos):
  clicar linha → Enter marca início na hora, nudges ±0,1s, ＋linha pra intro
  perdida (caso Toxicity sussurro), salvar → PUT /api/lines (alignMethod
  "manual", autoOffset zerado). O human-in-the-loop dos 2% restantes.
- ✅ (2026-07-18) Prévia default 15%.
- ✅ (2026-07-18) **Aviso de sync não verificado**: pill "⚠ revisar sync" no
  card (alignMethod fora de whisper/manual) + botão no player que abre o
  editor direto; "✍ letra sua" quando editada à mão.
- ✅ (2026-07-18) **Sidebar recolhível** (#sb-toggle fixo, body.sb-collapsed,
  estado em localStorage cfg:sbCollapsed; some no mobile ≤760px).
- ✅ (2026-07-18) **Temas de cores** nas configurações: palco (padrão), neon,
  esmeralda, brasa — :root[data-theme] sobrescreve as vars; localStorage
  cfg:theme. Limitação v1: bordas hardcoded (#2b1f3d etc.) não mudam.
- **Tirar emojis da UI** → ícones SVG que mudam de cor e combinam com a proposta.
- Revisão geral de responsividade (outras dimensões) e das opções do menu.
- Modo telão/fullscreen; nome das notas no lane.

### FASE FUTURA (ordem provável)
1. **Aviso de letra não-sincada** no card/player → atalho pro editor humano.
2. **Fila de processamento estilo Steam** (como a lista de downloads): ver a
   fila fora do repertório, reordenar (escolher a próxima), pausar, cancelar.
3. **Login/usuários distintos** (recordes por pessoa; base pro online).
4. Festa LAN (celular como controle via QR; gotcha mkcert) → duelo online
   (relay WebSocket) — detalhes na seção Multiplayer abaixo.
5. Pontuação de ritmo pra rap; GPU DirectML; backup/restore da data/.
6. **Disponibilizar na internet** (Coolify/Portainer — Dockerfile ✅
   2026-07-18; exposição pública fica pra MUITO depois, palavras do Marcus).

### Multiplayer local (feito) — referência
- ✅ (2026-07-14) **Dueto & Duelo local** (frontend puro): modal de setup,
  frases revezadas por verso (`assignOwners` gap-based), pontos por dono
  (`finalizeLine` → `mp.totals`), placar combinado (dueto) ou vencedor (duelo).
  Estado no objeto `mp`; um mic só. Linhas com tint por dono.

### REGRA DE OURO do sync (2026-07-15) + pesquisa de referência

**Regra de ouro**: linha sem canto real embaixo NÃO aparece. `drop_ghost_lines`
remove linha com energia ~0 na janela (banter ao vivo sobre intro de estúdio,
interpolação sobre silêncio). Sintomas que ela cura: "letra passando devagarinho
do nada", "preocupado em terminar no tempo da música e não do canto" (In The
End, Chop Suey, Gotye). A interpolação de linhas não-ancoradas é PERIGOSA — só
sobrevive se houver canto embaixo.

**Pesquisa (nomadkaraoke/python-lyrics-transcriber — projeto irmão)**: eles usam
(1) **anchor sequences**: n-grams da transcrição casados com as fontes de letra
pra corrigir POR LINHA (não score global); (2) múltiplas fontes (Genius, Spotify,
Musixmatch) além de LRCLIB; (3) **review humano em web UI** como etapa final —
conclusão deles: "nenhuma tooling faz isso bem consistentemente" sem humano.
Upgrades derivados pro nosso audit/pipeline:
- v5 do audit: anchor matching por linha → apontar A LINHA errada, não só a música.
- Editor de linha no player (arrastar início/fim) = nosso human-in-the-loop.
- Fontes extra de letra (Genius etc.) quando LRCLIB falha (caso "A Viagem").

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

## ‼️ INCIDENTE 2026-07-17: library.json zerada (e a recuperação)

Dois processos gravando ao mesmo tempo (batch_fix + genre_fill via PATCH +
servidor) **zeraram o arquivo inteiro** (NTFS alocou 1,38MB sem gravar os dados).
NUNCA rode dois escritores sem as proteções abaixo (agora no código):
- `_save_lib` ATÔMICA: tmp + fsync + os.replace, mantendo `library.json.bak`.
- `_cross_process_lock` (msvcrt em data/library.lock) em TODA escrita.
- `_load_lib` cai pro .bak se o principal corromper.
Recuperação que funcionou (rebuild_library.py): genre_fill_log tinha os 164
nomes NA ORDEM do índice (addedAt desc) e o mtime dos arquivos de media segue a
mesma ordem → casamento id↔nome validado com 12/12 âncoras conhecidas. Letras
(só existiam no índice) repostas por relyrics.py (busca+alinha as com stems).
Lição: log com nomes salvou tudo — logs verbosos são backup acidental. Backup
REAL da data/ segue no roadmap (agora com prioridade máxima).

## ALIGN v2 — EXECUÇÃO (2026-07-19)

Plano: `ALIGN_V2_HANDOFF.md`. Ferramentas novas: `server/measure_align.py`
(métrica oficial, grava data/align_metrics.json por tag), `server/ab_align.py`
(A/B de motores, não escreve na biblioteca), `server/realign_batch.py`.

### FASE A ✅ — máscara de fala cantada (`speech_map` + `sung_energy`)

Implementação: `speech_map(sid)` transcreve a MÚSICA INTEIRA com o modelo
rápido e persiste `[[a, b, no_speech_prob]]` em `stems/{id}/speechmap.json`
(DESVIO do handoff, deliberado: `transcribe_vocals` só cobre ~110s a partir
do onset — máscara parcial não pegaria solo no meio/fim, que é justo o caso
Samurai). `_speech_mask` monta o vetor; `sung_active`/`sung_energy` aplicam.
Consumidores trocados: `drop_ghost_lines`, `clamp_ends_to_voice`,
`uncovered_sung_regions` e o audit. **Fora da janela inspecionada NÃO julga**
(mantém energia crua) e **descarta a máscara se ela apagaria >92% da energia**
(transcrição furada não pode apagar a música). `vocal_start_from_energy` fica
na energia crua de propósito: ele só escolhe onde começar o clipe da
transcrição — depender do mapa seria circular.

**O que a máscara revelou** (mediana do erro linha×onset, régua bruta → régua
de fala; `frames_mascarados` = quanto do "canto" era instrumento):
| música | bruta | fala | mascarado | leitura |
|---|---|---|---|---|
| Samurai | 52ms | **79ms** | 33,6% | 156s de 288s NÃO era voz: a régua bruta premiava âncora na GAITA |
| Whisky a Go-Go | (cego: 2 onsets) | **2398ms** | 17,1% | energia saturada escondia 2,4s de atraso — só a máscara enxergou |
| Take Me Out | 532ms | 574ms | 25,9% | tempo change (fase C) |
| Vamos Fugir | 38ms | 37ms | 13,5% | ok |
| Samba Morrer | 24ms | 26ms | 5,3% | problema é melisma, não vazamento |
| Epitáfio | 28ms | 28ms | 0,0% | sem vazamento — é off-by-one (fase C) |
| **I Have a Dream (controle)** | 36ms | **36ms** | 0,5% | **intacto ✅** |

Lição: a régua antiga MENTIA a favor (instrumento vazado conta como onset
válido). A máscara não conserta o alinhamento sozinha — ela impede que
ghost/clamp/extensão ancorem em instrumento e torna a medição honesta.

### FASE B ✅ — motor HÍBRIDO whisper + CTC (decidido pelo A/B, não por gosto)

`server/ab_align.py`, mesmo texto-base, régua de fala (mediana ms; entre
parênteses quantas linhas ficaram verificáveis):

| música | whisper | MMS/CTC | vence |
|---|---|---|---|
| Samurai (gaita+melisma) | 48 (8) | **22 (13)** | CTC |
| Whisky a Go-Go | 1804 (1) | **1118 (3)** | CTC |
| Take Me Out (tempo change) | 788 (8) | **492 (3)** | CTC |
| Vamos Fugir | 32 (27) | 32 (34) | empate |
| Não Deixe o Samba Morrer | **26 (34)** | 36 (31) | whisper |
| Epitáfio | **28 (25)** | 38 (20) | whisper |
| **I Have a Dream (controle)** | **34 (22)** | 112 (19) | whisper |

**Placar 3×3×1 → o CTC NÃO vira titular** (regra do handoff: precisava de ≥5
sem piorar o controle; ele piorou o controle 34→112ms). Mas o padrão é
exatamente o que a teoria previa: **whisper é melhor quando consegue se
ancorar; o CTC é melhor quando o whisper DESISTE** (melisma, andamento
variável). Daí `hybrid_align_lines` (motor padrão, `engine="hibrido"`):
whisper alinha; `suspect_line_idx` marca as linhas que ele interpolou
(`interp`) ou esmagou (duração < 0,18s × nº de palavras = melisma pulado); só
essas recebem o tempo do CTC — e **só se couberem entre as vizinhas
confiáveis** (as âncoras firmes do whisper viram trilho, mesma ideia do
reconcile). Se poucas linhas são suspeitas (<8%), o CTC nem roda (economia).
Linha trocada fica marcada com `ctc: True`.

Custo: MMS_FA baixa 1,18GB na 1ª vez; ~2-4min/música em CPU quando aciona.

### FASE C ✅ — anchor-matching por linha + as CICATRIZES da execução

`anchor_fix_lines`: compara o TEXTO da linha com o que foi realmente cantado
(words.json) e reancora só as deslocadas. `full_transcribe` faz UMA passada
que alimenta máscara (A) e âncoras (C).

**‼️ A primeira versão DESTRUIU o controle** (I Have a Dream 36ms → 615ms,
29 → 19 linhas) e o teste de regressão pegou. Foi o melhor momento da
execução: o controle existe pra isso. Causas e travas (todas com teste):
1. **Comparação por caractere inflava a nota** ("ter chorado mais" × "ter
   amado mais" = 0,7 em caractere, 0,5 em palavra) → passou a comparar
   PALAVRA.
2. **Movia pro casamento de maior nota em ±25s** → em refrão repetido isso é
   a repetição errada. Agora: entre os de nota MÁXIMA, o MAIS PRÓXIMO.
3. **Não verificava se o lugar atual já estava certo** → agora só mexe se o
   que é cantado COMEÇANDO ali discorda (nota < 0,6), ancorado no início
   (janela solta vazava pra frase seguinte e dizia "está tudo bem").
4. **Sem teto** → mexer em >25% das linhas só passa se o conserto for
   COERENTE (mesmo sentido + destinos crescentes = assinatura do off-by-one);
   destinos embaralhados = não mexe em nada.
5. **Ordem checada contra a posição ANTIGA das vizinhas** → num deslocamento
   global todas se bloqueavam; agora checa contra as posições FINAIS.
6. `drop_ghost_lines` ganhou **teto de 25%** (com a máscara a energia ficou
   mais esparsa e a regra de ouro comeu 20 das 61 linhas de Vamos Fugir).

**‼️ BUG ESTRUTURAL achado no meio (o mais importante da sessão)**:
`extend_lyrics_with_transcript` sobrescreve `origSynced` com o texto
estendido (precisa, senão o próximo align desfaz a extensão) — então **uma
rodada ruim envenena o trilho PARA SEMPRE** e todo realinhamento futuro
parte do texto corrompido. Foi por isso que a 2ª rodada não voltou ao
baseline: reconstruía sobre escombros. Agora existe `pristineSynced` (fonte
humana original, gravada uma vez, nunca sobrescrita), `reset_to_pristine()` e
a flag `--fresh` do `align_v2_apply.py`. **Qualquer experimento de
alinhamento daqui pra frente TEM que rodar com --fresh**, senão mede ruído
acumulado.

Prova (controle, trilho limpo): whisper 40ms × híbrido 40ms — **idênticos**
(o híbrido nem aciona o CTC nessa música), contra 36ms do baseline: dentro
da tolerância. O híbrido não era o culpado; o trilho envenenado era.

### RESULTADO FINAL do ALIGN v2 (2026-07-19) — honesto

Régua de fala, trilho limpo (`--fresh`), motor híbrido, mediana do erro
linha×onset (e nº de linhas — perder letra é regressão mesmo com número bom):

| música | baseline | final | linhas | veredito |
|---|---|---|---|---|
| Whisky a Go-Go | 2398ms | **382ms** | 46 → 45 | **6× melhor** ✅ |
| Take Me Out | 574ms | 574ms | 33 → 33 | empate |
| Vamos Fugir | 37ms | 42ms | 61 → 61 | empate |
| Epitáfio | 28ms | 32ms | 33 → 33 | empate (4 linhas reancoradas) |
| Samba Morrer | 26ms | 34ms | 70 → 67 | empate (4 linhas do CTC) |
| Samurai | 79ms | 95ms | 39 → 35 | empate (4 do CTC, +9 extensão) |
| **I Have a Dream (controle)** | 36ms | 40ms | 29 → 29 | **intacto** ✅ |

**Leitura honesta**: 1 vitória grande, 6 empates dentro do ruído. O ganho da
v2 NÃO está na mediana das músicas que já estavam boas — está em (a) o pior
caso da biblioteca cair 6×, (b) a métrica passar a ser honesta (a régua
antiga premiava âncora em gaita), (c) o pipeline não ancorar mais em
instrumento daqui pra frente, (d) off-by-one virar detectável e corrigível
(Epitáfio: 4 linhas reancoradas sozinhas).

**Regra de projeto que saiu daqui (vale pra tudo)**: *ação destrutiva usa o
sinal conservador; ação de posicionamento usa o sinal preciso.* A máscara de
fala é precisa mas incompleta (backing vocal e sussurro escapam), então ela
posiciona (clamp/extensão/onset) e NUNCA apaga. `drop_ghost_lines` voltou pra
energia crua + teto de 25%.

**Só vale pra músicas (re)processadas**: máscara e âncoras dependem de
`speechmap.json`/`words.json`, que o pipeline gera no passo 4.5. Acervo
antigo continua como estava até passar por `align_v2_apply.py --fresh`.

### ALIGN v3 — EXECUTADO (fases 0 e 3) — estado final 2026-07-19

Motor `auto` (padrão): tenta o alinhamento GLOBAL (âncoras+gaps, não roda
modelo nenhum) e o HÍBRIDO, e escolhe por música pela **testemunha
independente** `onset_error_median` (energia do áudio). Selo `-suspeito`
quando a concordância < 0,65 → card mostra "⚠ revisar sync".

| música | baseline v2 | FINAL | nota |
|---|---|---|---|
| Whisky a Go-Go | 2398ms | **316ms** | 7,6× melhor |
| Epitáfio | 28ms / 33 linhas | 30ms / **35 linhas** | **1ª frase de volta** |
| I Have a Dream (controle) | 36ms | 40ms | intacto |
| Samba Morrer | 26ms | 32ms | empate |
| Vamos Fugir | 37ms | 39ms | empate |
| Samurai | 79ms | 98ms | empate (concordância 0,48 — segue marcada) |
| Take Me Out | 574ms | 574ms | empate (ASR no teto: 0,833) |

**As DUAS réguas e o que cada uma vê** (usar sempre as duas):
- `onset_error_median` (energia) — **independente do ASR**, é quem julga
  motor/timing. Cega em canto contínuo (vê ~58% das linhas).
- `alignment_agreement` (texto × canto transcrito) — vê letra errada e
  off-by-one, cobre quase tudo. **AUTO-REFERENTE para o motor global**: não
  serve pra julgá-lo. Vira o selo de qualidade da música.
- `agreement_ceiling` — separa erro de ASR de erro de alinhamento. Tetos
  medidos: 0,83–0,996 (o ASR está melhor do que eu supunha; o buraco é
  alinhamento).

### ALIGN v4 (a) ✅ — casador LOCAL com custo (Smith-Waterman)

`local_align_words()` substituiu o `difflib` dentro do motor global. O difflib
(Ratcliff-Obershelp) casa "o que der": sem penalidade de gap nem de divergência.
Smith-Waterman pontua — match ganha, buraco e divergência PAGAM — então acha
ilhas de confiança e **não força o resto** (o resto vira gap interpolado, que é
o comportamento honesto). Recursão nos retângulos antes/depois da melhor ilha
mantém a monotonicidade e cobre a música inteira.

`_sim_palavra()`: só ancora palavra **idêntica** se for curta (<5 letras).
'de'/'da'/'eu' casam em qualquer lugar e produzem âncora falsa — mesma família
do erro do anchor v1. Flexão ('coracao'/'coracoes', ≥0,8) ainda ancora.

**Medido nos 7 casos** (`server/ab_sw.py`, grava `data/ab_sw.json`):

| música | âncoras difflib → SW | onset pareado | acordo |
|---|---|---|---|
| Samurai | 118 → **136** | 185 → 204ms (n=4) | 0,562 → 0,802 |
| Samba Morrer | 242 → **253** | 186 → 186ms | 0,865 = |
| Epitáfio | 149 → 149 | 302 → 302ms | 0,895 = |
| Whisky a Go-Go | 145 → **166** | 4 → 10ms | 0,600 → 0,664 |
| Vamos Fugir | 194 → **196** | 334 → 324ms | 0,563 = |
| Take Me Out | 170 → **175** | **178 → 58ms** | 0,563 → 0,555 |
| I Have a Dream (controle) | 188 → **189** | 330 → 330ms | 0,932 = |

Âncora subiu em 6 de 7, onset não regrediu em lugar nenhum (Whisky 4→10ms está
abaixo da resolução do frame, ~32ms). **Leitura honesta**: o ganho do SW é em
ANCORAGEM (menos linha chutada por interpolação), não em onset. E o salto de
concordância do Samurai é em boa parte por construção — mais palavra ancorada
no tempo do ASR faz a linha concordar com o próprio ASR; a régua independente
diz empate ali. Não confundir as duas coisas.

**NÃO existe fallback "se o SW achar menos âncora, usa o difflib"** — chegou a
existir e foi removido. Difflib achar MAIS âncora significa âncora FALSA: no
Take Me Out ele casava 'I'/'know'/'you' soltos numa região onde a transcrição
está VAZIA (50s–82s sem nenhuma palavra) e cravava uma linha 20s fora do lugar.

`KARAOKE_ALIGN_SW=0` volta pro difflib (é assim que o A/B mede um contra o outro).

### ‼️ Duas cicatrizes de MÉTODO descobertas aqui (valem mais que a feature)

**1. Comparar A com B exige as MESMAS amostras.** `onset_error_median`
reseleciona quais linhas são verificáveis a cada chamada. Medindo cada lado
solto, o SW no Take Me Out apareceu como 5× PIOR (178 → 958ms); na comparação
pareada era 3× MELHOR (178 → 58ms). O veredito era artefato da metodologia.
Use `_erro_pareado()` / `linhas_verificaveis()` pra qualquer A/B daqui pra frente.

**2. A régua às vezes está quase cega — e isso tem que aparecer.** No Take Me
Out só **4 das 33 linhas** são verificáveis pela energia. Qualquer veredito ali
é evidência fraca. O A/B agora imprime `verificaveis: n/total` junto do número,
pra ninguém tratar 4 amostras como se fossem 33.

**3. Correção global se VALIDA antes de valer** (igual ao reconcile). A correção
de viés era um palpite único aplicado sem conferência, e errava nos dois
sentidos: desistia quando a amostra era torta (deixava o Take Me Out 870ms
adiantado) e, quando agia, ninguém checava. Agora `_vies_candidatos()` propõe
vários deslocamentos e a energia escolhe; empate ou piora = não mexe. A régua é
**veto, nunca alvo de busca** — se virar alvo, o número que a gente reporta
deixa de ser avaliação independente.

### ALIGN v4 (b) ⚠️ — SKIP do instrumental: implementado, NÃO comprovado

`_repartir_no_canto()` reparte as palavras de um buraco proporcionalmente ao
tempo em que há CANTO, não ao relógio (é o "skip state" dos alinhadores por
HMM: consumir áudio sem consumir texto). Sem isso, um instrumental de 30s dentro
do buraco recebe sua fatia de letra como se alguém estivesse cantando lá.

**Medição honesta: não provou nada.** Concordância (a régua independente da
energia): empate nos 7. Verdade humana: sem efeito. `em_silencio` melhorou em 3
de 7 e nunca piorou, mas essa métrica é circular (o skip usa energia pra
posicionar). O caso que motivou a feature, Take Me Out, não mudou.

Ficou no código porque é barato, tem teste, está atrás de `KARAOKE_ALIGN_SKIP=0`
e corrige um defeito VISÍVEL que régua nenhuma nossa enxerga (letra destacada
durante instrumental). Mas **não conte como ganho** — se atrapalhar, desliga.

Cicatriz dentro dela: a primeira guarda exigia ≥15% do buraco cantado e por isso
RECUSAVA os buracos longos (39s no Samurai, 40s no Whisky, 33s no Take Me Out)
— buraco longo é quase todo instrumental por definição, essa é a informação, não
o ruído. Guarda certa é tempo cantado ABSOLUTO.

## ‼️‼️ A DESCOBERTA QUE MUDA A RÉGUA (2026-07-19, ALIGN v4)

**As duas réguas que a gente usava vinham nos elogiando.** `measure_truth.py`
compara nosso alinhamento com o **LRC marcado por HUMANO** (`pristineSynced`,
do LRCLIB) — não passa pela nossa transcrição nem pela nossa energia. É o AAE
do MIREX (SOTA <200ms). O que ela mostrou:

| música | onset_error_median dizia | verdade humana diz |
|---|---|---|
| Take Me Out | 178ms | **695ms atrasado** |
| Vamos Fugir | ~39ms | **210ms** (viés +170ms) |

**Por que a régua de energia não via**: ela mede a distância até o onset MAIS
PRÓXIMO. Um alinhamento inteiro deslocado ~700ms continua caindo perto de
*algum* onset — deslocamento sistemático é invisível pra ela por construção.
Some-se a isso que ela enxerga só uma fração das linhas (4 de 33 no Take Me
Out) e o resultado é uma régua otimista.

**Estado real medido**: AAE mediano 452ms, 50% dentro de 300ms — longe dos
"30-40ms" que a métrica antiga reportava. Não houve regressão: o número sempre
foi esse; a régua é que era curta.

**Cobertura do novo ruler: só 2 de 123 músicas.** `pristineSynced` só existe
quando a letra veio de fonte JÁ sincronizada; o resto veio de texto puro
(letras.mus.br). ➡️ **Próximo passo de maior valor do projeto**: baixar LRC
sincronizado do LRCLIB pra biblioteca inteira SÓ COMO VERDADE (não como trilho),
com a guarda de versão, e aí sim ter um benchmark de verdade — é a fase 1 do
ALIGN_V3_PLAN (Jamendo) só que com a biblioteca do Marcus, de graça.

**Guarda obrigatória**: LRC do LRCLIB casa por TÍTULO/ARTISTA e MUITAS VEZES é
de outra gravação. O Whisky a Go-Go tem LRC que acaba em 153s numa música de
249s, deslocado 4,8s — usar como verdade dava AAE de 75 SEGUNDOS e culparia
nosso alinhamento por erro da fonte. `measure_truth.py` descarta LRC que não
cobre ≥75% da duração. **Verdade fundamental também se valida antes de valer.**

### O que isso significa pro veredito da (a) e da (b)

Contra a verdade humana, `global_align_lines` sozinho fica em 1595–1715ms
(Take Me Out) e 950–960ms (Vamos Fugir) — MUITO pior que o que está na
biblioteca (695/210ms), porque o que ship é o pipeline COMPLETO (escolha de
motor + reconcile + âncora + clamp), não esse intermediário. Nesse recorte o SW
ficou 120ms/10ms atrás do difflib, dentro do ruído de amostras de 10 e 5 linhas,
com um viés de ~1s dominando os dois. **Tradução honesta: a (a) melhora a
ANCORAGEM (fato medido: mais palavra com tempo vindo do canto real, em 6 de 7) e
não está provado que melhora o TEMPO FINAL.** Foi mantida por isso + por matar
uma classe de âncora falsa demonstrada; não por ter vencido.

## ➡️ Continua aberto no `ALIGN_V3_PLAN.md`: fases 1 e 2

Escrito 2026-07-19 a pedido do Marcus ("concordância perto de 1,00"), com
pesquisa densa. **Descoberta que muda a meta**: a concordância soma DOIS
erros — alinhamento e transcrição. Com WER de canto de ~20%, alinhamento
perfeito mediria ~0,85. Logo 1,00 é inalcançável por definição; a meta certa
é (0) medir o teto do ASR por música, (1) validar contra verdade humana
(Jamendo, AAE/PCS, SOTA <0,2s), (2) levantar o teto com large-v3 +
Mel-Band Roformer na GPU, (3) trocar as heurísticas por alinhamento GLOBAL
de sequências (Needleman-Wunsch/DTW: âncoras + gaps), (4) só então limiares.

## Pendências imediatas (próxima sessão COMEÇA por aqui)

1. **Ícones SVG no lugar dos emojis** da UI (mudam de cor, combinam com a
   proposta) + botão de esconder sidebar + responsividade (Fase 2).
2. **Fila de ~56 músicas** adicionadas pela família em 2026-07-18 processando;
   conferir depois: letras (o fallback letras.mus.br já estava ativo?), sync,
   gêneros. A do Hyldon sumiu da biblioteca — se readicionarem, o fallback pega.
3. Re-testar reportadas pós-whisper (Aerials 1ª linha → EDITOR HUMANO resolve
   agora; Take Me Out 190s FROUXO; Toxicity intro sussurrada → editor ＋linha).
4. Anchor-matching por linha (Fase 1.3) — apontar/corrigir A LINHA errada.
5. Take Me Out muda de andamento no meio — caso de teste perfeito do item 4.
6. Testar o Dockerfile num docker real (não testado — máquina local sem Docker
   no PATH); `docker compose up -d --build` no servidor doméstico.

### Validação canônica na ESCOLHA da letra (2026-07-19, "a letra tá sendo um
### PROBLEMA DO CARAIO" — Epitáfio e Flor de Tangerina)

Casos: Flor de Tangerina veio do LRCLIB como versão "(Ao Vivo)" (estrutura
diferente → desalinha no meio); Epitáfio ficou OFF-BY-ONE (1ª linha "Devia
ter amado mais" caiu como ghost e cada linha ficou com o texto da vizinha —
"ele se perde" no meio). Mitigação implementada em align_best_candidate:
1. **letras.mus.br como fonte canônica da escolha**: `source_similarity`
   (média harmônica da sobreposição de palavras) entre cada candidato e o
   texto do letras; entra no score com peso 1,2. Guardado em `srcMatch`.
2. **Melhor candidato discorda da fonte (srcMatch < 0.45)** → o texto do
   letras.mus.br VIRA a letra (plain; whisper cria o sync) — `source:
   "letras.mus.br"`.
3. **Castigo ao vivo** (`_is_live_title`): candidato "(Ao Vivo)/Live/
   Unplugged/Acústico" quando a NOSSA faixa não é ao vivo perde 0,25 no
   score do align e vai pro fim da fila no rank do fetch_lyrics.
Off-by-one residual (ghost engole 1ª linha e desloca textos) = caso do
anchor-matching por linha (Fase 1.3) — a validação canônica NÃO conserta
deslocamento, só identidade/versão.
**Desfecho (provas)**: Epitáfio off-by-one CONFIRMADO por transcrição do
trecho 9,8–14,3s = "Deve ter amado mais" onde o app mostrava "Ter chorado
mais"; realinhar não resolve (janelas esmagadas/borradas, não é shift
uniforme — script não conserta com segurança) → marcado whisper-suspeito
(pill revisar; editor ou anchor-matching #13 resolvem). Flor de Tangerina
RESOLVIDA: regra do takeover (candidato ao vivo + faixa estúdio + letras
disponível → texto de estúdio assume) devolveu "Hoje eu sonhei que ela
voltava"; source="letras.mus.br", 35 linhas whisper com words.

### Rodada de validação 2026-07-18 (noite) — anti-alucinação + editor v2

**Como UltraStar/UltraSinger sincronizam (resposta à pergunta do Marcus):**
UltraStar NÃO sincroniza nada — humanos marcam sílaba a sílaba num editor (o
usdb.eu inteiro é autoria manual). UltraSinger NÃO pega letra de fora — usa a
própria transcrição do whisper como letra, ou seja, ALUCINA IGUAL (e a
comunidade corrige na mão). Nosso modelo (letra humana de fonte + forced
alignment) já é o mais forte dos três; o único ponto que confiava no ouvido
da IA era a EXTENSÃO — corrigido hoje:
- **Validação anti-alucinação da extensão** (`_canon_or_none`): linha
  transcrita só entra se casa (SequenceMatcher ≥0.55, texto normalizado) com
  alguma linha da letra OFICIAL — e entra com o texto oficial, não o ouvido
  ("fazendo foque" → descartada; refrão real → texto certo). Sem plain salvo,
  busca na hora no letras.mus.br/lyrics.ovh. Caso-gatilho: Pisando Descalço.
- **Guardas anti-catástrofe no reconcile** (caso Another Brick: offset mediano
  -52,59s puxou 17 linhas pra tempo NEGATIVO): |offset|>20s → trilho recusado
  (skipped); expected<0 nunca aplica; invariante no align: linha t<0 é
  descartada, <4 linhas restantes → align falhou (mantém letra anterior).
  Trilho recusado → alignMethod "whisper-suspeito" → pill "⚠ revisar sync".
- **Editor v2**: (1) a linha editada MANDA — vizinha de trás apara o fim, as
  da frente são empurradas em cascata até sobrar folga (caso Já Sei Namorar:
  Intro 0→30,4s do Marcus era "comida" pela linha antiga em 4,2s; corrigido
  nos dados: 18 linhas empurradas); (2) **↩ automático**: 1ª edição manual
  guarda a versão automática em lyricsBackup; POST /api/lines/{sid}/restore
  desfaz tudo (Marcus apagou a letra inteira editando — nunca mais).
  Round-trip testado: salvar preserva words (offsets relativos + rematch por
  texto), restaurar volta byte a byte.
- **Docker TESTADO E FUNCIONANDO** (2026-07-18, Docker Desktop win):
  imagem 4,29GB, build ~10min. Dois gotchas de build: diffq não tem wheel
  linux/py3.12 e compila C → precisa `gcc libc6-dev` no apt (gcc sozinho
  falha com "stdlib.h not found"); _cross_process_lock ganhou fallback fcntl
  (msvcrt é só Windows — crashava o container no boot). Teste: container na
  8778 com data/ ISOLADA → landing 200, app inteiro renderiza, /api/songs ok,
  logs limpos. NÃO exercitado: pipeline de IA no container (separação baixa
  modelos ~600MB no 1º preparo — validar no servidor doméstico). ‼️ As travas
  NÃO conversam entre host e container: JAMAIS dois servidores na mesma data/.
  Rodar de verdade: `docker compose up -d --build` (volume ./data, porta 8777).

### Feito em 2026-07-18 (contexto rápido)
- Editor humano de linhas (front + PUT /api/lines) — ver Fase 2.
- Cards com ações na base, modal de exclusão, gavetas Steam, prévia 15%.
- Fallback de letras: letras.mus.br (busca solr JSONP `LetrasSug(...)` +
  scrape `div.lyric-original`, valida por overlap de tokens normalizados) e
  lyrics.ovh; texto puro flui pro whisper-align que cria o sync do zero
  (`align_lyrics_to_vocals` já aceitava `plain`). `fetch_plain_fallback`.
- setup.bat robusto: aceita py 3.11–3.13, RECUSA 3.14 com instrução (causa
  provável da falha no outro PC: python.org hoje instala 3.14 por padrão);
  requirements.txt agora é fonte única (inclui as deps de IA); erros com
  mensagem e pause em cada etapa. start.bat avisa se .venv não existe.
- Dockerfile (python:3.12-slim + ffmpeg apt + torch CPU do índice cpu) +
  docker-compose (volume ./data, KARAOKE_HOST=0.0.0.0, cache whisper no
  volume via XDG_CACHE_HOME) + .dockerignore. main.py: host/porta por env
  KARAOKE_HOST/KARAOKE_PORT (default 127.0.0.1:8777).

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
