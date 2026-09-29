/* Ljudboksspelare – klient.
   En sida med fyra vyer (bibliotek, spelare, statistik, inställningar).
   Servern äger all uppspelningslogik; klienten pollar /api/status
   (1 s när spelaren syns och spelar, annars glesare, aldrig när fönstret
   är dolt) och låter förloppsbaren glida mjukt mellan pollningarna. */
"use strict";

const $ = (id) => document.getElementById(id);
const h = (tag, cls, text) => {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
};
const svgIcon = (name) => {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const u = document.createElementNS("http://www.w3.org/2000/svg", "use");
  u.setAttribute("href", "#i-" + name);
  s.appendChild(u);
  return s;
};

// Inline (not <use>) so CSS can animate the fill path.
const HEART_SVG = '<svg viewBox="0 0 24 24"><path class="heart-fill" d="M12 20.3s-7.8-4.7-7.8-10.4A4.4 4.4 0 0 1 12 7.2a4.4 4.4 0 0 1 7.8 2.7c0 5.7-7.8 10.4-7.8 10.4z"/><path d="M12 20.3s-7.8-4.7-7.8-10.4A4.4 4.4 0 0 1 12 7.2a4.4 4.4 0 0 1 7.8 2.7c0 5.7-7.8 10.4-7.8 10.4z" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"/></svg>';

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode etc. */ } },
};

const state = {
  view: null,
  books: [],
  libVersion: null,
  libDirty: true,
  status: { book_id: null, playing: false },
  book: null,          // current book incl. chapters
  filter: store.get("abp-filter", "all"),
  sort: store.get("abp-sort", "series"),
  dragging: false,
  pollTimer: null,
  theme: "evening",
};

/* ------------------------------------------------------------ helpers */
function fmtClock(ms) {
  let s = Math.max(0, Math.floor((ms || 0) / 1000));
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  return (hh ? hh + ":" + String(mm).padStart(2, "0") : mm) + ":" + String(ss).padStart(2, "0");
}
function fmtDuration(ms) {
  const min = Math.round((ms || 0) / 60000);
  if (min < 1) return ms > 0 ? "< 1 min" : "0 min";
  const hh = Math.floor(min / 60), mm = min % 60;
  if (!hh) return mm + " min";
  return mm ? `${hh} h ${mm} min` : `${hh} h`;
}
function fmtPercent(fraction) {
  if (fraction > 0 && fraction < 0.01) return "< 1 %";
  return Math.floor((fraction || 0) * 100) + " %";
}
function fmtHours(ms) {
  const hrs = (ms || 0) / 3600000;
  if (ms > 0 && ms < 60000) return { v: "< 1", u: "min" };
  if (hrs < 1) return { v: Math.round((ms || 0) / 60000), u: "min" };
  return { v: hrs < 10 ? hrs.toFixed(1).replace(".", ",") : Math.round(hrs), u: "h" };
}

let toastTimer = null;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2800);
}

async function api(url, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
  };
  let res;
  try {
    res = await fetch(url, opts);
  } catch (e) {
    toast("Ingen kontakt med spelaren");
    throw e;
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const msg = (data && data.message) || "Något gick fel";
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}

function coverEl(url, cls) {
  const box = h("div", "cover" + (cls ? " " + cls : ""));
  setCover(box, url);
  return box;
}
function setCover(box, url) {
  if (box.dataset.src === (url || "")) return;
  box.dataset.src = url || "";
  box.textContent = "";
  const ph = svgIcon("book");
  ph.classList.add("ph");
  box.appendChild(ph);
  if (!url) return;
  const img = new Image();
  img.decoding = "async";
  img.loading = "lazy";
  img.alt = "";
  img.onload = () => { img.classList.add("loaded"); ph.remove(); };
  img.onerror = () => img.remove();
  img.src = url;
  box.appendChild(img);
}

function progressEl(fraction) {
  const p = h("div", "progress");
  const i = h("i");
  i.style.transform = `scaleX(${Math.max(0, Math.min(1, fraction || 0))})`;
  p.appendChild(i);
  return p;
}

function favButton(book) {
  const b = h("button", "fav pressable");
  b.setAttribute("aria-label", "Favorit");
  b.setAttribute("aria-pressed", String(!!book.favorite));
  b.innerHTML = HEART_SVG;
  b.addEventListener("click", (e) => { e.stopPropagation(); toggleFavorite(book.id, b); });
  return b;
}

