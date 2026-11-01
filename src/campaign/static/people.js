// Campaign members and invite links (only with sign-in on).

import { api, h, toast } from "./util.js";

const ROLE_LABEL = { dm: "DM", player: "Player" };
const ACCESS_HELP = {
  owner: "can change everything and manage members",
  editor: "can add material, ingest sessions and edit the world",
  viewer: "can look but not change anything",
};

// A form that creates an invite and shows its link once. `campaign` adds role and access.
export function inviteForm(url, { campaign = false, onCreated } = {}) {
  const role = h("select", { "aria-label": "Role" },
    h("option", { value: "player" }, "Player (spoiler-free view)"), h("option", { value: "dm" }, "DM (sees everything)"));
  const access = h("select", { "aria-label": "Access" },
    h("option", { value: "viewer" }, "Viewer: look only"), h("option", { value: "editor" }, "Editor: can change things"));
  const email = h("input", { type: "email", placeholder: "Only for this Google account (optional)", "aria-label": "Email" });
  const days = h("input", { type: "number", min: 1, max: 90, value: 7, "aria-label": "Days valid" });
  const uses = h("input", { type: "number", min: 1, max: 100, value: 1, "aria-label": "Number of uses" });
  const result = h("div");
  const form = h("form.card", {
    onsubmit: async (e) => {
      e.preventDefault();
      try {
        const r = await api("POST", url, {
          role: role.value, access: access.value, email: email.value.trim() || null,
          days: Number(days.value), max_uses: Number(uses.value),
        });
        form.reset();  // before showing the link, which sits inside the form
        days.value = 7;
        uses.value = 1;
        const link = h("input", { value: r.url, readonly: true, "aria-label": "Invite link", onfocus: (ev) => ev.target.select() });
        result.replaceChildren(
          h("p.small", "Send this link. It's shown only once."),
          h("div.invite-link", link, h("button", {
            type: "button",
            onclick: async () => {
              await navigator.clipboard.writeText(r.url).then(() => toast("Copied"), () => link.select());
            },
          }, "Copy")),
        );
        onCreated?.();
      } catch (err) {
        toast(err.message, true);
      }
    },
  },
    h("h2", campaign ? "Invite someone" : "Invite someone to the app"),
    campaign && h("div.identity", h("label.field", h("span", "Role"), role), h("label.field", h("span", "Access"), access)),
    h("div.identity",
      h("label.field", h("span", "Email (optional)"), email),
      h("label.field", h("span", "Valid for (days)"), days),
      h("label.field", h("span", "Uses"), uses)),
    h("div.actions", h("button.primary", { type: "submit" }, "Create link")),
    result,
  );
  return form;
}

export function pendingInvites(listUrl, revokeUrl) {
  const box = h("div");
  const refresh = async () => {
    const invites = await api("GET", listUrl);
    box.replaceChildren(
      h("h2.section-title", "Open invites"),
      invites.length === 0 ? h("p.empty", "None.") : h("div.table-scroll", h("table.people",
        h("tr", h("th", "For"), h("th", "Grants"), h("th", "Used"), h("th", "Expires"), h("th")),
        invites.map((i) => h("tr",
          h("td", i.email || "anyone with the link"),
          h("td", i.role ? `${ROLE_LABEL[i.role]}, ${i.access}` : "app access"),
          h("td", `${i.uses}/${i.max_uses}`),
          h("td", i.expires_at.slice(0, 10)),
          h("td", h("button.icon", {
            title: "Revoke", "aria-label": "Revoke invite",
            onclick: async () => {
              await api("DELETE", `${revokeUrl}/${i.id}`);
              refresh();
            },
          }, "×")),
        )),
      )),
    );
  };
  refresh();
  return { el: box, refresh };
}

export async function membersTab(c) {
  const base = `/api/campaigns/${c.slug}`;
  const box = h("div");
  const render = async () => {
    const { members, me, can_manage } = await api("GET", `${base}/members`);
    const change = async (m, patch) => {
      try {
        await api("PATCH", `${base}/members/${m.user_id}`, patch);
        if (m.user_id === me && patch.role) {
          location.reload();  // what you can see in every tab follows your role
          return;
        }
        toast(`Updated ${m.email}`);
      } catch (err) {
        toast(err.message, true);
      }
      render();
    };
    // Owners change anyone's role and access, their own included (the server keeps at least one owner).
    const select = (m, field, options) => can_manage
      ? h("select", {
        value: m[field], "aria-label": `${field} for ${m.email}`,
        onchange: (e) => {
          const lowering = m.user_id === me && field === "access" && e.target.value !== "owner";
          if (lowering && !confirm("Give up owning this campaign? You won't be able to manage its members.")) {
            e.target.value = m[field];
            return;
          }
          change(m, { [field]: e.target.value });
        },
      }, options.map(([v, label]) => h("option", { value: v }, label)))
      : h("span", options.find(([v]) => v === m[field])[1]);
    const invites = can_manage && pendingInvites(`${base}/invites`, `${base}/invites`);
    box.replaceChildren(
      h("div.table-scroll", h("table.people",
        h("tr", h("th", "Member"), h("th", "Role"), h("th", "Access"), h("th")),
        members.map((m) => h("tr",
          h("td", m.email, m.user_id === me && h("span.muted", " (you)")),
          h("td", select(m, "role", [["dm", "DM"], ["player", "Player"]])),
          h("td", select(m, "access", [["owner", "Owner"], ["editor", "Editor"], ["viewer", "Viewer"]]),
            h("div.muted.small", ACCESS_HELP[m.access])),
          h("td", (can_manage || m.user_id === me) && h("button.icon", {
            title: m.user_id === me ? "Leave campaign" : "Remove", "aria-label": m.user_id === me ? "Leave campaign" : `Remove ${m.email}`,
            onclick: async () => {
              const leaving = m.user_id === me;
              if (!confirm(leaving ? `Leave ${c.name}?` : `Remove ${m.email} from ${c.name}?`)) return;
              try {
                await api("DELETE", `${base}/members/${m.user_id}`);
                if (leaving) location.hash = "#/campaigns";
                else render();
              } catch (err) {
                toast(err.message, true);
              }
            },
          }, "×")),
        )),
      )),
      h("p.muted.small", "Players see only what the party knows; DMs see everything. Character sheets can be read by everyone in the campaign and changed by their owner and the DMs."),
      can_manage && inviteForm(`${base}/invites`, { campaign: true, onCreated: () => invites.refresh() }),
      can_manage && invites.el,
    );
  };
  await render();
  return box;
}
