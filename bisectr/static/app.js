// bisectr page: commit timeline, bisect replay with zoom, score, steps, culprit diff.

const $ = (id) => document.getElementById(id);
const fmt = (n) => n.toLocaleString("en-US");

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

async function api(path) {
  const r = await fetch(path);
  const body = await r.json().catch(() => null);
  if (!r.ok) throw new Error((body && typeof body.detail === "string" && body.detail) || `HTTP ${r.status}`);
  return body;
}

let repo = null; // {commits, list: [{i, sha, subject, parents, lane, broken}], ...}
let plan = null; // the search for the current bug: {bug, first_bad, candidates, start, steps, one_by_one}
let cursor = 0; // how many steps are shown
let timer = 0;
let domain = [0, 1]; // the visible commit range (zooms in as suspects shrink)
let W = 1000;

const svg = d3.select("#timeline");
const H = 170;
const LANE_Y = [62, 108];
const TOP = 34, BOTTOM = 136; // the band where suspects and marks are drawn
const LEFT = 64, RIGHT = 14;
const x = d3.scaleLinear();

// ---- drawing ---------------------------------------------------------------------------

function suspectsAt(c) {
  return c === 0 ? plan.start : plan.steps[c - 1].suspects;
}

function inRuns(runs, i) {
  return runs.some(([a, b]) => i >= a && i <= b);
}

function targetDomain(c) {
  const runs = suspectsAt(c);
  if (c === 0 || !runs.length) return [0, repo.commits - 1];
  const lo = runs[0][0], hi = runs[runs.length - 1][1];
  const pad = Math.max(3, (hi - lo) * 0.12);
  return [Math.max(0, lo - pad), Math.min(repo.commits - 1, hi + pad)];
}

function build() {
  W = Math.max(300, Math.round(svg.node().getBoundingClientRect().width));
  svg.attr("viewBox", `0 0 ${W} ${H}`).selectAll("*").remove();
  x.range([LEFT, W - RIGHT]);

  svg.append("defs").append("clipPath").attr("id", "plot")
    .append("rect").attr("x", LEFT - 2).attr("y", 0).attr("width", W - LEFT - RIGHT + 4).attr("height", H);
  ["main", "branches"].forEach((label, lane) => {
    svg.append("line").attr("class", "lane-line").attr("x1", LEFT).attr("x2", W - RIGHT)
      .attr("y1", LANE_Y[lane]).attr("y2", LANE_Y[lane]);
    svg.append("text").attr("class", "lane-label").attr("x", 4).attr("y", LANE_Y[lane] + 4).text(label);
  });
  const plot = svg.append("g").attr("clip-path", "url(#plot)");
  plot.append("g").attr("id", "bands");
  const links = [];
  repo.list.forEach((c) => c.parents.forEach((p) => links.push([p, c.i])));
  plot.append("g").selectAll("line").data(links).join("line").attr("class", "link");
  plot.append("g").attr("id", "commits").selectAll("rect").data(repo.list).join("rect")
    .attr("class", (c) => "commit" + (c.broken ? " broken" : ""));
  plot.append("g").attr("id", "marks");
  plot.append("g").attr("id", "flag");
  svg.append("text").attr("id", "range-note").attr("class", "axis-note").attr("x", LEFT).attr("y", H - 10);
  svg.append("rect").attr("class", "hit").attr("x", LEFT).attr("y", 0)
    .attr("width", W - LEFT - RIGHT).attr("height", H)
    .on("mousemove", hover).on("mouseleave", () => { $("tip").hidden = true; })
    .on("click", plantBug);
}