async function toggleFavorite(id, btn) {
  const book = state.books.find((b) => b.id === id) || (state.book && state.book.id === id ? state.book : null);
  const next = !(book && book.favorite);
  document.querySelectorAll(`.fav[data-id="${id}"]`).forEach((b) => b.setAttribute("aria-pressed", String(next)));
  btn.setAttribute("aria-pressed", String(next));
  if (next) {
    btn.classList.remove("pop");
    void btn.offsetWidth; // restart animation
    btn.classList.add("pop");
    setTimeout(() => btn.classList.remove("pop"), 550);
  }
  try {
    await api(`/api/books/${id}/favorite`, { favorite: next });
    state.books.forEach((b) => { if (b.id === id) b.favorite = next; });
    if (state.book && state.book.id === id) state.book.favorite = next;
    syncFavButtons();
    if (state.filter === "favorites") renderLibrary();
  } catch (e) {
    btn.setAttribute("aria-pressed", String(!next));
    toast(e.message);
  }
}
function syncFavButtons() {
  const fav = state.book && state.book.favorite;
  $("pFav").setAttribute("aria-pressed", String(!!fav));
}

/* ------------------------------------------------------------ routing */
const VIEWS = ["library", "player", "stats", "settings"];
function go(view, replace) {
  if (!VIEWS.includes(view)) view = "library";
  const hash = "#/" + view;
  if (location.hash !== hash) {
    if (replace) history.replaceState(null, "", hash); else location.hash = hash;
  }
  show(view);
}
function show(view) {
  if (state.view === view) return;
  state.view = view;
  VIEWS.forEach((v) => $("view-" + v).classList.toggle("active", v === view));
  document.querySelectorAll(".rail-btn").forEach((b) => {
    if (b.dataset.nav === view) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  updateMini();
  if (view === "library" && state.libDirty) loadBooks();
  if (view === "stats") loadStats();
  if (view === "settings") refreshScan();
  if (view === "player") { renderPlayer(); measureTrack(); }
  schedulePoll(0);
}
window.addEventListener("hashchange", () => show(location.hash.replace("#/", "") || "library"));
document.addEventListener("click", (e) => {
  const nav = e.target.closest("[data-nav]");
  if (nav) go(nav.dataset.nav);
});

/* ------------------------------------------------------------ library */
const SORTS = [
  ["series", "Serie"], ["title", "Titel"], ["recent", "Senast lyssnad"], ["added", "Nyast"],
];

async function loadBooks() {
  try {
    state.books = await api("/api/books");
    state.libDirty = false;
    renderLibrary();
  } catch (e) {
    $("library").replaceChildren(emptyState("Kunde inte läsa biblioteket", e.message));
  }
}

function emptyState(title, text, button) {
  const box = h("div", "empty");
  box.append(h("h2", null, title), h("p", null, text));
  if (button) box.appendChild(button);
  return box;
}

function filteredBooks() {
  let books = state.books.slice();
  if (state.filter === "favorites") books = books.filter((b) => b.favorite);
  if (state.filter === "progress") books = books.filter((b) => b.position_ms > 0 && b.progress < 0.95);
  const byTitle = (a, b) => a.title.localeCompare(b.title, "sv");
  if (state.sort === "title") books.sort(byTitle);
  // "Pågående" is a resume list: grouping by series makes no sense there.
  const recentFirst = state.sort === "recent" || (state.filter === "progress" && state.sort === "series");
  if (recentFirst) books.sort((a, b) => (b.last_played_at || 0) - (a.last_played_at || 0) || byTitle(a, b));
  if (state.sort === "added") books.sort((a, b) => (b.added_at || 0) - (a.added_at || 0) || byTitle(a, b));
  return books;
}

function bookCard(book) {
  const card = h("button", "card");
  card.dataset.id = book.id;
  card.appendChild(coverEl(book.cover_small));
  card.appendChild(h("div", "card-title", book.title));
  card.appendChild(h("div", "card-author", book.author || ""));
  if (book.position_ms > 0) card.appendChild(progressEl(book.progress));
  if (book.listen_count > 0) {
    const badge = h("span", "badge", book.listen_count > 1 ? `✓ ${book.listen_count}×` : "✓ Lyssnad");
    card.appendChild(badge);
  }
  const fav = favButton(book);
  fav.dataset.id = book.id;
  card.appendChild(fav);
  card.addEventListener("click", () => openBook(book.id));
  return card;
}

function fmtAgo(ts) {
  if (!ts) return "–";
  const d = new Date(ts * 1000), now = new Date();
  const days = Math.round((new Date(now.toDateString()) - new Date(d.toDateString())) / 86400000);
  if (days <= 0) return "idag " + d.toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" });
  if (days === 1) return "igår";
  if (days < 7) return days + " dagar sedan";
  return d.toLocaleDateString("sv-SE", { day: "numeric", month: "short" });
}

// One row per book in "Pågående": compact on small screens, with per-book
// statistics when there is room (container query in app.css).
function progressRow(book) {
  const row = h("div", "prow");
  row.dataset.id = book.id;
  row.appendChild(coverEl(book.cover_small));

  const main = h("button", "prow-main");
  main.append(h("div", "prow-title", book.title), h("div", "prow-author", book.author || ""));
  const bar = h("div", "prow-bar");
  bar.append(progressEl(book.progress), h("span", "prow-pct", fmtPercent(book.progress)));
  main.appendChild(bar);
  const left = book.duration_ms ? fmtDuration(book.duration_ms - book.position_ms) + " kvar" : "";
  const meta = h("div", "prow-meta", left);
  if (book.last_played_at) meta.appendChild(h("span", "prow-when", " · " + fmtAgo(book.last_played_at)));
  main.appendChild(meta);
  main.addEventListener("click", () => openBook(book.id));
  row.appendChild(main);

  const stats = h("dl", "prow-stats");
  const stat = (label, value) => { const d = h("div"); d.append(h("dt", null, label), h("dd", null, value)); stats.appendChild(d); };
  stat("Lyssnat", book.listened_ms > 0 ? fmtDuration(book.listened_ms) : "–");
  stat("Senast", fmtAgo(book.last_played_at));
  stat("Klar", book.listen_count ? book.listen_count + "×" : "–");
  row.appendChild(stats);

  const play = playButton();
  play.classList.add("prow-play");
  const isCurrent = state.status.book_id === book.id;
  play.classList.toggle("on", isCurrent && !!state.status.playing);
  play.setAttribute("aria-label", isCurrent && state.status.playing ? "Pausa" : "Fortsätt lyssna");
  play.addEventListener("click", () => resumeBook(book.id));
  row.appendChild(play);
  return row;
}

async function resumeBook(id) {
  try {
    if (state.status.book_id === id) {
      await command("/api/toggle");
    } else {
      await api("/api/select-book", { id });
      await api("/api/play", {});
      await loadCurrentBook();
    }
    renderLibrary();
  } catch (e) {
    toast(e.message);
  }
}

function renderLibrary() {
  document.querySelectorAll("#filters [data-filter]").forEach((c) =>
    c.setAttribute("aria-pressed", String(c.dataset.filter === state.filter)));
  $("sortLabel").textContent = SORTS.find((s) => s[0] === state.sort)[1];

  renderContinue();
  const root = $("library");
  const books = filteredBooks();
  if (!state.books.length) {
    const btn = h("button", "btn pressable");
    btn.append(svgIcon("refresh"), h("span", null, "Sök efter böcker"));
    btn.addEventListener("click", () => { go("settings"); startScan(); });
    root.replaceChildren(emptyState("Biblioteket är tomt",
      "Lägg ljudböcker (.m4b, .m4a, .mp3) i mappen books. De dyker upp automatiskt.", btn));
    return;
  }
  if (!books.length) {
    root.replaceChildren(emptyState(state.filter === "favorites" ? "Inga favoriter än" : "Inga pågående böcker",
      state.filter === "favorites" ? "Tryck på hjärtat på en bok för att spara den här." : "Böcker du har börjat lyssna på visas här."));
    return;
  }
  const frag = document.createDocumentFragment();
  if (state.filter === "progress") {
    const list = h("div", "prow-list");
    books.forEach((b) => list.appendChild(progressRow(b)));
    frag.appendChild(list);
  } else if (state.sort === "series") {
    const groups = new Map();
    books.forEach((b) => {
      const key = b.series || "Övrigt";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(b);
    });
    groups.forEach((list, name) => {
      const sec = h("section", "series");
      const title = h("h2", null, name);
      title.appendChild(h("small", null, list.length === 1 ? "1 bok" : list.length + " böcker"));
      const grid = h("div", "grid");
      list.forEach((b) => grid.appendChild(bookCard(b)));
      sec.append(title, grid);
      frag.appendChild(sec);
    });
  } else {
    const grid = h("div", "grid");
    books.forEach((b) => grid.appendChild(bookCard(b)));
    frag.appendChild(grid);
  }
  root.replaceChildren(frag);
}

function renderContinue() {
  const box = $("continue");
  const cur = state.status.book_id != null && state.books.find((b) => b.id === state.status.book_id);
  if (!cur || state.filter !== "all") { box.replaceChildren(); return; }
  const card = h("div", "continue");
  card.appendChild(coverEl(cur.cover_small));
  const text = h("div");
  text.append(h("div", "continue-label", state.status.playing ? "Spelas nu" : "Fortsätt lyssna"),
    h("div", "continue-title", cur.title),
    h("div", "continue-meta", `${cur.author || ""} · ${fmtPercent(cur.progress)}`));
  text.appendChild(progressEl(cur.progress));
  card.appendChild(text);
  const play = playButton();
  play.classList.toggle("on", !!state.status.playing);
  play.addEventListener("click", (e) => { e.stopPropagation(); togglePlay(); });
  card.appendChild(play);
  card.addEventListener("click", () => go("player"));
  box.replaceChildren(card);
  updateMini();
}

function playButton() {
  const b = h("button", "play pressable");
  b.setAttribute("aria-label", "Spela/pausa");
  const p = svgIcon("play"); p.classList.add("i-play");
  const q = svgIcon("pause"); q.classList.add("i-pause");
  b.append(p, q);
  return b;
}

$("filters").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-filter]");
  if (!chip) return;
  state.filter = chip.dataset.filter;
  store.set("abp-filter", state.filter);
  renderLibrary();
});
$("sortBtn").addEventListener("click", () => {
  const i = SORTS.findIndex((s) => s[0] === state.sort);
  state.sort = SORTS[(i + 1) % SORTS.length][0];
  store.set("abp-sort", state.sort);
  renderLibrary();
});

