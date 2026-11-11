// Campaign list, and per-campaign tabs for ingesting sessions and reference
// material and browsing what the assistant has recorded.

import { api, h, toast, enc, whoami, canEdit, live, streamPost, sessionNames, checkClaude } from "./util.js";
import { membersTab } from "./people.js";
import { characterCard, newCharacterForm } from "./sheet.js";
import { InventoryView } from "./inventory.js";
import { renderMarkdown, inline, statBlockFromData } from "./markdown.js";
import { encounterFence, encountersTab } from "./encounter.js";

export async function campaignList() {
  const [campaigns, info, me] = await Promise.all([
    api("GET", "/api/campaigns"), api("GET", "/api/info").catch(() => null), whoami()]);
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
    // Where the data lives is for whoever runs the server, not everyone who signs in.
    info && (me.local || me.is_admin) && h("p.muted.small.data-location",
      "Data is stored in ", h("code", info.data_dir),
      ` (schema v${info.schema_version}, ${info.originals.files} original files, ${(info.originals.bytes / 1e6).toFixed(1)} MB).`),
  );
}

const TABS = [
  ["ask", "Ask"],
  ["encounters", "Encounters"],
  ["ingest", "Sessions"],
  ["party", "Party"],
  ["reference", "Reference"],
  ["state", "World state"],
  ["members", "Members"],  // only with sign-in on
];

export async function campaignView(slug, tab, arg) {
  const [campaigns, me] = await Promise.all([api("GET", "/api/campaigns"), whoami()]);
  const c = campaigns.find((x) => x.slug === slug);
  if (!c) throw new Error(`No campaign "${slug}".`);
  const tabs = TABS.filter(([key]) => key !== "members" || !me.local);
  const body = h("div.tab-body", h("p.muted", "Loading…"));
  const render = { ask: askTab, encounters: encountersTab, ingest: sessionsTab, party: partyTab, reference: referenceTab, state: stateTab, journal: sessionsTab, members: membersTab }[tab] || sessionsTab;
  render(c, me, arg).then((el) => body.replaceChildren(el), (err) => body.replaceChildren(h("p.empty", err.message)));
  return h("section.page",
    h("div.page-head.campaign-head", h("h1", c.name), h(`span.badge.${c.role}`, c.role === "dm" ? "DM" : "Player"),
      !canEdit(c) && h("span.badge", "view only"), h("span.spacer"), quickFind(c)),
    h("nav.tabs", tabs.map(([key, label]) => h("a", { href: `#/campaigns/${slug}/${key}`,
      className: key === tab || (key === "ingest" && tab === "journal") ? "active" : "" }, label))),
    body,
  );
}

// -- ask: chat with the campaign assistant ----------------------------------------

// The tab opens ready for a new question, with recent conversations listed
// quietly under the suggestions. A conversation has its own address
// (#/campaigns/<slug>/ask/<id>), so reloading or going back keeps your place.
async function askTab(c, _me, convId) {
  const base = `/api/campaigns/${c.slug}`;
  const askHref = `#/campaigns/${c.slug}/ask`;
  let chats = await api("GET", `${base}/chats`);
  const home = campaignHome(c);
  let current = null;
  const thread = h("div.chat-thread");
  const head = h("div.chat-head");
  const input = h("textarea", { rows: 1, placeholder: c.role === "player"
    ? "Ask about the campaign or your character…"
    : "Ask, or tell it what happened…", "aria-label": "Message" });
  const send = h("button.primary.send", { type: "submit", title: "Send", "aria-label": "Send" }, "↑");
  const fences = { encounter: encounterFence(c.slug) };

  // What the assistant looked up on the way to an answer: one quiet line per run
  // of tool calls, opened to see each.
  const activity = () => {
    const list = h("ol");
    const label = h("span", "");
    const el = h("details.chat-activity", h("summary", label), list);
    el.add = (text) => {
      list.append(h("li", text));
      const n = list.children.length;
      label.textContent = n === 1 ? text : `${n} lookups · ${text}`;
    };
    return el;
  };
  const say = (role, text, usage) => (role === "user"
    ? h("div.chat-user", h("div.bubble", text))
    : h("div.chat-assistant", renderMarkdown(text, fences), usage && costLine(usage)));
  const turnsEls = (turns) => {
    const out = [];
    let act = null;
    for (const t of turns) {
      if (t.kind === "tool") {
        if (!act) out.push(act = activity());
        act.add(t.text);
      } else {
        act = null;
        out.push(say(t.role, t.text, t.usage));
      }
    }
    return out;
  };
  const scrollDown = () => thread.lastElementChild?.scrollIntoView({ block: "end", behavior: "smooth" });
  const chatLink = (x) => h("a.chat-past", { href: `${askHref}/${x.id}` },
    h("span.chat-past-title", x.title || "Untitled conversation"), h("span.chat-past-when", ago(x.updated_at)));
  // Earlier conversations, under the suggestions: the latest few, and the rest a click away.
  const RECENT = 4;
  const pastChats = () => {
    if (!chats.length) return "";
    const list = h("div.chat-past-list", chats.slice(0, RECENT).map(chatLink));
    const more = chats.length > RECENT && h("button.link", {
      type: "button",
      onclick: () => {
        list.replaceChildren(...chats.map(chatLink));
        more.remove();
      },
    }, `All ${chats.length} conversations`);
    return h("div.chat-history", h("h3", "Recent conversations"), list, more);
  };
  // Above an open conversation: its title, renaming and deleting it, and the way back to a new one.
  const fillHead = () => {
    const conv = chats.find((x) => x.id === current);
    if (!conv) return head.replaceChildren();
    head.replaceChildren(
      h("a.button", { href: askHref, title: "Ask something new" }, "+ New"),
      h("strong.chat-title", conv.title || "New conversation"),
      h("button", {
        title: "Rename this conversation", "aria-label": "Rename this conversation",
        onclick: async () => {
          const title = prompt("Name this conversation", conv.title || "")?.trim();
          if (!title || title === conv.title) return;
          try {
            Object.assign(conv, await api("PATCH", `${base}/chats/${current}`, { title }));
            fillHead();
          } catch (err) {
            toast(err.message, true);
          }
        },
      }, "✎"),
      h("button.danger", {
        onclick: async () => {
          if (!confirm("Delete this conversation?")) return;
          try {
            await api("DELETE", `${base}/chats/${current}`);
            location.replace(askHref);  // so going back doesn't land on what's gone
          } catch (err) {
            toast(err.message, true);
          }
        },
      }, "Delete"));
  };
  const welcome = () => {
    thread.replaceChildren(home, h("div.chat-welcome",
      h("p", "Ask anything about this campaign."),
      h("p.muted", c.role === "player"
        ? "It only knows what the party knows."
        : "It can look things up in the module and the world, roll on the book's tables, and record what happened at the table."),
      h("div.chat-suggestions", (c.role === "player"
        ? ["What do we know about our current quest?", "Remind me what happened last session", "Who is our patron?"]
        : ["What's coming up next for the party?", "Give me the stat block for the next fight", "Roll a random encounter"])
        .map((q) => h("button.suggestion", { type: "button", onclick: () => { input.value = q; form.requestSubmit(); } }, q))),
      pastChats()));
  };

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
          chats = [{ ...conv, title: shortTitle(text) }, ...chats];  // until the server's is ready
          // Its own address from now on; replaceState, so the page isn't drawn again.
          history.replaceState(null, "", `${askHref}/${current}`);
          fillHead();
          thread.replaceChildren();
        }
        input.value = "";
        fit();
        thread.append(say("user", text));
        const typing = thread.appendChild(h("div.chat-typing", { "aria-label": "Thinking" }, h("span"), h("span"), h("span")));
        scrollDown();
        let answer = null;  // {el, text} of the answer being streamed in
        let usage = null;
        let act = null;
        let frame = 0;
        const paint = () => {
          frame = 0;
          answer.el.replaceChildren(renderMarkdown(answer.text, fences));
        };
        await streamPost(`${base}/chats/${current}/messages`, { text }, (type, data) => {
          if (type === "text") {
            if (!data.trim() && !answer) return;  // the line break after a round of tool calls
            if (!answer) {
              answer = { el: h("div.chat-assistant"), text: "" };
              thread.insertBefore(answer.el, typing);
              act = null;
            }
            answer.text += data;
            frame ||= requestAnimationFrame(paint);  // re-render at most once a frame
          } else if (type === "tool") {
            if (!act) act = thread.insertBefore(activity(), typing);
            act.add(data);
            answer = null;  // text after a tool call starts a new paragraph
          } else if (type === "error") {
            thread.insertBefore(h("div.chat-error", data), typing);
            checkClaude();
          } else if (type === "done" && data.usage) {
            usage = data.usage;
          }
        });
        if (frame) { cancelAnimationFrame(frame); paint(); }
        if (usage && answer) answer.el.append(costLine(usage));
        typing.remove();
        chats = await api("GET", `${base}/chats`);
        fillHead();
      } catch (err) {
        toast(err.message, true);
      } finally {
        thread.querySelector(".chat-typing")?.remove();
        send.disabled = false;
        input.focus();
      }
    },
  }, h("div.composer", input, send), h("p.muted.small.hint", "Enter to send · Shift+Enter for a new line · tap dice to roll"));
  const fit = () => {  // grow with what's typed, up to the CSS max-height
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  };
  input.addEventListener("input", fit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  const conv = convId && await api("GET", `${base}/chats/${convId}`).catch(() => null);
  if (conv) {
    current = conv.id;
    fillHead();
    thread.replaceChildren(...turnsEls(conv.turns));
    requestAnimationFrame(() => thread.lastElementChild?.scrollIntoView({ block: "end" }));
  } else {
    if (convId) {  // deleted, or someone else's: start a new one instead
      history.replaceState(null, "", askHref);
      toast("That conversation isn't there any more.", true);
    }
    welcome();
    // Ready to type, except on a touch screen, where focusing would open the keyboard over the page.
    if (matchMedia("(pointer: fine)").matches) requestAnimationFrame(() => input.focus());
  }
  return h("div.chat", head, thread, form);
}

