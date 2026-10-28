// Campaign list, and per-campaign tabs for ingesting sessions and reference
// material and browsing what the assistant has recorded.

import { api, h, toast, enc } from "./util.js";

export async function campaignList() {
  const campaigns = await api("GET", "/api/campaigns");
  const name = h("input", { placeholder: "Campaign name", required: true, "aria-label": "Campaign name" });
  const role = h("select", { "aria-label": "Your role" },
    h("option", { value: "dm" }, "I'm the DM"), h("option", { value: "player" }, "I'm a player"));
  const create = async (e) => {
    e.preventDefault();
    try {
      const c = await api("POST", "/api/campaigns", { name: name.value.trim(), role: role.value });
      location.hash = `#/campaigns/${c.slug}`;
    } catch (err) {
      toast(err.message, true);
    }
  };
  return h("section.page",
    h("h1", "Campaigns"),
    h("div.cards",
      campaigns.length === 0 && h("p.empty", "No campaigns yet."),
      campaigns.map((c) => h("a.card", { href: `#/campaigns/${c.slug}` },
        h("strong", c.name), h(`span.badge.${c.role}`, c.role === "dm" ? "DM" : "Player"))),
    ),
    h("form.inline-form", { onsubmit: create }, name, role, h("button.primary", { type: "submit" }, "Create")),
    h("p.muted.small", "In a player campaign the assistant never sees DM-only material or anything the party hasn't discovered."),
  );
}

const TABS = [
  ["ingest", "Sessions"],
  ["reference", "Reference"],
  ["state", "World state"],
  ["journal", "Journal"],
];

export async function campaignView(slug, tab) {
  const campaigns = await api("GET", "/api/campaigns");
  const c = campaigns.find((x) => x.slug === slug);
  if (!c) throw new Error(`No campaign "${slug}".`);
  const body = h("div.tab-body", h("p.muted", "Loading…"));
  const render = { ingest: sessionsTab, reference: referenceTab, state: stateTab, journal: journalTab }[tab] || sessionsTab;
  render(c).then((el) => body.replaceChildren(el), (err) => body.replaceChildren(h("p.empty", err.message)));
  return h("section.page",
    h("div.page-head", h("h1", c.name), h(`span.badge.${c.role}`, c.role === "dm" ? "DM" : "Player")),
    h("nav.tabs", TABS.map(([key, label]) => h("a", { href: `#/campaigns/${slug}/${key}`, className: key === tab ? "active" : "" }, label))),
    body,
  );
}

// -- sessions & ingest ---------------------------------------------------------

async function sessionsTab(c) {
  const base = `/api/campaigns/${c.slug}`;
  const data = await api("GET", `${base}/sessions`);
  const list = h("div.session-list");
  const logPanel = h("div.job", { hidden: true });

  const renderSessions = (sessions) => list.replaceChildren(
    sessions.length === 0 ? h("p.empty", "No sessions ingested yet.") : "",
    ...[...sessions].reverse().map((s) => h("details.card.session",
      h("summary",
        h("span.session-num", `#${s.number}`),
        h("strong", s.title || "(untitled)"),
        s.played_on && h("span.muted", s.played_on)),
      h("div.prose", s.summary ? s.summary.split(/\n\s*\n/).map((p) => h("p", p)) : h("p.muted", "No summary.")),
      h("div.actions",
        h("a", { href: `#/campaigns/${c.slug}/journal` }, "Journal"),
        h("button.danger", {
          onclick: async () => {
            if (!confirm(`Undo everything the ingest of session ${s.number} recorded?`)) return;
            const r = await api("POST", `${base}/sessions/${s.number}/revert`);
            toast(`Reverted: ${r.restored} restored, ${r.deleted} removed, ${r.events} journal entries`);
            renderSessions((await api("GET", `${base}/sessions`)).sessions);
          },
        }, "Revert"),
      ),
    )),
  );
  renderSessions(data.sessions);

  const number = h("input", { type: "number", min: 1, value: data.next, "aria-label": "Session number" });
  const date = h("input", { type: "date", value: new Date().toISOString().slice(0, 10), "aria-label": "Date played" });
  const text = h("textarea", { rows: 8, placeholder: "Paste a transcript or your session notes…", "aria-label": "Transcript" });
  const file = h("input", { type: "file", accept: ".txt,.md,.mp3,.m4a,.wav,.ogg,.flac,.opus,.webm", "aria-label": "Transcript or recording file" });
  const submit = h("button.primary", { type: "submit" }, "Ingest session");

  const watch = (jobId) => {
    submit.disabled = true;
    logPanel.hidden = false;
    pollJob(jobId, logPanel, async (job) => {
      submit.disabled = false;
      if (job.status === "done") {
        toast(`Session ${job.result.session} recorded`);
        text.value = "";
        file.value = "";
        const fresh = await api("GET", `${base}/sessions`);
        number.value = fresh.next;
        renderSessions(fresh.sessions);
      }
    });
  };

  const form = h("form.card.ingest-form", {
    onsubmit: async (e) => {
      e.preventDefault();
      const fd = new FormData();
      if (file.files[0]) fd.append("file", file.files[0]);
      fd.append("text", text.value);
      if (number.value) fd.append("number", number.value);
      fd.append("played_on", date.value);
      try {
        const { job } = await api("POST", `${base}/ingest`, fd);
        watch(job);
      } catch (err) {
        toast(err.message, true);
      }
    },
  },
    h("h2", "Add a session"),
    h("div.identity", h("label.field", h("span", "Session #"), number), h("label.field", h("span", "Played on"), date)),
    text,
    h("div.file-row", h("span.muted", "or upload a transcript or recording:"), file),
    h("div.actions", submit, h("span.muted.small", "Claude reads it, updates NPCs, quests and the journal, and writes a recap. Takes a few minutes.")),
  );

  if (data.running.length) watch(data.running[0].id);
  return h("div", form, logPanel, h("h2.section-title", "Sessions"), list);
}

