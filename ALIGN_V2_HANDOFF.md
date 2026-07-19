# ALIGN v2 — Handoff de execução (para o Opus)

> **STATUS (2026-07-19, executado pelo Opus)**
> - **Fase A ✅ feita** (commit f4ec16a) — `speech_map`/`sung_energy`, números
>   no CLAUDE.md seção "ALIGN v2 — EXECUÇÃO". Desvio deliberado: mapa de fala
>   da MÚSICA INTEIRA (o handoff sugeria reusar `transcribe_vocals`, que só vê
>   ~110s e não pegaria solo no meio — justo o caso Samurai).
> - **Fase C ✅ código feito** (commit b64ae28) — `anchor_fix_lines` +
>   `full_transcribe` (uma passada alimenta máscara E âncoras). 47 testes.
> - **Fase B ⏳** — A/B `server/ab_align.py` rodando; decisão pela métrica.
> - Ferramentas novas: `measure_align.py`, `ab_align.py`, `align_v2_apply.py`.

Escrito em 2026-07-19 pelo Fable após pesquisa validada (fontes no CLAUDE.md,
seção "PESQUISA VALIDADA"). Este documento é autossuficiente: contém o que
implementar, em que ordem, como medir e quando parar. Leia o CLAUDE.md antes
para o contexto geral do projeto (OBRIGATÓRIO: seções REGRA DE OURO, Gotchas
de desenvolvimento e o incidente de 2026-07-17).

## Contexto em 30 segundos

- App: karaokê caseiro "Solta a Voz". Servidor FastAPI em `server/main.py`
  (porta 8777), front vanilla em `static/`. Python 3.13 no `.venv`
  (`.venv\Scripts\python.exe` — NUNCA o python do sistema).
- Pipeline por música: separação IA (stem de voz) → pitch.json (melodia +
  energy 0/1) → escolha de letra validada (LRCLIB × letras.mus.br) →
  **forced alignment com stable-ts/whisper "small"** → pós-processamento
  (reconcile com trilho LRC, clamp de fins, REGRA DE OURO drop_ghost_lines,
  extensão validada anti-alucinação).
- Testes: `.venv\Scripts\python.exe -m pytest tests -q` → **39 passing hoje.
  Nunca entregar com menos.**
- Commits SEM assinatura do Claude (regra do Marcus). PowerShell 5.1: usar
  `git commit -F arquivo.txt` (aspas/parênteses quebram -m).

## O problema (medido, não achado)

O whisper alinha por palavras FALADAS. Três classes de erro comprovadas:
1. **Melisma** — vogal sustentada ("Quaaaando" em Não Deixe o Samba Morrer,
   "Saaaai" em Samurai): o modelo desiste e PULA trechos inteiros.
2. **Instrumento vazado no stem de voz** — a gaita do Stevie Wonder
   (Samurai) cai no stem vocal e engana TODAS as regras de energia
   (ghost/clamp/extensão/onset): a letra ancora na gaita e atrasa.