// Find anything in the campaign's world by name as you type: a person, a monster, a place, an item.
// "/" or Ctrl-K gets to it from anywhere on the page.
function quickFind(c) {
  const input = h("input.quick-find-input", { type: "search", placeholder: "Find a person, monster, place…",
    title: "Press / or Ctrl-K to search from anywhere on the page", "aria-label": "Find in this campaign", autocomplete: "off" });
  const results = h("ol.quick-find-results", { hidden: true });
  let timer = 0;
  let hits = [];
  const go = (e) => { location.hash = `#/campaigns/${c.slug}/entity/${e.id}`; input.value = ""; results.hidden = true; };
  const show = () => {
    results.replaceChildren(...hits.slice(0, 8).map((e, i) => h(`li${i === 0 ? ".first" : ""}`,
      h("a", { href: `#/campaigns/${c.slug}/entity/${e.id}`, onclick: () => { results.hidden = true; } },
        h("strong", e.name), h("span.badge", kindName(e.kind)), e.description && h("span.muted.small", e.description)))));
    results.hidden = !hits.length;
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) { hits = []; show(); return; }
    timer = setTimeout(async () => {
      try {
        const found = (await api("GET", `/api/campaigns/${c.slug}/entities?q=${encodeURIComponent(q)}`)).entities;
        // Names first: what starts with it, then a word that does, then anywhere in a name, then the rest.
        const needle = q.toLowerCase();
        const rank = (e) => {
          const names = [e.name, ...e.aliases].map((x) => x.toLowerCase());
          if (names[0].startsWith(needle)) return 0;
          if (names.some((n) => n.split(/[\s,()'-]+/).some((w) => w.startsWith(needle)))) return 1;
          if (names.some((n) => n.includes(needle))) return 2;
          return 3;
        };
        hits = found.map((e, i) => [rank(e), i, e]).sort((x, y) => x[0] - y[0] || x[1] - y[1]).map((x) => x[2]);
        if (input.value.trim() === q) show();
      } catch { /* a failed lookup just shows nothing */ }
    }, 150);
  });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && hits[0]) { ev.preventDefault(); go(hits[0]); }
    if (ev.key === "Escape") { input.value = ""; hits = []; show(); input.blur(); }
  });
  input.addEventListener("blur", () => setTimeout(() => { results.hidden = true; }, 200));
  input.addEventListener("focus", () => { if (hits.length) results.hidden = false; });
  if (!quickFind.bound) {  // once for the page: the shortcut finds whichever campaign's box is showing
    quickFind.bound = true;
    document.addEventListener("keydown", (ev) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
      if ((ev.key === "/" && !typing) || (ev.key === "k" && (ev.ctrlKey || ev.metaKey))) {
        const box = document.querySelector(".quick-find-input");
        if (box) { ev.preventDefault(); box.focus(); box.select(); }
      }
    });
  }
  return h("div.quick-find", input, results);
}

