// Campaign list, and per-campaign tabs for ingesting sessions and reference
// material and browsing what the assistant has recorded.

import { api, h, toast, enc } from "./util.js";

export async function campaignList() {
  const [campaigns, info] = await Promise.all([api("GET", "/api/campaigns"), api("GET", "/api/info")]);
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
    h("p.muted.small.data-location",
      "Data is stored in ", h("code", info.data_dir),
      ` (schema v${info.schema_version}, ${info.originals.files} original files, ${(info.originals.bytes / 1e6).toFixed(1)} MB).`),
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
            toast(`Reverted session ${s.number}: ${r.facts} facts and ${r.events} journal entries removed, ${r.reopened} earlier facts reopened`);
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
        h("p.muted", `${job.result.facts} facts and ${job.result.journal_entries} journal ${job.result.journal_entries === 1 ? "entry" : "entries"} recorded.`),
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
  let timer;
  const refresh = async () => {
    clearTimeout(timer);
    if (!list.isConnected && timer !== undefined) return;  // tab was left
    const sources = await api("GET", `${base}/sources`);
    list.replaceChildren(
      sources.length === 0 ? h("p.empty", "Nothing loaded yet. Upload the module PDF and any notes.") : "",
      ...sources.map((s) => sourceRow(c, s, refresh)),
    );
    // Poll while anything is digesting.
    timer = sources.some((s) => s.digest.running) ? setTimeout(refresh, 2000) : null;
  };
  await refresh();

  const file = h("input", { type: "file", accept: ".pdf,.md,.markdown,.txt", required: true, "aria-label": "Reference file" });
  const title = h("input", { placeholder: "Title (defaults to file name)", "aria-label": "Title" });
  const kind = h("select", { "aria-label": "Kind" }, h("option", { value: "module" }, "Published module"), h("option", { value: "notes" }, "My notes"));
  const dmOnly = h("input", { type: "checkbox", checked: true });
  const digestNow = h("input", { type: "checkbox", checked: true });
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
      fd.append("digest_now", digestNow.checked);
      submit.disabled = true;
      submit.textContent = "Reading…";
      try {
        const r = await api("POST", `${base}/sources`, fd);
        toast(r.pending ? `Loaded: ${r.chunks} chunks, ${r.pending} sections to digest` : `Loaded: ${r.chunks} chunks, digest already cached`);
        form.reset();
        dmOnly.checked = digestNow.checked = true;
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
    h("label.check", digestNow, " Digest with Claude now ",
      h("span.muted.small", "(builds the outline and the NPC/monster/item catalog; only new or changed sections cost anything)")),
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

function digestLine(d) {
  if (d.running) return h("span.digest-status.running", `Digesting… ${d.done}/${d.total} sections`);
  if (!d.total) return h("span.digest-status", "Not digested");
  if (d.done === d.total) {
    const tokens = d.input_tokens + d.output_tokens;
    return h("span.digest-status.done", `Digested · ${d.entities} entries${tokens ? ` · ${Math.round(tokens / 1000)}k tokens` : ""}`);
  }
  return h("span.digest-status.partial",
    `${d.done}/${d.total} sections digested${d.failed ? `, ${d.failed} failed` : ""}`);
}

function sourceRow(c, s, refresh) {
  const base = `/api/campaigns/${c.slug}`;
  const d = s.digest;
  const canDigest = s.kind !== "transcript" && !d.running && (d.total === 0 || d.done < d.total);
  return h("div.source",
    h("div",
      h("a", { href: `#/campaigns/${c.slug}/source/${s.id}` }, h("strong", s.title)), " ",
      h("span.badge", s.kind), s.dm_only ? h("span.badge.dm", "DM only") : "",
      h("div.muted.small",
        `${s.chunks} chunks${s.first_page ? ` · pages ${s.first_page}–${s.last_page}` : ""} · `,
        s.has_original
          ? h("a", { href: `${base}/sources/${s.id}/original` }, "original")
          : h("span", { title: "Uploaded before originals were kept. Upload it again to enable re-indexing." }, "original not kept"),
        s.kind !== "transcript" ? [" · ", digestLine(d)] : ""),
    ),
    h("div.source-actions",
      canDigest && h("button", {
        onclick: async () => {
          try {
            await api("POST", `${base}/sources/${s.id}/digest`);
            refresh();
          } catch (err) {
            toast(err.message, true);
          }
        },
      }, d.done ? "Resume digest" : "Digest"),
      h("button.icon", {
        title: "Remove", "aria-label": `Remove ${s.title}`,
        onclick: async () => {
          if (!confirm(`Remove ${s.title}? The digest cache is kept, so re-uploading it later is free.`)) return;
          await api("DELETE", `${base}/sources/${s.id}`);
          refresh();
        },
      }, "×"),
    ),
  );
}

// -- one source's digest -----------------------------------------------------------

export async function sourceView(slug, sourceId) {
  const base = `/api/campaigns/${slug}`;
  const [campaigns, sources, outline, entities] = await Promise.all([
    api("GET", "/api/campaigns"), api("GET", `${base}/sources`),
    api("GET", `${base}/sources/${sourceId}/outline`), api("GET", `${base}/sources/${sourceId}/entities`),
  ]);
  const c = campaigns.find((x) => x.slug === slug);
  const src = sources.find((x) => x.id === Number(sourceId));
  if (!c || !src) throw new Error("No such source.");

  const children = {};
  outline.forEach((n) => (children[n.parent_id] ||= []).push(n));
  const pages = (n) => (n.page_start ? (n.page_end && n.page_end !== n.page_start ? ` pp. ${n.page_start}–${n.page_end}` : ` p. ${n.page_start}`) : "");
  const nodeEl = (n) => {
    const kids = children[n.id] || [];
    const head = h("summary",
      h("strong", n.title), h("span.muted.small", pages(n)),
      n.summary === null && h("span.badge", n.error ? "failed" : "pending"),
      n.entity_count ? h("span.muted.small", ` · ${n.entity_count} entries`) : "");
    const body = [
      n.summary ? h("p.node-summary", n.summary) : n.error ? h("p.error-text", n.error) : "",
      kids.map(nodeEl),
    ];
    return h("details.outline-node", { open: n.depth < 1 }, head, body);
  };

  const kinds = [...new Set(entities.map((e) => e.kind))].sort();
  const kind = h("select", { "aria-label": "Kind" }, h("option", { value: "" }, "All kinds"), kinds.map((k) => h("option", { value: k }, kindLabel(k))));
  const q = h("input", { type: "search", placeholder: "Filter by name or alias…", "aria-label": "Filter" });
  const grid = h("div.entity-grid");
  const render = () => {
    const needle = q.value.toLowerCase();
    const shown = entities.filter((e) => (!kind.value || e.kind === kind.value) &&
      (!needle || [e.name, ...e.aliases, e.summary].some((x) => x.toLowerCase().includes(needle))));
    grid.replaceChildren(shown.length ? "" : h("p.empty", entities.length ? "No matches." : "No catalog yet: it's built when the whole book is digested."),
      ...shown.map(refCard));
  };
  kind.addEventListener("change", render);
  q.addEventListener("input", render);
  render();

  return h("section.page",
    h("div.page-head", h("a", { href: `#/campaigns/${slug}/reference` }, `← ${c.name}`)),
    h("div.page-head", h("h1", src.title), src.kind !== "transcript" && digestLine(src.digest)),
    h("h2.section-title", "Outline"),
    outline.length ? h("div.outline", outline.filter((n) => n.parent_id === null).map(nodeEl)) : h("p.empty", "No outline yet."),
    h("h2.section-title", `Catalog (${entities.length})`),
    h("div.filters.two", kind, q),
    grid,
  );
}

function refCard(e) {
  const sb = e.stat_block;
  return h("div.card.entity",
    h("div.entity-head", h("strong", e.name), h("span.badge", e.kind),
      e.pages.length > 0 && h("span.muted.small", `p. ${e.pages.join(", ")}`)),
    e.aliases.length > 0 && h("p.muted.small", "aka ", e.aliases.join(" · ")),
    e.summary && h("p", e.summary),
    e.facts.length > 0 && h("ul.facts", e.facts.map((f) => h("li", f))),
    sb && h("details.statblock",
      h("summary", `AC ${sb.armor_class} · HP ${sb.hit_points}${sb.challenge ? ` · CR ${sb.challenge}` : ""}`),
      h("p.small", `Speed ${sb.speed}`),
      h("div.stat-abilities", ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"].map((a) =>
        h("div", h("span.label", a.slice(0, 3).toUpperCase()), h("span", sb[a] ?? "—")))),
      [...sb.traits, ...sb.actions, ...sb.other].map((t) => h("p.small", t)),
    ),
  );
}

// FTS snippets mark matches with [ ]; render them as <mark> without innerHTML.
function highlight(snippet) {
  return snippet.split(/(\[[^\]]*\])/).map((part) =>
    part.startsWith("[") && part.endsWith("]") ? h("mark", part.slice(1, -1)) : part);
}

