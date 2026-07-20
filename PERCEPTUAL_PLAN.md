# A régua do OUVIDO — pesquisa validada e plano (2026-07-19)

Pedido do Marcus: *"quero passar o meu tempo no app cantando e não editando na
mão as músicas"*. Pra isso a máquina precisa julgar como o ouvido dele julga —
senão a gente otimiza número e ele continua editando.

## O que a pesquisa entregou (e o que a gente estava errando)

**Fonte**: Lizé Masclef, Vaglio & Moussallam, *User-centered evaluation of
lyrics-to-audio alignment*, ISMIR 2021 (Deezer). Eles fizeram o experimento que
o MIREX nunca fez: pessoas julgando sincronia num setup de KARAOKÊ. O resultado
virou código na `mir_eval` (`karaoke_perceptual_metric`) — parâmetros de uma
skew-normal ajustada nos julgamentos humanos.

Três descobertas, e nós erramos nas três:

| o que a literatura diz | o que a gente fazia |
|---|---|
| o ótimo é **−67ms** (letra um tiquinho ANTES do canto) | mirávamos em **zero** |
| a curva é **assimétrica**: atrasar dói MUITO mais | mediamos **valor absoluto** |
| faixa de 90%: **−170ms a +40ms** (estreitíssima pro atraso) | tolerância simétrica |

O 0,3s de tolerância do MIREX, que a gente vinha usando como referência, **nunca
teve validação psicológica** — é isso que o artigo denuncia. Pro lado do atraso
a tolerância real é ~7× menor.

## Implementado

`perceptual_line_score(offset)` e `perceptual_score(sid, lines)` em `main.py`,
com `ALVO_PERCEPTUAL = -0.067`. A nota devolve média **e `ruins`** (linhas com
score <0,5), porque média esconde a linha que estraga a música — 95% perfeita
com 3 linhas fora é ruim de cantar, e nenhuma média mostra isso.

**Primeira validação contra o ouvido do Marcus** (ele não sabia dos números):

| música | veredito dele | nota | ruins |
|---|---|---|---|
| Epitáfio | "ficou perfeito" | **0,893** | 1 |
| I Have a Dream | controle bom | 0,784 | 4 |
| Psycho Killer | ruim | 0,690 | 4 |
| September | ruim | 0,547 | 5 |
| Samurai | marcada suspeita | 0,516 | 6 |
| Stayin' Alive | ruim | **0,339** | 3 |

Ordenou igual ao ouvido dele, sem ajuste pra isso acontecer. É a primeira
métrica nossa que faz isso — `alignment_agreement` dava 0,964 pro Bad Boys, que
ele reprova.

## Plano — na ordem, cada passo com controle (Epitáfio) e teste

### 1. Retarget: parar de mirar em zero  ⬅️ MAIOR GANHO, MAIS BARATO
A correção de viés (`_vies_candidatos`) hoje busca erro absoluto zero. Trocar o
alvo pra `ALVO_PERCEPTUAL` e o critério pra `perceptual_score`. É um escalar por
música: barato, reversível, e mexe em TODAS as músicas de uma vez.
**Aceite**: nota perceptual sobe na maioria e o Epitáfio não cai.

### 2. Nota perceptual como selo (substitui `agreement` no card)
O selo "⚠ revisar sync" passa a sair da nota do ouvido, não da concordância —
que já demonstrou aprovar música ruim (Bad Boys 0,964).

### 3. Levar o humano SÓ às linhas ruins
`perceptual_score` já devolve quais linhas têm score <0,5. O editor abre nelas,
em vez de o Marcus caçar o erro. É o que converte "editar música" em "confirmar
3 linhas" — atende direto o pedido dele.

### 4. Consertar o ponto cego da régua
Bad Boys e Final Countdown ficam "sem medição": menos de 4 linhas verificáveis
pela energia. Enquanto isso não fecha, existe música ruim que passa batida.
Caminho: usar as âncoras de PALAVRA (que o SW já produz) como onsets adicionais,
não só os onsets de frase.

### 5. Só então reprocessar a biblioteca
107 das 123 músicas ainda usam o motor `whisper` antigo e 111 não têm
transcrição. Reprocessar ANTES de ter a régua certa é gastar CPU pra otimizar a
coisa errada — por isso este passo é o último, não o primeiro.

## Estado da biblioteca com a régua nova (2026-07-19)

A nota perceptual vem da ENERGIA, não da transcrição → funciona nas 111 músicas
sem `words.json` que estavam sem nota nenhuma. **115 de 123 avaliadas** (antes:
12). Mediana **0,690**; só **20% acima de 0,8**; 16% abaixo de 0,4.

Piores: What I've Done (0,013), My Heart Will Go On (0,055), Toxicity (0,152),
Vento No Litoral (0,199 — **está em versão AO VIVO, viola nossa regra**),
The Killing Moon (0,211), La Isla Bonita (0,225 — a concordância dava 0,843!).

Melhores: Girls Just Want to Have Fun (0,936), Tiro ao Álvaro (0,921),
Creep (0,901, 1 linha ruim em 34).

## O resto que a MESMA fonte oferece (e por que destrava d e e)

Mesmo grupo (Deezer / Vaglio, Hennequin, Moussallam):

**1. DALI — verdade humana de verdade.** `deezer/MultilingualLyricsToAudioAlignment`
publica os splits do DALI: milhares de músicas com letra alinhada por humano em
nível de palavra/nota. É o benchmark que a gente tentou improvisar com LRC do
LRCLIB — e que falhou porque LRC casa por título e vem de outra gravação. Com
DALI dá pra calibrar de verdade, inclusive o viés de exibição do LRC.

**2. Alinhamento por FONEMA, independente de língua** (ISMIR 2020): BiLSTM com
CTC sobre FONEMAS em vez de palavras. É a chave que faltava — **nossa biblioteca
é majoritariamente em português**, e o whisper erra muito mais em PT que em EN.
Todo o nosso teto de ASR (0,83–0,996) é limitado por isso.

### Isso REESCREVE (d) e (e)

- **(d) era "prompt-biasing com a letra"** — enviesar o decodificador com o texto
  que já conhecemos. Risco alto de contaminação, ganho preso ao WER do modelo.
  **Vira**: casar por FONEMA, não por palavra. A letra oficial a gente já tem;
  convertida em fonemas, ela casa com o canto mesmo quando o ASR escreveu a
  palavra errada — que é o caso dominante em português. Sem contaminar, porque
  não mexe no decodificador.
- **(e) era "duas passadas"** — re-transcrever regiões fracas. Caro e ainda
  limitado pelo mesmo ASR. **Vira**: segunda passada só nas linhas com nota
  perceptual <0,5 (a régua já diz quais são), e por fonema. Custo cai muito
  porque são poucas linhas, e o alvo passa a ser o que o ouvido reclama.

Ou seja: as duas deixam de mirar "acertar mais palavra" e passam a mirar
"acertar o TEMPO das linhas que incomodam" — que é o que o Marcus sente.

## Cuidados que não podem ser esquecidos

- **A régua nova é energia-derivada** → circular pra julgar qualquer coisa que
  USE energia pra posicionar. Pro passo 1 isso importa: validar TAMBÉM contra o
  LRC humano e, no fim, contra o ouvido do Marcus.
- **O LRC humano tem viés próprio** (marca a linha antes, de propósito, pro
  cantor ler). Não dá pra mirar nele direto — precisa descontar esse viés antes.
- **O ouvido do Marcus é a autoridade final.** Nenhuma dessas notas substitui
  ele apertar play. O papel delas é ESCOLHER as 3 músicas que ele testa, não
  decidir no lugar dele.
