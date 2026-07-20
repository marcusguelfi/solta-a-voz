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