async function openBook(id) {
  try {
    if (state.status.book_id !== id) {
      await api("/api/select-book", { id });
      state.book = null;
    }
    await loadCurrentBook();
    go("player");
  } catch (e) {
    toast(e.message);
    if (e.status === 404) { state.libDirty = true; loadBooks(); }
  }
}

/* ------------------------------------------------------------ player */
async function loadCurrentBook() {
  try {
    state.book = await api("/api/current-book");
    state.status = await api("/api/status");
  } catch (e) {
    state.book = null;
  }
  renderPlayer();
}

function chapterAt(ms) {
  const chapters = (state.book && state.book.chapters) || [];
  if (!chapters.length) return { index: -1, start: 0, end: state.book ? state.book.duration_ms : 0, title: "" };
  const s = ms / 1000 + 0.05;
  let i = chapters.findIndex((c) => c.start <= s && s < c.end);
  if (i < 0) i = s < chapters[0].start ? 0 : chapters.length - 1;
  const c = chapters[i];
  return { index: i, start: c.start * 1000, end: c.end * 1000, title: c.title };
}

function renderPlayer() {
  const book = state.book;
  $("noBook").hidden = !!book;
  $("player").hidden = !book;
  if (!book) return;
  setCover($("pCover"), book.cover);
  $("pSeries").textContent = book.series ? book.series + (book.volume ? " · Del " + book.volume : "") : "";
  $("pTitle").textContent = book.title;
  $("pAuthor").textContent = book.author || "";
  syncFavButtons();
  updateProgress(false);
}

