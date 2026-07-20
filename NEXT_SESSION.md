# PRÓXIMA SESSÃO — comece por aqui (escrito 2026-07-20)

## Estado em uma frase

Temos **três réguas** medindo a biblioteca inteira e um **porteiro** barrando
alinhamento colapsado. Nada foi CONSERTADO ainda — 47 de 123 músicas estão
marcadas. O conserto é o próximo passo.

## Comandos que funcionam (copiar e colar)

```bash
cd "C:/Users/marcu/dev/karaoke-app"
KARAOKE_NO_WORKER=1 .venv/Scripts/python.exe -m pytest tests/test_core.py -q   # 75 testes
KARAOKE_NO_WORKER=1 .venv/Scripts/python.exe server/ab_sw.py        # A/B casador
KARAOKE_NO_WORKER=1 .venv/Scripts/python.exe server/ab_skip.py      # A/B skip
KARAOKE_NO_WORKER=1 .venv/Scripts/python.exe server/measure_truth.py # LRC humano
KARAOKE_NO_WORKER=1 .venv/Scripts/python.exe server/bench_truth.py  # bancada 11 músicas
```

Commit: `git commit -F /tmp/msg.txt` (PowerShell 5.1 quebra com `-m` por causa
de aspas/parênteses; `$TMPDIR` NÃO existe no Git Bash daqui — use `/tmp/`).
**Sem assinatura do Claude nos commits.**

## ‼️ GOTCHAS — erros que EU cometi nesta sessão. Não repita.

### 1. Comparar A com B exige as MESMAS amostras
`onset_error_median` RESELECIONA quais linhas são verificáveis a cada chamada.
Medindo cada lado solto, o Smith-Waterman apareceu **5× pior** no Take Me Out
(178→958ms) quando na comparação pareada era **3× melhor** (178→58ms). Eu quase
descartei uma feature boa por causa disso. Use `_erro_pareado()` /
`linhas_verificaveis()` em QUALQUER A/B.

### 2. Toda régua tem ponto cego — e o ponto cego é onde mora o bug
- `onset_error_median` mede distância ao onset MAIS PRÓXIMO → **não vê
  deslocamento sistemático**. Dizia 178ms numa música 695ms atrasada.
- `alignment_agreement` compara com a NOSSA transcrição → **auto-referente**
  pro motor global, e cega onde o ASR não transcreveu nada.
- `display_coverage` vê letra faltando/amontoada → **cega pro tempo deslocado**.
- `perceptual_score` vem da ENERGIA → **circular** pra julgar qualquer coisa
  que USE energia pra posicionar (o skip, o retarget).
Regra: quem posiciona com o sinal X não pode ser julgado pelo sinal X.

### 3. Verdade fundamental TAMBÉM se valida
O LRC do LRCLIB casa por título/artista e **muitas vezes é de outra gravação**.
Whisky a Go-Go tem LRC que acaba em 153s numa música de 249s → dava AAE de **75
SEGUNDOS** e culpava nosso alinhamento por erro da fonte. `measure_truth.py`
descarta LRC que não cobre ≥75% da duração. **Mantenha essa guarda.**

### 4. O LRC humano tem viés PRÓPRIO
Ele marca a linha ANTES do canto de propósito, pro cantor conseguir ler. Por
isso ele diz que o Epitáfio (que o Marcus jura estar perfeito) está 400ms fora.
**Não mire no LRC direto** — desconte esse viés antes.

### 5. Correção global se VALIDA antes de valer
Vale pro reconcile, pro viés e pro que vier. Aplique numa CÓPIA, meça, e só
mantenha se melhorar. A correção de viés era um palpite aplicado sem conferência
e errava nos DOIS sentidos (desistia quando precisava, agia sem checar).

### 6. Guarda por FRAÇÃO recusa justamente o caso que importa
Minha primeira versão do skip exigia ≥15% do buraco cantado — e com isso
recusava os buracos de 39s/40s/33s, que são exatamente os que a feature existe
pra resolver. Buraco longo é quase todo instrumental POR DEFINIÇÃO. Use
limiar ABSOLUTO quando o caso de uso é "buraco grande".

### 7. Mais âncora não é melhor âncora
O difflib achava MAIS âncoras que o Smith-Waterman — casando `I`/`know`/`you`
soltos numa região onde a transcrição está VAZIA, cravando linha 20s fora.
**Não** existe fallback "se o SW achar menos, usa o difflib". Foi removido de
propósito.