// Where things stand, above a new question: the last session, the quests the party has taken up (the
// table has touched them, and they aren't done; the book's other quests stay in the World tab), the
// party, and the fights ready to run. Filled in as it arrives, so the question box is ready at once.
const DONE = /^(completed?|done|failed|resolved|abandoned|finished)$/i;
function campaignHome(c) {
  const base = `/api/campaigns/${c.slug}`;
  const el = h("div.campaign-home");
  Promise.all([
    api("GET", `${base}/sessions`), api("GET", `${base}/entities?kind=quest&in_play=true`),
    api("GET", `${base}/characters`), api("GET", `${base}/encounters`).catch(() => []),
  ]).then(([sessions, quests, party, fights]) => {
    const last = sessions.sessions[sessions.sessions.length - 1];
    const open = quests.entities.filter((q) => !DONE.test(q.current.status || "")).slice(0, 6);
    const ready = fights.slice(0, 3);
    const link = (href, text) => h("a", { href }, text);
    const parts = [
      last && h("div.home-item.home-last", h("span.home-label", "Last time"),
        h("div", link(`#/campaigns/${c.slug}/ingest`, `#${last.number} ${last.title || "(untitled)"}`),
          last.summary && h("p.small", last.summary.split(/(?<=[.!?])\s/)[0]))),
      open.length > 0 && h("div.home-item", h("span.home-label", "Quests under way"),
        h("div", open.map((q, i) => [i ? " · " : "", link(`#/campaigns/${c.slug}/entity/${q.id}`, q.name)]))),
      party.length > 0 && h("div.home-item", h("span.home-label", "The party"),
        h("div", party.map((p, i) => [i ? " · " : "", link(`#/characters/${p.id}`, p.name),
          p.hp ? h("span.muted.small", ` ${p.hp.current}/${p.hp.max} HP`) : ""]))),
      ready.length > 0 && h("div.home-item", h("span.home-label", "Fights ready"),
        h("div", ready.map((f, i) => [i ? " · " : "", link(`#/campaigns/${c.slug}/encounters/${f.id}`, f.title),
          f.state?.started ? h("span.muted.small", ` round ${f.state.round}`) : ""]))),
    ].filter(Boolean);
    if (parts.length) el.replaceChildren(h("h3", "Where things stand"), ...parts);
  }, () => {});
  return el;
}

// The question as a title until the server writes a proper one (the same cut it makes).
function shortTitle(text, limit = 60) {
  const words = text.split(/\s+/).filter(Boolean).join(" ");
  if (words.length <= limit) return words;
  return `${words.slice(0, limit).replace(/\s+\S*$/, "").replace(/[,;:.-]+$/, "")}…`;
}

// "just now", "5 min ago", "3 h ago", "yesterday", "Oct 12".
function ago(iso) {
  const then = new Date(iso);
  const s = (Date.now() - then) / 1000;
  if (!Number.isFinite(s)) return "";
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 2 * 86400) return "yesterday";
  return then.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// What an answer cost, folded away under it: "14.2k tokens · 4 lookups · 21s · $0.06".