// -- world state ---------------------------------------------------------------------

const KIND_LABELS = { npc: "NPCs", pc: "Player characters", lore: "Lore" };
const kindLabel = (k) => KIND_LABELS[k] || `${k}s`;
const range = (f) => (f.valid_to === null ? `since session ${f.valid_from}`
  : f.valid_to === f.valid_from ? `during session ${f.valid_from}` : `sessions ${f.valid_from}–${f.valid_to}`);
const ORIGINS = { ingest: (f) => `session ${f.run_session}`, chat: () => "chat", manual: () => "edited by hand" };
const sourceLabel = (f) => (f.origin === "book" || !ORIGINS[f.origin] ? f.source || f.origin : ORIGINS[f.origin](f))
  + (f.pages?.length ? ` p. ${f.pages.join(", ")}` : "");

async function stateTab(c) {
  const base = `/api/campaigns/${c.slug}`;
  const inPlay = h("input", { type: "checkbox" });
  const kind = h("select", { "aria-label": "Kind" }, h("option", { value: "" }, "All kinds"));
  const status = h("select", { "aria-label": "Status" }, h("option", { value: "" }, "Any status"));
  const q = h("input", { type: "search", placeholder: "Filter by name, alias or fact…", "aria-label": "Filter" });
  const out = h("div");
  let all = [];

  const load = async () => {
    all = (await api("GET", `${base}/entities${inPlay.checked ? "?in_play=true" : ""}`)).entities;
    const fill = (sel, values, label) => {
      const keep = sel.value;
      sel.replaceChildren(h("option", { value: "" }, label), values.map((v) => h("option", { value: v }, v)));
      sel.value = values.includes(keep) ? keep : "";
    };
    fill(kind, [...new Set(all.map((e) => e.kind))].sort(), "All kinds");
    fill(status, [...new Set(all.map((e) => e.current.status).filter(Boolean))].sort(), "Any status");
    render();
  };
  const render = () => {
    const needle = q.value.toLowerCase();
    const shown = all.filter((e) =>
      (!kind.value || e.kind === kind.value) && (!status.value || e.current.status === status.value) &&
      (!needle || [e.name, ...e.aliases, e.headline || ""].some((x) => x.toLowerCase().includes(needle))));
    const groups = {};
    shown.forEach((e) => (groups[e.kind] ||= []).push(e));
    out.replaceChildren(
      all.length === 0 ? h("p.empty", inPlay.checked
        ? "Nothing in play yet. Ingest a session, or tell the assistant what happened."
        : "The world is empty. Digest a module or ingest a session.") : "",
      ...Object.entries(groups).map(([k, items]) => h("div",
        h("h2.section-title", `${kindLabel(k)} (${items.length})`),
        h("div.entity-grid", items.map((e) => worldCard(c, e))))),
    );
  };
  inPlay.addEventListener("change", load);
  [kind, status].forEach((el) => el.addEventListener("change", render));
  q.addEventListener("input", render);
  await load();
  return h("div",
    h("div.filters", kind, status, q),
    h("label.check", inPlay, " Only what the table has interacted with"),
    out);
}