### 8. Minhas hipóteses foram refutadas 3 vezes. Meça ANTES de afirmar.
- "linhas curtas punem o Samurai" → estavam INFLANDO a nota (0,411→0,337).
- "a cauda explica o Bad Boys" → não explicava (mediana 1,000, zero linha ruim).
  A explicação era cobertura: 70% do canto SEM letra na tela.
- "o viés estragou o Take Me Out" → o viés nem tinha sido aplicado ali.
Diga "vou verificar", não "é por causa de".

### 9. Não reproduza LETRA no terminal
Despejar letra inteira dispara bloqueio (e é problema de direito autoral).
Diagnostique com **tempo + 2 primeiras palavras**, ou por contagem/similaridade.

## Ordem recomendada (o plano está em PERCEPTUAL_PLAN.md)

1. **Psycho Killer** — não tem `words.json` NEM transcrição. Passa o pipeline
   completo nele. É a lacuna que o selo novo não pega (cob 0,823, percept 0,690
   e o Marcus reprova). Entender por que ele passa é mais valioso que consertar.
2. **Retarget −67ms** (`ALVO_PERCEPTUAL`): `_vies_candidatos` hoje mira erro
   absoluto ZERO. O ótimo do ouvido é −67ms, e atrasar dói MUITO mais que
   adiantar (faixa de 90%: −170ms a +40ms). Escalar por música, barato,
   reversível, mexe nas 115 de uma vez. **Validar contra LRC humano + ouvido do
   Marcus, NÃO contra a nota perceptual** (seria circular — gotcha 2).
3. **Reprocessar as 47 marcadas** com o porteiro ativo. Epitáfio de CONTROLE:
   se ele cair, reverta. Baseline já gravado em `data/bench_truth.json`.
4. **Editor abre nas linhas ruins** — `perceptual_score` já devolve quais têm
   score <0,5. É o que transforma "editar a música" em "confirmar 3 linhas", e
   é o pedido literal do Marcus: *"quero passar meu tempo cantando, não
   editando"*.
5. **(d) e (e) reescritas**: viraram casamento por FONEMA (a biblioteca é
   majoritariamente em PT e o whisper erra muito mais em PT que em EN) e
   segunda passada SÓ nas linhas com nota <0,5. Fonte: Vaglio et al., ISMIR
   2020 + dataset DALI (`deezer/MultilingualLyricsToAudioAlignment`).

## Achados de dados que não se perdem

- **107 das 123** músicas usam o motor `whisper` ANTIGO; **111 sem `words.json`**.
- Cobertura mediana **0,801**; 26 abaixo de 0,7; 4 abaixo de 0,5.
- Nota perceptual mediana **0,690**; só 20% acima de 0,8.
- **Vento No Litoral está em versão AO VIVO** — viola a regra "só estúdio",
  troca o arquivo.
- Só **2 de 123** têm LRC humano confiável hoje (o resto veio de texto puro).

## Regras do projeto que não mudam

- Commits **sem** assinatura/co-author do Claude.
- Só versão **ESTÚDIO** das músicas (ao vivo arruína a separação).
- **NUNCA** dois servidores na mesma pasta `data/` (lock não atravessa
  host/container — causou a corrupção de 2026-07-17).
- Scripts: sempre `KARAOKE_NO_WORKER=1` + python do venv + escrita só via
  `main._update_entry`.
- **O ouvido do Marcus é a autoridade final.** As notas servem pra ESCOLHER
  quais 3 músicas ele testa, não pra decidir no lugar dele.

## Adendo — `perdidas`, o discriminador que faltava (fim da sessão 2026-07-20)

Psycho Killer NÃO tem deriva (delta −0,058s entre 1ª e 2ª metade — não é LRC de
outra gravação). Ele tem **20 linhas ótimas e 4 impossíveis** (>0,7s do canto),
em 44,1s / 77,4s / 92,6s / 131,2s. A imagem que o Marcus mandou era em 1:23 =
83s, colada na linha de 77,4s. Bate exatamente.

`perceptual_score` agora devolve `perdidas` (linhas >0,7s fora) e `onde` (os
timestamps). **Separou o veredito do Marcus perfeitamente**: Epitáfio 0/22 e
I Have a Dream 0/21 (ele aprova); Psycho Killer 4/24, Samurai 4/13,
September 1/10, Stayin' Alive 1/5 (ele reprova).

Média NUNCA vai capturar isso — 20 linhas boas diluem 4 desastres e a nota fica
em 0,690, "aprovado". **Uma única linha impossível estraga a música.**

Números da biblioteca: **262 linhas impossíveis de cantar**, cada uma com
timestamp. Selo em `perdidas>=2` marca 80 de 123; em `>=1` marcava 103 (84%,
vira ruído). `perdidas`/`onde` são gravados SEMPRE — a função deles é alimentar
o editor, não avisar.

