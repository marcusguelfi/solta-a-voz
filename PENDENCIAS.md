# Pendências organizadas (2026-07-20, depois do teste cantado do Marcus)

Ordem por **retorno / custo**, não por ordem de descoberta.

---

## 1. LETRA DE OUTRA VERSÃO — ✅ causa-raiz corrigida, falta varrer

**Diagnóstico**: a guarda de duração só valia pra letra SEM sincronia:
`if dur_diff > 20 and not best.get("syncedLyrics")`. Um LRC de 313s ganhou numa
gravação de 260s. Letra sincronizada da versão errada é **pior** que texto puro
da certa — o erro vem disfarçado de precisão.

**Feito**: `rank()` agora põe duração ANTES de ter sincronia, e a guarda de 20s
vale pra todo candidato. Teste de cicatriz no `test_core.py`.

**Falta**: 7 músicas já contaminadas (letra cobre <72% da duração):
Love Will Tear Us Apart (0,62), Send Me An Angel (0,65), A Dança (0,68),
Ghostbusters (0,70), Stayin' Alive (0,71), Eu Vou Estar (0,72), Zombie (0,72).
→ re-buscar letra com o `rank` novo e realinhar. Várias já estão no fundo do
ranking perceptual, então deve dar ganho grande.

## 2. Validar a letra contra o ÁUDIO (não só contra a duração)

O que consertou o Psycho Killer na mão: entre candidatos, escolher o que mais
casa com a TRANSCRIÇÃO. Nossa letra casava 12/34 linhas; a certa, 17/35.

**Como**: depois de `full_transcribe`, medir o acordo texto×transcrição de cada
candidato e trocar se algum for claramente melhor. Só roda quando já existe
transcrição, então não custa nada a mais.

**⚠️ Armadilha documentada**: teto baixo NÃO prova letra errada. "Qu'est-ce que
c'est", "Fa-fa-fa-fa" e versos em francês são reais e dão teto 0 porque o ASR em
inglês não os transcreve. Só o conjunto (verso ausente + duração incompatível)
prova. Exigir margem grande antes de trocar.

## 3. Linhas ESMAGADAS e ARRASTADAS — detector pronto, falta corrigir

`duracao_suspeita()` já acha (123 esmagadas, 60 arrastadas em 52 músicas) e o
editor já leva o humano até elas. Falta o pipeline **corrigir sozinho**:
- esmagada (<0,12s por palavra): esticar até o próximo início ou até o canto
  acabar, o que vier antes;
- arrastada (>3s com >60% de silêncio): cortar o fim onde o canto para
  (`trim_tails` já faz algo parecido — checar por que não pegou o Bad Boys 2:30).

## 4. Gráfico de pitch some em trechos — NÃO INVESTIGADO

Relato do Marcus no Bad Boys: "às vezes o 'bad boy' não aparecia no gráfico de
baixo pra pontuar". Subsistema de pitch/pontuação (`pitch.json`, `extract_pitch`
com pyin), não alinhamento. Hipótese a testar: pyin não acha f0 em voz gritada/
falada ou o `fmin=65/fmax=1000` corta. Medir cobertura de f0 × energia cantada.

## 5. (d) e (e) do ALIGN v4 — reescritas, ainda valem

Viraram: casar por **FONEMA** (a biblioteca é majoritariamente PT e o whisper
erra muito mais em PT que em EN) e segunda passada só nas linhas com nota <0,5.
Fonte: Vaglio et al. ISMIR 2020 + dataset DALI.
**Só depois de 1–3**: adiantar transcrição não resolve letra errada nem duração.

## 6. Lote das ~100 marcadas

Por último de propósito. Cada régua nova nasceu de um defeito que o ouvido do
Marcus achou e os números não (3 vezes em uma sessão). Rodar o lote antes de
fechar 1–3 assa o erro em 100 músicas.
**Protocolo**: 3–4 músicas → ele canta → só então o lote, com Epitáfio de
controle e backup do `library.json`.

---

## Limpeza de código feita
- `_vies_vs_onsets` removida (substituída por `_vies_candidatos`, 0 usos).
- Varredura das 109 funções de `main.py`: as outras 12 "sem uso" são rotas
  FastAPI (usadas por decorator) — **não remover**.
