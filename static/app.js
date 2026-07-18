/* ============ SOLTA A VOZ — player de karaokê ============
 *
 * Áudio (motor "engine"):
 *  - "stems" (modo principal): voz e instrumental são faixas separadas pela IA,
 *    tocadas como AudioBufferSource em sync de sample. Slider Voz = volume real
 *    da voz do cantor original (padrão 0% = karaokê puro).
 *  - "centercut": SÓ fallback de emergência quando os stems falham no load —
 *    atenua o canal central (mid/side) do áudio original em tempo real.
 *    (música não preparada nem abre o player; ver isReady)
 *
 * Letra: lyrics.lines do servidor traz início E fim de cada frase CANTADA
 * (forced alignment); a linha acende/apaga seguindo a cantoria, com LYRIC_LEAD
 * de antecipação e ajuste manual opcional (menu ☰).
 *
 * Pontuação: mic -> pitch por autocorrelação -> comparação com pitch.json
 * (melodia do cantor original), tolerante a oitava. Nota 0-100 por frase.
 *
 * Pitch lane: canvas com as notas/ritmo do cantor rolando + rastro da sua voz.
 * Fila da festa: ids em localStorage, auto-avanço no fim da música.
 */

const $ = (id) => document.getElementById(id);
const audio = $("audio");

// ---------------------------------------------------------------- estado
let songs = [];
let current = null;
let lyrLines = [];
let manualOffset = 0;      // ajuste fino do usuário (por música)
let autoOffset = 0;        // calculado pelo servidor (por música)
let rafId = null;
let seeking = false;
let pollTimer = null;

const PROCESSING = new Set(["queued", "separating", "analyzing", "aligning"]);
const STATUS_LABEL = {
  queued: "na fila de preparo…",
  separating: "🤖 separando a voz…",
  analyzing: "🎼 analisando a melodia…",
  aligning: "🎧 alinhando com a cantoria…",
};

