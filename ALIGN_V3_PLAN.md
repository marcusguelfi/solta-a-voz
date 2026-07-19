# ALIGN v3 — plano para levar a concordância ao teto real

> **STATUS (2026-07-19): Fases 0 e 3 EXECUTADAS.** Fases 1 (Jamendo) e 2 (GPU)
> continuam abertas. Resultado medido nos 7 casos:
>
> | música | teto do ASR | antes | global |
> |---|---|---|---|
> | Epitáfio | 0,996 | 0,898 | **0,989** |
> | I Have a Dream (controle) | 0,965 | 0,885 | **0,946** |
> | Vamos Fugir | 0,905 | 0,825 | **0,883** |
> | Samba Morrer | 0,935 | 0,828 | **0,862** |
> | Samurai | 0,947 | 0,481 | **0,668** |
> | Whisky a Go-Go | 0,849 | 0,634 | 0,505 ✗ |
> | Take Me Out | 0,833 | 0,798 | 0,774 ✗ |
>
> **‼️ ARMADILHA (achada ao validar, quase entrou em produção)**: a tabela
> acima usa a CONCORDÂNCIA, que é **auto-referente para o motor global** — ele
> põe cada linha no tempo das palavras da transcrição e é medido contra essa
> mesma transcrição. Ele concorda consigo mesmo por construção. A régua de
> ENERGIA (independente do ASR) mostrou o oposto: Epitáfio 30ms → 396ms,
> controle 40ms → 332ms, Vamos Fugir 42ms → 883ms. O global herda o viés dos
> timestamps do Whisper (~300-400ms). **Regra**: métrica que compartilha a
> fonte com o método NÃO julga o método. Quem decide o motor agora é
> `onset_error_median` (energia); a concordância só desempata quando faltam
> onsets verificáveis.
>
> **A medição do teto corrigiu o pessimismo deste plano**: os tetos são ALTOS
> (0,83–0,996), então o buraco é quase todo de ALINHAMENTO, não de ASR — e
> perto de 1,00 É alcançável onde a transcrição é boa (Epitáfio: 0,989 com
> teto 0,996). Como o global perde em 2 de 7, o motor virou `auto`: tenta o
> global (que não roda modelo nenhum — só usa a transcrição existente) e,
> se ele já estiver no teto, aceita de cara (mais rápido E melhor); senão
> roda o híbrido e fica com o que mais concordar com o canto.

Escrito em 2026-07-19 após pesquisa densa (fontes no fim), a pedido do Marcus:
"quero que a concordância chegue muito perto de 1,00". **Nenhum código foi
mudado para escrever este plano** — ele existe para ser discutido antes.

## 1. A verdade sobre a meta 1,00 (leia isto primeiro)

Nossa `concordância` mede: *o texto da linha bate com as palavras que a
TRANSCRIÇÃO diz que foram cantadas ali?* Ela tem **duas fontes de erro
somadas**:

```
concordância = f(qualidade do ALINHAMENTO, qualidade da TRANSCRIÇÃO)
```

Se o Whisper ouve "fazendo foque" onde se canta "fazendo fogueira", a linha
pode estar no lugar PERFEITO e ainda assim pontuar baixo. Ou seja: **existe um
teto imposto pela transcrição, e 1,00 é inalcançável por definição** — a
literatura reporta WER de 15-25% em canto (Whisper large-v2 deu 24,6% de WER
em letras vietnamitas; canto é muito mais difícil que fala).

Traduzindo: com WER de ~20%, um alinhamento PERFEITO mediria ~0,80-0,90.
**Samurai em 0,48 e Whisky a Go-Go em 0,63 estão bem abaixo do teto — esses
têm erro real de alinhamento. Epitáfio 0,90 e o controle 0,88 provavelmente
já estão NO teto.**

Portanto a meta correta não é "1,00", é:
1. **Medir o teto** (quanto da distância é transcrição, quanto é alinhamento);
2. **Levantar o teto** (transcrição e separação melhores);
3. **Encostar no teto** (alinhamento global no lugar das heurísticas por linha);
4. **Validar contra verdade humana** (Jamendo), onde "certo" não é opinião
   nossa: o alvo público é **AAE < 0,2s**, o estado da arte.

## 2. O que a pesquisa mostrou

- **Métricas padrão do campo**: AAE (erro absoluto médio no início/fim de cada
  unidade) e PCS (% do tempo com segmento correto). SOTA em Jamendo:
  **AAE < 0,2s** (abordagem contrastiva, ICASSP 2023). Nossas medianas de
  30-40ms nas músicas boas são ótimas — mas medidas com régua PRÓPRIA, em
  subconjunto: sem verdade humana, é auto-avaliação.
- **Existe verdade humana disponível**: `f90/jamendolyrics` (79 músicas com
  letra alinhada à mão, V2 com anotações novas) e `Jam-ALT` (AudioShake).
  Podemos rodar nosso pipeline neles e comparar com o mundo.
