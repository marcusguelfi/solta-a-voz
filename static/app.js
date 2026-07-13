/* ============ SOLTA A VOZ — player de karaokê ============
 * Dois modos de áudio:
 *  - "stems": música preparada por IA — voz e instrumental são faixas separadas
 *    (AudioBufferSource, sync perfeito). Slider Voz = volume real da voz (padrão 0%).
 *  - "centercut": modo rápido pra música ainda não preparada — atenua o canal
 *    central (mid/side) em tempo real.
 * Letra: LRC do LRCLIB + offset automático (início real do canto detectado no
 * stem de voz pelo servidor) + ajuste manual opcional.
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
  ctx: null, limiter: null, mode: null,
  // stems
  buffers: null, sources: [], vocalG: null, instG: null,
  startedAt: 0, startOffset: 0, playing: false, stopping: false,
  // centercut
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
  ref: null,            // {hop, midi[]} — melodia de referência
  samples: [],          // amostras do mic: {t (tempo do áudio), midi}
  lastSample: null,
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
  $("score-chip").hidden = false;
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
  score.lastSample = null;
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
  score.maxPossible += 100;
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
  score.total += pts;
  score.lineResults.push(pts);
  $("score-val").textContent = score.total.toLocaleString("pt-BR");
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

  for (let i = 0; i < wins.length; i++) {
    const [a, b] = wins[i];
    if (b < t0 || a > t1) continue;
    const inNow = i === nowWinIdx;
    const mode = windowMode(i);
    const arr = mode === "melody" ? midi : energy;
    const isOn = mode === "melody" ? (v) => v !== null : (v) => v === 1;
    const k0 = Math.max(0, Math.floor(Math.max(a, t0) / hop));
    const k1 = Math.min(arr.length - 1, Math.ceil(Math.min(b, t1) / hop));
    let segStart = null, segSum = 0, segN = 0, prevM = null;
    const flush = (endK) => {
      if (segStart !== null && segN >= 2) {
        const x1 = X(segStart * hop), x2 = X(endK * hop);
        const yy = mode === "melody" ? Y(segSum / segN) : h / 2;
        ctx.fillStyle = colorFor(endK * hop, inNow);
        ctx.beginPath();
        ctx.roundRect(x1, yy - 3.5, Math.max(x2 - x1, 3), 7, 3.5);
        ctx.fill();
      }
      segStart = null; segSum = 0; segN = 0;
    };
    for (let k = k0; k <= k1; k++) {
      const v = arr[k];
      const on = isOn(v);
      if (!on || (mode === "melody" && prevM !== null && Math.abs(v - prevM) > 0.7)) flush(k);
      if (on) {
        if (segStart === null) segStart = k;
        if (mode === "melody") segSum += v;
        segN++;
      }
      prevM = mode === "melody" && on ? v : null;
    }
    flush(k1);
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

async function loadSongs() {
  const fresh = await api("/api/songs");
  const sameList = fresh.length === songs.length &&
    fresh.every((s, i) => s.id === songs[i]?.id);
  songs = fresh;
  if (sameList && cardEls.size) {
    songs.forEach(updateCardStatus); // atualização in-place: nada de piscar o grid
  } else {
    renderGrid();
  }
  clearTimeout(pollTimer);
  if (songs.some((s) => PROCESSING.has(s.status))) {
    pollTimer = setTimeout(() => loadSongs().catch(() => {}), 4000);
  }
}

function diffOf(song) {
  return song?.lyrics?.difficulty?.label || null;
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
  return `
    <span class="pill">${fmtTime(song.duration)}</span>
    ${diff ? `<span class="pill diff ${diff.toLowerCase()}">${diff}</span>` : ""}
    ${hasSync ? `<span class="pill">letra sync</span>` : ""}
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
}

function renderGrid() {
  const grid = $("song-grid");
  grid.innerHTML = "";
  cardEls.clear();
  $("empty-msg").hidden = songs.length > 0;
  if (!songs.length) $("add-panel").hidden = false; // palco vazio: já abre o form
  $("song-count").textContent = songs.length
    ? `${songs.length} música${songs.length > 1 ? "s" : ""}` : "";
  songs.forEach((song, i) => {
    const card = document.createElement("div");
    card.className = "song-card";
    card.style.animationDelay = `${Math.min(i * 0.05, 0.4)}s`;
    const coverURL = song.hasCover || song.thumb ? `/api/cover/${song.id}` : null;
    card.innerHTML = `
      ${coverURL
        ? `<img class="cover" src="${coverURL}" alt="" loading="lazy"
             onerror="this.outerHTML='<div class=cover-fallback>${(song.title || "?")[0].toUpperCase()}</div>'">`
        : `<div class="cover-fallback">${(song.title || "?")[0].toUpperCase()}</div>`}
      <button class="card-del" title="Remover">✕</button>
      <button class="card-play" title="Cantar! (dá pra cantar até enquanto prepara)">▶</button>
      <div class="card-body">
        <div class="card-title"></div>
        <div class="card-artist"></div>
        <div class="card-meta"></div>
        <div class="prog" hidden><i></i></div>
      </div>`;
    card.querySelector(".card-title").textContent = song.title || "Sem título";
    card.querySelector(".card-artist").textContent = song.artist || "—";
    card.querySelector(".card-del").onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Remover "${song.title}" do repertório?`)) return;
      await api(`/api/songs/${song.id}`, { method: "DELETE" });
      loadSongs();
    };
    card.onclick = () => openPlayer(song);
    grid.appendChild(card);
    const refs = {
      card,
      meta: card.querySelector(".card-meta"),
      prog: card.querySelector(".prog"),
      progFill: card.querySelector(".prog i"),
    };
    cardEls.set(song.id, refs);
    updateCardStatus(song);
  });
}

$("add-toggle").onclick = () => {
  const panel = $("add-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) $("url-input").focus();
};

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
    const song = await api("/api/link", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    $("url-input").value = "";
    toast(`🎵 "${song.title}" baixada! Preparando o karaokê…`);
    $("add-panel").hidden = true;
    await loadSongs();
    warmLyrics(song);
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
      ? lyr.lines.map((l) => ({ t: l.t, end: l.end, text: l.text }))
      : parseLRC(lyr.synced);
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
}

function updateOffsetLabel() {
  const total = manualOffset;
  $("off-val").textContent = (total >= 0 ? "+" : "") + total.toFixed(1).replace(".", ",") + "s";
  $("off-auto").textContent = autoOffset
    ? `(auto ${autoOffset >= 0 ? "+" : ""}${autoOffset.toFixed(1).replace(".", ",")}s)` : "";
}

// tempo da letra: posição do áudio menos o offset automático, mais o ajuste manual.
// LYRIC_LEAD acende a linha um pouco ANTES do canto — como karaokê de verdade,
// pra dar tempo de ler (a pontuação usa o tempo real do áudio, não é afetada).
const LYRIC_LEAD = 0.45;
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
      const sample = { t: getTime(), midi: 69 + 12 * Math.log2(f / 440) };
      score.samples.push(sample);
      score.lastSample = sample;
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
    const p = Math.min(100, Math.max(0, ((t - cur.t) / Math.max(curEnd - cur.t, 0.1)) * 100));
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
  current = song;
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
  if (localStorage.getItem("mic:pref") === "1" && !score.enabled) {
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
  enginePause();
  stopSources();
  disableMic();
  $("results").hidden = true;
  $("score-chip").hidden = true;
  engine.buffers = null;
  audio.removeAttribute("src");
  cancelAnimationFrame(rafId);
  $("player-view").hidden = true;
  $("library-view").style.display = "";
  current = null;
  loadSongs().catch(() => {});
}

$("back-btn").onclick = closePlayer;

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
  if (e.key === "Escape") closePlayer();
  if (e.key === "ArrowRight") engineSeek(getTime() + 5);
  if (e.key === "ArrowLeft") engineSeek(getTime() - 5);
});

// ---------------------------------------------------------------- boot
paintRange($("seek"));
loadMixerFor("centercut");
loadSongs().catch((err) => toast("Erro ao carregar repertório: " + err.message, true));