// ---------------------------------------------------------------- helpers
function fmtTime(s) {
  s = Math.max(0, Math.round(s || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function toast(msg, isErr = false) {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

function paintRange(input) {
  const pct = ((input.value - input.min) / (input.max - input.min)) * 100;
  input.style.setProperty("--val", pct + "%");
}

// ---------------------------------------------------------------- motor de áudio
const engine = {
  ctx: null, limiter: null, mode: null, // "stems" | "centercut"
  // stems: relógio = ctx.currentTime - startedAt + startOffset (ver getTime)
  buffers: null, sources: [], vocalG: null, instG: null,
  startedAt: 0, startOffset: 0, playing: false,
  // centercut (fallback): grafo mid/side pendurado no <audio>
  cc: null,
};

function ensureCtx() {
  if (engine.ctx) return;
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const limiter = ctx.createDynamicsCompressor();
  limiter.threshold.value = -3; limiter.knee.value = 0;
  limiter.ratio.value = 12; limiter.attack.value = 0.002; limiter.release.value = 0.25;
  limiter.connect(ctx.destination);
  engine.ctx = ctx;
  engine.limiter = limiter;
  engine.vocalG = ctx.createGain();
  engine.instG = ctx.createGain();
  engine.vocalG.connect(limiter);
  engine.instG.connect(limiter);
}

function buildCenterCut() {
  if (engine.cc) return;
  const ctx = engine.ctx;
  const src = ctx.createMediaElementSource(audio);
  const split = ctx.createChannelSplitter(2);
  src.connect(split);
  const g = (v) => { const n = ctx.createGain(); n.gain.value = v; return n; };

  const midBus = g(1);
  const mL = g(0.5), mR = g(0.5);
  split.connect(mL, 0); split.connect(mR, 1);
  mL.connect(midBus); mR.connect(midBus);

  const sideL = g(1), sideR = g(1);
  const sL0 = g(0.5), sL1 = g(-0.5), sR0 = g(-0.5), sR1 = g(0.5);
  split.connect(sL0, 0); split.connect(sL1, 1); sL0.connect(sideL); sL1.connect(sideL);
  split.connect(sR0, 0); split.connect(sR1, 1); sR0.connect(sideR); sR1.connect(sideR);

  const midLow = ctx.createBiquadFilter();
  midLow.type = "lowpass"; midLow.frequency.value = 140; midLow.Q.value = 0.7;
  const midHigh = ctx.createBiquadFilter();
  midHigh.type = "highpass"; midHigh.frequency.value = 140; midHigh.Q.value = 0.7;
  midBus.connect(midLow); midBus.connect(midHigh);

  const vocalGain = g(0.25);
  midHigh.connect(vocalGain);
  const instL = g(1), instR = g(1), instC = g(1);
  sideL.connect(instL); sideR.connect(instR); midLow.connect(instC);

  const merger = ctx.createChannelMerger(2);
  instL.connect(merger, 0, 0);
  instR.connect(merger, 0, 1);
  instC.connect(merger, 0, 0); instC.connect(merger, 0, 1);
  vocalGain.connect(merger, 0, 0); vocalGain.connect(merger, 0, 1);
  merger.connect(engine.limiter);

  engine.cc = { vocalGain, instGains: [instL, instR, instC] };
}

function stopSources() {
  const old = engine.sources;
  engine.sources = []; // primeiro esvazia: onended atrasado das antigas vira no-op
  old.forEach((s) => { try { s.stop(); } catch {} });
}

function stemsPlayFrom(offset) {
  const ctx = engine.ctx;
  stopSources();
  const now = ctx.currentTime + 0.04;
  for (const key of ["vocals", "instrumental"]) {
    const s = ctx.createBufferSource();
    s.buffer = engine.buffers[key];
    s.connect(key === "vocals" ? engine.vocalG : engine.instG);
    s.start(now, Math.min(offset, s.buffer.duration));
    engine.sources.push(s);
  }
  const src0 = engine.sources[0];
  src0.onended = () => {
    // fontes antigas (descartadas por seek/pausa/troca) disparam onended atrasado
    if (!engine.sources.includes(src0)) return;
    if (engine.playing) {
      engine.playing = false;
      engine.startOffset = getDuration();
      $("play-btn").textContent = "▶";
      if (score.enabled) showResults();
      else playNextInQueue(); // festa não para: emenda a próxima da fila
    }
  };
  engine.startedAt = now;
  engine.startOffset = offset;
  engine.playing = true;
}

function getTime() {
  if (engine.mode === "stems") {
    if (!engine.playing) return engine.startOffset;
    return Math.min(engine.ctx.currentTime - engine.startedAt + engine.startOffset,
                    getDuration());
  }
  return audio.currentTime;
}

function getDuration() {
  if (engine.mode === "stems") return engine.buffers?.instrumental?.duration || 0;
  return audio.duration || current?.duration || 0;
}

function enginePlay() {
  engine.ctx?.resume();
  if (engine.mode === "stems") {
    if (!engine.playing) stemsPlayFrom(engine.startOffset >= getDuration() - 0.2 ? 0 : engine.startOffset);
  } else {
    audio.play();
  }
  $("play-btn").textContent = "⏸";
}

function enginePause() {
  if (engine.mode === "stems") {
    if (engine.playing) {
      engine.startOffset = getTime();
      engine.playing = false;
      stopSources();
    }
  } else {
    audio.pause();
  }
  $("play-btn").textContent = "▶";
}

function engineSeek(t) {
  t = Math.max(0, Math.min(t, getDuration()));
  if (engine.mode === "stems") {
    if (engine.playing) stemsPlayFrom(t);
    else engine.startOffset = t;
  } else {
    audio.currentTime = t;
  }
  syncScoreCursor();
}

function engineIsPlaying() {
  return engine.mode === "stems" ? engine.playing : !audio.paused;
}

function applyMixer() {
  if (!engine.ctx) return;
  const t = engine.ctx.currentTime;
  const vocal = $("vocal-slider").value / 100;
  const inst = $("inst-slider").value / 100;
  if (engine.mode === "stems") {
    engine.vocalG.gain.setTargetAtTime(vocal, t, 0.03);
    engine.instG.gain.setTargetAtTime(inst, t, 0.03);
  } else if (engine.cc) {
    engine.cc.vocalGain.gain.setTargetAtTime(vocal, t, 0.03);
    engine.cc.instGains.forEach((n) => n.gain.setTargetAtTime(inst, t, 0.03));
  }
}

// ---------------------------------------------------------------- pontuação
// mic -> pitch por autocorrelação -> comparação com a melodia do cantor original
// (extraída do stem de voz no servidor). Nota por frase, oitava não importa.
const score = {
  enabled: false, micStream: null, analyser: null, buf: null,
  ref: null,            // pitch.json: {hop, midi[] (null=sem nota), energy[] 0/1}
  samples: [],          // amostras do mic: {t (tempo do áudio), midi}
  nextToScore: 0, // próxima linha da letra a ser pontuada
  total: 0, maxPossible: 0, lineResults: [],
};
let micTickCount = 0;

function detectPitch(buf, sampleRate) {
  const SIZE = buf.length;
  let rms = 0;
  for (let i = 0; i < SIZE; i++) rms += buf[i] * buf[i];
  rms = Math.sqrt(rms / SIZE);
  if (rms < 0.012) return null; // silêncio

  const c = new Float32Array(SIZE);
  for (let lag = 0; lag < SIZE; lag++) {
    let sum = 0;
    for (let i = 0; i < SIZE - lag; i++) sum += buf[i] * buf[i + lag];
    c[lag] = sum;
  }
  let d = 0;
  while (d < SIZE - 1 && c[d] > c[d + 1]) d++;
  let maxval = -1, maxpos = -1;
  for (let i = d; i < SIZE; i++) {
    if (c[i] > maxval) { maxval = c[i]; maxpos = i; }
  }
  if (maxpos <= 0 || maxval / c[0] < 0.3) return null; // pouco periódico = não é nota
  let T0 = maxpos;
  const x1 = c[T0 - 1], x2 = c[T0], x3 = c[T0 + 1] ?? x2;
  const a = (x1 + x3 - 2 * x2) / 2, b = (x3 - x1) / 2;
  if (a) T0 = T0 - b / (2 * a);
  const f = sampleRate / T0;
  if (f < 60 || f > 1200) return null;
  return f;
}

async function enableMic() {
  ensureCtx();
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  const src = engine.ctx.createMediaStreamSource(stream);
  const an = engine.ctx.createAnalyser();
  an.fftSize = 2048;
  an.smoothingTimeConstant = 0;
  src.connect(an); // só análise — o mic não toca nas caixas
  score.micStream = stream;
  score.analyser = an;
  score.buf = new Float32Array(an.fftSize);
  score.enabled = true;
  localStorage.setItem("mic:pref", "1");
  $("mic-btn").classList.add("on");
  $("mic-btn").textContent = "🎤 pontuando";
  if (!mp.active) $("score-chip").hidden = false; // no mp os chips dos jogadores mostram
  if (engine.mode !== "stems") {
    toast("A pontuação precisa do preparo por IA — prepare a música na biblioteca");
  } else if (!score.ref) {
    toast("Sem análise de melodia pra essa música ainda — prepare-a de novo se ela é antiga");
  } else {
    toast("Microfone ligado — solta a voz! 🎤 (fones = pontuação mais precisa)");
  }
}

function disableMic() {
  score.micStream?.getTracks().forEach((t) => t.stop());
  score.micStream = null;
  score.analyser = null;
  score.enabled = false;
  $("mic-btn").classList.remove("on");
  $("mic-btn").textContent = "🎤 pontuar";
}

function resetScore() {
  score.samples = [];
  score.nextToScore = 0;
  score.total = 0;
  score.maxPossible = 0;
  score.lineResults = [];
  $("score-val").textContent = "0";
  $("results").hidden = true;
}

// seek pula frases: pontuação recomeça da primeira frase que ainda vai tocar
function syncScoreCursor() {
  const lt = lyricTime();
  let i = 0;
  while (i < lyrLines.length &&
         ((lyrLines[i].end ?? lyrLines[i].t + 6) + 0.7) <= lt) i++;
  score.nextToScore = i;
}

// menor erro (em semitons, oitava dobrada) entre a nota cantada e a referência
// numa janela de ±350ms — absorve a latência do mic e pequenos atrasos
function bestRefError(t, sungMidi) {
  const { hop, midi } = score.ref;
  const k0 = Math.max(0, Math.floor((t - 0.35) / hop));
  const k1 = Math.min(midi.length - 1, Math.ceil((t + 0.1) / hop));
  let best = null;
  for (let k = k0; k <= k1; k++) {
    const r = midi[k];
    if (r === null) continue;
    let d = (sungMidi - r) % 12;
    if (d > 6) d -= 12;
    if (d < -6) d += 12;
    d = Math.abs(d);
    if (best === null || d < best) best = d;
  }
  return best;
}

function finalizeLine(i) {
  if (!score.enabled || !score.ref || engine.mode !== "stems") return;
  const line = lyrLines[i];
  if (!line) return;
  const lineEnd = (line.end ?? lyrLines[i + 1]?.t ?? line.t + 6) + 0.3;
  const a0 = line.t + autoOffset - manualOffset; // janela em tempo do áudio
  const a1 = Math.min(lineEnd + autoOffset - manualOffset, a0 + 15);
  const { hop, midi } = score.ref;
  let refVoiced = 0;
  for (let k = Math.max(0, Math.floor(a0 / hop));
       k < Math.min(midi.length, Math.ceil(a1 / hop)); k++) {
    if (midi[k] !== null) refVoiced++;
  }
  if (refVoiced < 8) return; // linha sem melodia mensurável (vinheta, fala…)

  const mics = score.samples.filter((s) => s.t >= a0 && s.t < a1);
  let pts = 0;
  let label = "Cadê a voz? 👀";
  if (mics.length >= 3) {
    let sum = 0, matched = 0;
    for (const s of mics) {
      const err = bestRefError(s.t, s.midi);
      if (err === null) continue;
      matched++;
      sum += err <= 0.75 ? 1 : err <= 1.5 ? 0.7 : err <= 2.5 ? 0.35 : 0;
    }
    if (matched >= 3) {
      pts = Math.round(100 * (sum / matched));
      label = pts >= 90 ? "PERFEITO! ✨" : pts >= 75 ? "Mandou bem!" :
              pts >= 55 ? "Boa!" : pts >= 30 ? "Quase…" : "Ops… 🙈";
    }
  }
  // roteia a pontuação: pro dono da frase (multiplayer) ou pro placar único
  if (mp.active) {
    const o = mp.owner[i] ?? 0;
    mp.totals[o] += pts;
    mp.maxes[o] += 100;
    mp.results[o].push(pts);
    updateMpChips();
  } else {
    score.total += pts;
    score.maxPossible += 100;
    score.lineResults.push(pts);
    $("score-val").textContent = score.total.toLocaleString("pt-BR");
  }
  showRating(label, pts);
}

function showRating(label, pts) {
  const el = $("rating-pop");
  el.textContent = pts > 0 ? `${label} +${pts}` : label;
  el.className = "rating-pop " +
    (pts >= 90 ? "t0" : pts >= 75 ? "t1" : pts >= 55 ? "t2" : pts >= 30 ? "t3" : "t4");
  el.hidden = false;
  el.style.animation = "none";
  void el.offsetWidth; // reinicia a animação
  el.style.animation = "";
  clearTimeout(showRating._t);
  showRating._t = setTimeout(() => { el.hidden = true; }, 1600);
}

function showResults() {
  // fecha as frases que ainda não pontuaram (a última raramente fecha sozinha)
  while (score.nextToScore < lyrLines.length &&
         lyrLines[score.nextToScore].t < lyricTime() + 1) {
    finalizeLine(score.nextToScore++);
  }
  if (mp.active) { showMpResults(); return; }
  $("res-single").hidden = false;
  $("res-mp").hidden = true;
  if (!score.maxPossible) return;
  const pct = (score.total / score.maxPossible) * 100;
  const grade = pct >= 93 ? "S" : pct >= 82 ? "A" : pct >= 68 ? "B" :
                pct >= 50 ? "C" : pct >= 30 ? "D" : "E";
  $("res-grade").textContent = grade;
  $("res-grade").className = "grade-" + grade;
  $("res-total").textContent =
    `${score.total.toLocaleString("pt-BR")} de ${score.maxPossible.toLocaleString("pt-BR")} pontos`;
  const perfect = score.lineResults.filter((p) => p >= 90).length;
  $("res-detail").textContent =
    `${perfect} frase${perfect === 1 ? "" : "s"} perfeita${perfect === 1 ? "" : "s"} · ${score.lineResults.length} pontuadas`;
  const key = "best:" + current.id;
  const prev = parseInt(localStorage.getItem(key) || "0");
  if (score.total > prev) {
    localStorage.setItem(key, String(score.total));
    $("res-record").textContent = prev
      ? `🏆 novo recorde! (antes: ${prev.toLocaleString("pt-BR")})` : "🏆 recorde registrado!";
  } else {
    $("res-record").textContent = `recorde: ${prev.toLocaleString("pt-BR")}`;
  }
  $("res-next").hidden = getQueue().length === 0;
  $("results").hidden = false;
}

// ---- pitch lane: gráfico horizontal com as notas do cantor original rolando
// (janela: 2s de passado, 6s de futuro) + rastro colorido da SUA voz por cima.
// Sempre visível no modo IA; o rastro aparece quando o mic está ligado.
let laneRange = null;
let laneModes = []; // por frase: "melody" (notas) ou "rhythm" (rap falado)

// janelas das frases da letra em tempo do ÁUDIO — a melodia só aparece dentro
// delas (o que o pyin detecta fora é sobra da separação: violão, reverb…)
function getLaneWindows() {
  if (!lyrLines.length) return null;
  const shift = autoOffset - manualOffset;
  return lyrLines.map((l, i) => [
    l.t + shift - 0.3,
    (l.end ?? lyrLines[i + 1]?.t ?? l.t + 6) + shift + 0.3,
  ]);
}

function computeLaneRange() {
  const wins = getLaneWindows();
  const { hop, midi } = score.ref;
  const notes = [];
  let wi = 0;
  for (let k = 0; k < midi.length; k++) {
    const m = midi[k];
    if (m === null) continue;
    if (wins) {
      const tA = k * hop;
      while (wi < wins.length && wins[wi][1] < tA) wi++;
      if (!(wi < wins.length && tA >= wins[wi][0])) continue;
    }
    notes.push(m);
  }
  notes.sort((a, b) => a - b);
  if (notes.length < 20) return null;
  const lo = notes[Math.floor(notes.length * 0.03)] - 2;
  const hi = notes[Math.floor(notes.length * 0.97)] + 2;
  return [lo, Math.max(hi, lo + 10)];
}

function drawLane() {
  const lane = $("pitch-lane");
  if (engine.mode !== "stems" || !score.ref) {
    lane.hidden = true;
    return;
  }
  if (!laneRange) laneRange = computeLaneRange();
  if (!laneRange) { lane.hidden = true; return; }
  lane.hidden = false;

  const dpr = window.devicePixelRatio || 1;
  const w = lane.clientWidth, h = lane.clientHeight;
  if (!w || !h) return;
  if (lane.width !== Math.round(w * dpr)) {
    lane.width = Math.round(w * dpr);
    lane.height = Math.round(h * dpr);
  }
  const ctx = lane.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const now = getTime();
  const t0 = now - 2, t1 = now + 6;
  const X = (t) => ((t - t0) / (t1 - t0)) * w;
  const Y = (m) => h - ((m - laneRange[0]) / (laneRange[1] - laneRange[0])) * (h - 10) - 5;
  const { hop, midi } = score.ref;

  // por frase da letra: CANTO vira notas na altura certa; RAP FALADO (sem
  // afinação detectável) vira blocos de ritmo na linha central — o flow.
  // Fora de frase não desenha nada (sobra da separação não é canto).
  const wins = getLaneWindows() || [[0, getDuration() || t1]];
  const energy = score.ref.energy;
  const nowWinIdx = wins.findIndex((w) => now >= w[0] && now <= w[1]);
  const colorFor = (endT, inNow) => inNow ? "rgba(255,179,71,.95)"
    : endT < now ? "rgba(93,79,116,.5)" : "rgba(157,143,176,.85)";

  const windowMode = (i) => {
    if (!energy) return "melody";
    if (laneModes[i] === undefined) {
      const [a, b] = wins[i];
      const ka = Math.max(0, Math.floor(a / hop));
      const kb = Math.min(midi.length, Math.ceil(b / hop));
      let pitched = 0;
      for (let k = ka; k < kb; k++) if (midi[k] !== null) pitched++;
      laneModes[i] = pitched / Math.max(1, kb - ka) >= 0.25 ? "melody" : "rhythm";
    }
    return laneModes[i];
  };

  // desenha uma camada de segmentos dentro de [a,b]∩[t0,t1]
  const drawSegs = (arr, isOn, a, b, inNow, opts) => {
    const k0 = Math.max(0, Math.floor(Math.max(a, t0) / hop));
    const k1 = Math.min(arr.length - 1, Math.ceil(Math.min(b, t1) / hop));
    let segStart = null, segSum = 0, segN = 0, prevM = null;
    const flush = (endK) => {
      if (segStart !== null && segN >= 2) {
        const x1 = X(segStart * hop), x2 = X(endK * hop);
        // quantização à la UltraSinger: a NOTA exibida é o semitom mais próximo
        // da média do segmento — lane limpo, sem vibrato/slide serrilhando.
        // Só o desenho: a pontuação segue comparando com o midi cru.
        const yy = opts.pitched ? Y(Math.round(segSum / segN)) : h / 2;
        ctx.fillStyle = opts.faint
          ? (inNow ? "rgba(255,179,71,.30)" : "rgba(157,143,176,.22)")
          : colorFor(endK * hop, inNow);
        ctx.beginPath();
        ctx.roundRect(x1, yy - opts.hpx / 2, Math.max(x2 - x1, 3), opts.hpx, opts.hpx / 2);
        ctx.fill();
      }
      segStart = null; segSum = 0; segN = 0;
    };
    for (let k = k0; k <= k1; k++) {
      const v = arr[k];
      const on = isOn(v);
      if (!on || (opts.pitched && prevM !== null && Math.abs(v - prevM) > 0.7)) flush(k);
      if (on) {
        if (segStart === null) segStart = k;
        if (opts.pitched) segSum += v;
        segN++;
      }
      prevM = opts.pitched && on ? v : null;
    }
    flush(k1);
  };

  for (let i = 0; i < wins.length; i++) {
    const [a, b] = wins[i];
    if (b < t0 || a > t1) continue;
    const inNow = i === nowWinIdx;
    if (windowMode(i) === "melody") {
      // faixa fina de energia embaixo (toda a frase marcada) + notas por cima
      if (energy) drawSegs(energy, (v) => v === 1, a, b, inNow, { pitched: false, hpx: 3, faint: true });
      drawSegs(midi, (v) => v !== null, a, b, inNow, { pitched: true, hpx: 7 });
    } else {
      drawSegs(energy, (v) => v === 1, a, b, inNow, { pitched: false, hpx: 7 });
    }
  }

  // linha do "agora"
  ctx.strokeStyle = "rgba(255,179,71,.75)";
  ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(X(now), 4); ctx.lineTo(X(now), h - 4); ctx.stroke();

  // rastro da sua voz (dobrado pra oitava da melodia; cor = afinação)
  for (const s of score.samples) {
    if (s.t < t0 || s.t > now) continue;
    const k = Math.round(s.t / hop);
    const r = midi[k] ?? midi[k - 1] ?? midi[k + 1];
    const anchor = r ?? (laneRange[0] + laneRange[1]) / 2;
    const m = s.midi - Math.round((s.midi - anchor) / 12) * 12;
    let color = "rgba(244,238,248,.45)";
    if (r != null) {
      const err = Math.abs((((s.midi - r) % 12) + 18) % 12 - 6);
      color = err <= 0.75 ? "#3ddc84" : err <= 1.75 ? "#ffb347" : "#ff2d78";
    }
    ctx.fillStyle = color;
    ctx.beginPath(); ctx.arc(X(s.t), Y(m), 3, 0, 7); ctx.fill();
  }
}

$("mic-btn").onclick = async () => {
  if (score.enabled) {
    localStorage.setItem("mic:pref", "0"); // desligou de propósito
    disableMic();
    return;
  }
  try {
    await enableMic();
  } catch (err) {
    toast("Não consegui acessar o microfone: " + err.message, true);
  }
};

$("res-again").onclick = () => {
  resetScore();
  if (mp.active) {
    mp.totals = [0, 0]; mp.maxes = [0, 0]; mp.results = [[], []];
    updateMpChips();
  }
  engineSeek(0);
  enginePlay();
};
$("res-back").onclick = () => { $("results").hidden = true; closePlayer(); };

// sliders têm memória separada por modo (no modo IA a voz padrão é 0%)
function loadMixerFor(mode) {
  const defs = mode === "stems" ? { vocal: 0, inst: 100 } : { vocal: 25, inst: 100 };
  for (const [key, input, label] of [
    ["vocal", $("vocal-slider"), $("vocal-pct")],
    ["inst", $("inst-slider"), $("inst-pct")],
  ]) {
    const saved = localStorage.getItem(`mix:${key}:${mode}`);
    input.value = saved !== null ? saved : defs[key];
    label.textContent = input.value + "%";
    paintRange(input);
  }
  $("mode-hint").textContent = mode === "stems"
    ? "🤖 modo IA — voz e instrumental 100% separados"
    : "⚡ modo rápido (center-cut) — o preparo por IA deixa a separação perfeita";
  applyMixer();
}

// ---------------------------------------------------------------- biblioteca
const cardEls = new Map(); // id -> {card, meta, prog, progFill}

// busca + ordenação + filtro de gênero (a view é derivada; `songs` é a fonte)
const libFilter = {
  q: "", sort: "recent", genre: null,
  view: localStorage.getItem("cfg:libView") || "all", // tudo junto por padrão
};
let lastViewKey = "";

function bestOf(id) {
  return parseInt(localStorage.getItem("best:" + id) || "0");
}

function viewSongs() {
  let list = songs.slice();
  const q = libFilter.q.trim().toLowerCase();
  if (q) {
    list = list.filter((s) =>
      `${s.title || ""} ${s.artist || ""}`.toLowerCase().includes(q));
  }
  if (libFilter.genre) list = list.filter((s) => (s.genre || "") === libFilter.genre);
  const cmp = {
    recent: (a, b) => (b.addedAt || 0) - (a.addedAt || 0),
    title: (a, b) => (a.title || "").localeCompare(b.title || "", "pt"),
    artist: (a, b) => (a.artist || "").localeCompare(b.artist || "", "pt"),
    diff: (a, b) => (a.lyrics?.difficulty?.score ?? 999) - (b.lyrics?.difficulty?.score ?? 999),
    dur: (a, b) => (a.duration || 0) - (b.duration || 0),
    best: (a, b) => bestOf(b.id) - bestOf(a.id),
  }[libFilter.sort];
  return cmp ? list.sort(cmp) : list;
}

function renderGenreChips() {
  const box = $("genre-chips");
  const counts = new Map();
  songs.forEach((s) => {
    if (s.genre) counts.set(s.genre, (counts.get(s.genre) || 0) + 1);
  });
  box.innerHTML = "";
  if (!counts.size) return;
  const mk = (label, value) => {
    const b = document.createElement("button");
    b.className = "g-chip" + (libFilter.genre === value ? " on" : "");
    b.textContent = label;
    b.onclick = () => {
      libFilter.genre = libFilter.genre === value ? null : value;
      renderGrid();
      renderGenreChips();
    };
    box.appendChild(b);
  };
  mk("todos", null);
  [...counts.entries()].sort((a, b) => b[1] - a[1])
    .forEach(([g, n]) => mk(`${g} (${n})`, g));
}

// ---- prévia no hover: segurou o mouse ~2,5s no card, toca um trechinho
// com fade in/out e volume comedido — ninguém merece susto
const preview = { audio: new Audio(), timer: null, fade: null };
let PREVIEW_VOL = parseFloat(localStorage.getItem("cfg:previewVol") ?? "0.15");

function fadeTo(target, ms, done) {
  clearInterval(preview.fade);
  const steps = 12;
  const step = (target - preview.audio.volume) / steps;
  let i = 0;
  preview.fade = setInterval(() => {
    i++;
    preview.audio.volume = Math.min(1, Math.max(0, preview.audio.volume + step));
    if (i >= steps) {
      clearInterval(preview.fade);
      if (done) done();
    }
  }, ms / steps);
}

// autoplay: hover NÃO é gesto de usuário — destrava o elemento no 1º clique
// em qualquer lugar (toca um wav silencioso dentro do gesto)
const SILENT_WAV = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=";
document.addEventListener("pointerdown", function unlockPreview() {
  document.removeEventListener("pointerdown", unlockPreview);
  preview.audio.muted = true;
  preview.audio.src = SILENT_WAV;
  preview.audio.play().catch(() => {}).finally(() => {
    preview.audio.pause();
    preview.audio.removeAttribute("src");
    preview.audio.muted = false;
  });
});

function stopPreview() {
  clearTimeout(preview.timer);
  preview.timer = null;
  const cleanup = () => {
    preview.audio.pause();
    preview.audio.removeAttribute("src");
    document.querySelectorAll(".song-card.previewing")
      .forEach((c) => c.classList.remove("previewing"));
  };
  if (!preview.audio.paused) fadeTo(0, 350, cleanup); // fade out suave
  else { clearInterval(preview.fade); cleanup(); }
}

function startPreviewSoon(songId, card) {
  clearTimeout(preview.timer);
  preview.timer = setTimeout(() => {
    const s = songs.find((x) => x.id === songId);
    if (!s || !isReady(s) || !$("player-view").hidden) return;
    preview.audio.src = `/api/audio/${s.id}`;
    preview.audio.onloadedmetadata = () => {
      try { preview.audio.currentTime = Math.floor((s.duration || 60) * 0.3); } catch {}
      preview.audio.volume = 0;
      preview.audio.play().then(() => fadeTo(PREVIEW_VOL, 900)).catch(() => {});
    };
    card.classList.add("previewing");
  }, 2500);
}

async function loadSongs() {
  songs = await api("/api/songs");
  const viewKey = viewSongs().map((s) => s.id).join(",");
  if (viewKey === lastViewKey && cardEls.size) {
    songs.forEach(updateCardStatus); // atualização in-place: nada de piscar o grid
  } else {
    renderGrid();
  }
  renderGenreChips();
  renderQueue();
  clearTimeout(pollTimer);
  if (songs.some((s) => PROCESSING.has(s.status))) {
    pollTimer = setTimeout(() => loadSongs().catch(() => {}), 4000);
  }
}

function diffOf(song) {
  return song?.lyrics?.difficulty?.label || null;
}

// música só libera quando o preparo completo terminou (sync de letra + melodia)
function isReady(song) {
  return song?.status === "ready" && song?.stems;
}

function metaHTML(song) {
  const diff = diffOf(song);
  const hasSync = !!(song?.lyrics?.synced || song?.lyrics?.lines);
  let statusPill;
  if (PROCESSING.has(song.status)) {
    statusPill = `<span class="pill working">${STATUS_LABEL[song.status] || "preparando…"}</span>`;
  } else if (song.status === "ready" && song.stems) {
    statusPill = `<span class="pill ready">✓ karaokê pronto</span>`;
  } else if (song.status === "error") {
    statusPill = `<span class="pill error" title="${(song.errorMsg || "").replace(/"/g, "&quot;")}">falhou — tentar de novo</span>`;
  } else {
    statusPill = `<span class="pill prepare">preparar karaokê</span>`;
  }
  // sync "fino" = passou pelo whisper-align ou foi editado à mão; o resto
  // (LRC bruto/offset global) merece um aviso — e o editor resolve
  const unverified = song.status === "ready" && song.stems && hasSync &&
    !["whisper", "manual"].includes(song.lyrics?.alignMethod);
  return `
    <span class="pill">${fmtTime(song.duration)}</span>
    ${diff ? `<span class="pill diff ${diff.toLowerCase()}">${diff}</span>` : ""}
    ${hasSync ? `<span class="pill${song.lyrics?.alignMethod === "manual" ? " manual" : ""}">${song.lyrics?.alignMethod === "manual" ? "✍ letra sua" : "letra sync"}</span>` : ""}
    ${unverified ? `<span class="pill warn" title="letra sem sync fino — abra a música e use ☰ → editar tempos">⚠ revisar sync</span>` : ""}
    ${statusPill}`;
}

function bindMetaActions(song, refs) {
  const retry = refs.meta.querySelector(".pill.error, .pill.prepare");
  if (retry) retry.onclick = async (e) => {
    e.stopPropagation();
    try {
      await api(`/api/process/${song.id}`, { method: "POST" });
      loadSongs();
    } catch (err) { toast(err.message, true); }
  };
}

function updateCardStatus(song) {
  const refs = cardEls.get(song.id);
  if (!refs) return;
  const html = metaHTML(song);
  if (refs.meta.dataset.snapshot !== html) {
    refs.meta.dataset.snapshot = html;
    refs.meta.innerHTML = html;
    bindMetaActions(song, refs);
  }
  const busy = PROCESSING.has(song.status);
  refs.prog.hidden = !busy;
  if (busy) refs.progFill.style.width = `${song.progress || 3}%`;
  refs.card.classList.toggle("locked", !isReady(song));
}

function renderGrid() {
  const grid = $("song-grid");
  grid.innerHTML = "";
  cardEls.clear();
  const view = viewSongs();
  lastViewKey = view.map((s) => s.id).join(",");
  const filtered = view.length !== songs.length;
  $("empty-msg").hidden = view.length > 0;
  $("empty-msg").textContent = songs.length && !view.length
    ? "nada com esse filtro 🔎 — limpa a busca ou o gênero"
    : 'palco vazio… clica em "＋ adicionar música" e solta a voz 🎤';
  if (!songs.length) $("add-panel").hidden = false; // palco vazio: já abre o form
  $("song-count").textContent = songs.length
    ? (filtered ? `${view.length} de ${songs.length}` :
       `${songs.length} música${songs.length > 1 ? "s" : ""}`) : "";
  // gavetas estilo Steam na visão padrão; grid plano quando o Marcus pede
  // "todos juntos" ou quando há busca/filtro/ordenação ativos
  const shelves = libFilter.view !== "all" && !libFilter.q && !libFilter.genre &&
    libFilter.sort === "recent" && songs.some((s) => s.genre);
  grid.classList.toggle("as-shelves", shelves);
  const vt = $("view-toggle");
  if (vt) vt.textContent = libFilter.view === "all" ? "🗂 por gênero" : "▦ todos juntos";
  if (!shelves) {
    view.forEach((song, i) => grid.appendChild(makeCard(song, i)));
    return;
  }
  const groups = new Map();
  view.forEach((s) => {
    const g = s.genre || "sem gênero";
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(s);
  });
  [...groups.entries()]
    .sort((a, b) => b[1].length - a[1].length)
    .forEach(([g, list]) => {
      const shelf = document.createElement("section");
      shelf.className = "shelf";
      const head = document.createElement("div");
      head.className = "shelf-head";
      head.innerHTML = `<h3></h3><span class="shelf-count"></span>
        <span class="shelf-nav"><button class="btn-mini sh-prev" title="anteriores">‹</button><button class="btn-mini sh-next" title="próximas">›</button></span>`;
      head.querySelector("h3").textContent = g;
      head.querySelector(".shelf-count").textContent =
        `${list.length} música${list.length > 1 ? "s" : ""}`;
      const row = document.createElement("div");
      row.className = "shelf-row";
      list.forEach((song, i) => row.appendChild(makeCard(song, i)));
      head.querySelector(".sh-prev").onclick = () => row.scrollBy({ left: -row.clientWidth * 0.8, behavior: "smooth" });
      head.querySelector(".sh-next").onclick = () => row.scrollBy({ left: row.clientWidth * 0.8, behavior: "smooth" });
      makeDragScroll(row);
      shelf.append(head, row);
      grid.appendChild(shelf);
    });
}

$("view-toggle").onclick = () => {
  libFilter.view = libFilter.view === "all" ? "shelves" : "all";
  localStorage.setItem("cfg:libView", libFilter.view);
  renderGrid();
};

// arrastar a gaveta com o mouse (estilo Steam) — setinhas viram acessório.
// Depois de arrastar de verdade (>6px), o clique que fecha o gesto é engolido
// no capture pra não abrir a música sem querer.
function makeDragScroll(row) {
  let down = false, dragged = false, startX = 0, startScroll = 0;
  row.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    down = true; dragged = false;
    startX = e.clientX;
    startScroll = row.scrollLeft;
  });
  row.addEventListener("pointermove", (e) => {
    if (!down) return;
    const dx = e.clientX - startX;
    if (!dragged && Math.abs(dx) > 6) {
      dragged = true;
      row.classList.add("dragging"); // desliga o scroll-snap durante o arrasto
      try { row.setPointerCapture(e.pointerId); } catch {}
      stopPreview();
    }
    if (dragged) row.scrollLeft = startScroll - dx;
  });
  const release = () => { down = false; row.classList.remove("dragging"); };
  row.addEventListener("pointerup", release);
  row.addEventListener("pointercancel", release);
  row.addEventListener("click", (e) => {
    if (dragged) { e.stopPropagation(); e.preventDefault(); dragged = false; }
  }, true);
}

function makeCard(song, i) {
  const card = document.createElement("div");
  card.className = "song-card";
  card.style.animationDelay = `${Math.min(i * 0.05, 0.4)}s`;
  const coverURL = song.hasCover || song.thumb ? `/api/cover/${song.id}` : null;
  card.innerHTML = `
    <div class="cover-wrap">
      ${coverURL
        ? `<img class="cover" src="${coverURL}" alt="" loading="lazy"
             onerror="this.outerHTML='<div class=cover-fallback>${(song.title || "?")[0].toUpperCase()}</div>'">`
        : `<div class="cover-fallback">${(song.title || "?")[0].toUpperCase()}</div>`}
      <div class="card-record" hidden></div>
      <div class="card-actions">
        <button class="card-queue" title="Adicionar à fila da festa">➕</button>
        <button class="card-play" title="Cantar!">▶</button>
        <button class="card-del" title="Remover">✕</button>
      </div>
    </div>
    <div class="card-body">
      <div class="card-title"></div>
      <div class="card-artist"></div>
      <div class="card-meta"></div>
      <div class="prog" hidden><i></i></div>
    </div>`;
  card.querySelector(".card-title").textContent = song.title || "Sem título";
  card.querySelector(".card-artist").textContent = song.artist || "—";
  const best = parseInt(localStorage.getItem("best:" + song.id) || "0");
  const rec = card.querySelector(".card-record");
  rec.textContent = best ? `🏆 recorde: ${best.toLocaleString("pt-BR")}` : "🏆 sem recorde — seja o 1º!";
  rec.hidden = false;
  card.querySelector(".card-del").onclick = async (e) => {
    e.stopPropagation();
    if (!await askConfirm(`Remover "${song.title}" do repertório?`,
      "a música, as faixas separadas e a letra sincronizada vão embora junto")) return;
    await api(`/api/songs/${song.id}`, { method: "DELETE" });
    loadSongs();
  };
  card.querySelector(".card-queue").onclick = (e) => {
    e.stopPropagation();
    const cur = songs.find((s) => s.id === song.id) || song;
    if (!isReady(cur)) {
      toast("essa ainda está em preparo — entra na fila quando ficar pronta");
      return;
    }
    setQueue([...getQueue(), song.id]);
    toast(`🎶 "${song.title}" entrou na fila da festa`);
  };
  card.onclick = () => {
    const cur = songs.find((s) => s.id === song.id) || song;
    if (!isReady(cur)) {
      toast(PROCESSING.has(cur.status)
        ? `🎛 "${cur.title}" ainda em preparo (${cur.progress || 0}%) — libera quando o sync estiver perfeito`
        : `essa música precisa do preparo — usa o "preparar karaokê" no card`);
      return;
    }
    openPlayer(cur);
  };
  // prévia: mouse parado ~3s no card toca um trechinho da música
  card.onmouseenter = () => startPreviewSoon(song.id, card);
  card.onmouseleave = () => stopPreview();
  cardEls.set(song.id, {
    card,
    meta: card.querySelector(".card-meta"),
    prog: card.querySelector(".prog"),
    progFill: card.querySelector(".prog i"),
  });
  updateCardStatus(song);
  return card;
}

// modal de confirmação estilizado (nada de confirm() nativo quebrando o clima)
function askConfirm(msg, detail) {
  return new Promise((resolve) => {
    $("confirm-msg").textContent = msg;
    $("confirm-detail").textContent = detail || "";
    $("confirm").hidden = false;
    const done = (v) => { $("confirm").hidden = true; resolve(v); };
    $("confirm-yes").onclick = () => done(true);
    $("confirm-no").onclick = () => done(false);
  });
}

$("add-toggle").onclick = () => {
  const panel = $("add-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) $("url-input").focus();
};

// sidebar (app.html): os botões novos proxiam os originais (ids preservados)
$("sb-add")?.addEventListener("click", () => $("add-toggle").click());
$("sb-dueto")?.addEventListener("click", () => { mp.mode = "dueto"; openMpSetup(); });
$("sb-duelo")?.addEventListener("click", () => { mp.mode = "duelo"; openMpSetup(); });

// ---------------------------------------------------------------- configurações
function applyLyrSize(scale) {
  document.documentElement.style.setProperty("--lyr-scale", scale);
}
applyLyrSize(localStorage.getItem("cfg:lyrSize") || "1");

function openSettings() {
  $("cfg-preview-vol").value = Math.round(PREVIEW_VOL * 100);
  $("cfg-pv-val").textContent = Math.round(PREVIEW_VOL * 100) + "%";
  $("cfg-lead").value = Math.round(LYRIC_LEAD * 100);
  $("cfg-lead-val").textContent = LYRIC_LEAD.toFixed(2).replace(".", ",") + "s";
  $("cfg-lyr-size").value = localStorage.getItem("cfg:lyrSize") || "1";
  $("cfg-theme").value = localStorage.getItem("cfg:theme") || "palco";
  $("settings").hidden = false;
}
$("sb-config")?.addEventListener("click", openSettings);
$("cfg-close").onclick = () => { $("settings").hidden = true; };
$("cfg-preview-vol").oninput = () => {
  PREVIEW_VOL = $("cfg-preview-vol").value / 100;
  localStorage.setItem("cfg:previewVol", String(PREVIEW_VOL));
  $("cfg-pv-val").textContent = Math.round(PREVIEW_VOL * 100) + "%";
};
$("cfg-lead").oninput = () => {
  LYRIC_LEAD = $("cfg-lead").value / 100;
  localStorage.setItem("cfg:lyricLead", String(LYRIC_LEAD));
  $("cfg-lead-val").textContent = LYRIC_LEAD.toFixed(2).replace(".", ",") + "s";
};
$("cfg-lyr-size").onchange = () => {
  localStorage.setItem("cfg:lyrSize", $("cfg-lyr-size").value);
  applyLyrSize($("cfg-lyr-size").value);
};
// tema de cores: paletas via :root[data-theme] (style.css); "palco" = padrão
function applyTheme(name) {
  if (name && name !== "palco") document.documentElement.dataset.theme = name;
  else delete document.documentElement.dataset.theme;
}
applyTheme(localStorage.getItem("cfg:theme"));
$("cfg-theme").onchange = () => {
  localStorage.setItem("cfg:theme", $("cfg-theme").value);
  applyTheme($("cfg-theme").value);
};

// sidebar recolhível — o estado sobrevive entre visitas
function applySbCollapsed(on) {
  document.body.classList.toggle("sb-collapsed", on);
  const t = $("sb-toggle");
  if (t) {
    t.textContent = on ? "›" : "‹";
    t.title = on ? "mostrar menu" : "esconder menu";
  }
  localStorage.setItem("cfg:sbCollapsed", on ? "1" : "0");
}
$("sb-toggle")?.addEventListener("click", () =>
  applySbCollapsed(!document.body.classList.contains("sb-collapsed")));
if (localStorage.getItem("cfg:sbCollapsed") === "1") applySbCollapsed(true);
$("cfg-clear-records").onclick = async () => {
  if (!await askConfirm("Zerar TODOS os recordes?", "apaga os recordes salvos neste navegador — sem volta")) return;
  Object.keys(localStorage).filter((k) => k.startsWith("best:"))
    .forEach((k) => localStorage.removeItem(k));
  toast("recordes zerados — bora fazer história de novo 🏆");
  renderGrid();
};

let searchDebounce = null;
$("lib-search").oninput = () => {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => {
    libFilter.q = $("lib-search").value;
    renderGrid();
  }, 150);
};
$("lib-sort").onchange = () => {
  libFilter.sort = $("lib-sort").value;
  renderGrid();
};

// ---------------------------------------------------------------- fila da festa
function getQueue() {
  try { return JSON.parse(localStorage.getItem("queue") || "[]"); } catch { return []; }
}

function setQueue(q) {
  localStorage.setItem("queue", JSON.stringify(q));
  renderQueue();
}

function renderQueue() {
  const bar = $("queue-bar");
  const q = getQueue().filter((id) => songs.some((s) => s.id === id));
  bar.hidden = q.length === 0;
  const list = $("queue-list");
  list.innerHTML = "";
  q.forEach((id, i) => {
    const s = songs.find((x) => x.id === id);
    const chip = document.createElement("span");
    chip.className = "q-chip";
    chip.textContent = `${i + 1}. ${s.title}`;
    const x = document.createElement("button");
    x.className = "q-x";
    x.textContent = "×";
    x.title = "Tirar da fila";
    x.onclick = () => {
      const q2 = getQueue();
      q2.splice(i, 1);
      setQueue(q2);
    };
    chip.appendChild(x);
    list.appendChild(chip);
  });
}

function playNextInQueue() {
  const q = getQueue();
  while (q.length) {
    const id = q.shift();
    const s = songs.find((x) => x.id === id);
    if (s && isReady(s)) {
      setQueue(q);
      openPlayer(s);
      return true;
    }
  }
  setQueue(q);
  return false;
}

$("queue-play").onclick = () => {
  if (!playNextInQueue()) toast("fila vazia — usa o ➕ nos cards pra montar a festa");
};
$("queue-clear").onclick = () => setQueue([]);
$("res-next").onclick = () => {
  $("results").hidden = true;
  if (!playNextInQueue()) closePlayer();
};

// ---------------------------------------------------------------- multiplayer local (dueto/duelo)
// Um mic passando entre dois jogadores. As frases da letra se revezam por verso
// (gap-based) — cada frase pontua pro seu dono. Dueto = placar combinado
// (cooperativo); Duelo = dois placares + vencedor. Mesma engine, resultado diferente.
const mp = {
  armed: false, active: false, mode: "duelo",
  players: [{ name: "", emoji: "🎤" }, { name: "", emoji: "🎶" }],
  owner: [], totals: [0, 0], maxes: [0, 0], results: [[], []],
};
const MP_EMOJIS = ["🎤", "🎶", "🦄", "🐯", "🔥", "🌟", "👑", "🎸", "🐸", "🦊", "💜", "⚡"];

function loadMpPlayers() {
  try {
    const saved = JSON.parse(localStorage.getItem("mp:players") || "null");
    if (saved && saved.length === 2) mp.players = saved;
  } catch {}
  mp.players[0].emoji ||= "🎤";
  mp.players[1].emoji ||= "🎶";
}
function playerName(o) { return mp.players[o].name || `Jogador ${o + 1}`; }

function buildEmojiRows() {
  document.querySelectorAll("#mp-setup .mp-player").forEach((pl) => {
    const o = +pl.dataset.p;
    const row = pl.querySelector(".mp-emoji-row");
    row.innerHTML = "";
    MP_EMOJIS.forEach((e) => {
      const b = document.createElement("button");
      b.className = "mp-emoji" + (mp.players[o].emoji === e ? " sel" : "");
      b.textContent = e;
      b.onclick = () => {
        mp.players[o].emoji = e;
        row.querySelectorAll(".mp-emoji").forEach((x) => x.classList.toggle("sel", x.textContent === e));
      };
      row.appendChild(b);
    });
    pl.querySelector(".mp-name-in").value = mp.players[o].name;
  });
}

function openMpSetup() {
  loadMpPlayers();
  buildEmojiRows();
  document.querySelectorAll(".mp-mode-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === mp.mode));
  $("mp-setup").hidden = false;
}
$("mp-toggle").onclick = openMpSetup;
$("mp-setup-cancel").onclick = () => { $("mp-setup").hidden = true; };
document.querySelectorAll(".mp-mode-btn").forEach((b) => {
  b.onclick = () => {
    mp.mode = b.dataset.mode;
    document.querySelectorAll(".mp-mode-btn").forEach((x) => x.classList.toggle("active", x === b));
  };
});
$("mp-start").onclick = () => {
  document.querySelectorAll("#mp-setup .mp-player").forEach((pl) => {
    mp.players[+pl.dataset.p].name = pl.querySelector(".mp-name-in").value.trim();
  });
  localStorage.setItem("mp:players", JSON.stringify(mp.players));
  mp.armed = true;
  $("mp-setup").hidden = true;
  const label = mp.mode === "duelo" ? "⚔️ Duelo" : "🎶 Dueto";
  const sep = mp.mode === "duelo" ? "vs" : "+";
  $("mp-banner-text").textContent =
    `${label}: ${mp.players[0].emoji} ${playerName(0)} ${sep} ${mp.players[1].emoji} ${playerName(1)}`;
  $("mp-banner").hidden = false;
  toast("modo 2 jogadores armado — escolha a música 🎵");
};
$("mp-cancel").onclick = () => { mp.armed = false; $("mp-banner").hidden = true; };

// revezamento das frases por verso: troca de cantor num silêncio > 2,5s ou a
// cada 6 frases seguidas (pra ninguém cantar a música inteira sozinho)
function assignOwners() {
  mp.owner = [];
  if (!mp.active || !lyrLines.length) return;
  let turn = 0, run = 0;
  for (let i = 0; i < lyrLines.length; i++) {
    if (i > 0) {
      const prev = lyrLines[i - 1];
      const gap = lyrLines[i].t - (prev.end ?? prev.t);
      if (gap > 2.5 || run >= 6) { turn ^= 1; run = 0; }
    }
    mp.owner[i] = turn;
    lyrLines[i].el.dataset.owner = turn;
    run++;
  }
}

function startMultiplayer() {
  mp.active = true;
  mp.armed = false;
  mp.totals = [0, 0]; mp.maxes = [0, 0]; mp.results = [[], []];
  $("mp-banner").hidden = true;
  $("score-chip").hidden = true;
  $("mp-scores").hidden = false;
  for (const o of [0, 1]) {
    const chip = $(`mp-chip-${o}`);
    chip.querySelector(".mpc-name").textContent = `${mp.players[o].emoji} ${playerName(o)}`;
    chip.querySelector("b").textContent = "0";
  }
  if (!score.enabled) enableMic().catch(() => {}); // pontuação exige mic
}

function stopMultiplayer() {
  mp.active = false;
  $("mp-scores").hidden = true;
  $("turn-indic").hidden = true;
}

function updateMpChips() {
  for (const o of [0, 1]) {
    $(`mp-chip-${o}`).querySelector("b").textContent = mp.totals[o].toLocaleString("pt-BR");
  }
}

function updateTurnIndicator(idx) {
  const el = $("turn-indic");
  const li = Math.min(Math.max(idx >= 0 ? idx : score.nextToScore, 0), mp.owner.length - 1);
  const o = mp.owner[li] ?? 0;
  el.className = "turn-indic p" + o;
  el.textContent = `🎤 ${mp.players[o].emoji} ${playerName(o)}`;
  el.hidden = false;
  $("mp-chip-0").classList.toggle("turn", o === 0);
  $("mp-chip-1").classList.toggle("turn", o === 1);
}

function pctToGrade(pct) {
  return pct >= 93 ? "S" : pct >= 82 ? "A" : pct >= 68 ? "B" :
         pct >= 50 ? "C" : pct >= 30 ? "D" : "E";
}

function showMpResults() {
  $("res-single").hidden = true;
  $("res-mp").hidden = false;
  for (const o of [0, 1]) {
    const p = mp.players[o], t = mp.totals[o], m = mp.maxes[o] || 0;
    const g = m ? pctToGrade((t / m) * 100) : "—";
    const side = $(`res-mp-p${o}`);
    side.classList.remove("win");
    side.innerHTML =
      `<div class="mp-emoji">${p.emoji}</div>` +
      `<div class="mp-name">${playerName(o)}</div>` +
      `<div class="mp-pts">${t.toLocaleString("pt-BR")}</div>` +
      `<div class="mp-grade grade-${g}">${g}</div>`;
  }
  if (mp.mode === "dueto") {
    $("res-mp-title").textContent = "Dueto 🎶";
    const tot = mp.totals[0] + mp.totals[1], max = mp.maxes[0] + mp.maxes[1];
    $("res-mp-verdict").textContent = max
      ? `Juntos: ${tot.toLocaleString("pt-BR")} pontos — nota ${pctToGrade((tot / max) * 100)}! 🎉`
      : "cadê a voz? 🎤";
  } else {
    $("res-mp-title").textContent = "Duelo ⚔️";
    const d = mp.totals[0] - mp.totals[1];
    if (d === 0) {
      $("res-mp-verdict").textContent = "Empate! 🤝";
    } else {
      const w = d > 0 ? 0 : 1;
      $(`res-mp-p${w}`).classList.add("win");
      $("res-mp-verdict").textContent =
        `${mp.players[w].emoji} ${playerName(w)} venceu por ${Math.abs(d).toLocaleString("pt-BR")}!`;
    }
  }
  $("res-next").hidden = true;
  $("results").hidden = false;
}

loadMpPlayers();

// busca a letra logo após adicionar: o badge de dificuldade aparece no card
// em segundos, sem esperar o pipeline chegar na etapa de alinhamento
async function warmLyrics(song) {
  try { await api(`/api/lyrics/${song.id}`); } catch {}
  loadSongs().catch(() => {});
}

// ---------------------------------------------------------------- adicionar
function setBusy(label) {
  $("add-progress").hidden = !label;
  if (label) $("add-progress-label").textContent = label;
  $("url-btn").disabled = !!label;
}

async function uploadFile(file) {
  setBusy(`subindo "${file.name}"…`);
  try {
    const fd = new FormData();
    fd.append("file", file);
    const song = await api("/api/upload", { method: "POST", body: fd });
    toast(`🎵 "${song.title}" no repertório! Preparando o karaokê…`);
    $("add-panel").hidden = true;
    await loadSongs();
    warmLyrics(song);
  } catch (err) {
    toast("Erro no upload: " + err.message, true);
  } finally {
    setBusy(null);
  }
}

async function addLink() {
  const url = $("url-input").value.trim();
  if (!url) return;
  setBusy("baixando do link… isso pode levar um minutinho");
  try {
    const res = await api("/api/link", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    $("url-input").value = "";
    $("add-panel").hidden = true;
    if (res.playlist) {
      toast(`🎶 importando ${res.count} músicas da playlist — vão aparecendo aqui…`);
      await loadSongs();
      // as músicas entram uma a uma (download em background); garante o polling
      [3000, 8000, 15000].forEach((ms) => setTimeout(() => loadSongs().catch(() => {}), ms));
    } else {
      toast(`🎵 "${res.title}" baixada! Preparando o karaokê…`);
      await loadSongs();
      warmLyrics(res);
    }
  } catch (err) {
    toast("Erro no download: " + err.message, true);
  } finally {
    setBusy(null);
  }
}

const dz = $("dropzone");
dz.onclick = () => $("file-input").click();
dz.onkeydown = (e) => { if (e.key === "Enter") $("file-input").click(); };
$("file-input").onchange = (e) => {
  if (e.target.files[0]) uploadFile(e.target.files[0]);
  e.target.value = "";
};
["dragenter", "dragover"].forEach((ev) =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files[0];
  if (file) uploadFile(file);
});
$("url-btn").onclick = addLink;
$("url-input").onkeydown = (e) => { if (e.key === "Enter") addLink(); };

// ---------------------------------------------------------------- letra
function parseLRC(text) {
  const out = [];
  for (const raw of (text || "").split("\n")) {
    const times = [...raw.matchAll(/\[(\d+):(\d+(?:\.\d+)?)\]/g)];
    const line = raw.replace(/\[[^\]]*\]/g, "").trim();
    if (!times.length || !line) continue;
    for (const m of times) out.push({ t: parseInt(m[1]) * 60 + parseFloat(m[2]), text: line });
  }
  return out.sort((a, b) => a.t - b.t);
}

function renderLyrics(lyr) {
  const scroller = $("lyrics-scroller");
  const fallback = $("lyrics-fallback");
  scroller.innerHTML = "";
  lyrLines = [];

  if (lyr?.lines || lyr?.synced) {
    fallback.hidden = true;
    scroller.hidden = false;
    // "lines" tem início E fim de cada frase CANTADA (forced alignment no servidor)
    lyrLines = lyr.lines
      ? lyr.lines.map((l) => ({ t: l.t, end: l.end, text: l.text, words: l.words }))
      : parseLRC(lyr.synced);
    // words = [[dt, dEnd, palavra], ...] relativos ao início da linha; pré-computa
    // a posição em CARACTERES de cada palavra pro preenchimento palavra-a-palavra
    lyrLines.forEach((line) => {
      if (!line.words || line.words.length < 2) return;
      let pos = 0;
      line.wchars = line.words.map(([dt, de, w]) => {
        let i = line.text.indexOf(w, pos);
        if (i < 0) i = pos;
        pos = i + w.length;
        return [dt, de, i, pos];
      });
    });
    lyrLines.forEach((line) => {
      const el = document.createElement("div");
      el.className = "lyr-line";
      const span = document.createElement("span");
      span.className = "fill";
      span.textContent = line.text;
      el.appendChild(span);
      scroller.appendChild(el);
      line.el = el;
      line.span = span;
    });
    return;
  }

  scroller.hidden = true;
  fallback.hidden = false;
  $("retry-artist").value = current?.artist || "";
  $("retry-title").value = current?.title || "";
  const plain = $("plain-lyrics");
  if (lyr?.plain) {
    $("fallback-msg").textContent = "Sem sincronia pra essa 😕 — mas segue a letra:";
    plain.textContent = lyr.plain;
    plain.hidden = false;
  } else {
    $("fallback-msg").textContent = "Nenhuma letra encontrada 😕 — confere o nome e tenta de novo:";
    plain.hidden = true;
  }
}

$("retry-btn").onclick = async () => {
  const artist = $("retry-artist").value.trim();
  const title = $("retry-title").value.trim();
  if (!artist && !title) return;
  $("retry-btn").disabled = true;
  try {
    const q = new URLSearchParams({ artist, title });
    const lyr = await api(`/api/lyrics/${current.id}?${q}`);
    if (!lyr.found) toast("Ainda nada… tenta variar o nome (sem 'feat', ao vivo etc.)", true);
    else toast("Letra encontrada! 🎉");
    current.lyrics = lyr;
    if (typeof lyr.autoOffset === "number") {
      autoOffset = lyr.autoOffset;
      updateOffsetLabel();
    }
    renderLyrics(lyr);
    setDiffBadge(lyr);
    loadSongs();
  } catch (err) {
    toast(err.message, true);
  } finally {
    $("retry-btn").disabled = false;
  }
};

function setDiffBadge(lyr) {
  const badge = $("p-diff");
  const d = lyr?.difficulty;
  badge.className = "diff-badge" + (d ? " " + d.label.toLowerCase() : "");
  badge.textContent = d ? `${d.label} · ${d.wpm} ppm` : "sem medição";
  badge.title = d ? `${d.words} palavras em ${d.lines} linhas — ${d.wpm} palavras/min cantado` : "";
  // aviso: letra existe mas não passou pelo sync fino nem por edição humana
  $("sync-warn").hidden = !(lyr?.found &&
    !["whisper", "manual"].includes(lyr.alignMethod));
}

function updateOffsetLabel() {
  const total = manualOffset;
  $("off-val").textContent = (total >= 0 ? "+" : "") + total.toFixed(1).replace(".", ",") + "s";
  $("off-auto").textContent = autoOffset
    ? `(auto ${autoOffset >= 0 ? "+" : ""}${autoOffset.toFixed(1).replace(".", ",")}s)` : "";
}

// % preenchido da linha ativa. Com words (timestamps por palavra do whisper,
// achado da pesquisa UltraStar): o preenchimento anda pelo comprimento em
// caracteres de cada palavra CANTADA — karaokê de verdade. Sem words (letras
// antigas, linhas intercaladas/editadas): interpolação linear de sempre.
function fillPercent(line, rel, dur) {
  if (!line.wchars) return Math.min(100, Math.max(0, (rel / dur) * 100));
  const total = line.text.length || 1;
  let chars = 0;
  for (const [dt, de, c0, c1] of line.wchars) {
    if (rel >= de) { chars = c1; continue; } // palavra já cantada inteira
    if (rel < dt) break;                     // ainda não chegou nesta
    chars = c0 + (c1 - c0) * ((rel - dt) / Math.max(de - dt, 0.05));
    break;
  }
  return Math.min(100, (chars / total) * 100);
}

// tempo da letra: posição do áudio menos o offset automático, mais o ajuste manual.
// LYRIC_LEAD acende a linha um pouco ANTES do canto — como karaokê de verdade,
// pra dar tempo de ler (a pontuação usa o tempo real do áudio, não é afetada).
let LYRIC_LEAD = parseFloat(localStorage.getItem("cfg:lyricLead") ?? "0.45");
function lyricTime() {
  return getTime() - autoOffset + manualOffset + LYRIC_LEAD;
}

// loop de sincronia
function tick() {
  rafId = requestAnimationFrame(tick);
  const dur = getDuration();

  if (!seeking) {
    $("seek").value = dur ? (getTime() / dur) * 1000 : 0;
    paintRange($("seek"));
    $("cur-time").textContent = fmtTime(getTime());
    $("tot-time").textContent = fmtTime(dur);
  }

  // amostra o microfone (~15x/s) enquanto toca no modo IA
  if (score.enabled && score.analyser && engine.mode === "stems" && engine.playing
      && ++micTickCount % 4 === 0) {
    score.analyser.getFloatTimeDomainData(score.buf);
    const f = detectPitch(score.buf, engine.ctx.sampleRate);
    if (f) {
      score.samples.push({ t: getTime(), midi: 69 + 12 * Math.log2(f / 440) });
      if (score.samples.length > 3000) score.samples.splice(0, 1500);
    }
  }
  drawLane();

  if (!lyrLines.length) return;
  const t = lyricTime();
  let idx = -1;
  for (let i = 0; i < lyrLines.length; i++) {
    if (lyrLines[i].t <= t) idx = i; else break;
  }
  // a linha segue a CANTORIA: só fica acesa enquanto a frase é cantada —
  // terminou a frase (outro, solo, lá-lá-lá), apaga e vira "done"
  const cur = idx >= 0 ? lyrLines[idx] : null;
  const curEnd = cur ? (cur.end ?? lyrLines[idx + 1]?.t ?? cur.t + 6) : 0;
  const pastEnd = !!cur && cur.end != null && t > cur.end + 0.8;
  lyrLines.forEach((line, i) => {
    const active = i === idx && !pastEnd;
    line.el.classList.toggle("active", active);
    line.el.classList.toggle("done", i < idx || (i === idx && pastEnd));
    // limpa o preenchimento das não-ativas (senão "cantar de novo" fica tudo pintado)
    if (!active && line.span.style.backgroundImage) line.span.style.backgroundImage = "";
  });

  // pontuação: fecha cada frase quando a janela DELA termina (imune a seek e outro)
  if (score.enabled) {
    while (score.nextToScore < lyrLines.length) {
      const ln = lyrLines[score.nextToScore];
      const end = ln.end ?? lyrLines[score.nextToScore + 1]?.t ?? ln.t + 6;
      if (t > end + 0.7) finalizeLine(score.nextToScore++);
      else break;
    }
  }

  if (mp.active) updateTurnIndicator(idx);

  // contagem regressiva antes da primeira linha e em pausas longas
  const next = lyrLines[idx + 1];
  const cd = $("countdown");
  let showCd = false;
  if (next) {
    const wait = next.t - t;
    const inIntro = idx === -1 && wait > 1;
    const inBreak = idx >= 0 && wait > 5 && (pastEnd || t > curEnd + 1.5);
    if ((inIntro || inBreak) && wait < 60) {
      showCd = true;
      cd.querySelectorAll("i").forEach((dot, n) => {
        dot.classList.toggle("on", wait <= 3 - n);
      });
    }
  }
  cd.hidden = !showCd;

  // preenchimento da linha ativa; sem linha ativa, a rolagem centraliza a PRÓXIMA
  if (cur && !pastEnd) {
    const p = fillPercent(cur, t - cur.t, Math.max(curEnd - cur.t, 0.1));
    cur.span.style.backgroundImage =
      `linear-gradient(90deg, #ff2d78, #ffb347 ${p}%, #f4eef8 ${p}%)`;
  }
  const focus = cur && !pastEnd ? cur : (next ?? cur ?? lyrLines[0]);
  if (focus) {
    const box = $("lyrics-box");
    const scroller = $("lyrics-scroller");
    const target = box.clientHeight / 2 - (focus.el.offsetTop + focus.el.offsetHeight / 2);
    scroller.style.transform = `translateY(${target}px)`;
    scroller.style.margin = "0 auto";
  }
}

// ---------------------------------------------------------------- player
async function openPlayer(song) {
  if (!isReady(song)) {
    toast("essa música ainda não terminou o preparo 🎛");
    return;
  }
  current = song;
  stopPreview();
  $("player-view").hidden = false;
  $("library-view").style.display = "none";
  $("p-title").textContent = song.title || "Sem título";
  $("p-artist").textContent = song.artist || "—";
  $("tot-time").textContent = fmtTime(song.duration);
  manualOffset = parseFloat(localStorage.getItem("lyroff:" + song.id) || "0");
  autoOffset = song.autoOffset || 0;
  updateOffsetLabel();

  ensureCtx();
  engine.ctx.resume();

  // o guard isReady acima garante stems; o ramo centercut abaixo sobrevive
  // apenas como cinto de segurança (e como fallback no catch do load)
  const useStems = song.status === "ready" && song.stems;
  engine.mode = useStems ? "stems" : "centercut";
  loadMixerFor(engine.mode);

  // pontuação: melodia de referência + mic (se o usuário deixou ligado)
  resetScore();
  score.ref = null;
  laneRange = null;
  laneModes = [];
  $("pitch-lane").hidden = true;
  if (useStems) {
    fetch(`/api/pitch/${song.id}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((p) => { if (current === song) score.ref = p; })
      .catch(() => {});
  }
  // multiplayer armado -> inicia dueto/duelo; senão garante modo single
  if (mp.armed && useStems) startMultiplayer();
  else stopMultiplayer();
  if (!mp.active && localStorage.getItem("mic:pref") === "1" && !score.enabled) {
    enableMic().catch(() => {});
  }

  if (useStems) {
    audio.pause();
    audio.removeAttribute("src");
    $("play-btn").disabled = true;
    $("p-artist").textContent = (song.artist || "—") + " · carregando faixas…";
    try {
      const [vocals, instrumental] = await Promise.all(
        ["vocals", "instrumental"].map(async (k) => {
          const r = await fetch(`/api/stems/${song.id}/${k}`);
          if (!r.ok) throw new Error("stem não encontrado");
          return engine.ctx.decodeAudioData(await r.arrayBuffer());
        }));
      if (current !== song) return;
      engine.buffers = { vocals, instrumental };
      engine.startOffset = 0;
      $("p-artist").textContent = song.artist || "—";
      enginePlay();
    } catch (err) {
      toast("Erro ao carregar faixas separadas — usando modo rápido. " + err.message, true);
      engine.mode = "centercut";
      loadMixerFor("centercut");
      buildCenterCut();
      audio.src = `/api/audio/${song.id}`;
      enginePlay();
    } finally {
      $("play-btn").disabled = false;
    }
  } else {
    buildCenterCut();
    audio.src = `/api/audio/${song.id}`;
    enginePlay();
  }

  setDiffBadge(song.lyrics);
  renderLyrics(song.lyrics);
  if (mp.active) assignOwners();
  if (!song.lyrics) {
    $("fallback-msg").textContent = "Buscando a letra…";
    $("lyrics-fallback").hidden = false;
    $("lyrics-scroller").hidden = true;
    try {
      const lyr = await api(`/api/lyrics/${song.id}`);
      if (current !== song) return;
      song.lyrics = lyr;
      renderLyrics(lyr);
      setDiffBadge(lyr);
      loadSongs();
    } catch {
      renderLyrics(null);
    }
  }

  cancelAnimationFrame(rafId);
  tick();
}

function closePlayer() {
  if (editMode) exitEdit(false);
  enginePause();
  stopSources();
  disableMic();
  stopMultiplayer();
  $("results").hidden = true;
  $("score-chip").hidden = true;
  $("player-menu").hidden = true;
  engine.buffers = null;
  audio.removeAttribute("src");
  cancelAnimationFrame(rafId);
  $("player-view").hidden = true;
  $("library-view").style.display = "";
  current = null;
  loadSongs().catch(() => {});
}

$("back-btn").onclick = closePlayer;

$("menu-btn").onclick = (e) => {
  e.stopPropagation();
  $("player-menu").hidden = !$("player-menu").hidden;
};
document.addEventListener("click", (e) => {
  const menu = $("player-menu");
  if (!menu.hidden && !menu.contains(e.target) && e.target.id !== "menu-btn") {
    menu.hidden = true;
  }
});

$("play-btn").onclick = () => {
  if (engineIsPlaying()) enginePause();
  else enginePlay();
};

audio.onended = () => { if (engine.mode === "centercut") $("play-btn").textContent = "▶"; };

const seek = $("seek");
seek.oninput = () => {
  seeking = true;
  paintRange(seek);
  $("cur-time").textContent = fmtTime((seek.value / 1000) * getDuration());
};
seek.onchange = () => {
  engineSeek((seek.value / 1000) * getDuration());
  seeking = false;
};

["vocal-slider", "inst-slider"].forEach((id) => {
  const input = $(id);
  input.oninput = () => {
    paintRange(input);
    $(id === "vocal-slider" ? "vocal-pct" : "inst-pct").textContent = input.value + "%";
    const key = id === "vocal-slider" ? "vocal" : "inst";
    localStorage.setItem(`mix:${key}:${engine.mode || "centercut"}`, input.value);
    applyMixer();
  };
});

function nudgeOffset(delta) {
  manualOffset = Math.round((manualOffset + delta) * 10) / 10;
  updateOffsetLabel();
  if (current) localStorage.setItem("lyroff:" + current.id, String(manualOffset));
}
$("off-minus").onclick = () => nudgeOffset(-0.5);
$("off-plus").onclick = () => nudgeOffset(0.5);

document.addEventListener("keydown", (e) => {
  if ($("player-view").hidden) return;
  if (e.target.tagName === "INPUT") return;
  if (e.code === "Space") { e.preventDefault(); $("play-btn").click(); }
  if (editMode && e.key === "Enter") { e.preventDefault(); editSet("t", getTime()); }
  if (e.key === "Escape") {
    if (editMode) { exitEdit(true); toast("edição descartada"); }
    else closePlayer();
  }
  if (e.key === "ArrowRight") engineSeek(getTime() + 5);
  if (e.key === "ArrowLeft") engineSeek(getTime() - 5);
});

// --------------------------------------------- editor humano de linhas
// O último recurso DEFINITIVO: o que a IA não enxerga (sussurro que a separação
// não capturou, harmonia suave, virada de andamento), a pessoa marca de ouvido.
// Fluxo: ☰ → "editar tempos" → clica na linha → play → Enter quando o canto
// começa. Ao salvar, tudo vai em tempo do áudio; o servidor zera o autoOffset
// e o ajuste manual local é limpo — a letra editada vira a verdade absoluta.
let editMode = false;
let editSel = -1;
const editShift = () => autoOffset - manualOffset; // linha + shift = áudio

function enterEdit() {
  if (!current) return;
  if (mp.active) { toast("finalize o dueto/duelo antes de editar 😉", true); return; }
  disableMic();
  editMode = true;
  $("edit-bar").hidden = false;
  $("player-menu").hidden = true;
  $("lyrics-scroller").hidden = false;
  $("lyrics-fallback").hidden = true;
  document.body.classList.add("editing");
  selectEditLine(-1);
}

function exitEdit(discard) {
  editMode = false;
  editSel = -1;
  $("edit-bar").hidden = true;
  document.body.classList.remove("editing");
  if (discard && current) renderLyrics(current.lyrics); // volta ao salvo
}

function selectEditLine(i) {
  editSel = i;
  lyrLines.forEach((l, j) => l.el.classList.toggle("edit-sel", j === i));
  const has = i >= 0;
  $("edit-tools").hidden = !has;
  $("edit-text").hidden = !has;
  $("edit-hint").hidden = has;
  if (has) {
    $("edit-text").value = lyrLines[i].text;
    refreshEditTimes();
  }
}

const fmtEdit = (s) => {
  s = Math.max(0, s);
  return `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0").replace(".", ",")}`;
};

function refreshEditTimes() {
  const l = lyrLines[editSel];
  if (!l) return;
  $("edit-t").textContent = fmtEdit(l.t + editShift());
  $("edit-end").textContent = fmtEdit(l.end + editShift());
}

// define início/fim da linha selecionada a partir de um tempo NO DOMÍNIO DO ÁUDIO
function editSet(field, audioT) {
  const l = lyrLines[editSel];
  if (!l) return;
  l[field] = Math.max(0, +(audioT - editShift()).toFixed(2));
  if (l.end < l.t + 0.3) {
    if (field === "t") l.end = +(l.t + 0.3).toFixed(2);
    else l.t = Math.max(0, +(l.end - 0.3).toFixed(2));
  }
  resolveOverlaps(editSel);
  refreshEditTimes();
}

// A LINHA EDITADA MANDA: vizinhos que ficarem por cima são empurrados
// (caso Já Sei Namorar: intro esticada até 30s "comida" pela linha antiga).
// Pra frente: cada linha começa depois do fim da anterior, em cascata até
// sobrar espaço. Pra trás: a anterior é aparada pra terminar antes.
function resolveOverlaps(from) {
  for (let i = from + 1; i < lyrLines.length; i++) {
    const prev = lyrLines[i - 1], l = lyrLines[i];
    if (l.t >= prev.end + 0.05) break; // já cabe: cascata termina
    l.t = +(prev.end + 0.05).toFixed(2);
    if (l.end < l.t + 0.3) l.end = +(l.t + 0.3).toFixed(2);
  }
  for (let i = from - 1; i >= 0; i--) {
    const next = lyrLines[i + 1], l = lyrLines[i];
    if (l.end <= next.t - 0.05) break;
    l.end = +Math.max(next.t - 0.05, 0.35).toFixed(2);
    if (l.t > l.end - 0.3) l.t = +Math.max(0, l.end - 0.3).toFixed(2);
  }
}
const editNudge = (field, d) => {
  const l = lyrLines[editSel];
  if (l) editSet(field, l[field] + editShift() + d);
};

$("et-now").onclick = () => editSet("t", getTime());
$("ee-now").onclick = () => editSet("end", getTime());
$("et-minus").onclick = () => editNudge("t", -0.1);
$("et-plus").onclick = () => editNudge("t", +0.1);
$("ee-minus").onclick = () => editNudge("end", -0.1);
$("ee-plus").onclick = () => editNudge("end", +0.1);

$("edit-play-line").onclick = () => {
  const l = lyrLines[editSel];
  if (!l) return;
  engineSeek(Math.max(0, l.t + editShift() - 1.5));
  if (!engineIsPlaying()) enginePlay();
};

$("edit-text").oninput = () => {
  const l = lyrLines[editSel];
  if (!l) return;
  l.text = $("edit-text").value;
  l.span.textContent = l.text || "…";
};

$("edit-del").onclick = () => {
  const l = lyrLines[editSel];
  if (!l) return;
  l.el.remove();
  lyrLines.splice(editSel, 1);
  selectEditLine(-1);
};

$("edit-add").onclick = () => {
  if (!editMode) return;
  const t = +(getTime() - editShift()).toFixed(2);
  const line = { t, end: +(t + 3).toFixed(2), text: "" };
  let i = lyrLines.findIndex((l) => l.t > t);
  if (i < 0) i = lyrLines.length;
  const el = document.createElement("div");
  el.className = "lyr-line";
  const span = document.createElement("span");
  span.className = "fill";
  span.textContent = "…";
  el.appendChild(span);
  line.el = el;
  line.span = span;
  const scroller = $("lyrics-scroller");
  scroller.insertBefore(el, scroller.children[i] || null);
  lyrLines.splice(i, 0, line);
  selectEditLine(i);
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  $("edit-text").focus();
};

$("edit-save").onclick = async () => {
  const shift = editShift();
  const lines = lyrLines
    .map((l) => ({ t: +(l.t + shift).toFixed(2), end: +(l.end + shift).toFixed(2), text: l.text.trim() }))
    .filter((l) => l.text);
  if (!lines.length) { toast("a letra ficou vazia — nada salvo", true); return; }
  $("edit-save").disabled = true;
  try {
    const res = await api(`/api/lines/${current.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lines }),
    });
    current.lyrics = res;
    autoOffset = 0;
    manualOffset = 0;
    localStorage.removeItem("lyroff:" + current.id);
    updateOffsetLabel();
    setDiffBadge(res);
    exitEdit(false);
    renderLyrics(res);
    loadSongs();
    toast("letra salva — agora ela obedece você 🖊");
  } catch (err) {
    toast("erro ao salvar: " + err.message, true);
  } finally {
    $("edit-save").disabled = false;
  }
};

$("edit-cancel").onclick = () => { exitEdit(true); toast("edição descartada"); };
$("edit-btn").onclick = enterEdit;

// rede de segurança do editor: a 1ª edição manual guarda a versão automática
// no servidor — daqui dá pra voltar mesmo depois de salvar besteira
$("edit-restore").onclick = async () => {
  if (!await askConfirm("Voltar pra versão automática?",
    "as edições manuais SALVAS desta música serão descartadas")) return;
  try {
    const res = await api(`/api/lines/${current.id}/restore`, { method: "POST" });
    current.lyrics = res.lyrics;
    autoOffset = res.autoOffset || 0;
    manualOffset = 0;
    localStorage.removeItem("lyroff:" + current.id);
    updateOffsetLabel();
    setDiffBadge(res.lyrics);
    exitEdit(false);
    renderLyrics(res.lyrics);
    loadSongs();
    toast("versão automática restaurada ↩");
  } catch (err) {
    toast(err.message, true);
  }
};
$("sync-warn").onclick = () => {
  enterEdit();
  toast("modo edição: dê play e vá marcando o início das linhas com Enter ⏱");
};

$("lyrics-scroller").addEventListener("click", (e) => {
  if (!editMode) return;
  const el = e.target.closest(".lyr-line");
  if (!el) return;
  selectEditLine(lyrLines.findIndex((l) => l.el === el));
});

// ---------------------------------------------------------------- boot
paintRange($("seek"));
loadMixerFor("centercut");
loadSongs().catch((err) => toast("Erro ao carregar repertório: " + err.message, true));