➡️ **Isso torna o item 4 do plano o de maior retorno agora**: o editor abrindo
em `onde` transforma "editar a música" em "confirmar 2 linhas". Os timestamps
já existem, é só a UI.

## ✅ FEITO 2026-07-20 (fim da sessão): editor guiado

`server/rescore.py` grava as 3 réguas em toda a biblioteca sem realinhar (não
roda modelo — seguro). Rodado: 122 atualizadas, 80 marcadas, 262 linhas ruins.

Front: card mostra "⚠ 4 linhas" (não mais "revisar sync"); editor abre NA
primeira linha errada com áudio 2s antes; botão `⚠ linha 2/4 · próxima` circula;
linha suspeita em âmbar só no modo edição. Verificado no navegador.

### Gotcha 10 — probe errado dá falso negativo no teste
Psycho Killer toca pelo motor `stems`: `audio.currentTime` fica 0 e o seek
PARECE quebrado. Use `getTime()` / `engine.startOffset`. Quase reportei bug
inexistente.

### Gotcha 11 — rotas do app
O app NÃO está em `/` nem em `/app`. `/` é a landing; o app é **`/app.html`**
(StaticFiles com html=True montado na raiz). Cards abrem por `.card-play`, não
por clique no card.

### Antes de subir servidor: CHECAR A PORTA
Regra do `data/` (incidente 2026-07-17). `curl -s -m 4 http://127.0.0.1:8777/`
antes de `preview_start`, e `preview_stop` ao terminar. `.claude/launch.json`
já existe com o alvo `karaoke`.

## ➡️ Próximo (a ordem não mudou)
1. Retarget −67ms (`ALVO_PERCEPTUAL`) — validar contra LRC humano + ouvido.
2. Reprocessar as 80 marcadas, Epitáfio de CONTROLE.
3. (d)/(e) por FONEMA.

## ✅ FEITO 2026-07-20 (cont.): régua monotônica — o ponto cego fechado