let trackWidth = 0;
function measureTrack() { trackWidth = $("track").clientWidth; }
if ("ResizeObserver" in window) new ResizeObserver(measureTrack).observe($("track"));

let lastChapterIndex = null;
function updateProgress(animate) {
  const st = state.status, book = state.book;
  if (!book || st.book_id !== book.id) return;
  const pos = st.time || 0;
  const ch = chapterAt(pos);
  const len = Math.max(1, ch.end - ch.start);
  const chapterChanged = ch.index !== lastChapterIndex;
  lastChapterIndex = ch.index;

  $("pChapter").textContent = ch.index >= 0
    ? `${ch.index + 1}/${book.chapters.length} · ${ch.title}` : "Hela boken";
  $("playBtn").classList.toggle("on", !!st.playing);
  $("playBtn").setAttribute("aria-label", st.playing ? "Pausa" : "Spela");
  $("tNow").textContent = fmtClock(pos - ch.start);
  $("tLeft").textContent = "−" + fmtClock(ch.end - pos);
  const total = st.length || book.duration_ms || 0;
  $("bookLeft").textContent = total
    ? `${Math.floor((pos / total) * 100)} % av boken · ${fmtDuration(total - pos)} kvar` : "";
  const sleeping = st.sleep_remaining != null;
  $("sleepBtn").classList.toggle("active", sleeping);
  $("sleepLabel").textContent = sleeping ? `${Math.ceil(st.sleep_remaining / 60)} min kvar` : "Sovtimer";
  $("sleepOff").hidden = !sleeping;

  if (state.dragging) return;
  // While playing, aim one poll ahead and let a 1 s linear transition carry
  // the bar there, so it glides continuously at 60 fps with one update/s.
  const lead = st.playing && animate ? 1000 : 0;
  const frac = Math.max(0, Math.min(1, (pos + lead - ch.start) / len));
  const smooth = animate && st.playing && !chapterChanged;
  $("track").classList.toggle("smooth", smooth);
  setTrack(frac);
  const track = $("track");
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", String(Math.round(len / 1000)));
  track.setAttribute("aria-valuenow", String(Math.round((pos - ch.start) / 1000)));
}
function setTrack(frac) {
  $("trackFill").style.transform = `scaleX(${frac})`;
  $("trackKnob").style.transform = `translateX(${frac * trackWidth}px)`;
}