function costLine(u) {
  const k = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`);
  const total = u.input_tokens + u.cache_read_tokens + u.cache_write_tokens + u.output_tokens;
  const parts = [`${k(total)} tokens`];
  if (u.tool_calls) parts.push(`${u.tool_calls} lookup${u.tool_calls === 1 ? "" : "s"}`);
  if (u.seconds) parts.push(`${Math.round(u.seconds)}s`);
  if (u.cost_usd != null) parts.push(`$${u.cost_usd < 0.01 ? u.cost_usd.toFixed(3) : u.cost_usd.toFixed(2)}`);
  // Folded away: what an answer cost is there for whoever wants it, not a price tag on every question.
  return h("details.chat-cost", {
    title: `${u.model}: ${u.input_tokens} input, ${u.cache_read_tokens} cached, ${u.cache_write_tokens} written to cache, `
      + `${u.output_tokens} output, in ${u.api_calls} calls`,
  }, h("summary", { "aria-label": "What this answer cost" }, "ⓘ"), h("span", parts.join(" · ")));
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
  const inventory = new InventoryView(c.slug);
  return h("div",
    cards,
    h("div.inventory-wrap", inventory.el),
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
    h("span.muted.small", "A photo or PDF of a paper sheet. The assistant reads it into a new character you can then "
      + "correct; it takes a minute or two and carries on if you leave the page."));
}

// -- sessions & ingest ---------------------------------------------------------

// The campaign's timeline: each session, newest first, opening to its recap and what happened in it
// (the journal), and the sessions' notes waiting for the DM's review.
async function sessionsTab(c) {
  const base = `/api/campaigns/${c.slug}`;
  const [data, events] = await Promise.all([api("GET", `${base}/sessions`), api("GET", `${base}/journal`)]);
  const list = h("div.session-list");
  let journal = events;

  const happened = (n) => {
    const items = journal.filter((e) => e.session === n);
    return items.length > 0 && h("div.session-events", h("h3", "What happened"), h("ol", items.map((e) => h("li", e.text,
      e.entities.length > 0 && h("span.event-entities", " — ",
        e.entities.map((x, i) => [i ? ", " : "", h("a", { href: `#/campaigns/${c.slug}/entity/${x.id}` }, x.name)]))))));
  };
  const renderSessions = (sessions) => list.replaceChildren(
    sessions.length === 0 && !journal.length ? h("p.empty", "No sessions yet.") : "",
    ...[...sessions].reverse().map((s) => h("details.card.session",
      h("summary",
        h("span.session-num", `#${s.number}`),
        h("strong", s.title || "(untitled)"),
        s.kind === "recap" && h("span.muted", s.covers ? `a recap of about ${s.covers} sessions` : "a recap"),
        s.played_on && h("span.muted", s.played_on)),
      h("div.prose", s.summary ? s.summary.split(/\n\s*\n/).map((p) => h("p", p)) : h("p.muted", "No recap.")),
      happened(s.number),
      uploads(s),
    )),
  );
  // What a session was read from: each upload on its own, to take back without the rest.
  const uploads = (s) => s.uploads.length > 0 && h("div.session-uploads", h("h3", "Read from"), h("ul", s.uploads.map((u) => h("li",
    u.source_id ? h("a", { href: `#/campaigns/${c.slug}/source/${u.source_id}` }, u.name) : h("span", u.name),
    h("span.muted.small", ` · ${u.facts} facts, ${u.events} journal entries`),
    canEdit(c) && h("button.danger.small", {
      onclick: async () => {
        const rest = s.uploads.length > 1 ? " The session's other uploads stay." : "";
        if (!confirm(`Take back what “${u.name}” recorded about session ${s.number}?${rest}`)) return;
        const r = await api("POST", `${base}/uploads/${u.id}/revert`);
        toast(`Took back “${u.name}”: ${r.facts} facts and ${r.events} journal entries removed, ${r.reopened} earlier facts reopened`
          + (r.session_removed ? `; session ${s.number} had nothing else, so it's gone` : ""));
        refresh();
      },
    }, "Revert")))));
  // What the book set up before play, if anything was logged for it.
  const beforePlay = () => happened(0) && h("details.card.session", h("summary", h("span.session-num", "#0"), h("strong", "Before play")), happened(0));
  const renderAll = (sessions) => { renderSessions(sessions); const b = beforePlay(); if (b) list.append(b); };
  renderAll(data.sessions);

  // Session notes the assistant has read, waiting for the DM: what it would record, to accept, reword
  // or leave out. Nothing in the campaign changes until Apply.
  const reviews = h("div.reviews");
  const refresh = async () => {
    const [fresh, ev] = await Promise.all([api("GET", `${base}/sessions`), api("GET", `${base}/journal`)]);
    journal = ev;
    renderAll(fresh.sessions);
    renderReviews(fresh.reviews);
    which.update(fresh.sessions, fresh.next);
  };
  const EDITABLE = { record_fact: "statement", add_journal_entry: "text" };  // the words a DM may want to change
  const reviewCard = (job) => {
    const r = job.result;
    const title = h("input", { value: r.title || "", placeholder: "Title", "aria-label": "Session title" });
    const summary = h("textarea", { rows: 4, "aria-label": "Recap" }, r.summary || "");
    const rows = r.proposals.map((p, i) => {
      const keep = h("input", { type: "checkbox", checked: true, "aria-label": `Keep: ${p.summary}` });
      const field = EDITABLE[p.tool];
      const words = field && h("input.proposal-words", { value: p.args[field] || "", "aria-label": "Its wording" });
      // Whose fact it is, and the attribute it sets ("status → dead"), beside the words.
      const about = p.tool === "record_fact" ? h("span.proposal-about", p.args.entity,
        p.args.attribute ? h("span.muted", ` · ${attributeLabel(p.args.attribute)} → ${p.args.value}`) : "")
        : p.tool === "add_journal_entry" ? h("span.proposal-about", "Journal") : null;
      return { i, keep, field, words, el: h("li.proposal", h("label", keep, about || "", field ? words : h("span", p.summary))) };
    });
    const count = () => rows.filter((x) => x.keep.checked).length;
    const apply = h("button.primary", {
      onclick: async () => {
        const accepted = rows.filter((x) => x.keep.checked).map((x) => x.i);
        const edits = Object.fromEntries(rows.filter((x) => x.field && x.words.value !== r.proposals[x.i].args[x.field])
          .map((x) => [x.i, { [x.field]: x.words.value }]));
        try {
          const made = await api("POST", `${base}/ingests/${job.id}/apply`, { accepted, edits, title: title.value, summary: summary.value });
          toast(`Session ${job.session} recorded: ${made.made} changes` + (made.problems.length ? `; ${made.problems.length} couldn't be made` : ""),
            made.problems.length > 0);
          if (made.problems.length) alert(`These couldn't be made:\n\n${made.problems.join("\n")}`);
          refresh();
        } catch (err) {
          toast(err.message, true);
        }
      },
    }, `Apply ${count()} changes`);
    rows.forEach((x) => x.keep.addEventListener("change", () => { apply.textContent = `Apply ${count()} changes`; }));
    return h("section.card.review",
      h("h2", r.adding ? `More about session ${job.session}: what ${job.name} adds` : `Session ${job.session}: what ${job.name} says`),
      h("p.muted.small", `The assistant read it and would record these${r.adding ? ", on top of what the session already has" : ""}. `
        + "Untick what's wrong, reword what's off, then apply. Nothing has changed yet."),
      h("label.field", h("span", "Title"), title),
      h("label.field", h("span", "Recap"), summary),
      h("h3", `Changes (${rows.length})`),
      h("ol.proposals", rows.map((x) => x.el)),
      h("div.actions", apply, h("button", {
        onclick: async () => {
          if (!confirm(`Leave out everything from ${job.name}? The campaign stays as it is.`)) return;
          await api("POST", `${base}/ingests/${job.id}/discard`);
          refresh();
        },
      }, "Discard")));
  };
  const renderReviews = (list) => reviews.replaceChildren(...(canEdit(c) ? list.map(reviewCard) : []));
  renderReviews(data.reviews || []);

  const which = sessionChoice();
  which.update(data.sessions, data.next);
  const text = h("textarea", { rows: 8, placeholder: "Paste a transcript, your notes, a recap…", "aria-label": "Transcript or notes" });
  const file = h("input", { type: "file", accept: ".txt,.md,.mp3,.m4a,.wav,.ogg,.flac,.opus,.webm", "aria-label": "Transcript, notes or recording file" });
  const submit = h("button.primary", { type: "submit" }, "Add to the campaign");

  // Ingests are jobs the server works through in the background: they carry on
  // whether or not this page is open, and survive restarts.
  const jobs = jobList(c, "ingest", data.ingests, list, {
    title: (job) => `Session ${job.session}: ${job.name}`,
    result: (job) => h("div.job-result",
      h("h3", `Session ${job.result.session}: ${job.result.title || "(untitled)"}`),
      h("p.muted", job.result.review === "pending" ? `Read: ${job.result.proposals.length} changes for you to review, above.`
        : job.result.review === "discarded" ? "Left out." : `${job.result.facts} facts and ${job.result.journal_entries} journal entries recorded.`)),
    onFinish: async (job) => {
      toast(`${job.name} is read: review what it would change`);
      refresh();
    },
  });

  const form = h("form.card.ingest-form", {
    onsubmit: async (e) => {
      e.preventDefault();
      const fd = new FormData();
      if (file.files[0]) fd.append("file", file.files[0]);
      fd.append("text", text.value);
      which.append(fd);
      try {
        const r = await api("POST", `${base}/ingest`, fd);
        text.value = "";
        file.value = "";
        if (which.isNew()) which.update(which.sessions, r.session + 1);
        jobs.watch({ id: r.job });
      } catch (err) {
        toast(err.message, true);
      }
    },
  },
    h("h2", "Add to a session"),
    which.el,
    text,
    h("div.file-row", h("span.muted", "or upload a transcript, notes or a recording:"), file),
    h("div.actions", submit, h("span.muted.small",
      "Anything about what happened in a session: a transcript, your notes, a player's recap. The assistant reads it, "
      + "works out what it changes about NPCs, quests and the journal, and brings that back for you to review. "
      + "Takes a few minutes, and carries on in the background: you can close this page.")),
    h("p.muted.small", "Background, lore or plans for later aren't something that happened in a session: add those under ",
      h("a", { href: `#/campaigns/${c.slug}/reference` }, "Reference"), "."),
  );

  // What's been played first; adding to it folds away beneath, open when there's nothing yet.
  return h("div", reviews, h("h2.section-title", "Sessions"), list, jobs.el,
    canEdit(c) && h("details.add-panel", { open: !data.sessions.length }, h("summary", "+ Add to a session"), form));
}