function worldCard(c, e) {
  const attrs = Object.entries(e.current);
  return h("a.card.entity", { href: `#/campaigns/${c.slug}/entity/${e.id}` },
    h("div.entity-head", h("strong", e.name),
      e.current.status && h(`span.badge.status-${e.current.status.replace(/\W+/g, "-")}`, e.current.status)),
    e.aliases.length > 0 && h("p.muted.small", "aka ", e.aliases.slice(0, 4).join(" · ")),
    attrs.filter(([k]) => k !== "status").length > 0 && h("dl", attrs.filter(([k]) => k !== "status")
      .flatMap(([k, v]) => [h("dt", k), h("dd", v)])),
    e.headline && h("p", e.headline),
    h("p.muted.small", `${e.current_facts} current · ${e.past_facts} past${e.table_activity ? "" : " · from the book only"}`),
  );
}

export async function entityView(slug, id) {
  const base = `/api/campaigns/${slug}`;
  const [campaigns, e, list] = await Promise.all([
    api("GET", "/api/campaigns"), api("GET", `${base}/entities/${id}`), api("GET", `${base}/entities`),
  ]);
  const c = campaigns.find((x) => x.slug === slug);
  const now = e.as_of;
  const body = h("div.entity-body");

  const slider = h("input", { type: "range", min: 0, max: Math.max(now, 0), value: now, "aria-label": "As of session" });
  const sliderLabel = h("span.as-of-label");
  const renderAt = () => {
    const t = Number(slider.value);
    sliderLabel.textContent = t === now ? `Now (session ${now})` : `As of session ${t}`;
    body.replaceChildren(...factSections(c, e, t, t === now));
  };
  slider.addEventListener("input", renderAt);

  const summary = h("div.card.summary",
    e.summary ? h("p", e.summary) : h("p.muted", "No summary for the current facts yet."),
    h("button", {
      onclick: async (ev) => {
        ev.target.disabled = true;
        ev.target.textContent = "Writing…";
        try {
          const r = await api("POST", `${base}/entities/${id}/summary`);
          summary.firstChild.replaceWith(h("p", r.summary));
          ev.target.remove();
        } catch (err) {
          toast(err.message, true);
          ev.target.disabled = false;
          ev.target.textContent = "Write summary";
        }
      },
    }, e.summary ? "" : "Write summary"),
  );
  if (e.summary) summary.lastChild.remove();

  const others = list.entities.filter((x) => x.id !== e.id);
  const mergeInput = h("input", { list: "merge-options", placeholder: "Another entity's name", "aria-label": "Entity to merge" });
  const merge = h("form.inline-form", {
    onsubmit: async (ev) => {
      ev.preventDefault();
      const other = others.find((x) => x.name === mergeInput.value);
      if (!other) return toast("Pick an entity from the list", true);
      if (!confirm(`Merge ${other.name} into ${e.name}? Its facts, aliases and events move here.`)) return;
      await api("POST", `${base}/entities/${id}/merge`, { other_id: other.id });
      location.reload();
    },
  }, mergeInput, h("datalist", { id: "merge-options" }, others.map((x) => h("option", { value: x.name }, x.kind))),
    h("button", { type: "submit" }, "Merge into this"));

  renderAt();
  return h("section.page",
    h("div.page-head", h("a", { href: `#/campaigns/${slug}/state` }, `← ${c.name}`)),
    h("div.page-head", h("h1", e.name), h("span.badge", e.kind)),
    e.aliases.length > 0 && h("p.muted", "Also known as ", e.aliases.join(" · ")),
    summary,
    now > 0 && h("div.as-of", sliderLabel, slider),
    body,
    h("details.card", h("summary", "Same thing as another entry?"), h("p.muted.small", "Merging keeps both histories; the other name becomes an alias."), merge),
  );
}

