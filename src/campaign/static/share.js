// What another app shared to Campaign: a session transcript from Pixel Recorder
// (or any text) arrives through Android's share sheet. The service worker (sw.js)
// holds it on this device; this page says which campaign and session it is and
// starts the ingest, the same job "Add session notes" runs.

import { api, h, toast, canEdit } from "./util.js";
import { sessionChoice } from "./campaigns.js";

const SHARE_CACHE = "campaign-share";
const PENDING = "/share/pending";
const LAST_CAMPAIGN = "share-campaign";

// The share waiting on this device, or null.
export async function pendingShare() {
  if (!("caches" in window)) return null;
  try {
    const res = await (await caches.open(SHARE_CACHE)).match(PENDING);
    const shared = res && await res.json();
    return shared && shared.text.trim() ? shared : null;
  } catch {
    return null;
  }
}

async function forgetShare() {
  try { await (await caches.open(SHARE_CACHE)).delete(PENDING); } catch { /* nothing to forget */ }
}

const remembered = () => { try { return localStorage.getItem(LAST_CAMPAIGN); } catch { return null; } };
const remember = (slug) => { try { localStorage.setItem(LAST_CAMPAIGN, slug); } catch { /* per-device nicety */ } };

export async function shareView() {
  const shared = await pendingShare();
  if (!shared) {
    return h("section.page",
      h("h1", "Share a transcript"),
      h("p.empty", "Nothing has been shared to Campaign on this device."),
      h("p", "To send a session's transcript from Pixel Recorder: install Campaign on the phone "
        + "(in Chrome, ⋮ → Add to Home screen → Install), then in Recorder touch and hold the recording, "
        + "tap Share, choose Transcript (.txt), and pick Campaign."),
      h("p.muted.small", "Turn on Speaker labels in Recorder's settings first, and rename the speakers to the players' "
        + "names if you can: the assistant then knows who said what."));
  }

  const campaigns = (await api("GET", "/api/campaigns")).filter(canEdit);
  const discard = h("button", {
    onclick: async () => {
      if (!confirm("Throw away the shared transcript?")) return;
      await forgetShare();
      location.hash = "#/campaigns";
    },
  }, "Discard");
  if (!campaigns.length) {
    return h("section.page", h("h1", "Shared transcript"),
      h("p.empty", "You can't add session notes to any campaign. Make one on the Campaigns page, or ask its owner to make you an editor."),
      h("div.actions", discard));
  }

  const last = remembered();
  const campaign = h("select", { "aria-label": "Campaign" },
    campaigns.map((c) => h("option", { value: c.slug, selected: c.slug === last }, c.name)));
  const which = sessionChoice();
  const text = h("textarea", { rows: 14, "aria-label": "Transcript or notes", value: shared.text });
  const submit = h("button.primary", { type: "submit", disabled: true }, "Add to the campaign");

  // The campaign's sessions, to add to one or start the next, for whichever campaign is picked.
  const loadSessions = async () => {
    submit.disabled = true;
    try {
      const data = await api("GET", `/api/campaigns/${campaign.value}/sessions`);
      which.update(data.sessions, data.next);
      submit.disabled = false;
    } catch (err) {
      toast(err.message, true);
    }
  };
  campaign.addEventListener("change", loadSessions);
  loadSessions();

  const lines = shared.text.split("\n").filter((l) => l.trim()).length;
  const name = shared.name || `${shared.title || "Shared transcript"}.txt`;
  return h("section.page",
    h("h1", "Shared transcript"),
    h("p.muted", `${name} · ${lines.toLocaleString()} lines`),
    h("form.card.ingest-form", {
      onsubmit: async (e) => {
        e.preventDefault();
        if (!text.value.trim()) return toast("The transcript is empty.", true);
        const slug = campaign.value;
        const fd = new FormData();
        fd.append("file", new Blob([text.value], { type: "text/plain" }), name);
        which.append(fd);
        submit.disabled = true;
        try {
          const r = await api("POST", `/api/campaigns/${slug}/ingest`, fd);
          await forgetShare();
          remember(slug);
          toast(`Reading ${name} for session ${r.session}`);
          location.hash = `#/campaigns/${slug}/ingest`;
        } catch (err) {
          submit.disabled = false;
          toast(err.message, true);
        }
      },
    },
      h("div.identity", h("label.field", h("span", "Campaign"), campaign)),
      which.el,
      text,
      h("div.actions", submit, discard, h("span.muted.small",
        "A new session, or one already recorded, which this adds to. Trim anything that isn't the game if you like. "
        + "The assistant reads it in the background and brings back what it would change, for you to review "
        + "before anything is recorded."))),
  );
}