3. **Off-by-one / refrão repetido** — 1ª linha vira fantasma e todas ficam
   com o texto da vizinha (Epitáfio: PROVADO por transcrição do trecho
   9,8–14,3s = "Deve ter amado mais" onde o app mostrava "Ter chorado
   mais"); letras de texto puro (letras.mus.br) não têm trilho de LRC e
   escorregam nos refrões.

## Os 7 casos de referência (a suíte de regressão)

| Música | id | Classe de erro |
|---|---|---|
| Samurai (Djavan) | `736e6ae52b47` | gaita no stem + melisma |
| Não Deixe o Samba Morrer (Alcione) | `8a44ede95182` | melisma + pulos |
| Epitáfio (Titãs) | `66011072d2ae` | off-by-one (provado) |
| Whisky a Go-Go (Roupa Nova) | `d69d8f8e7c97` | atrasos pontuais |
| Vamos Fugir (Skank) | `496798166b42` | atrasos pontuais |
| Take Me Out (Franz Ferdinand) | buscar por nome | mudança de andamento |
| **I Have a Dream (ABBA)** | buscar por nome | **CONTROLE — perfeita, NÃO REGREDIR** |

Buscar id por nome: `main._load_lib()` e casar substring em
`f"{artist} {title}".lower()` (padrão usado em `server/realign_batch.py`).

**Métrica oficial** (já implementada em `server/audit.py`):
`phrase_onsets(energy, hop)` + `timing_errors(lines, onsets)` → mediana em ms.
Rodar ANTES e DEPOIS de cada fase para os 7. Aceite por fase abaixo.
`server/realign_batch.py "trecho do nome"` reprocessa uma música no regime
completo (usar após cada mudança de motor).

## FASE A — Máscara de fala-cantada (fazer PRIMEIRO: barata, mata a gaita)

**Ideia** (validada na literatura — Lehner et al. ICASSP 2014: instrumentos
de pitch contínuo são O falso positivo clássico de detecção de voz; a
discriminação vencedora é LINGUÍSTICA): a transcrição do whisper (que já
fazemos e cacheamos) reporta `no_speech_prob` por segmento. Gaita = zero
fonemas = no_speech alto. Usar isso como máscara sobre `energy`.

**Implementação:**
1. Em `transcribe_vocals` (server/main.py): além do texto, PERSISTIR no
   `stems/{id}/transcript.json` os segmentos com `{start, end, no_speech_prob}`
   (o resultado do stable-ts/whisper já traz; hoje jogamos fora).
2. Nova função `sung_energy(sid) -> list[int]`: carrega pitch.json energy e
   ZERA janelas cujo segmento correspondente tem `no_speech_prob > 0.5` (ou
   sem segmento nenhum cobrindo, com margem ±0,3s). Cache em módulo.
3. Trocar `energy` por `sung_energy(sid)` em: `drop_ghost_lines`,
   `clamp_ends_to_voice`, `uncovered_sung_regions`, `vocal_start_from_energy`
   (via chamador), e no audit (`phrase_onsets` recebe o array — passar o
   mascarado). NÃO mexer no lane do front (energy visual pode continuar cru).
4. Transcrição pode não existir (música antiga sem transcript.json com
   segmentos): fallback = energy cru (comportamento atual). Sem transcript
   confiável (`transcript_is_reliable` False) → energy cru também.

**Aceite fase A:** Samurai e Samba Morrer melhoram a mediana do audit (ou
visivelmente param de ancorar na gaita/pular); I Have a Dream inalterada
(±10ms); 39+ testes verdes (adicionar teste unitário de `sung_energy` com
segmentos fake). Rodar `realign_batch` nos 7 depois de integrar.

## FASE B — Alinhador CTC (MMS_FA) lado a lado com o whisper

**Candidato validado**: `torchaudio.pipelines.MMS_FA` +
`torchaudio.functional.forced_align` (CTC multilíngue, 1100+ línguas com
PORTUGUÊS; torch já está instalado). Alternativa embrulhada: pacote
`ctc-forced-aligner` (MahmoudAshraf97). Por que resolve melisma: o token
blank do CTC absorve duração — vogal esticada alinha por construção.
Treinado em FALA, mas wav2vec2 transfere bem pra canto (Ou et al. 2022).

**Implementação:**
1. `mms_align_lines(sid, line_texts) -> list[dict] | None` espelhando a
   assinatura de `whisper_align_lines`: entrada vocals.mp3 (16kHz mono via
   ffmpeg/librosa), texto normalizado por linha (MMS usa alfabeto romano —
   PT já é; tirar pontuação), saída [{t, end, text, words}] com words
   RELATIVAS ao início da linha (formato atual — ver renderLyrics/fillPercent).
2. Script A/B `server/ab_align.py`: para os 7 casos, roda whisper e MMS no
   MESMO texto-base, imprime mediana do audit de cada um por música. NADA de
   trocar motor antes desses números.
3. **Adoção por métrica**:
   - MMS vence em ≥5 dos 7 sem piorar o controle → MMS vira motor titular
     e whisper fica só para transcrição/identidade.
   - Senão → híbrido por linha: linhas que o whisper marcou `_ok=False`
     (interpoladas), esmagadas (duração < nº palavras × 0,15s) ou puladas
     recebem o tempo do MMS.
4. O pós-processamento (reconcile/clamp/ghost/extensão) permanece IGUAL —
   ele opera sobre lines, agnóstico ao motor. Com a Fase A pronta, ele já
   estará usando sung_energy.

**Aceite fase B:** números do A/B documentados no CLAUDE.md (mesmo formato
do experimento beat-snap); decisão tomada PELA MÉTRICA; controle intacto.

## FASE C — Anchor-matching por linha (mata off-by-one e refrão escorregado)

1. Transcrever com word timestamps (o "small" já faz em
   `transcribe_region_lines`; generalizar para a música inteira, cachear).
2. Para cada linha da letra: buscar a melhor janela de n-grams na
   transcrição (SequenceMatcher sobre texto normalizado, como
   `_canon_or_none`). Score baixo + vizinhas com score alto deslocadas de
   ±1 linha = OFF-BY-ONE DETECTADO (caso Epitáfio: deve acusar).
3. Reancorar SÓ as linhas discordantes ao tempo da janela casada (âncoras
   fortes: ≥3 palavras de conteúdo, similaridade ≥0,6). Nunca mexer em
   linha concordante.
4. Vira também flag de audit (`LINHA DESLOCADA`) antes de virar correção
   automática — primeiro reportar, medir falsos positivos nos 7, depois
   ligar a correção.

**Aceite fase C:** Epitáfio detectado e corrigido automaticamente (validar
com o áudio: a linha em ~10,2s deve ser "Devia ter amado mais"); controle
intacto; falso positivo zero nos 7.

## O que NÃO fazer (já provado/decidido — não redescobrir)

- **Beat-snap**: REPROVADO empiricamente (piorou nas 3 testadas — números
  no CLAUDE.md). Não tentar de novo.
- **Não realinhar** música com `alignMethod: "manual"` sem guardar
  `lyricsBackup` (o Marcus editou algumas na mão; POST
  /api/lines/{sid}/restore é o desfazer).
- **Nunca** dois servidores na MESMA `data/` (host + Docker inclusive) —
  travas não conversam entre plataformas; corrupção de 2026-07-17.
- **Nunca** t negativo em linha; reconcile com |offset|>20s = trilho
  recusado (guardas já existem — não remover).
- Scripts auxiliares: sempre `KARAOKE_NO_WORKER=1` + venv python + escrita
  só via `main._update_entry` (atômica + travada).
- Front mudou? Bump de `?v=` em style.css/app.js no app.html (cache).

## Definição de pronto (geral)

1. As 6 músicas-problema audivelmente melhores; I Have a Dream intacta.
2. Medianas do audit ANTES/DEPOIS documentadas no CLAUDE.md por fase.
3. pytest ≥ 39 verdes (novos testes para sung_energy, mms_align_lines e
   detecção de off-by-one com dados sintéticos).
4. Fila/worker continua funcionando (novas músicas passam pelo motor novo).
5. Commits pequenos por fase, sem assinatura, push em
   github.com/marcusguelfi/solta-a-voz (main).
