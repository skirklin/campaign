// Campaign list, and per-campaign tabs for ingesting sessions and reference
// material and browsing what the assistant has recorded.

import { api, h, toast, enc, whoami, canEdit, live, streamPost } from "./util.js";
import { membersTab } from "./people.js";
import { characterCard, newCharacterForm } from "./sheet.js";

export async function campaignList() {
  const [campaigns, info] = await Promise.all([api("GET", "/api/campaigns"), api("GET", "/api/info").catch(() => null)]);
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
        h("strong", c.name),
        h("span", h(`span.badge.${c.role}`, c.role === "dm" ? "DM" : "Player"), " ", c.access !== "owner" && h("span.badge", c.access)))),
    ),
    h("form.inline-form", { onsubmit: create }, name, role, h("button.primary", { type: "submit" }, "Create")),
    h("p.muted.small", "In a player campaign the assistant never sees DM-only material or anything the party hasn't discovered."),
    info && h("p.muted.small.data-location",
      "Data is stored in ", h("code", info.data_dir),
      ` (schema v${info.schema_version}, ${info.originals.files} original files, ${(info.originals.bytes / 1e6).toFixed(1)} MB).`),
  );
}

const TABS = [
  ["ask", "Ask"],
  ["ingest", "Sessions"],
  ["party", "Party"],
  ["reference", "Reference"],
  ["state", "World state"],
  ["journal", "Journal"],
  ["members", "Members"],  // only with sign-in on
];

export async function campaignView(slug, tab) {
  const [campaigns, me] = await Promise.all([api("GET", "/api/campaigns"), whoami()]);
  const c = campaigns.find((x) => x.slug === slug);
  if (!c) throw new Error(`No campaign "${slug}".`);
  const tabs = TABS.filter(([key]) => key !== "members" || !me.local);
  const body = h("div.tab-body", h("p.muted", "Loading…"));
  const render = { ask: askTab, ingest: sessionsTab, party: partyTab, reference: referenceTab, state: stateTab, journal: journalTab, members: membersTab }[tab] || sessionsTab;
  render(c, me).then((el) => body.replaceChildren(el), (err) => body.replaceChildren(h("p.empty", err.message)));
  return h("section.page",
    h("div.page-head", h("h1", c.name), h(`span.badge.${c.role}`, c.role === "dm" ? "DM" : "Player"),
      !canEdit(c) && h("span.badge", "view only")),
    h("nav.tabs", tabs.map(([key, label]) => h("a", { href: `#/campaigns/${slug}/${key}`, className: key === tab ? "active" : "" }, label))),
    body,
  );
}

// -- ask: chat with the campaign assistant ----------------------------------------

async function askTab(c) {
  const base = `/api/campaigns/${c.slug}`;
  let chats = await api("GET", `${base}/chats`);
  let current = null;
  const thread = h("div.chat-thread");
  const pick = h("select", { "aria-label": "Conversation" });
  const input = h("textarea", { rows: 3, placeholder: c.role === "player"
    ? "Ask about the campaign: what the party knows, rules, your character…"
    : "Ask about the module, the world, or tell it what happened…", "aria-label": "Message" });
  const send = h("button.primary", { type: "submit" }, "Send");

  const bubble = (role, kind, text) => h(`div.chat-${kind === "tool" ? "tool" : role}`, text);
  const fillPick = () => pick.replaceChildren(
    h("option", { value: "" }, chats.length ? "New conversation" : "No conversations yet"),
    ...chats.map((x) => h("option", { value: x.id }, x.title || "(untitled)")),
  );
  const open = async (id) => {
    current = id ? Number(id) : null;
    pick.value = id || "";
    if (!current) {
      thread.replaceChildren(h("p.empty", "Ask anything about this campaign. "
        + (c.role === "player" ? "It only knows what the party knows." : "It can look things up in the module and the world, and record what happened.")));
      return;
    }
    const conv = await api("GET", `${base}/chats/${current}`);
    thread.replaceChildren(...conv.turns.map((t) => bubble(t.role, t.kind, t.text)));
    thread.scrollTop = thread.scrollHeight;
  };
  pick.addEventListener("change", () => open(pick.value));

  const form = h("form.chat-form", {
    onsubmit: async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      send.disabled = true;
      try {
        if (!current) {
          const conv = await api("POST", `${base}/chats`);
          current = conv.id;
          chats = [conv, ...chats];
          if (thread.querySelector(".empty")) thread.replaceChildren();
        }
        input.value = "";
        thread.append(bubble("user", "text", text));
        let answer = null;
        await streamPost(`${base}/chats/${current}/messages`, { text }, (type, data) => {
          if (type === "text") {
            if (!data.trim() && !answer) return;  // the line break after a round of tool calls
            answer ||= thread.appendChild(bubble("assistant", "text", ""));
            answer.textContent += data;
          } else if (type === "tool") {
            thread.append(bubble("assistant", "tool", data));
            answer = null;  // text after a tool call starts a new paragraph
          } else if (type === "error") {
            thread.append(h("div.chat-error", data));
          }
          thread.scrollTop = thread.scrollHeight;
        });
        chats = await api("GET", `${base}/chats`);
        fillPick();
        pick.value = current;
      } catch (err) {
        toast(err.message, true);
      } finally {
        send.disabled = false;
        input.focus();
      }
    },
  }, input, h("div.actions", send, h("span.muted.small", "Enter to send, Shift+Enter for a new line")));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  fillPick();
  await open(chats[0]?.id || "");
  return h("div.chat",
    h("div.chat-head", pick,
      h("button", { onclick: () => open("") }, "New"),
      h("button.danger", {
        onclick: async () => {
          if (!current || !confirm("Delete this conversation?")) return;
          await api("DELETE", `${base}/chats/${current}`);
          chats = chats.filter((x) => x.id !== current);
          fillPick();
          open(chats[0]?.id || "");
        },
      }, "Delete")),
    thread, form);
}