async function command(url, body) {
  try {
    const st = await api(url, body || {});
    if (st && "book_id" in st) applyStatus(st, false);
    else schedulePoll(0);
  } catch (e) {
    toast(e.message);
  }
}
function togglePlay() { command("/api/toggle"); }
$("playBtn").addEventListener("click", togglePlay);
$("miniPlay").addEventListener("click", togglePlay);
$("prevBtn").addEventListener("click", () => command("/api/prev-chapter"));
$("nextBtn").addEventListener("click", () => command("/api/next-chapter"));
$("backBtn").addEventListener("click", () => command("/api/seek", { ms: -15000 }));
$("fwdBtn").addEventListener("click", () => command("/api/seek", { ms: 30000 }));
$("pFav").addEventListener("click", () => state.book && toggleFavorite(state.book.id, $("pFav")));

// Seek by dragging the bar (pointer events cover touch and mouse).
(function () {
  const track = $("track");
  let ch = null, frac = 0;
  const fracAt = (e) => {
    const r = track.getBoundingClientRect();
    return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  };
  track.addEventListener("pointerdown", (e) => {
    if (!state.book) return;
    state.dragging = true;
    track.setPointerCapture(e.pointerId);
    track.classList.remove("smooth");
    track.classList.add("dragging");
    measureTrack();
    ch = chapterAt(state.status.time || 0);
    frac = fracAt(e);
    setTrack(frac);
    $("tNow").textContent = fmtClock(frac * (ch.end - ch.start));
  });
  track.addEventListener("pointermove", (e) => {
    if (!state.dragging) return;
    frac = fracAt(e);
    setTrack(frac);
    $("tNow").textContent = fmtClock(frac * (ch.end - ch.start));
  });
  const end = (e) => {
    if (!state.dragging) return;
    state.dragging = false;
    track.classList.remove("dragging");
    const to = Math.round(ch.start + frac * (ch.end - ch.start));
    state.status.time = to;
    command("/api/seek", { to_ms: to });
  };
  track.addEventListener("pointerup", end);
  track.addEventListener("pointercancel", () => { state.dragging = false; updateProgress(false); });
  track.addEventListener("keydown", (e) => {
    const d = { ArrowRight: 10000, ArrowLeft: -10000 }[e.key];
    if (d) { e.preventDefault(); command("/api/seek", { ms: d }); }
  });
})();

// Swipes on the cover: left/right = next/previous chapter, down = back to library.
function onSwipe(el, handler) {
  let sx = 0, sy = 0, t = 0, active = false;
  el.addEventListener("pointerdown", (e) => { sx = e.clientX; sy = e.clientY; t = Date.now(); active = true; });
  el.addEventListener("pointerup", (e) => {
    if (!active) return;
    active = false;
    const dx = e.clientX - sx, dy = e.clientY - sy;
    if (Date.now() - t > 700) return;
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) handler(dx < 0 ? "left" : "right");
    else if (Math.abs(dy) > 70 && Math.abs(dy) > Math.abs(dx) * 1.5) handler(dy > 0 ? "down" : "up");
  });
  el.addEventListener("pointercancel", () => { active = false; });
}
onSwipe($("pCover"), (dir) => {
  if (dir === "left") { command("/api/next-chapter"); toast("Nästa kapitel"); }
  if (dir === "right") { command("/api/prev-chapter"); toast("Föregående kapitel"); }
  if (dir === "down") go("library");
});