// Which session an upload is about: a new one (with the date it was played), or one already
// recorded, which it adds to. update() fills it from GET /sessions; append() puts it in a form.
export function sessionChoice() {
  const pick = h("select", { "aria-label": "Session" });
  const date = h("input", { type: "date", value: new Date().toISOString().slice(0, 10), "aria-label": "Date played" });
  const dateField = h("label.field", h("span", "Played on"), date);
  const choice = {
    sessions: [],
    el: h("div.identity", h("label.field", h("span", "Session"), pick), dateField),
    isNew: () => pick.value === "new",
    update(sessions, next) {
      const was = pick.value;
      choice.sessions = sessions;
      choice.next = next;
      pick.replaceChildren(h("option", { value: "new" }, `New: session ${next}`),
        ...[...sessions].reverse().map((s) => h("option", { value: s.number },
          `Add to #${s.number}${s.title ? `: ${shortTitle(s.title, 40)}` : ""}`)));
      if ([...pick.options].some((o) => o.value === was)) pick.value = was;
      dateField.hidden = !choice.isNew();
    },
    append(fd) {
      if (choice.isNew()) {
        fd.append("number", choice.next);
        fd.append("played_on", date.value);
      } else {
        fd.append("number", pick.value);
      }
    },
  };
  pick.addEventListener("change", () => { dateField.hidden = !choice.isNew(); });
  return choice;
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

// Claude's own words in a job's log (its reasoning while it reads) are hidden
// unless asked for; the choice is remembered, and applies to every job.
const REASONING_KEY = "campaign.showReasoning";
let showReasoning = false;
try {
  showReasoning = localStorage.getItem(REASONING_KEY) === "1";
} catch {}

function setReasoning(on) {
  showReasoning = on;
  try {
    localStorage.setItem(REASONING_KEY, on ? "1" : "0");
  } catch {}
  for (const el of document.querySelectorAll(".job")) el.classList.toggle("show-reasoning", on);
  for (const b of document.querySelectorAll(".reasoning-toggle")) b.textContent = on ? "Hide reasoning" : "Show reasoning";
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
  const toggle = h("button.link.reasoning-toggle", { hidden: true, onclick: () => setReasoning(!showReasoning) },
    showReasoning ? "Hide reasoning" : "Show reasoning");
  const el = h("div.job", h("div.job-head", title, statusEl, toggle, actions), out, result);
  el.classList.toggle("show-reasoning", showReasoning);
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
      if (entry.kind === "text") toggle.hidden = false;
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
        toast(!r.pending ? "Loaded, and already read"
          : r.digesting ? `Loaded: reading it now (${estimateText(r.estimate)})`
          : `Loaded: not read yet, ${estimateText(r.estimate)}`);
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
    h("label.check", digestNow, " Have the assistant read it now ",
      h("span.muted.small", "(outlines it and catalogs its people, places, things and stat blocks. Leave it off to see what that costs first)")),
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
  return h("div", h("h2.section-title", "Books and notes"), list,
    canEdit(c) && h("details.add-panel", h("summary", "+ Add a book or notes"), form),
    h("h2.section-title", "Search"), h("form.inline-form", { onsubmit: search }, q, h("button", { type: "submit" }, "Search")), results);
}

const usd = (x) => (x === null || x === undefined ? "cost unknown" : x < 0.01 ? "<$0.01" : `$${x.toFixed(2)}`);
const ktok = (n) => `${Math.round(n / 1000)}k tokens`;
const estimateText = (e) => (e && e.calls ? `about ${usd(e.usd)} to read` : "nothing to pay for");
// The particulars (calls, tokens, model), for whoever wants them, on hover rather than on the page.
const estimateDetail = (e) => (e && e.calls ? `${e.calls} calls with ${e.model}, roughly ${ktok(e.input_tokens)} in and ${ktok(e.output_tokens)} out` : "");

// Where reading a book has got to. "Reading" is the assistant going through the book once, to
// outline it and catalog its people, places, things and stat blocks.
function digestLine(d) {
  const spentDetail = d.input_tokens + d.output_tokens ? `${ktok(d.input_tokens + d.output_tokens)}, ${usd(d.spent_usd)}` : "";
  if (d.running) return h("span.digest-status.running", { title: spentDetail }, `Reading… ${d.done} of ${d.total} parts`);
  if (d.done === d.total && d.total) {
    return h("span.digest-status.done", { title: spentDetail && `Reading it cost ${spentDetail}` }, `Read · ${d.entities.toLocaleString()} entries`);
  }
  const left = d.estimate?.calls ? ` · ${estimateText(d.estimate)}` : "";
  if (d.stuck) return h("span.digest-status.partial", { title: spentDetail },
    `Read ${d.done} of ${d.total} parts; the rest couldn't be read`);
  if (!d.done) return h("span.digest-status", { title: estimateDetail(d.estimate) }, `Not read yet${left}`);
  return h("span.digest-status.partial", { title: estimateDetail(d.estimate) },
    `Read ${d.done} of ${d.total} parts${d.failed ? `, ${d.failed} couldn't be` : ""}${left}`);
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
      h("span.badge", s.kind), " ",
      s.kind === "transcript" || !canEdit(c) || c.role !== "dm"
        ? (s.dm_only ? h("span.badge.dm", "DM only") : "")
        : h("select.visibility", {
          value: s.dm_only ? "dm" : "all", "aria-label": `Who can see ${s.title}`,
          title: "DM only: players and their assistant never see it. Everyone: players see it, and what the world took from it.",
          onchange: async (e) => {
            const dmOnly = e.target.value === "dm";
            if (!dmOnly && !confirm(`Show ${s.title} to players? They'll see its text and everything the world took from it.`)) {
              e.target.value = "dm";
              return;
            }
            try {
              await api("PATCH", `${base}/sources/${s.id}`, { dm_only: dmOnly });
              toast(dmOnly ? `${s.title} is DM only` : `Players can see ${s.title}`);
              refresh();
            } catch (err) {
              toast(err.message, true);
              e.target.value = s.dm_only ? "dm" : "all";
            }
          },
        }, h("option", { value: "dm" }, "DM only"), h("option", { value: "all" }, "Everyone")),
      h("div.muted.small",
        s.first_page ? `pages ${s.first_page}–${s.last_page} · ` : "",
        s.has_original ? [h("a", { href: `${base}/sources/${s.id}/original` }, "download"), " · "] : "",
        s.kind !== "transcript" ? digestLine(d) : ""),
    ),
    h("div.source-actions",
      canDigest && h("button", {
        onclick: async () => {
          const e = d.estimate;
          if (e?.calls && !confirm(`Have the assistant read ${s.title}? About ${usd(e.usd)}.\n\n`
            + `It outlines the book and catalogs its people, places, things and stat blocks, so answers can draw on them. `
            + `${estimateDetail(e)}.`
            + (e.calibrated_on ? ` (Based on ${e.calibrated_on} parts already read with this model.)` : " (A first guess: it gets better once a few parts have been read.)"))) return;
          try {
            await api("POST", `${base}/sources/${s.id}/digest`);
            refresh();
          } catch (err) {
            toast(err.message, true);
          }
        },
      }, d.stuck ? "Retry the failed parts" : d.done ? "Finish reading" : "Read the book"),
      canStop && h("button", {
        title: "Stop reading: what's already being read finishes and is kept",
        onclick: async () => {
          await api("DELETE", `${base}/sources/${s.id}/digest`);
          refresh();
        },
      }, "Stop"),
      canEdit(c) && h("button.icon", {
        title: "Remove", "aria-label": `Remove ${s.title}`,
        onclick: async () => {
          if (!confirm(`Remove ${s.title}? What was read is kept, so uploading it again later costs nothing.`)) return;
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
    e.facts.length > 0 && h("ul.facts", e.facts.map((f) => h("li", inline(f)))),
    sb && h("details.statblock",
      h("summary", `Stat block · AC ${sb.armor_class} · HP ${sb.hit_points}${sb.challenge ? ` · CR ${sb.challenge}` : ""}`),
      statBlockFromData(e.name, sb)),
  );
}

// FTS snippets mark matches with [ ]; render them as <mark> without innerHTML.
function highlight(snippet) {
  return snippet.split(/(\[[^\]]*\])/).map((part) =>
    part.startsWith("[") && part.endsWith("]") ? h("mark", part.slice(1, -1)) : part);
}

// -- world state ---------------------------------------------------------------------
// The world record is written for the assistant: short notes, many from the book, each with its
// sessions and source. These pages read it back for people: what a thing is, where it stands now,
// what has happened to it session by session, and what the book says, with where each note came
// from kept to a tooltip.

const KIND_LABELS = { npc: "NPCs", pc: "Player characters", lore: "Lore" };
const kindLabel = (k) => KIND_LABELS[k] || `${k.charAt(0).toUpperCase()}${k.slice(1)}s`;
const KIND_NAMES = { npc: "NPC", pc: "Player character", lore: "Lore" };
export const kindName = (k) => KIND_NAMES[k] || `${k.charAt(0).toUpperCase()}${k.slice(1)}`;
const capitalize = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const clip = (s, most) => (s.length <= most ? s : `${s.slice(0, most - 1).trimEnd()}…`);
// `name` names a session (sessionNames): recaps by their title.
// Nothing for what's been true from the start and still is (the book's facts): the source says enough.
const range = (f, name) => (f.valid_to === null ? (f.valid_from === 0 ? "" : `since ${name(f.valid_from)}`)
  : f.valid_to === f.valid_from ? `during ${name(f.valid_from)}` : `${name(f.valid_from)} to ${name(f.valid_to)}`);
const ORIGINS = { ingest: (f, name) => name(f.run_session), chat: () => "chat", manual: () => "edited by hand" };
const sourceLabel = (f, name) => (f.origin === "book" || !ORIGINS[f.origin] ? f.source || f.origin : ORIGINS[f.origin](f, name))
  + (f.pages?.length ? ` p. ${f.pages.join(", ")}` : "");
// Where a note came from and when it held, for its tooltip.
const provenance = (f, name) => [range(f, name), sourceLabel(f, name),
  f.party_knows_from === null ? "the party doesn't know this" : ""].filter(Boolean).join(" · ");
// Values that are for running the game, not for reading: the stat block a creature uses is shown as one.
const NOT_READ = ["uses_stat_block", "stat_block", "read_aloud", "mechanics"];
const statusBadge = (v) => v && h(`span.badge.status-${v.replace(/\W+/g, "-")}`, v);

// Page numbers as a reader cites them: [5, 6, 7, 10, 11, 16] is "pp. 5–7, 10–11, 16".
export function pageRange(pages) {
  const sorted = [...new Set(pages)].sort((a, b) => a - b);
  const runs = [];
  for (const p of sorted) {
    const last = runs[runs.length - 1];
    if (last && p === last[1] + 1) last[1] = p; else runs.push([p, p]);
  }
  const shown = runs.slice(0, 6).map(([a, b]) => (a === b ? `${a}` : `${a}–${b}`)).join(", ");
  return sorted.length ? `${sorted.length > 1 ? "pp." : "p."} ${shown}${runs.length > 6 ? ", …" : ""}` : "";
}

// The book's notes without the ones another says in full: "Ruled by King Acastus" goes when
// "Ruled by King Acastus, one of the last descendants of the Dragonlords" is there too.
export function withoutRepeats(notes, text = (n) => n.statement) {
  const key = (s) => ` ${s.toLowerCase().replace(/[^\w\s]/g, " ").split(/\s+/).filter(Boolean)
    .join(" ").replace(/^(the|a|an) /, "")} `;
  const keys = notes.map((n) => key(text(n)));
  return notes.filter((_, i) => !keys.some((k, j) => j !== i && k.includes(keys[i])
    && (k.length > keys[i].length || j < i)));
}

async function stateTab(c) {
  const base = `/api/campaigns/${c.slug}`;
  const everything = h("input", { type: "checkbox" });
  const kind = h("select", { "aria-label": "Kind" });
  const status = h("select", { "aria-label": "Status" });
  const q = h("input", { type: "search", placeholder: "Find by name or anything said about it…", "aria-label": "Filter" });
  const intro = h("p.muted.small");
  const out = h("div");
  const [{ entities: all }, name] = await Promise.all([api("GET", `${base}/entities`), sessionNames(c.slug)]);
  const fill = (sel, values, label, show = (v) => v) => sel.replaceChildren(
    h("option", { value: "" }, label), ...values.map((v) => h("option", { value: v }, show(v))));
  fill(kind, [...new Set(all.map((e) => e.kind))].sort((a, b) => kindRank(a) - kindRank(b)), "All kinds", kindLabel);
  fill(status, [...new Set(all.map((e) => e.current.status).filter(Boolean))].sort(), "Any status");

  const render = () => {
    const needle = q.value.trim().toLowerCase();
    // What the table has met is the world as people know it; asking for anything else (a kind, a
    // search, the box) opens up everything the reference material describes.
    const browsing = everything.checked || !!needle || !!kind.value || !!status.value;
    const shown = all.filter((e) => (browsing || e.table_activity > 0 || e.kind === "pc")
      && (!kind.value || e.kind === kind.value) && (!status.value || e.current.status === status.value)
      && (!needle || [e.name, ...e.aliases, e.description || "", e.latest?.text || ""]
        .some((x) => x.toLowerCase().includes(needle))));
    intro.textContent = browsing
      ? `${shown.length} of ${all.length} people, places and things.`
      : "Who and what the party has met, and what happened to them. Search, or pick a kind, to look through everything the reference material describes.";
    const groups = {};
    shown.forEach((e) => (groups[e.kind] ||= []).push(e));
    out.replaceChildren(
      all.length === 0 ? h("p.empty", "The world is empty. Have the assistant read a book, or add session notes.")
        : shown.length === 0 ? h("p.empty", browsing ? "Nothing matches."
          : "Nothing has happened in play yet. Tell the assistant what happened, or add session notes.") : "",
      ...Object.entries(groups).sort(([a], [b]) => kindRank(a) - kindRank(b)).map(([k, items]) => h("div",
        h("h2.section-title", `${kindLabel(k)} (${items.length})`),
        h("div.entity-grid", items.map((e) => worldCard(c, e, name, browsing))))),
    );
  };
  everything.addEventListener("change", render);
  [kind, status].forEach((el) => el.addEventListener("change", render));
  q.addEventListener("input", render);
  render();
  return h("div",
    h("div.filters", kind, status, q),
    h("label.check", everything, " Everything from the reference material"),
    intro,
    out);
}

// The kinds of thing in the world, in the order a DM reaches for them.
const KIND_ORDER = ["pc", "npc", "monster", "location", "quest", "item", "faction", "lore"];
const kindRank = (k) => (KIND_ORDER.includes(k) ? KIND_ORDER.indexOf(k) : KIND_ORDER.length);

// An attribute's name as a person would say it: "uses_stat_block" -> "Stat block".
const ATTRIBUTE_LABELS = { uses_stat_block: "Stat block" };
export const attributeLabel = (k) => ATTRIBUTE_LABELS[k] || (k.charAt(0).toUpperCase() + k.slice(1)).replace(/_/g, " ");

// One thing in the world: what it is, where it stands, and the last thing that happened to it.
function worldCard(c, e, name, browsing) {
  const attrs = Object.entries(e.current).filter(([k]) => k !== "status" && !NOT_READ.includes(k));
  const latest = e.latest && e.latest.text !== e.description && e.latest;
  return h("a.card.entity", { href: `#/campaigns/${c.slug}/entity/${e.id}` },
    h("div.entity-head", h("strong", e.name), statusBadge(e.current.status),
      browsing && e.table_activity > 0 && h("span.badge.in-play", { title: "Something has happened to it in play" }, "in play")),
    e.description && h("p", clip(e.description, 220)),
    attrs.length > 0 && h("dl", attrs.flatMap(([k, v]) => [h("dt", attributeLabel(k)), h("dd", v)])),
    latest && h("p.latest", h("span.latest-when", `${capitalize(name(latest.session))}: `), clip(latest.text, 200)),
  );
}

export async function entityView(slug, id) {
  const base = `/api/campaigns/${slug}`;
  const [campaigns, e, list, name] = await Promise.all([
    api("GET", "/api/campaigns"), api("GET", `${base}/entities/${id}`), api("GET", `${base}/entities`), sessionNames(slug),
  ]);
  const c = campaigns.find((x) => x.slug === slug);
  e.sessionName = name;
  const now = e.as_of;
  const body = h("div.entity-body");
  let editing = false;

  const slider = h("input", { type: "range", min: 0, max: Math.max(now, 0), value: now, "aria-label": "As of session" });
  const sliderLabel = h("span.as-of-label");
  const others = list.entities.filter((x) => x.id !== e.id);
  const mergeInput = h("input", { list: "merge-options", placeholder: "Another entry's name", "aria-label": "Entry to merge" });
  const merge = h("details.card", h("summary", "Merge with another entry…"),
    h("p.muted.small", "For two entries that are the same thing: both histories are kept, and the other name becomes another name for this one."),
    h("form.inline-form", {
      onsubmit: async (ev) => {
        ev.preventDefault();
        const other = others.find((x) => x.name === mergeInput.value);
        if (!other) return toast("Pick an entry from the list", true);
        if (!confirm(`Merge ${other.name} into ${e.name}? Its notes, names and history move here.`)) return;
        await api("POST", `${base}/entities/${id}/merge`, { other_id: other.id });
        location.reload();
      },
    }, mergeInput, h("datalist", { id: "merge-options" }, others.map((x) => h("option", { value: x.name }, kindName(x.kind)))),
      h("button", { type: "submit" }, "Merge into this")));
  const editButton = canEdit(c) && h("button.link.small", {
    onclick: () => { editing = !editing; editButton.textContent = editing ? "Done" : "Edit"; renderAt(); },
  }, "Edit");
  const renderAt = () => {
    const t = Number(slider.value);
    sliderLabel.textContent = t === now ? `now (after ${name(now)})` : `after ${name(t)}`;
    body.replaceChildren(...factSections(c, e, t, t === now, editing).filter(Boolean), editing ? merge : "");
  };
  slider.addEventListener("input", renderAt);

  // The summary written from everything known about it, when there is one; otherwise a quiet
  // button to write one, rather than an empty box at the top of the page.
  const summary = e.summary ? h("div.card.summary", h("p", e.summary)) : canEdit(c) && h("button.link.small.summarize", {
    title: "Write a short summary from everything known about it, past and present",
    onclick: async (ev) => {
      ev.target.disabled = true;
      ev.target.textContent = "Writing a summary…";
      try {
        const r = await api("POST", `${base}/entities/${id}/summary`);
        ev.target.replaceWith(h("div.card.summary", h("p", r.summary)));
      } catch (err) {
        toast(err.message, true);
        ev.target.disabled = false;
        ev.target.textContent = "Summarize";
      }
    },
  }, "Summarize");

  const secret = new Set(e.secret_aliases || []);
  renderAt();
  return h("section.page",
    h("div.page-head", h("a", { href: `#/campaigns/${slug}/state` }, `← ${c.name}`)),
    h("div.page-head", h("h1", e.name), h("span.badge", kindName(e.kind)), statusBadge(e.current.status),
      e.character && h("a", { href: `#/characters/${e.character.id}` }, "Character sheet →"),
      h("span.spacer"), !e.summary && summary, editButton),
    e.aliases.length > 0 && h("p.muted", "Also known as ", e.aliases.map((a) =>
      (secret.has(a) ? `${a} (party doesn't know)` : a)).join(" · ")),
    e.summary && summary,
    now > 0 && h("label.as-of", h("span.as-of-title", "Show it as it was "), sliderLabel, slider),
    body,
  );
}

function factSections(c, e, t, isNow, editing) {
  const name = e.sessionName;
  const base = `/api/campaigns/${c.slug}`;
  const facts = e.all_facts.filter((f) => f.valid_from <= t);
  const openAt = (f) => f.valid_to === null || f.valid_to > t;
  const fromBook = (f) => f.origin === "book";
  const TREASURE = "Treasure: ";
  const current = facts.filter((f) => openAt(f) && f.modality === "fact");
  const values = current.filter((f) => f.attribute && !NOT_READ.includes(f.attribute));
  const bookNotes = current.filter((f) => fromBook(f) && !f.attribute && !f.statement.startsWith(TREASURE));
  const treasure = current.filter((f) => !f.attribute && f.statement.startsWith(TREASURE));
  const statBlocks = current.filter((f) => f.attribute === "stat_block");
  const readAloud = current.filter((f) => f.attribute === "read_aloud");
  const maybe = facts.filter((f) => openAt(f) && f.modality !== "fact");
  const changed = facts.filter((f) => fromBook(f) && !openAt(f));
  const events = e.all_events.filter((ev) => ev.session <= t);

  const endButton = (f) => editing && isNow && openAt(f) && h("button.link.small.end-fact", {
    title: "It stops being true now; it stays in the history",
    onclick: async () => {
      await api("POST", `${base}/facts/${f.id}/end`);
      location.reload();
    },
  }, "no longer true");
  const secretMark = (f) => f.party_knows_from === null && h("span.badge.secret", { title: "The party doesn't know this" }, "secret");
  const note = (f, text, extra = "") => h(`li.note${extra}`, { title: provenance(f, name) },
    h("span", text || inline(f.statement)), secretMark(f), endButton(f));

  // What happened in play, session by session: the journal, and what was recorded then. A note
  // that says what an entry already says is left out; one that has stopped being true is struck.
  const said = new Set(events.map((ev) => ev.text.trim().toLowerCase()));
  const played = facts.filter((f) => !fromBook(f) && f.modality === "fact" && !NOT_READ.includes(f.attribute)
    && !f.statement.startsWith(TREASURE) && !said.has(f.statement.trim().toLowerCase()));
  const sessions = [...new Set([...events.map((ev) => ev.session), ...played.map((f) => f.valid_from)])].sort((a, b) => a - b);
  const history = sessions.length > 0 && h("section.card.history",
    h("h2", "What's happened"),
    sessions.map((n) => h("div.history-session",
      h("h3", name.heading(n)),
      h("ul.notes",
        events.filter((ev) => ev.session === n).map((ev) => h("li.note", h("span", inline(ev.text)))),
        played.filter((f) => f.valid_from === n).map((f) => note(f, null, openAt(f) ? "" : ".ended"))))));

  // What the book says, by book: its source named once, near-repeats folded together.
  const books = new Map();
  bookNotes.forEach((f) => (books.get(f.source) || books.set(f.source, []).get(f.source)).push(f));
  const fromBooks = [...books].map(([source, notes]) => h("section.card.book-notes",
    h("h2", `From ${source || "the reference material"}`, h("span.muted.small.pages", ` ${pageRange(notes.flatMap((f) => f.pages || []))}`)),
    h("ul.notes", withoutRepeats(notes).map((f) => note(f)))));

  const add = editing && isNow && (() => {
    const statement = h("input", { placeholder: `Something that's now true about ${e.name}`, required: true, "aria-label": "Note" });
    const attribute = h("input", { list: "attr-options", placeholder: "status, location…", "aria-label": "What it sets" });
    const value = h("input", { placeholder: "e.g. dead, Vallaki", "aria-label": "Value" });
    // A DM's notes are the DM's until they say otherwise; in a player's campaign the party knows it all.
    const known = h("input", { type: "checkbox", checked: c.role !== "dm" });
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
      h("h2", "Add a note"),
      statement,
      h("details", h("summary.muted.small", "It changes a status, location or the like…"),
        h("p.muted.small", "The old value moves to the history."),
        h("div.identity", attribute, value)),
      h("datalist", { id: "attr-options" }, ["status", "location", "holder", "disposition", "leader", "allegiance", "condition"].map((a) => h("option", { value: a }))),
      h("label.check", known, " The party knows this"),
      h("p.muted.small", "Or tell the assistant in the Ask tab what happened, and it records it."),
      h("div.actions", h("button.primary", { type: "submit" }, "Add")),
    );
  })();

  const empty = !values.length && !history && !fromBooks.length && !treasure.length && !statBlocks.length
    && !readAloud.length && !maybe.length && !changed.length && !e.borrowed_stat_block;
  return [
    values.length > 0 && h("section.card.now",
      h("h2", isNow ? "Now" : `At the end of ${name(t)}`),
      h("dl.now-values", values.flatMap((f) => [h("dt", attributeLabel(f.attribute)),
        h("dd", { title: provenance(f, name) }, f.attribute === "status" ? statusBadge(f.value) : f.value, secretMark(f), endButton(f))]))),
    history,
    maybe.length > 0 && h("section.card", h("h2", "Rumors and possibilities"),
      h("ul.notes", maybe.map((f) => note(f, `${f.modality === "rumor" ? "Rumor: " : "Possible: "}${f.statement}`)))),
    ...readAloud.map((f) => h("figure.read-aloud",
      h("figcaption", "Read aloud"), h("blockquote", f.value), h("span.fact-meta", sourceLabel(f, name)))),
    ...fromBooks,
    treasure.length > 0 && h("section.card.treasure",
      h("h2", "Treasure"),
      h("ul.notes", treasure.map((f) => note(f, inline(f.statement.slice(TREASURE.length))))),
      isNow && h("p.muted.small", "When the party takes something, mark it no longer true (Edit) and add it to the party's inventory (Party tab).")),
    changed.length > 0 && h("section.card", h("h2", "No longer as the book has it"),
      h("ul.notes", changed.map((f) => note(f, null, ".ended")))),
    ...statBlocks.map((f) => h("div.statblock-wrap",
      statBlockFromData(e.name, JSON.parse(f.value)), h("p.fact-meta", sourceLabel(f, name)))),
    // No block of its own, but one it runs on ("Heleka, a sea hag"): that one, saying whose it is.
    !statBlocks.length && e.borrowed_stat_block && h("div.statblock-wrap",
      h("p.small.muted.borrowed", "Runs on the ", h("a", { href: `#/campaigns/${c.slug}/entity/${e.borrowed_stat_block.from.id}` },
        e.borrowed_stat_block.from.name), " stat block"),
      statBlockFromData(`${e.name} (${e.borrowed_stat_block.from.name})`, e.borrowed_stat_block.stat_block)),
    empty && h("p.empty", isNow ? "Nothing recorded yet." : `Nothing recorded by the end of ${name(t)}.`),
    add,
  ];
}