function pollJob(jobId, panel, onDone) {
  const lines = [];
  const out = h("div.job-log");
  const statusEl = h("span.job-status.running", "Running");
  panel.replaceChildren(h("div.job-head", h("strong", "Ingest"), statusEl), out);
  let since = 0;
  const tick = async () => {
    let job;
    try {
      job = await api("GET", `/api/jobs/${jobId}?since=${since}`);
    } catch (err) {
      statusEl.textContent = err.message;
      statusEl.className = "job-status failed";
      return onDone({ status: "failed" });
    }
    // The server resends from `from`, since the last text entry may have grown.
    job.log.forEach((entry, i) => {
      const idx = job.from + i;
      const el = lines[idx] || (lines[idx] = out.appendChild(h(`div.log-${entry.kind}`)));
      el.textContent = entry.text;
    });
    since = job.from + job.log.length;
    out.scrollTop = out.scrollHeight;
    if (job.status === "running") return setTimeout(tick, 1000);
    statusEl.textContent = job.status === "done" ? "Done" : `Failed: ${job.error}`;
    statusEl.className = `job-status ${job.status}`;
    if (job.status === "done" && job.result) {
      panel.append(h("div.job-result",
        h("h3", `Session ${job.result.session}: ${job.result.title || "(untitled)"}`),
        h("p.muted", `${job.result.journal_entries} journal ${job.result.journal_entries === 1 ? "entry" : "entries"} recorded.`),
      ));
    }
    onDone(job);
  };
  tick();
}

// -- reference material ----------------------------------------------------------

async function referenceTab(c) {
  const base = `/api/campaigns/${c.slug}`;
  const list = h("div.rows");
  const refresh = async () => {
    const sources = await api("GET", `${base}/sources`);
    list.replaceChildren(
      sources.length === 0 ? h("p.empty", "Nothing loaded yet. Upload the module PDF and any notes.") : "",
      ...sources.map((s) => h("div.source",
        h("div",
          h("strong", s.title), " ",
          h("span.badge", s.kind), s.dm_only ? h("span.badge.dm", "DM only") : "",
          h("div.muted.small", `${s.chunks} chunks${s.first_page ? ` · pages ${s.first_page}–${s.last_page}` : ""}`)),
        h("button.icon", {
          title: "Remove", "aria-label": `Remove ${s.title}`,
          onclick: async () => {
            if (!confirm(`Remove ${s.title}?`)) return;
            await api("DELETE", `${base}/sources/${s.id}`);
            refresh();
          },
        }, "×"),
      )),
    );
  };
  await refresh();

  const file = h("input", { type: "file", accept: ".pdf,.md,.markdown,.txt", required: true, "aria-label": "Reference file" });
  const title = h("input", { placeholder: "Title (defaults to file name)", "aria-label": "Title" });
  const kind = h("select", { "aria-label": "Kind" }, h("option", { value: "module" }, "Published module"), h("option", { value: "notes" }, "My notes"));
  const dmOnly = h("input", { type: "checkbox", checked: true });
  kind.addEventListener("change", () => (dmOnly.checked = kind.value === "module"));
  const submit = h("button.primary", { type: "submit" }, "Upload");
  const form = h("form.card", {
    onsubmit: async (e) => {
      e.preventDefault();
      const fd = new FormData();
      fd.append("file", file.files[0]);
      fd.append("title", title.value);
      fd.append("kind", kind.value);
      fd.append("dm_only", dmOnly.checked);
      submit.disabled = true;
      submit.textContent = "Reading…";
      try {
        const r = await api("POST", `${base}/sources`, fd);
        toast(`Loaded ${r.title}: ${r.chunks} chunks`);
        form.reset();
        dmOnly.checked = true;
        refresh();
      } catch (err) {
        toast(err.message, true);
      } finally {
        submit.disabled = false;
        submit.textContent = "Upload";
      }
    },
  },
    h("h2", "Add reference material"),
    h("div.identity", file, title, kind),
    h("label.check", dmOnly, " DM only ", h("span.muted.small", "(hidden from the assistant in player campaigns)")),
    h("div.actions", submit),
  );

  const q = h("input", { type: "search", placeholder: "Search the material, e.g. \"grick\" or \"Ireena\"", "aria-label": "Search" });
  const results = h("div.results");
  const search = async (e) => {
    e.preventDefault();
    const hits = await api("GET", `${base}/search?q=${enc(q.value)}`);
    results.replaceChildren(hits.length ? "" : h("p.empty", "No matches."), ...hits.map((hit) => {
      const full = h("div.chunk-text", { hidden: true });
      return h("div.hit",
        h("button.link", {
          onclick: async () => {
            if (full.hidden && !full.childNodes.length) {
              const rows = await api("GET", `${base}/chunks/${hit.id}`);
              full.append(...rows.map((r) => h("pre", r.text)));
            }
            full.hidden = !full.hidden;
          },
        }, h("strong", hit.heading || hit.source), h("span.muted", ` ${hit.source}${hit.page ? ` p${hit.page}` : ""}`)),
        h("p.snippet", highlight(hit.snippet)),
        full,
      );
    }));
  };
  return h("div", form, h("h2.section-title", "Loaded"), list,
    h("h2.section-title", "Search"), h("form.inline-form", { onsubmit: search }, q, h("button", { type: "submit" }, "Search")), results);
}