`casar_linhas_onsets()`: casa linha↔onset em SEQUÊNCIA (DP), no lugar de "onset
mais próximo". Conserta dois defeitos de uma vez:
1. **atribuição** — não precisa mais exigir pausa de 0,8s antes da linha, então
   cobre muito mais (Psycho Killer 24→32 linhas, September 10→19, Stayin' 5→9);
2. **cegueira a deslocamento uniforme** — Take Me Out: verdade humana diz +695ms
   atrasado, régua velha dizia 178ms (cega), régua NOVA mede **+530ms**.

O erro absoluto SOBE (178→1704ms) e isso é honestidade, não regressão: a linha
deixa de "escapar" pro onset conveniente ao lado.

Separação do veredito do Marcus preservada e mais nítida:
Epitáfio 0,894 e I Have a Dream 0,792 (ele aprova) contra Psycho Killer 0,680,
September 0,442, Stayin' Alive 0,217 (ele reprova).

### Gotcha 12 — custo de pulo alto demais ESCONDE o pior
Com `pulo=0.9`, pular os dois lados (1,8) saía mais barato que casar uma linha
2s fora — e as PIORES linhas sumiam da conta, o oposto do que a régua serve.
`pulo=1.8` (> max_dist/2) faz qualquer casamento dentro de 3s ser preferido.
Regra geral: em matching com skip, confira se o skip não está comendo o sinal.

### Bad Boys / Final Countdown continuam "sem medição" — e ISSO é o diagnóstico
Só 2 das 38 linhas do Bad Boys têm algum onset a menos de 3s. Não é a régua
falhando: é que a letra não está onde há canto. Quem os reprova é a
`display_coverage` (0,298 e 0,030). As réguas se cobrem.

Após `rescore.py` com a régua nova: **99 de 123 marcadas, 502 linhas perdidas**
(antes 80 e 262) — os números pioraram porque a régua enxerga mais, não porque
o alinhamento piorou.

## ⚠️ Gotchas 13 e 14 (reprocessamento das 3 — 2026-07-20)

### 13. `--fresh` só é seguro com `pristineSynced` — e só 5 de 123 têm
Sem ele, `reset_to_pristine` cai no `align_best_candidate`, que **RE-BUSCA a
letra na internet**. Em lote isso re-buscaria a letra de ~118 músicas, com risco
de pegar versão AO VIVO (já nos mordeu no Flor de Tangerina). Trava posta em
`align_v2_apply.py`: `--fresh` é ignorado (com log) quando não há pristine.

### 14. A RÉGUA muda quando a música ganha transcrição
`sung_energy` usa a máscara de fala (`speechmap.json`). Música sem transcrição
é medida com energia CRUA; depois de transcrita, com energia MASCARADA. São
instrumentos diferentes.

Eu comparei "0,680 antes × 0,424 depois" no Psycho Killer e concluí REGRESSÃO —
errado. Medindo os dois estados com a régua atual: **0,333 (antes) × 0,424
(depois)** = melhorou. Quase revertí uma melhoria. É o gotcha 1 (comparar com
instrumentos diferentes) numa roupa nova.

**Regra**: ao comparar antes/depois de um reprocessamento que cria
`speechmap.json`, meça os DOIS estados com o estado final da régua — nunca use
número guardado de antes.

## Resultado do teste das 3 (aguardando o ouvido do Marcus)
| música | nota | perdidas | cobertura | veredito da régua |
|---|---|---|---|---|
| Epitáfio (CONTROLE) | 0,894 | 0 | 0,866 | intacto ✅ |
| Bad Boys | 0,718 | 1 | 0,298 → **0,903** | grande melhora ✅ |
| Psycho Killer | 0,424 | 5 | 0,719 | melhorou (0,333→0,424), ainda ruim |

## ‼️ Gotcha 15 — o TERCEIRO ponto cego, achado de OUVIDO (Bad Boys 2:30)

O Marcus cantou e achou o que três réguas não viam: linha 56 durava **0,40s**
(5 palavras — pisca e some) e a 57 ficava **6,13s** travada na tela.

- `perceptual_score` só olha o INÍCIO da linha → cego
- `display_coverage` só olha o TOTAL coberto (dava 0,903, ótimo) → cego
- `alignment_agreement` compara texto, não tempo de exibição → cego

`duracao_suspeita(sid, lines)` fecha isso. **Duração longa NÃO é defeito por si**
— nota sustentada dura mesmo (o detector cru reprovava 7 linhas do I Have a
Dream, que ele aprova). O defeito é a linha ficar na tela **sem ninguém
cantando**: quem decide é a energia, não o relógio (>60% muda + >3s).

Validado: Bad Boys pega 150,9 e 151,3 (exatamente o que ele ouviu);
Epitáfio zerado; Psycho Killer 2 esmagadas + 13 arrastadas.
Biblioteca: 52 de 123 afetadas — 123 esmagadas, 60 arrastadas.

**LIÇÃO GERAL**: cada régua nova nasceu de um defeito que o ouvido dele achou e
os números não. Antes de soltar lote, vale sempre uma rodada de canto — está
saindo mais barato que qualquer análise.

## ⏳ PENDENTE, relatado pelo Marcus e AINDA NÃO investigado
"às vezes o 'bad boy' não aparecia no gráfico de baixo pra pontuar" — é o
subsistema de PITCH/pontuação (pitch.json), não o alinhamento. Não olhei ainda.

## ‼️ Gotcha 16 — LETRA DE OUTRA VERSÃO (Psycho Killer, achado pelo ouvido)

Marcus: "de 1:23 a 1:34 dá pra ouvir cantando, mas não tem nada a ver com o que
aparece". Não era alinhamento: **a letra era de outra gravação**.

O ASR ouvia "You're stirring a conversation, you can't even finish it" e NENHUMA
linha da nossa letra continha "conversation" — faltava um verso inteiro.
Nossa letra casava 12/34 linhas com o áudio; o LRCLIB tinha candidatos com
17/35 E com o verso. A nossa veio de uma versão de **313s**; a gravação tem 260s.
Mesma classe do Whisky a Go-Go. Depois da troca: verso presente em 82,9s (=1:23),
nota 0,424→0,526, esmagadas 2→0.

### Como escolher letra: pergunte ao ÁUDIO, não ao ranking da fonte
Critério que funcionou: entre os candidatos com duração compatível (±5s), pegar
o que mais casa com a TRANSCRIÇÃO. Isso deveria estar no pipeline de seleção de
letra — hoje não está. **É o próximo item de maior valor**: se a letra está
errada, alinhador nenhum salva, e nenhuma das nossas 4 réguas acusava direito.

### ⚠️ Cuidado ao medir "letra estrangeira" pelo teto
Eu quase reportei "62% das linhas são estrangeiras". Falso: "Qu'est-ce que c'est",
"Fa-fa-fa-fa" e os versos em FRANCÊS são reais, e o teto dá 0 porque o ASR em
inglês não os transcreve. Teto baixo = limite do ASR OU letra errada — só o
conjunto (verso ausente + duração incompatível) prova.

### Gotcha 17 — edição por script sem conferir
Meu `sed`/replace inseriu `"duracao": dur` no dict mas NÃO a linha que calcula
`dur` (a âncora não bateu). Resultado: NameError em produção, alinhamento com 0
linhas. **Sempre rodar os testes depois de edição por script** — o pytest pegou.

## Estado das 3 testadas (veredito do Marcus + réguas)
| música | ouvido dele | nota | obs |
|---|---|---|---|
| Epitáfio | controle, ok | 0,894 | intacto |
| Bad Boys | "BEEEM melhor", defeito em 2:30 | 0,718 | defeito localizado e marcado |
| Psycho Killer | verso errado em 1:23 | 0,526 | letra trocada, verso no lugar |

## Fim da sessão 2026-07-20 — estado e a PRÓXIMA PISTA

Feito depois do teste cantado: causa-raiz da letra de outra versão corrigida no
`rank` (duração antes de ter sincronia), `corrigir_duracoes` no pipeline (as
duas pontas do defeito do Bad Boys 2:30), limpeza (`_vies_vs_onsets` removida),
`PENDENCIAS.md` e `refetch_lyrics.py`.

Trocas de letra aplicadas (medidas nos dois estados com a MESMA régua):
| música | linhas | nota | perdidas | cobertura |
|---|---|---|---|---|
| Eu Vou Estar | 23→**31** | 0,437→**0,668** | 5→**0** | 0,591→**0,821** |
| Send Me An Angel | 44→44 | 0,849→0,852 | 1→1 | 0,731→0,720 |

### ➡️ COMECE POR AQUI: o alinhamento TRUNCA O FIM da música
Stayin' Alive tem letra cobrindo **96%** da duração e alinhamento parando em
**71%**. Não é a letra (5 das 7 que eu suspeitei estavam certas — ver a
autocorreção no PENDENCIAS.md). É o alinhamento perdendo o trecho final.
Suspeitos: `drop_ghost_lines` comendo o fim, ou o motor sem âncora lá.
Isso explicaria várias das piores notas de uma vez — é a pista mais quente.

### Docker: NÃO TESTADO
Docker Desktop estava **pausado manualmente**; só dá pra retomar pela interface
(ícone da baleia → Unpause). `docker desktop start` responde "already running" e
o daemon segue recusando. `.claude/launch.json` já existe com o alvo `karaoke`.
Antes de subir qualquer coisa: conferir a porta 8777 (regra do `data/` — lock
não atravessa host/container).

## 🐳 DOCKER — TESTADO 2026-07-20 (funciona, com ressalvas)

Teste de fumaça com `solta-a-voz:latest` (imagem de 37h atrás), container em
porta 8899 com pasta `data` DESCARTÁVEL (nunca monte a real num teste):

| item | resultado |
|---|---|
| landing `/` | HTTP 200 |
| app `/app.html` | HTTP 200 |
| API `/api/songs` | 200, `[]` (biblioteca vazia = volume novo, correto) |
| volume | criou `library.lock`, `media/`, `models/`, `stems/` |
| ffmpeg | 7.1.5 ✓ |
| torch | 2.13.0+**cpu** ✓ (índice cpu funcionou, imagem 4,29GB) |

### Build NOVO (com o código de hoje) — validado também
`docker build` do zero: **1,08GB** contra 4,29GB da imagem antiga, e agora com
`faster-whisper` presente. Landing/app/API os três em 200.

### Ressalvas encontradas
- **`faster_whisper` faltava na imagem antiga** — entrou no `requirements.txt`
  depois dela. O código degrada bem (`_get_faster_whisper()` cai no stable-ts),
  mas perde o caminho rápido. **Rebuild é obrigatório quando requirements muda.**
- **`torchaudio` não carregava** (`OSError: _torchaudio.abi3.so`) nas DUAS
  imagens: o `torch` vinha do índice CPU e o `torchaudio` entrava depois, como
  dependência transitiva do índice PADRÃO — versões incompatíveis. **Corrigido
  no Dockerfile** (instala os dois juntos, do mesmo índice). Falta rebuildar
  pra confirmar. Afeta só o motor MMS, que é opcional e tem fallback — some
  sem avisar, que é o pior tipo de defeito.
- Build completo demora **muito** (torch + deps): ~20min nesta máquina.

### Regra que virou comentário no docker-compose.yml
O compose monta `./data:/app/data` — a MESMA pasta do `start.bat`. Rodar os dois
juntos corrompe o `library.json` (lock não atravessa host/container, incidente
de 2026-07-17). Aviso agora está no próprio arquivo.