function update() {
  x.domain([domain[0] - 0.5, domain[1] + 0.5]);
  const step = x(1) - x(0);
  const w = Math.max(1, Math.min(14, step * 0.7));
  const runs = suspectsAt(cursor);
  const done = plan.steps.slice(0, cursor);

  svg.select("#bands").selectAll("rect").data(runs).join("rect").attr("class", "sus-band")
    .attr("x", ([a]) => x(a - 0.5)).attr("width", ([a, b]) => Math.max(1, x(b + 0.5) - x(a - 0.5)))
    .attr("y", TOP).attr("height", BOTTOM - TOP);
  svg.selectAll(".link")
    .attr("x1", ([p]) => x(p)).attr("y1", ([p]) => LANE_Y[repo.list[p].lane])
    .attr("x2", ([, c]) => x(c)).attr("y2", ([, c]) => LANE_Y[repo.list[c].lane]);
  svg.select("#commits").selectAll("rect")
    .attr("x", (c) => x(c.i) - w / 2).attr("width", w)
    .attr("y", (c) => LANE_Y[c.lane] - 8).attr("height", 16).attr("rx", Math.min(2, w / 2))
    .classed("out", (c) => !inRuns(runs, c.i));

  const marks = svg.select("#marks").selectAll("g").data(done).join((enter) => {
    const g = enter.append("g");
    g.append("line");
    g.append("circle");
    return g;
  });
  marks.select("line").attr("class", (s, k) => `mark ${s.verdict}` + (k === done.length - 1 ? " now" : ""))
    .attr("x1", (s) => x(s.i)).attr("x2", (s) => x(s.i)).attr("y1", TOP - 6).attr("y2", BOTTOM);
  marks.select("circle").attr("class", (s) => `mark-dot ${s.verdict}`)
    .attr("cx", (s) => x(s.i)).attr("cy", TOP - 8).attr("r", 4);

  const finished = cursor === plan.steps.length;
  const flag = finished && plan.first_bad !== null ? [plan.first_bad] : [];
  svg.select("#flag").selectAll("text").data(flag).join("text").attr("class", "bug-flag")
    .attr("x", (i) => x(i)).attr("y", 16).attr("text-anchor", "middle").text("▼ first bad");

  const shown = Math.round(domain[1] - domain[0] + 1);
  svg.select("#range-note").text(
    shown >= repo.commits ? `all ${fmt(repo.commits)} commits` : `zoomed: commits ${fmt(Math.round(domain[0]))}–${fmt(Math.round(domain[1]))}`);
}

function zoomTo(target, ms) {
  const from = domain.slice();
  const interp = d3.interpolateArray(from, target);
  if (ms <= 0) { domain = target; update(); return; }
  const t = d3.timer((elapsed) => {
    const k = d3.easeCubicInOut(Math.min(1, elapsed / ms));
    domain = interp(k);
    update();
    if (k >= 1) t.stop();
  });
}

// ---- the replay -------------------------------------------------------------------------

function describeStep(k) {
  if (k === 0) {
    return `${fmt(plan.start.reduce((n, [a, b]) => n + b - a + 1, 0))} suspects: every commit after ${esc(repo.good)} could hold the bug.`;
  }
  const s = plan.steps[k - 1];
  const c = repo.list[s.i];
  const why = { good: "test passed: it and everything before it are clean",
                bad: "test failed: the bug is here or before it",
                skip: "cannot test (broken build): try another" }[s.verdict];
  return `Step <b>${k}</b> of ${plan.steps.length}: tested <code>${c.sha.slice(0, 8)}</code> → ` +
    `<b class="v ${s.verdict}">${s.verdict}</b> (${why}). Suspects ${fmt(s.before)} → <b>${fmt(s.after)}</b>.`;
}

function show(k, animate = true) {
  cursor = k;
  $("now").innerHTML = describeStep(k);
  document.querySelectorAll("#steps li").forEach((li, j) => li.classList.toggle("done", j < k));
  zoomTo(targetDomain(k), animate ? 450 : 0);
  update();
  if (k === plan.steps.length) finish();
}

async function finish() {
  stop();
  $("bar-b").style.width = `${Math.max(1, 100 * plan.steps.length / Math.max(plan.one_by_one, plan.steps.length))}%`;
  $("num-b").textContent = fmt(plan.steps.length);
  if (plan.first_bad === null) {
    $("culprit-title").textContent = "No single culprit";
    $("culprit").textContent = "Untestable commits sit next to the bug, so it could be any of:\n" +
      plan.candidates.map((i) => `  ${repo.list[i].sha.slice(0, 10)}  ${repo.list[i].subject}`).join("\n");
    return;
  }
  const c = repo.list[plan.first_bad];
  $("culprit-title").textContent = `The culprit: ${c.sha.slice(0, 10)}`;
  try {
    const d = await api(`/api/commit/${c.sha}`);
    $("culprit").innerHTML = d.text.split("\n").map((l) =>
      l.startsWith("+") && !l.startsWith("+++") ? `<span class="add">${esc(l)}</span>`
        : l.startsWith("-") && !l.startsWith("---") ? `<span class="del">${esc(l)}</span>` : esc(l)).join("\n");
  } catch (e) {
    $("culprit").textContent = e.message;
  }
}

