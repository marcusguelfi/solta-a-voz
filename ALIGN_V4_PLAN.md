# ALIGN v4 — as 5 correções (pesquisa validada 2026-07-19)

O problema-raiz, nomeado pela literatura: **letra e áudio NÃO são sequências
1:1** (instrumental, verso não cantado, ASR que não ouviu). Nosso pipeline
força cada linha a uma posição — daí o buraco entre teto e concordância.

## a) Alinhamento LOCAL (Smith-Waterman) no lugar do casamento sem custo
Hoje: `difflib` (Ratcliff-Obershelp), sem penalidade de gap nem pontuação —
casa "o que der". Correto: match gain + mismatch/gap penalty → acha ilhas de
alta confiança e **não força o resto**.
**Aceite**: cobertura ancorada sobe e onset não piora nos 7 casos.

## b) SKIP explícito (consumir áudio sem consumir texto)
Padrão dos HMM de alinhamento de letra (short-pause states opcionais). É o que
impede instrumental longo de arrastar a letra.
**Aceite**: Samurai/Whisky (muito instrumental) melhoram sem piorar controle.

## c) CONFIANÇA POR LINHA + métrica com cobertura  ← COMEÇA POR AQUI
Linha em bloco âncora = confiável; linha interpolada = incerta. Muda 3 coisas:
1. **Métrica**: reportar concordância NAS ANCORADAS + % de cobertura. Hoje a
   métrica global mistura o que sabemos com o que chutamos e pune injustamente
   (Samurai: 45% das linhas têm <5 palavras — impossíveis de ancorar sozinhas;
   0,41 de concordância é característica da LETRA, não defeito nosso).
2. **UI**: não fingir precisão que não temos; marcar linha incerta.
3. **Editor**: levar o humano direto às linhas incertas.
**Aceite**: Samurai deixa de ser falso-suspeito; controle segue confiável.

## d) PROMPT-BIASING do ASR com a letra oficial  ⚠️ potente e perigoso
`initial_prompt`/`hotwords` (faster-whisper) enviesa o decoder pro vocabulário
que a gente JÁ CONHECE — a letra. Sobe a taxa de acerto de palavra e, com ela,
as âncoras. Limite: só os últimos ~224 tokens do prompt entram, então precisa
ser compacto (palavras distintivas, não a letra inteira).
**⚠️ ARMADILHA (mesma família do erro que já cometemos)**: transcrição
enviesada pela letra deixa de ser fonte INDEPENDENTE — ela passa a concordar
com a letra por construção, inflando concordância e criando âncoras falsas.
**Regra obrigatória**: manter DUAS transcrições — a enviesada só para
ALINHAR, e a limpa (sem prompt) para MEDIR/validar. Nunca medir com a
enviesada.
**Aceite**: onset (régua independente) melhora; se só a concordância subir, é
contaminação, não ganho.

## e) DUAS PASSADAS (iterative refinement)
Alinhar → usar o alinhamento pra re-transcrever cada região COM o contexto
local da letra → realinhar o resíduo. A literatura mostra que a 2ª passada
absorve a maior parte do sinal recuperável.
**Aceite**: ganho medido nos 7; custo de CPU aceitável (só nas regiões fracas).

---

# ESTADO DA EXECUÇÃO (2026-07-19)

- **(c) ✅ feito** — `alignment_quality()`. Resultado contrariou a hipótese: as
  linhas curtas INFLAVAM a nota do Samurai (0,411 → 0,337). A marcação dele é
  legítima.
- **(a) ✅ feito** — `local_align_words()` (Smith-Waterman). Âncoras sobem em 6
  de 7. Ganho em ANCORAGEM, não comprovado em tempo final.
- **(b) ⚠️ feito, não comprovado** — `_repartir_no_canto()`. Empate em tudo que
  dá pra medir sem circularidade. Mantido atrás de `KARAOKE_ALIGN_SKIP`.
- **(d) e (e) — NÃO COMEÇAR ANTES DE LER ISTO** ⬇️

## ‼️ A régua mudou: leia antes de continuar

`server/measure_truth.py` (novo) mede contra **LRC marcado por humano** — a
única régua não-circular que temos. Ela revelou que `onset_error_median` estava
nos elogiando: Take Me Out marcava 178ms e está **695ms atrasado**; a biblioteca
tem AAE real de 452ms, não os "30-40ms" que a gente reportava.

Isso reordena o plano. **(d) e (e) são caras e miram a taxa de acerto de palavra
— mas o erro que a verdade humana mostra é VIÉS SISTEMÁTICO de ~700ms**, que
transcrição melhor não conserta. Fazer (d)/(e) agora é otimizar a coisa errada.

### Ordem nova, recomendada

1. **Verdade pra biblioteca inteira**: baixar LRC sincronizado do LRCLIB SÓ COMO
   VERDADE (nunca como trilho — contamina), com a guarda de versão de
   `measure_truth.py`. Hoje só 2 de 123 músicas são verificáveis. Sem isso,
   qualquer decisão daqui pra frente continua sendo tomada no escuro.
2. **Atacar o viés sistemático** com a régua nova na mão. É o maior erro medido
   e o mais barato de corrigir (é um escalar por música).
3. Só então (d) prompt-biasing e (e) duas passadas — e com as DUAS transcrições,
   conforme a armadilha já descrita abaixo.

## Ordem de execução (original, mantida como registro)
c (barato, corrige métrica e UI) → a (troca o casador) → b (skip) →
d (com as duas transcrições) → e (se ainda sobrar buraco).
Cada uma com: teste unitário + medição nos 7 casos + controle intacto.
