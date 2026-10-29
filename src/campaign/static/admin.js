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