function stop() {
  clearInterval(timer);
  timer = 0;
  $("play").textContent = plan && cursor === plan.steps.length ? "Replay" : "Play";
}

function play() {
  if (timer) { stop(); return; }
  if (cursor === plan.steps.length) reset();
  $("play").textContent = "Pause";
  const tick = () => (cursor < plan.steps.length ? show(cursor + 1) : stop());
  tick();
  timer = setInterval(tick, +$("speed").value);
}

function reset() {
  stop();
  $("culprit-title").textContent = "The culprit";
  $("culprit").textContent = "Press Play.";
  $("bar-b").style.width = "0";
  $("num-b").textContent = "–";
  show(0, true);
}

function fillPanels() {
  const bug = repo.list[plan.bug];
  const real = plan.bug === repo.real_bug;
  $("bar-l").style.width = "100%";
  $("num-l").textContent = fmt(plan.one_by_one);
  $("score-note").innerHTML =
    `${real ? "The hidden bug" : "Your bug"} is commit #${fmt(plan.bug + 1)} of ${fmt(repo.commits)} ` +
    `(<code>${bug.sha.slice(0, 8)}</code> ${esc(bug.subject)}). Testing one by one from the oldest takes ` +
    `${fmt(plan.one_by_one)} tests; binary search needs about ${Math.ceil(Math.log2(repo.commits))}.`;
  $("steps").innerHTML = plan.steps.map((s) => {
    const c = repo.list[s.i];
    return `<li><span class="v ${s.verdict}">${s.verdict}</span> <code>${c.sha.slice(0, 8)}</code> ` +
      `${fmt(s.before)} → ${fmt(s.after)} · ${esc(c.subject)}</li>`;
  }).join("");
}

async function load(bug) {
  stop();
  plan = await api(bug === undefined ? "/api/bisect" : `/api/bisect?bug=${bug}`);
  fillPanels();
  reset();
}

// ---- hover and click ---------------------------------------------------------------------

function nearest(ev) {
  const [mx] = d3.pointer(ev);
  return Math.max(0, Math.min(repo.commits - 1, Math.round(x.invert(mx))));
}

function hover(ev) {
  const c = repo.list[nearest(ev)];
  const tested = plan.steps.slice(0, cursor).find((s) => s.i === c.i);
  const tip = $("tip");
  tip.innerHTML = `<b>#${fmt(c.i + 1)}</b> <code>${c.sha.slice(0, 10)}</code><br>${esc(c.subject)}` +
    `<br><span class="hint">${c.lane ? "feature branch" : "main"}${c.broken ? " · broken build" : ""}` +
    `${tested ? ` · tested: ${tested.verdict}` : ""} · click to hide the bug here</span>`;
  tip.hidden = false;
  const box = tip.parentElement.getBoundingClientRect();
  const left = Math.min(ev.clientX - box.left + 12, box.width - tip.offsetWidth - 4);
  tip.style.left = `${Math.max(0, left)}px`;
  tip.style.top = `${ev.clientY - box.top + 14}px`;
}

function plantBug(ev) {
  $("tip").hidden = true;
  load(nearest(ev)).then(play).catch((e) => { $("now").textContent = e.message; });
}

// ---- start --------------------------------------------------------------------------------

$("play").addEventListener("click", play);
$("step").addEventListener("click", () => { stop(); if (cursor < plan.steps.length) show(cursor + 1); });
$("reset").addEventListener("click", reset);

let drawnWidth = 0;
window.addEventListener("resize", () => {
  clearTimeout(window._rz);
  window._rz = setTimeout(() => {
    const w = Math.round(svg.node().getBoundingClientRect().width);
    if (!repo || w === drawnWidth) return;
    drawnWidth = w;
    build();
    update();
  }, 150);
});

async function start() {
  repo = await api("/api/repo");
  $("repo").textContent = `${repo.repo} · ${fmt(repo.commits)} commits · ${repo.merges} merges · ${repo.untestable} broken builds`;
  domain = [0, repo.commits - 1];
  build();
  drawnWidth = W;
  await load();
}

start().catch((e) => { $("now").textContent = `Could not load: ${e.message}`; });