function factSections(c, e, t, isNow) {
  const base = `/api/campaigns/${c.slug}`;
  const facts = e.all_facts.filter((f) => f.valid_from <= t);
  const openAt = (f) => f.valid_to === null || f.valid_to > t;
  const current = facts.filter((f) => openAt(f) && f.modality === "fact");
  const attrs = current.filter((f) => f.attribute && f.attribute !== "stat_block");
  const statements = current.filter((f) => !f.attribute);
  const statBlocks = current.filter((f) => f.attribute === "stat_block");
  const maybe = facts.filter((f) => openAt(f) && f.modality !== "fact");
  const past = facts.filter((f) => !openAt(f));
  const events = e.all_events.filter((ev) => ev.session <= t);

  const endButton = (f) => isNow && openAt(f) && h("button.icon", {
    title: "No longer true", "aria-label": "Mark as no longer true",
    onclick: async () => {
      await api("POST", `${base}/facts/${f.id}/end`);
      location.reload();
    },
  }, "×");
  const factRow = (f, text) => h("li.fact",
    h("span", text || f.statement),
    h("span.fact-meta", range(f), " · ", sourceLabel(f), f.party_knows_from === null ? " · party doesn't know" : ""),
    endButton(f));

  const add = isNow && (() => {
    const statement = h("input", { placeholder: "Something that's now true", required: true, "aria-label": "Statement" });
    const attribute = h("input", { list: "attr-options", placeholder: "attribute (optional)", "aria-label": "Attribute" });
    const value = h("input", { placeholder: "value", "aria-label": "Value" });
    const known = h("input", { type: "checkbox", checked: true });
    return h("form.card.add-fact", {
      onsubmit: async (ev) => {
        ev.preventDefault();
        try {
          await api("POST", `${base}/entities/${e.id}/facts`, {
            statement: statement.value, attribute: attribute.value || null, value: value.value || null, party_knows: known.checked,
          });
          location.reload();
        } catch (err) {
          toast(err.message, true);
        }
      },
    },
      h("h2", "Add a fact"),
      statement,
      h("div.identity", attribute, value),
      h("datalist", { id: "attr-options" }, ["status", "location", "holder", "disposition", "leader", "allegiance", "condition"].map((a) => h("option", { value: a }))),
      h("label.check", known, " The party knows this"),
      h("p.muted.small", "With an attribute, this replaces the current value, which moves to history."),
      h("div.actions", h("button.primary", { type: "submit" }, "Add")),
    );
  })();

  return [
    h("section.card",
      h("h2", isNow ? "True now" : `True at the end of session ${t}`),
      attrs.length > 0 && h("ul.facts.attrs", attrs.map((f) => factRow(f, h("span", h("strong", f.attribute), ": ", f.value)))),
      statements.length ? h("ul.facts", statements.map((f) => factRow(f))) : h("p.empty", "Nothing recorded."),
      statBlocks.map((f) => statBlockEl(JSON.parse(f.value), sourceLabel(f))),
    ),
    maybe.length > 0 && h("section.card", h("h2", "Rumors and possibilities"),
      h("ul.facts", maybe.map((f) => factRow(f, `${f.modality === "rumor" ? "Rumor: " : "Possible: "}${f.statement}`)))),
    past.length > 0 && h("section.card", h("h2", "No longer true"), h("ul.facts.past", past.map((f) => factRow(f)))),
    events.length > 0 && h("section.card", h("h2", "Events"),
      h("ol.events", events.map((ev) => h("li", h("span.session-num", `S${ev.session}`), " ", ev.text)))),
    add,
  ];
}