/* ------------------------------------------------------------ sheets */
let openSheetEl = null;
function openSheet(el) {
  openSheetEl = el;
  el.classList.add("open");
  $("scrim").classList.add("open");
}
function closeSheet() {
  if (!openSheetEl) return;
  openSheetEl.classList.remove("open");
  $("scrim").classList.remove("open");
  openSheetEl = null;
}
$("scrim").addEventListener("click", closeSheet);
document.querySelectorAll(".sheet").forEach((s) => onSwipe(s, (d) => d === "down" && closeSheet()));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });

$("chapterBtn").addEventListener("click", () => {
  const book = state.book;
  if (!book || !book.chapters.length) return;
  const list = $("chapterList");
  const cur = chapterAt(state.status.time || 0).index;
  const frag = document.createDocumentFragment();
  book.chapters.forEach((c, i) => {
    const item = h("button", "list-item" + (i === cur ? " current" : ""));
    item.append(h("span", "n", String(i + 1)), h("span", "t", c.title), h("span", "d", fmtClock((c.end - c.start) * 1000)));
    item.addEventListener("click", () => { closeSheet(); command("/api/seek", { to_ms: Math.ceil(c.start * 1000) }); });
    frag.appendChild(item);
  });
  list.replaceChildren(frag);
  openSheet($("chapterSheet"));
  const current = list.querySelector(".current");
  if (current) current.scrollIntoView({ block: "center" });
});

$("sleepBtn").addEventListener("click", () => openSheet($("sleepSheet")));
$("sleepSheet").addEventListener("click", (e) => {
  const opt = e.target.closest("[data-sleep]");
  if (!opt) return;
  const minutes = Number(opt.dataset.sleep);
  closeSheet();
  if (minutes > 0) { command("/api/sleep-timer", { minutes }); toast(`Sovtimer: ${minutes} min`); }
  else { command("/api/sleep-timer/cancel"); toast("Sovtimer av"); }
});

/* ------------------------------------------------------------ mini player */
// The mini player is redundant while the "continue listening" card is visible.
let continueOnScreen = true; // until the observer reports, avoids a flash of the mini player
if ("IntersectionObserver" in window) {
  new IntersectionObserver((entries) => {
    continueOnScreen = entries[0].isIntersecting;
    updateMini();
  }, { root: $("view-library") }).observe($("continue"));
}
function updateMini() {
  const mini = $("mini");
  const book = state.book;
  const cardShown = state.view === "library" && continueOnScreen && $("continue").childElementCount > 0;
  const visible = !!book && state.status.book_id === book.id && !cardShown &&
    (state.view === "library" || state.view === "stats");
  mini.classList.toggle("hidden", !visible);
  if (!visible) return;
  setCover($("miniCover"), book.cover_small);
  $("miniTitle").textContent = book.title;
  const ch = chapterAt(state.status.time || 0);
  $("miniSub").textContent = ch.title || book.author || "";
  $("miniPlay").classList.toggle("on", !!state.status.playing);
  const total = state.status.length || book.duration_ms || 1;
  $("miniFill").style.transform = `scaleX(${Math.min(1, (state.status.time || 0) / total)})`;
}
$("miniOpen").addEventListener("click", () => go("player"));
onSwipe($("mini"), (d) => d === "up" && go("player"));

/* ------------------------------------------------------------ polling */
function applyStatus(st, animate) {
  const prev = state.status;
  state.status = st;
  if (st.book_id !== (state.book && state.book.id)) {
    if (st.book_id == null) { state.book = null; renderPlayer(); }
    else loadCurrentBook();
  }
  if (st.library_version != null && st.library_version !== state.libVersion) {
    const first = state.libVersion == null;
    state.libVersion = st.library_version;
    if (!first) {
      state.libDirty = true;
      if (state.view === "library") loadBooks();
    }
  }
  if (state.view === "player") updateProgress(animate);
  updateMini();
  if (state.view === "library" && (prev.playing !== st.playing || prev.book_id !== st.book_id)) {
    if (state.filter === "progress") renderLibrary(); else renderContinue();
  }
  schedulePoll();
}