// FTS snippets mark matches with [ ]; render them as <mark> without innerHTML.
function highlight(snippet) {
  return snippet.split(/(\[[^\]]*\])/).map((part) =>
    part.startsWith("[") && part.endsWith("]") ? h("mark", part.slice(1, -1)) : part);
}

// -- world state ---------------------------------------------------------------------

async function stateTab(c) {
  const base = `/api/campaigns/${c.slug}`;
  const all = await api("GET", `${base}/entities`);
  const kinds = [...new Set(all.map((e) => e.kind))].sort();
  const statuses = [...new Set(all.map((e) => e.status).filter(Boolean))].sort();
  const kind = h("select", { "aria-label": "Kind" }, h("option", { value: "" }, "All kinds"), kinds.map((k) => h("option", { value: k }, k)));
  const status = h("select", { "aria-label": "Status" }, h("option", { value: "" }, "Any status"), statuses.map((s) => h("option", { value: s }, s)));
  const q = h("input", { type: "search", placeholder: "Filter by name…", "aria-label": "Filter" });
  const out = h("div");

  const render = () => {
    const needle = q.value.toLowerCase();
    const shown = all.filter((e) =>
      (!kind.value || e.kind === kind.value) && (!status.value || e.status === status.value) &&
      (!needle || e.name.toLowerCase().includes(needle) || e.summary.toLowerCase().includes(needle)));
    const groups = {};
    shown.forEach((e) => (groups[e.kind] ||= []).push(e));
    out.replaceChildren(
      all.length === 0 ? h("p.empty", "Nothing tracked yet. Ingest a session, or tell the assistant what happened in `campaign chat`.") : "",
      ...Object.entries(groups).map(([k, items]) => h("div",
        h("h2.section-title", `${kindLabel(k)} (${items.length})`),
        h("div.entity-grid", items.map(entityCard)))),
    );
  };
  [kind, status].forEach((el) => el.addEventListener("change", render));
  q.addEventListener("input", render);
  render();
  return h("div", h("div.filters", kind, status, q), out);
}

const KIND_LABELS = { npc: "NPCs", pc: "Player characters", lore: "Lore" };
const kindLabel = (k) => KIND_LABELS[k] || `${k}s`;

function entityCard(e) {
  const data = Object.entries(e.data);
  return h("div.card.entity",
    h("div.entity-head", h("strong", e.name),
      e.status && h(`span.badge.status-${e.status.replace(/\W+/g, "-")}`, e.status),
      !e.party_knows && h("span.badge.dm", "party doesn't know")),
    e.summary && h("p", e.summary),
    data.length > 0 && h("dl", data.flatMap(([k, v]) => [h("dt", k.replace(/_/g, " ")), h("dd", formatValue(v))])),
  );
}

function formatValue(v) {
  if (Array.isArray(v)) return v.length && typeof v[0] !== "object" ? v.join(", ") : h("ul", v.map((x) => h("li", formatValue(x))));
  if (v && typeof v === "object") return Object.entries(v).map(([k, x]) => `${k}: ${typeof x === "object" ? JSON.stringify(x) : x}`).join("; ");
  return String(v);
}

// -- journal ---------------------------------------------------------------------------

async function journalTab(c) {
  const base = `/api/campaigns/${c.slug}`;
  const [events, { sessions }] = await Promise.all([api("GET", `${base}/journal`), api("GET", `${base}/sessions`)]);
  if (!events.length) return h("p.empty", "The journal is empty.");
  const titles = Object.fromEntries(sessions.map((s) => [s.number, s.title]));
  const groups = new Map();
  events.forEach((e) => {
    const key = e.session ?? "other";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(e);
  });
  return h("div.journal", [...groups.entries()].reverse().map(([n, items]) => h("section",
    h("h2.section-title", n === "other" ? "Between sessions" : `Session ${n}${titles[n] ? ` — ${titles[n]}` : ""}`),
    h("ol", items.map((e) => h("li", e.text))),
  )));
}