function statBlockEl(sb, source) {
  return h("details.statblock",
    h("summary", `Stat block: AC ${sb.armor_class} · HP ${sb.hit_points}${sb.challenge ? ` · CR ${sb.challenge}` : ""}`, h("span.fact-meta", ` ${source}`)),
    h("p.small", `Speed ${sb.speed}`),
    h("div.stat-abilities", ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"].map((a) =>
      h("div", h("span.label", a.slice(0, 3).toUpperCase()), h("span", sb[a] ?? "—")))),
    [...sb.traits, ...sb.actions, ...sb.other].map((x) => h("p.small", x)),
  );
}

// -- journal ---------------------------------------------------------------------------

async function journalTab(c) {
  const base = `/api/campaigns/${c.slug}`;
  const [events, { sessions }] = await Promise.all([api("GET", `${base}/journal`), api("GET", `${base}/sessions`)]);
  if (!events.length) return h("p.empty", "The journal is empty.");
  const titles = Object.fromEntries(sessions.map((s) => [s.number, s.title]));
  const groups = new Map();
  events.forEach((e) => {
    if (!groups.has(e.session)) groups.set(e.session, []);
    groups.get(e.session).push(e);
  });
  return h("div.journal", [...groups.entries()].reverse().map(([n, items]) => h("section",
    h("h2.section-title", n === 0 ? "Before play" : `Session ${n}${titles[n] ? ` — ${titles[n]}` : ""}`),
    h("ol", items.map((e) => h("li", e.text,
      e.entities.length > 0 && h("span.event-entities", " — ",
        e.entities.map((x, i) => [i ? ", " : "", h("a", { href: `#/campaigns/${c.slug}/entity/${x.id}` }, x.name)]))))),
  )));
}