function pollDelay() {
  if (document.hidden) return null;
  if (state.status.playing) return state.view === "player" ? 1000 : 3000;
  return 6000;
}
function schedulePoll(delay) {
  clearTimeout(state.pollTimer);
  const d = delay != null ? delay : pollDelay();
  if (d == null) return;
  state.pollTimer = setTimeout(poll, d);
}
async function poll() {
  try {
    const st = await fetch("/api/status").then((r) => r.json());
    applyStatus(st, true);
  } catch (e) {
    schedulePoll(5000);
  }
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearTimeout(state.pollTimer); else schedulePoll(0);
});

/* ------------------------------------------------------------ stats */
async function loadStats() {
  const root = $("stats");
  let s;
  try { s = await api("/api/stats"); } catch (e) { root.replaceChildren(emptyState("Kunde inte läsa statistiken", e.message)); return; }
  const frag = document.createDocumentFragment();

  const tiles = h("div", "tiles");
  const tile = (label, ms, raw) => {
    const t = h("div", "tile");
    t.appendChild(h("div", "tile-label", label));
    const v = h("div", "tile-value");
    if (raw != null) v.textContent = raw;
    else { const f = fmtHours(ms); v.textContent = f.v; v.appendChild(h("small", null, " " + f.u)); }
    t.appendChild(v);
    return t;
  };
  tiles.append(tile("Totalt", s.total_ms), tile("Idag", s.today_ms), tile("Denna vecka", s.week_ms),
    tile("Denna månad", s.month_ms), tile("Lyssnat klart", 0, String(s.books_finished)));
  frag.appendChild(tiles);

  const panels = h("div", "panels");

  // Weekly bars – one series, so no legend; tap a bar to read its value.
  const weekPanel = h("div", "panel");
  weekPanel.appendChild(h("h2", null, "Lyssningstid per vecka"));
  const readout = h("div", "bar-readout");
  const bars = h("div", "bars");
  const axis = h("div", "bar-axis");
  const max = Math.max(1, ...s.weeks.map((w) => w.ms));
  const showWeek = (w) => { readout.innerHTML = ""; readout.append(h("b", null, fmtDuration(w.ms)), document.createTextNode(` · vecka ${w.label.slice(1)}`)); };
  s.weeks.forEach((w, i) => {
    const bar = h("button", "bar" + (i === s.weeks.length - 1 ? " now" : ""));
    bar.setAttribute("aria-label", `Vecka ${w.label.slice(1)}: ${fmtDuration(w.ms)}`);
    const fill = h("i");
    bar.appendChild(fill);
    requestAnimationFrame(() => requestAnimationFrame(() => { fill.style.transform = `scaleY(${w.ms / max})`; }));
    bar.addEventListener("click", () => {
      bars.querySelectorAll(".sel").forEach((b) => b.classList.remove("sel"));
      bar.classList.add("sel");
      showWeek(w);
    });
    bars.appendChild(bar);
    axis.appendChild(h("span", null, w.label));
  });
  showWeek(s.weeks[s.weeks.length - 1]);
  weekPanel.append(readout, bars, axis);
  panels.appendChild(weekPanel);

  const rankPanel = (title, list, note, valueFn, showPos) => {
    const p = h("div", "panel");
    p.appendChild(h("h2", null, title));
    if (!list.length) { p.appendChild(h("div", "panel-note", note)); return p; }
    list.forEach((b, i) => {
      const row = h("div", "rank");
      if (showPos) row.appendChild(h("span", "pos", String(i + 1)));
      row.appendChild(coverEl(b.cover_small));
      const text = h("div", "rank-text");
      text.append(h("div", "rank-title", b.title), h("div", "rank-sub", b.author || ""));
      row.appendChild(text);
      row.appendChild(h("div", "rank-val", valueFn(b)));
      row.addEventListener("click", () => openBook(b.id));
      p.appendChild(row);
    });
    return p;
  };
  const countText = (b) => b.listen_count ? `${b.listen_count}× · ` : "";
  panels.appendChild(rankPanel("Mest lyssnade", s.top.slice(0, 5),
    "Topplistan fylls på automatiskt när du lyssnar.", (b) => countText(b) + fmtDuration(b.total_listened_ms), true));
  panels.appendChild(rankPanel("Favoriter", s.favorites,
    "Tryck på hjärtat på en bok för att lägga till den här.", (b) => fmtPercent(b.progress), false));

  const perBook = h("div", "panel");
  perBook.appendChild(h("h2", null, "Per bok"));
  if (!s.books.length) perBook.appendChild(h("div", "panel-note", "Ingen lyssningstid registrerad än."));
  s.books.forEach((b) => {
    const row = h("div", "rank");
    const text = h("div", "rank-text");
    text.append(h("div", "rank-title", b.title),
      h("div", "rank-sub", `${b.listen_count} ${b.listen_count === 1 ? "genomlyssning" : "genomlyssningar"} · ${fmtDuration(b.total_listened_ms)}`));
    row.appendChild(text);
    row.appendChild(progressEl(b.progress));
    row.appendChild(h("div", "rank-val", fmtPercent(b.progress)));
    perBook.appendChild(row);
  });
  panels.appendChild(perBook);

  frag.appendChild(panels);
  root.replaceChildren(frag);
}

