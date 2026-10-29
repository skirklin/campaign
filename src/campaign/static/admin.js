// The admin page: everyone who can sign in, and invites to the app itself.

import { api, h, toast, whoami } from "./util.js";
import { inviteForm, pendingInvites } from "./people.js";

export async function adminView() {
  const me = await whoami();
  if (me.local || !me.is_admin) throw new Error("Admins only.");
  const table = h("div.table-scroll");
  const render = async () => {
    const users = await api("GET", "/api/admin/users");
    table.replaceChildren(h("table.people",
      h("tr", h("th", "User"), h("th", "Campaigns"), h("th", "Last seen"), h("th", "Admin"), h("th")),
      users.map((u) => h("tr",
        h("td", u.email, u.id === me.id && h("span.muted", " (you)")),
        h("td", u.campaigns),
        h("td", u.last_seen_at ? u.last_seen_at.slice(0, 10) : "never"),
        h("td", h("input", {
          type: "checkbox", checked: !!u.is_admin, disabled: u.id === me.id, "aria-label": `${u.email} is admin`,
          onchange: async (e) => {
            try {
              await api("PATCH", `/api/admin/users/${u.id}`, { is_admin: e.target.checked });
            } catch (err) {
              toast(err.message, true);
            }
            render();
          },
        })),
        h("td", u.id !== me.id && h("button.icon", {
          title: "Remove user", "aria-label": `Remove ${u.email}`,
          onclick: async () => {
            if (!confirm(`Remove ${u.email}? They're signed out at once and lose access to every campaign.`)) return;
            await api("DELETE", `/api/admin/users/${u.id}`);
            render();
          },
        }, "×")),
      )),
    ));
  };
  await render();
  const invites = pendingInvites("/api/admin/invites", "/api/admin/invites");
  return h("section.page",
    h("h1", "Admin"),
    h("h2.section-title", "Users"),
    table,
    h("p.muted.small", "People usually join through a campaign's Members tab. An app invite lets someone in without a campaign, so they can start their own."),
    inviteForm("/api/admin/invites", { onCreated: () => invites.refresh() }),
    invites.el,
  );
}

// Your account: connecting the Claude app over MCP, and the apps you've connected.
export async function accountView() {
  const me = await whoami();
  if (me.local) throw new Error("Sign-in is off, so there's no account. Use `campaign mcp` for MCP.");
  const list = h("div");
  const render = async () => {
    const apps = await api("GET", "/api/me/apps");
    list.replaceChildren(apps.length === 0 ? h("p.empty", "No apps connected.") : h("table.people",
      h("tr", h("th", "App"), h("th", "Connected"), h("th")),
      apps.map((a) => h("tr",
        h("td", a.client_name || a.client_id),
        h("td", a.last_used.slice(0, 10)),
        h("td", h("button", {
          onclick: async () => {
            if (!confirm(`Disconnect ${a.client_name || "this app"}? It loses access at once.`)) return;
            await api("DELETE", `/api/me/apps/${encodeURIComponent(a.client_id)}`);
            render();
          },
        }, "Disconnect")),
      ))));
  };
  await render();
  const url = `${location.origin}/mcp`;
  return h("section.page",
    h("h1", "Account"),
    h("p.muted", "Signed in as ", h("strong", me.email)),
    h("h2.section-title", "Use your campaigns from the Claude app"),
    h("ol.steps",
      h("li", "In Claude (claude.ai, desktop or mobile), open Settings → Connectors and add a custom connector."),
      h("li", "Use this URL: ", h("code", url), " ",
        h("button.keep-enabled", { onclick: () => navigator.clipboard.writeText(url).then(() => toast("Copied")) }, "Copy")),
      h("li", "Claude sends you here to sign in and asks you to allow it. After that it can look things up in "
        + "your campaigns, and you can hand it a scan of a character sheet to enter."),
    ),
    h("p.muted.small", "It acts as you: in a campaign where you're a player it sees only what the party knows, "
      + "and it can change only what you could change here."),
    h("h2.section-title", "Connected apps"),
    list,
  );
}