- **Separação MUDA o alinhamento** (ISMIR 2025, "Evaluating Lyrics Alignment
  under Source Separated Conditions"): quanto melhor o vocal, melhor o
  alinhamento, e o resultado **varia muito conforme o separador**. Nosso
  MDX-Net é antigo; Mel-Band Roformer / BS-Roformer são o estado da arte
  (vocais ~11,2 dB de SDR contra ~9 do MDX). **É exatamente o problema da
  gaita do Samurai: atacar na separação é atacar na raiz.**
- **Tamanho do modelo de transcrição importa muito para LETRA**: large-v3 é
  claramente superior a medium/small em letras; turbo tropeça em canto. Nós
  usamos "small" para alinhar e "base" para identidade.
- **nomadkaraoke/karaoke-gen** (o sistema de produção mais próximo do nosso)
  faz: transcrição com timestamp de palavra → **anchor sequences** contra a
  letra oficial → **gap sequences** corrigidas por regras (e LLM) → **UI de
  revisão humana**. Eles preferem AudioShake (comercial) para transcrever.
- **Alinhador contrastivo multilíngue** (`jhuang448/LyricsAlignment-Multilingual`,
  MIT, checkpoint pronto): pede vocais separados, é a família que atingiu
  AAE < 0,2s. Candidato a 3º motor — falta confirmar cobertura de PT.

## 3. As fases (ordem por custo × impacto)

### FASE 0 — Separar as duas fontes de erro (barato, faça ANTES de tudo)
Sem isto estaremos otimizando no escuro.
1. `agreement_ceiling(sid)`: mede a concordância usando a transcrição como
   referência *dela mesma* — para cada linha, o melhor casamento em QUALQUER
   posição (não só na atual). Isso separa: se o melhor casamento também é
   baixo, o problema é transcrição; se é alto e o daqui é baixo, é alinhamento.
2. Rodar nos 7 casos → tabela "teto × atual" por música.
3. **Aceite**: saber, por música, quanto do buraco é nosso e quanto é do ASR.

### FASE 1 — Verdade humana (o que falta para sair do achismo)
1. Baixar `f90/jamendolyrics` (79 músicas, licença aberta).
2. `server/bench_jamendo.py`: roda nosso pipeline e calcula **AAE e PCS**
   padrão do MIREX.
3. **Aceite**: saber nosso AAE real contra o SOTA (<0,2s). Vira o placar
   permanente do projeto — sem isso, "melhorou" continua sendo opinião.

### FASE 2 — Levantar o teto (maior impacto isolado, precisa de GPU)
1. **Transcrição large-v3** (ou faster-whisper large) no lugar do "small" para
   o `full_transcribe` (anchors + métrica). Em CPU é proibitivo; na GTX 1060
   com CUDA/faster-whisper fica viável.
2. **Separação Mel-Band Roformer / BS-Roformer** no lugar do MDX-Net. Ataca a
   gaita do Samurai na RAIZ (menos vazamento = menos falso "canto"), e o
   ISMIR 2025 confirma que isso melhora o alinhamento inteiro.
3. **Aceite**: re-medir Fase 0 e 1 — o teto tem que subir, e o AAE cair.
   Se não cair, não adotar (regra do A/B que já usamos).

### FASE 3 — Alinhamento GLOBAL no lugar das heurísticas por linha
Hoje temos `anchor_fix_lines` (remendo por linha, com 6 travas empíricas).
O correto é o que a literatura e o nomadkaraoke fazem:
1. **Alinhamento global de sequências** (Needleman-Wunsch/DTW) entre a
   sequência de palavras TRANSCRITAS e a sequência de palavras da LETRA.
2. Blocos casados = **âncoras** (tempo confiável, vem do áudio).
3. Trechos não casados = **gaps** → interpolar entre âncoras vizinhas
   (melisma e refrão repetido caem aqui naturalmente).
4. Isso **substitui** `reconcile_with_lrc` (que já se provou perigoso — caso
   Epitáfio) e o `anchor_fix_lines` heurístico, com uma única formulação sem
   premissa de offset global (logo, imune a mudança de andamento — Take Me Out).
5. **Aceite**: AAE melhora em Jamendo E a concordância sobe nos 7 casos, com
   o controle intacto. Se o global perder do atual, mantém-se o atual.

### FASE 4 — Só então ajustar limiares e casos de borda
Com teto conhecido, verdade humana e alinhamento global, aí sim mexer em
`min_score`, `bad_score`, `SPEECH_NSP_MAX` etc. — com números, não com achismo.

## 4. O que NÃO fazer (já provado nesta sessão)
- **Não** deixar o `reconcile_with_lrc` mandar por decreto (destruiu o
  Epitáfio; hoje ele só passa se aumentar a concordância — manter assim).
- **Não** usar a máscara de fala para APAGAR letra (ela é precisa mas
  incompleta) — só para posicionar.
- **Não** confiar na régua de onsets sozinha: ela vê 58% das linhas e
  **premiava** âncora em instrumento vazado.
- **Não** medir sem `--fresh` (o trilho envenenado falseia tudo).
- **Não** repetir beat-snap (reprovado com números).

## 5. Expectativa honesta
Com Fase 0+1 sabemos onde estamos de verdade. Com Fase 2 (GPU) o teto sobe e
Samurai/Whisky a Go-Go devem melhorar de verdade. Com Fase 3 o alinhamento
deixa de depender de heurística e passa a ter formulação única. **Meta
realista: concordância no teto do ASR (~0,85-0,92 na maioria) e AAE < 0,3s
medido em Jamendo — com o caminho aberto para <0,2s (SOTA).** Prometer 1,00
seria mentir sobre como a métrica funciona.

## Fontes
- MIREX Lyrics-to-Audio Alignment (métricas AAE/PCS) — music-ir.org/mirex
- Jamendo dataset: github.com/f90/jamendolyrics · Jam-ALT: audioshake.github.io/jam-alt
- ISMIR 2025, "Evaluating Lyrics Alignment under Source Separated Conditions"
- Contrastive alignment (AAE < 0,2s): arxiv.org/html/2306.07744 ·
  github.com/jhuang448/LyricsAlignment-Multilingual (MIT, checkpoint pronto)
- Mel-Band RoFormer: arxiv.org/pdf/2310.01809 · BS-RoFormer: arxiv.org/pdf/2309.02612
- nomadkaraoke/karaoke-gen (anchor/gap + revisão humana)
- Whisper em letras (large-v3 superior; WER de canto): openwhispr.com,
  arxiv.org/pdf/2510.22295 (VietLyrics, WER 24,6%)