/* ------------------------------------------------------------ settings */
let scanTimer = null;
async function startScan() {
  try {
    await api("/api/scan-library", {});
  } catch (e) { toast(e.message); }
  refreshScan();
}
$("scanBtn").addEventListener("click", startScan);

async function refreshScan() {
  clearTimeout(scanTimer);
  let s;
  try { s = await api("/api/scan-status"); } catch (e) { return; }
  const btn = $("scanBtn");
  btn.disabled = s.running;
  btn.classList.toggle("spin", s.running);
  btn.querySelector("span").textContent = s.running ? "Söker…" : "Sök efter böcker";
  $("scanProgress").hidden = !s.running;
  if (s.running) {
    const frac = s.total ? s.done / s.total : 0;
    $("scanFill").style.transform = `scaleX(${frac})`;
    $("scanText").textContent = s.phase === "reading" && s.total
      ? `Läser ${s.done + 1} av ${s.total}${s.current ? " – " + s.current : ""}` : "Letar efter filer…";
    scanTimer = setTimeout(refreshScan, 500);
  } else if (s.last_result) {
    const r = s.last_result;
    const parts = [];
    if (r.added) parts.push(`${r.added} nya`);
    if (r.updated) parts.push(`${r.updated} uppdaterade`);
    if (r.relinked) parts.push(`${r.relinked} flyttade`);
    if (r.missing) parts.push(`${r.missing} borttagna`);
    if (r.restored) parts.push(`${r.restored} återfunna`);
    if (r.errors) parts.push(`${r.errors} kunde inte läsas`);
    const when = new Date(s.last_finished * 1000).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" });
    $("scanText").textContent = `Senaste sökning ${when}: ${parts.length ? parts.join(", ") : "inga ändringar"}.`;
    if (state.libVersion !== s.library_version) { state.libVersion = s.library_version; state.libDirty = true; }
  } else {
    $("scanText").textContent = "";
  }
  $("watchText").textContent = s.watching ? `Bevakar mappen (${s.watching}).` : "Automatisk bevakning är avstängd.";
}

/* ------------------------------------------------------------ theme */
function resolveTheme(pref) {
  if (pref === "light" || pref === "dark") return pref;
  if (pref === "system") return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  const hr = new Date().getHours();
  return hr >= 19 || hr < 7 ? "dark" : "light";
}
function applyTheme() {
  const t = resolveTheme(state.theme);
  document.documentElement.dataset.theme = t;
  store.set("abp-theme-resolved", t);
  document.querySelectorAll("[data-theme-opt]").forEach((c) =>
    c.setAttribute("aria-pressed", String(c.dataset.themeOpt === state.theme)));
}
$("themeChips").addEventListener("click", async (e) => {
  const c = e.target.closest("[data-theme-opt]");
  if (!c) return;
  state.theme = c.dataset.themeOpt;
  applyTheme();
  try { await api("/api/settings", { theme: state.theme }); } catch (err) { toast(err.message); }
});
setInterval(applyTheme, 60000);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);

/* ------------------------------------------------------------ start */
(async function init() {
  try {
    const settings = await api("/api/settings");
    state.theme = settings.theme || "evening";
  } catch (e) { /* keep default */ }
  applyTheme();
  try { state.status = await api("/api/status"); state.libVersion = state.status.library_version; } catch (e) { /* offline */ }
  if (state.status.book_id != null) await loadCurrentBook();
  go(location.hash.replace("#/", "") || "library", true);
  if (state.view !== "library") loadBooks();
})();