// -- party ---------------------------------------------------------------------

async function partyTab(c) {
  const base = `/api/campaigns/${c.slug}`;
  const [party, mine, scans] = await Promise.all([
    api("GET", `${base}/characters`), api("GET", "/api/characters"), api("GET", `${base}/jobs?kind=scan`)]);
  const cards = h("div.cards");
  const renderParty = (list) => cards.replaceChildren(
    list.length === 0 ? h("p.empty", "No characters in this campaign yet.") : "",
    ...list.map((x) => characterCard(x)));
  renderParty(party);
  // Sheets being read from scans: jobs, so they carry on in the background.
  const jobs = jobList(c, "scan", scans.filter((j) => j.state !== "done" && j.state !== "cancelled"), cards, {
    anyone: true,  // your own scans, even without edit access; the server checks
    title: (job) => `Reading ${job.name}`,
    result: (job) => h("div.job-result",
      h("p", h("a", { href: `#/characters/${job.result.character}` }, `Open ${job.result.name}`),
        job.result.unreadable.length ? h("span.muted", ` · some things weren't clear; they're listed in the sheet's notes`) : "")),
    onFinish: async () => renderParty(await api("GET", `${base}/characters`)),
  });
  // Your characters elsewhere, which can be copied into this campaign.
  const elsewhere = mine.filter((x) => x.mine && x.campaign.slug !== c.slug);
  const pick = h("select", { "aria-label": "Character to copy" },
    elsewhere.map((x) => h("option", { value: x.id }, `${x.name} (${x.campaign.name})`)));
  return h("div",
    cards,
    h("h2.section-title", "Add a character"),
    newCharacterForm([], c),
    scanForm(c, jobs),
    jobs.el,
    elsewhere.length > 0 && h("form.inline-form", {
      onsubmit: async (e) => {
        e.preventDefault();
        try {
          const r = await api("POST", `/api/characters/${pick.value}/copy`, { campaign: c.slug });
          location.hash = `#/characters/${r.id}`;
        } catch (err) {
          toast(err.message, true);
        }
      },
    }, h("span.muted", "Or bring one of yours from another campaign:"), pick, h("button", { type: "submit" }, "Copy here")),
    elsewhere.length > 0 && h("p.muted.small", "A copy is a separate sheet: changes here don't touch the original."),
  );
}

// Upload a scan or PDF of a paper sheet; Claude reads it into a new character, in the background.
function scanForm(c, jobs) {
  const file = h("input", { type: "file", accept: ".pdf,.png,.jpg,.jpeg,.webp", hidden: true });
  const btn = h("button", { type: "button", onclick: () => file.click() }, "From a scan or PDF…");
  file.addEventListener("change", async () => {
    const fd = new FormData();
    fd.append("file", file.files[0]);
    btn.disabled = true;
    try {
      jobs.watch({ id: (await api("POST", `/api/campaigns/${c.slug}/characters/from-scan`, fd)).job });
    } catch (err) {
      toast(err.message, true);
    }
    btn.disabled = false;
    file.value = "";
  });
  return h("p.inline-form", btn, file,
    h("span.muted.small", "A photo or PDF of a paper sheet. Claude reads it into a new character you can then "
      + "correct; it takes a minute or two and carries on if you leave the page."));
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
        canEdit(c) && h("button.danger", {
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

  // Ingests are jobs the server works through in the background: they carry on
  // whether or not this page is open, and survive restarts.
  const jobs = jobList(c, "ingest", data.ingests, list, {
    title: (job) => `Session ${job.session}: ${job.name}`,
    result: (job) => h("div.job-result",
      h("h3", `Session ${job.result.session}: ${job.result.title || "(untitled)"}`),
      h("p.muted", `${job.result.facts} facts and ${job.result.journal_entries} journal ${job.result.journal_entries === 1 ? "entry" : "entries"} recorded.`)),
    onFinish: async (job) => {
      toast(`Session ${job.result.session} recorded`);
      const fresh = await api("GET", `${base}/sessions`);
      number.value = fresh.next;
      renderSessions(fresh.sessions);
    },
  });

  const form = h("form.card.ingest-form", {
    onsubmit: async (e) => {
      e.preventDefault();
      const fd = new FormData();
      if (file.files[0]) fd.append("file", file.files[0]);
      fd.append("text", text.value);
      if (number.value) fd.append("number", number.value);
      fd.append("played_on", date.value);
      try {
        const r = await api("POST", `${base}/ingest`, fd);
        text.value = "";
        file.value = "";
        number.value = r.session + 1;
        jobs.watch({ id: r.job });
      } catch (err) {
        toast(err.message, true);
      }
    },
  },
    h("h2", "Add a session"),
    h("div.identity", h("label.field", h("span", "Session #"), number), h("label.field", h("span", "Played on"), date)),
    text,
    h("div.file-row", h("span.muted", "or upload a transcript or recording:"), file),
    h("div.actions", submit, h("span.muted.small",
      "Claude reads it, updates NPCs, quests and the journal, and writes a recap. Takes a few minutes, "
      + "and carries on in the background: you can close this page.")),
  );

  return h("div", canEdit(c) && form, jobs.el, h("h2.section-title", "Sessions"), list);
}

// -- jobs ------------------------------------------------------------------------

const JOB_STATES = { wanted: "Queued", running: "Running", done: "Done", failed: "Failed", cancelled: "Cancelled" };

// The jobs of one kind, as cards: the ones in `initial`, ones this page starts
// (watch), and ones started elsewhere that the live stream reports. `owner` is
// an element of the tab, so the stream closes when the tab goes away.
// opts: title(job), result(job) -> element, onFinish(job), anyone (whether
// people who can't edit may retry and cancel: their own jobs, say).
function jobList(c, kind, initial, owner, opts) {
  const el = h("div.jobs");
  const panels = new Map();
  const watch = (job) => {
    if (panels.has(job.id)) return;
    const panel = jobPanel(c, job.id, opts);
    panels.set(job.id, panel);
    el.append(panel.el);
  };
  initial.forEach(watch);
  live(c.slug, owner, {
    job: (job) => {
      if (job.kind !== kind) return;
      if (!panels.has(job.id) && (job.state === "wanted" || job.state === "running")) watch(job);  // started elsewhere
      panels.get(job.id)?.update(job);
    },
  });
  return { el, watch };
}

// One job's card: its state, Retry/Cancel, and its log. It loads the whole
// log once; after that, update() applies what the live stream sends (new
// lines, and the last line again while it's still growing).
function jobPanel(c, id, opts) {
  const base = `/api/campaigns/${c.slug}`;
  const lines = new Map();
  const out = h("div.job-log");
  const statusEl = h("span.job-status.running", "Queued");
  const actions = h("span.job-actions");
  const title = h("strong", "…");
  const result = h("div");
  const el = h("div.job", h("div.job-head", title, statusEl, actions), out, result);
  let finished = false;
  const act = (label, method, path, cls = "") => (canEdit(c) || opts.anyone) && h(`button${cls}`, {
    onclick: async () => {
      try {
        update(await api(method, `${base}/jobs/${id}${path}`) || { id, state: "cancelled", log: [] });
      } catch (err) {
        toast(err.message, true);
      }
    },
  }, label);
  const update = (job) => {
    if (job.name) title.textContent = opts.title(job);
    if (job.log?.length && job.log[0].seq === 0 && lines.size) {  // a retry starts the log over
      lines.clear();
      out.replaceChildren();
    }
    for (const entry of job.log || []) {
      const line = lines.get(entry.seq) || out.appendChild(h(`div.log-${entry.kind}`));
      lines.set(entry.seq, line);
      line.textContent = entry.text;
    }
    out.scrollTop = out.scrollHeight;
    const waiting = job.state === "wanted" && job.retry_at;
    statusEl.textContent = waiting ? `Retrying at ${new Date(job.retry_at * 1000).toLocaleTimeString()} (${job.error})`
      : job.state === "failed" ? `Failed: ${job.error}` : JOB_STATES[job.state];
    statusEl.className = `job-status ${job.state === "wanted" ? "running" : job.state}`;
    actions.replaceChildren(
      job.state === "failed" || job.state === "cancelled" ? act("Retry", "POST", "/retry") : "",
      job.state === "wanted" || job.state === "running" ? act("Cancel", "DELETE", "", ".danger") : "",
    );
    if (job.state === "done" && job.result && !finished) {
      finished = true;
      result.replaceChildren(opts.result(job));
      opts.onFinish?.(job);
    }
  };
  api("GET", `${base}/jobs/${id}`).then(update, (err) => (statusEl.textContent = err.message));
  return { el, update };
}

// -- reference material ----------------------------------------------------------

async function referenceTab(c) {
  const base = `/api/campaigns/${c.slug}`;
  const list = h("div.rows");
  const render = (sources) => list.replaceChildren(
    sources.length === 0 ? h("p.empty", "Nothing loaded yet. Upload the module PDF and any notes.") : "",
    ...sources.map((s) => sourceRow(c, s, refresh)),
  );
  const refresh = async () => render(await api("GET", `${base}/sources`));
  await refresh();
  live(c.slug, list, { sources: render });  // digest progress as it happens

  const file = h("input", { type: "file", accept: ".pdf,.md,.markdown,.txt", required: true, "aria-label": "Reference file" });
  const title = h("input", { placeholder: "Title (defaults to file name)", "aria-label": "Title" });
  const kind = h("select", { "aria-label": "Kind" }, h("option", { value: "module" }, "Published module"), h("option", { value: "notes" }, "My notes"));
  const dmOnly = h("input", { type: "checkbox", checked: true });
  const digestNow = h("input", { type: "checkbox" });
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
        toast(!r.pending ? `Loaded: ${r.chunks} chunks, digest already cached`
          : r.digesting ? `Loaded: digesting ${r.pending} sections (${estimateText(r.estimate)})`
          : `Loaded: ${r.pending} sections to digest, ${estimateText(r.estimate)}`);
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
    h("label.check", digestNow, " Digest with Claude now ",
      h("span.muted.small", "(builds the outline and the NPC/monster/item catalog. Leave it off to see a cost estimate first; only new or changed sections cost anything)")),
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
  return h("div", canEdit(c) && form, h("h2.section-title", "Loaded"), list,
    h("h2.section-title", "Search"), h("form.inline-form", { onsubmit: search }, q, h("button", { type: "submit" }, "Search")), results);
}

const usd = (x) => (x === null || x === undefined ? "cost unknown" : x < 0.01 ? "<$0.01" : `$${x.toFixed(2)}`);
const ktok = (n) => `${Math.round(n / 1000)}k tokens`;
const estimateText = (e) => (e && e.calls ? `about ${usd(e.usd)} with ${e.model}` : "nothing to pay for");

function digestLine(d) {
  const spent = d.input_tokens + d.output_tokens ? ` · ${usd(d.spent_usd)} so far` : "";
  if (d.running) return h("span.digest-status.running", `Digesting… ${d.done}/${d.total} sections${spent}`);
  if (d.done === d.total && d.total) {
    const tokens = d.input_tokens + d.output_tokens;
    return h("span.digest-status.done", `Digested · ${d.entities} entries${tokens ? ` · ${ktok(tokens)}, ${usd(d.spent_usd)}` : ""}`);
  }
  const left = d.estimate?.calls ? ` · ${d.estimate.calls} calls left, ${estimateText(d.estimate)}` : "";
  if (d.stuck) return h("span.digest-status.partial",
    `${d.done}/${d.total} sections digested; the rest failed or wait on a failure${spent}`);
  if (!d.done) return h("span.digest-status", `Not digested${left}`);
  return h("span.digest-status.partial",
    `${d.done}/${d.total} sections digested${d.failed ? `, ${d.failed} failed` : ""}${spent}${left}`);
}

function sourceRow(c, s, refresh) {
  const base = `/api/campaigns/${c.slug}`;
  const d = s.digest;
  // Digesting is a target, not a job: Digest/Retry set it (safely, any number of times), Stop clears it,
  // and the server's loop works toward it whether or not this page is open.
  const canDigest = canEdit(c) && s.kind !== "transcript" && !d.running && (d.total === 0 || d.done < d.total);
  const canStop = canEdit(c) && d.running;
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
          const e = d.estimate;
          if (e?.calls && !confirm(`Digest ${s.title} with ${e.model}?\n\n${e.calls} calls, roughly ${ktok(e.input_tokens)} in and ${ktok(e.output_tokens)} out: ${usd(e.usd)}.`
            + (e.calibrated_on ? `\n(Based on ${e.calibrated_on} sections already digested with this model.)` : "\n(A first guess: it gets better once a few sections have been digested with this model.)"))) return;
          try {
            await api("POST", `${base}/sources/${s.id}/digest`);
            refresh();
          } catch (err) {
            toast(err.message, true);
          }
        },
      }, d.stuck ? "Retry failed" : d.done ? "Resume digest" : "Digest"),
      canStop && h("button", {
        title: "Stop digesting: calls already in flight finish and are kept",
        onclick: async () => {
          await api("DELETE", `${base}/sources/${s.id}/digest`);
          refresh();
        },
      }, "Stop"),
      canEdit(c) && h("button.icon", {
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
  const campaigns = await api("GET", "/api/campaigns");
  const c = campaigns.find((x) => x.slug === slug);
  if (!c) throw new Error("No such source.");

  // Survive re-renders while a digest is running: which sections are open,
  // and the entities already fetched for each.
  const open = new Set();
  const sectionEntities = new Map();
  let entities = [];
  let wasRunning = null;
  const status = h("span");
  const outlineEl = h("div.outline");
  const catalogTitle = h("h2.section-title");
  const kind = h("select", { "aria-label": "Kind" });
  const q = h("input", { type: "search", placeholder: "Filter by name or alias…", "aria-label": "Filter" });
  const grid = h("div.entity-grid");

  const pages = (n) => (n.page_start ? (n.page_end && n.page_end !== n.page_start ? ` pp. ${n.page_start}–${n.page_end}` : ` p. ${n.page_start}`) : "");

  // A finished section's own entities, as extracted (or merged) at that level.
  const entityList = (n) => {
    const box = h("div.entity-grid.section-entities");
    const show = (list) => box.replaceChildren(...list.map(refCard));
    const btn = h("button.link", {
      onclick: async () => {
        if (open.has(`e${n.id}`)) {
          open.delete(`e${n.id}`);
          box.replaceChildren();
          btn.textContent = `Show ${n.entity_count} entries`;
          return;
        }
        if (!sectionEntities.has(n.id)) sectionEntities.set(n.id, (await api("GET", `${base}/sources/${sourceId}/outline/${n.id}`)).entities);
        open.add(`e${n.id}`);
        show(sectionEntities.get(n.id));
        btn.textContent = "Hide entries";
      },
    }, open.has(`e${n.id}`) ? "Hide entries" : `Show ${n.entity_count} entries`);
    if (open.has(`e${n.id}`) && sectionEntities.has(n.id)) show(sectionEntities.get(n.id));
    return [btn, box];
  };

  const renderOutline = (outline) => {
    const children = {};
    outline.forEach((n) => (children[n.parent_id] ||= []).push(n));
    // [digested, total] over a node and everything under it.
    const progress = (n) => (children[n.id] || []).map(progress)
      .reduce(([d, t], [cd, ct]) => [d + cd, t + ct], [n.summary !== null ? 1 : 0, 1]);
    const nodeEl = (n) => {
      const kids = children[n.id] || [];
      const [done, total] = progress(n);
      const head = h("summary",
        h("strong", n.title), h("span.muted.small", pages(n)),
        n.summary === null && h("span.badge", n.error ? "failed" : kids.length && done ? `${done}/${total} done` : "pending"),
        n.entity_count ? h("span.muted.small", ` · ${n.entity_count} entries`) : "");
      const body = [
        n.summary ? h("p.node-summary", n.summary) : n.error ? h("p.error-text", n.error) : "",
        n.entity_count ? entityList(n) : "",
        kids.map(nodeEl),
      ];
      const el = h("details.outline-node", { open: open.has(n.id) || (open.size === 0 && n.depth < 1) }, head, body);
      el.addEventListener("toggle", () => (el.open ? open.add(n.id) : open.delete(n.id)));
      if (el.open) open.add(n.id);
      return el;
    };
    outlineEl.replaceChildren(...(outline.length ? outline.filter((n) => n.parent_id === null).map(nodeEl) : [h("p.empty", "No outline yet.")]));
  };

  const renderCatalog = () => {
    catalogTitle.textContent = `Catalog (${entities.length})`;
    const keep = kind.value;
    const kinds = [...new Set(entities.map((e) => e.kind))].sort();
    kind.replaceChildren(h("option", { value: "" }, "All kinds"), ...kinds.map((k) => h("option", { value: k }, kindLabel(k))));
    kind.value = kinds.includes(keep) ? keep : "";
    const needle = q.value.toLowerCase();
    const shown = entities.filter((e) => (!kind.value || e.kind === kind.value) &&
      (!needle || [e.name, ...e.aliases, e.summary].some((x) => x.toLowerCase().includes(needle))));
    grid.replaceChildren(shown.length ? "" : h("p.empty", entities.length ? "No matches." : "No catalog yet: it's built when the whole book is digested. Open a finished section above to see what it found."),
      ...shown.map(refCard));
  };
  kind.addEventListener("change", renderCatalog);
  q.addEventListener("input", renderCatalog);

  const head = h("h1");
  const refresh = async () => {
    const [sources, outline] = await Promise.all([
      api("GET", `${base}/sources`), api("GET", `${base}/sources/${sourceId}/outline`),
    ]);
    const src = sources.find((x) => x.id === Number(sourceId));
    if (!src) throw new Error("No such source.");
    head.textContent = src.title;
    status.replaceChildren(src.kind !== "transcript" ? digestLine(src.digest) : "");
    renderOutline(outline);
    const running = src.digest.running;
    // The catalog only changes when a digest finishes, so fetch it on load and after a run.
    if (wasRunning === null || (wasRunning && !running)) {
      entities = await api("GET", `${base}/sources/${sourceId}/entities`);
      renderCatalog();
    }
    wasRunning = running;
  };
  await refresh();
  // Re-read the outline when this source's digest moves on.
  let progress = null;
  live(slug, outlineEl, {
    sources: (all) => {
      const d = all.find((x) => x.id === Number(sourceId))?.digest;
      const now = d && `${d.done}/${d.failed}/${d.running}`;
      if (progress !== null && now !== progress) refresh();
      progress = now;
    },
  });

  return h("section.page",
    h("div.page-head", h("a", { href: `#/campaigns/${slug}/reference` }, `← ${c.name}`)),
    h("div.page-head", head, status),
    h("h2.section-title", "Outline"),
    outlineEl,
    catalogTitle,
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
      sel.replaceChildren(h("option", { value: "" }, label), ...values.map((v) => h("option", { value: v }, v)));
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
    body.replaceChildren(...factSections(c, e, t, t === now).filter(Boolean));
  };
  slider.addEventListener("input", renderAt);

  const summary = h("div.card.summary",
    e.summary ? h("p", e.summary) : h("p.muted", "No summary for the current facts yet."),
    canEdit(c) && h("button", {
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
  if (e.summary && canEdit(c)) summary.lastChild.remove();

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
    h("div.page-head", h("h1", e.name), h("span.badge", e.kind),
      e.character && h("a", { href: `#/characters/${e.character.id}` }, "Character sheet →")),
    e.aliases.length > 0 && h("p.muted", "Also known as ", e.aliases.join(" · ")),
    summary,
    now > 0 && h("div.as-of", sliderLabel, slider),
    body,
    canEdit(c) && h("details.card", h("summary", "Same thing as another entry?"), h("p.muted.small", "Merging keeps both histories; the other name becomes an alias."), merge),
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

  const endButton = (f) => isNow && canEdit(c) && openAt(f) && h("button.icon", {
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

  const add = isNow && canEdit(c) && (() => {
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
      statements.length > 0 && h("ul.facts", statements.map((f) => factRow(f))),
      attrs.length + statements.length === 0 && h("p.empty", "Nothing recorded."),
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
