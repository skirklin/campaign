"""Users, campaign memberships and invites.

Only used when sign-in is on. Without it (local use), whoever runs the
server owns everything and none of this applies.

A membership has two parts:
- **role**, dm or player, decides what you see: players get the
  spoiler-filtered world, the same as a player campaign.
- **access**, owner, editor or viewer, decides what you can change. Editors
  add material, ingest sessions and edit the world. Owners also manage
  members and invites.

People join by invite link. An invite grants a role and access in one
campaign (or, for admins, just access to the app), expires, has a use
limit, and can be tied to one Google account. Only a hash of its token is
stored.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Campaign, Store, now

ACCESS_RANK = {"viewer": 0, "editor": 1, "owner": 2}


class InviteError(Exception):
    """An invite that can't be used, with a reason fit to show the person holding it."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    def __init__(self, store: Store):
        self.store = store
        self.conn = store.conn

    # -- users ------------------------------------------------------------------

    def user(self, user_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def user_by_email(self, email: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM users WHERE email = ?", (email.strip(),)).fetchone()
        return dict(row) if row else None

    def users(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT u.*, (SELECT count(*) FROM memberships m WHERE m.user_id = u.id) AS campaigns"
            " FROM users u ORDER BY u.email"
        )
        return [dict(r) for r in rows]

    def create_user(self, email: str, is_admin: bool = False) -> dict[str, Any]:
        email = email.strip().lower()
        if "@" not in email:
            raise ValueError(f"{email!r} isn't an email address")
        with self.conn:
            self.conn.execute(
                "INSERT INTO users (email, is_admin, created_at) VALUES (?, ?, ?)"
                " ON CONFLICT (email) DO UPDATE SET is_admin = max(is_admin, excluded.is_admin)",
                (email, int(is_admin), now()),
            )
        return self.user_by_email(email)

    def set_admin(self, user_id: int, is_admin: bool) -> None:
        with self.conn:
            self.conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))

    def delete_user(self, user_id: int) -> bool:
        """Remove a user and their memberships. Their character sheets stay, ownerless."""
        with self.conn:
            return self.conn.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount > 0

    def seen(self, user_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (now(), user_id))

    # -- memberships --------------------------------------------------------------

    def membership(self, campaign_id: int, user_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM memberships WHERE campaign_id = ? AND user_id = ?", (campaign_id, user_id)
        ).fetchone()
        return dict(row) if row else None

    def members(self, campaign_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT u.id AS user_id, u.email, m.role, m.access, m.created_at FROM memberships m"
            " JOIN users u ON u.id = m.user_id WHERE m.campaign_id = ?"
            " ORDER BY CASE m.access WHEN 'owner' THEN 0 WHEN 'editor' THEN 1 ELSE 2 END, u.email",
            (campaign_id,),
        )
        return [dict(r) for r in rows]

    def add_member(self, campaign_id: int, user_id: int, role: str, access: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO memberships (campaign_id, user_id, role, access, created_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (campaign_id, user_id) DO UPDATE SET role = excluded.role, access = excluded.access",
                (campaign_id, user_id, role, access, now()),
            )

    def update_member(self, campaign_id: int, user_id: int, role: str | None, access: str | None) -> None:
        current = self.membership(campaign_id, user_id)
        if current is None:
            raise KeyError("not a member")
        if current["access"] == "owner" and access not in (None, "owner"):
            self._keep_an_owner(campaign_id, user_id)
        self.add_member(campaign_id, user_id, role or current["role"], access or current["access"])

    def remove_member(self, campaign_id: int, user_id: int) -> bool:
        m = self.membership(campaign_id, user_id)
        if m and m["access"] == "owner":
            self._keep_an_owner(campaign_id, user_id)
        with self.conn:
            return self.conn.execute(
                "DELETE FROM memberships WHERE campaign_id = ? AND user_id = ?", (campaign_id, user_id)
            ).rowcount > 0

    def _keep_an_owner(self, campaign_id: int, leaving: int) -> None:
        others = self.conn.execute(
            "SELECT count(*) FROM memberships WHERE campaign_id = ? AND access = 'owner' AND user_id != ?",
            (campaign_id, leaving),
        ).fetchone()[0]
        if not others:
            raise ValueError("a campaign needs at least one owner; make someone else an owner first")

    def campaigns_for(self, user_id: int) -> list[tuple[Campaign, str]]:
        """The user's campaigns, each as they see it (their role), with their access."""
        rows = self.conn.execute(
            "SELECT c.slug, m.role, m.access FROM memberships m JOIN campaigns c ON c.id = m.campaign_id"
            " WHERE m.user_id = ? ORDER BY c.name",
            (user_id,),
        ).fetchall()
        return [
            (replace(self.store.campaign(r["slug"]), role=r["role"], access=r["access"]), r["access"]) for r in rows
        ]

    def claim_unowned(self, user_id: int) -> list[str]:
        """Make the user owner of every campaign nobody owns (e.g. ones made with the CLI)."""
        unowned = self.conn.execute(
            "SELECT id, slug, role FROM campaigns c WHERE NOT EXISTS"
            " (SELECT 1 FROM memberships m WHERE m.campaign_id = c.id AND m.access = 'owner')"
        ).fetchall()
        for c in unowned:
            self.add_member(c["id"], user_id, c["role"], "owner")
        return [c["slug"] for c in unowned]

    # -- invites ------------------------------------------------------------------

    def create_invite(
        self, campaign_id: int | None, role: str | None = "player", access: str | None = "viewer",
        email: str | None = None, created_by: int | None = None, days: int = 7, max_uses: int = 1,
    ) -> tuple[dict[str, Any], str]:
        """Returns the invite and its token. The token is never stored, so show it now."""
        if campaign_id is not None and (role not in ("dm", "player") or access not in ("editor", "viewer")):
            raise ValueError("role is dm or player; access is editor or viewer")
        if not 1 <= days <= 90 or not 1 <= max_uses <= 100:
            raise ValueError("invites last 1-90 days and allow 1-100 uses")
        token = secrets.token_urlsafe(24)
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
        with self.conn:
            invite_id = self.conn.execute(
                "INSERT INTO invites (token_hash, campaign_id, role, access, email, created_by, created_at,"
                " expires_at, max_uses) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_hash(token), campaign_id, role if campaign_id else None, access if campaign_id else None,
                 email.strip().lower() if email else None, created_by, now(), expires, max_uses),
            ).lastrowid
        return self.invite(invite_id), token

    def invite(self, invite_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT i.id, i.campaign_id, i.role, i.access, i.email, i.created_by, i.created_at, i.expires_at,"
            " i.max_uses, i.uses, i.revoked, c.slug, c.name AS campaign_name FROM invites i"
            " LEFT JOIN campaigns c ON c.id = i.campaign_id WHERE i.id = ?",
            (invite_id,),
        ).fetchone()
        return dict(row) if row else None

    def pending_invites(self, campaign_id: int | None) -> list[dict[str, Any]]:
        """Unused, unexpired, unrevoked invites for a campaign (or app-only ones when None)."""
        rows = self.conn.execute(
            "SELECT i.id, i.role, i.access, i.email, i.created_at, i.expires_at, i.max_uses, i.uses,"
            " u.email AS created_by FROM invites i LEFT JOIN users u ON u.id = i.created_by"
            " WHERE i.campaign_id IS ? AND i.revoked = 0 AND i.uses < i.max_uses AND i.expires_at > ?"
            " ORDER BY i.id DESC",
            (campaign_id, now()),
        )
        return [dict(r) for r in rows]

    def revoke_invite(self, invite_id: int, campaign_id: int | None) -> bool:
        with self.conn:
            return self.conn.execute(
                "UPDATE invites SET revoked = 1 WHERE id = ? AND campaign_id IS ?", (invite_id, campaign_id)
            ).rowcount > 0

    def check_invite(self, token: str, email: str | None = None) -> dict[str, Any]:
        """The invite for `token` if it can be used (by `email`, when given), else InviteError."""
        row = self.conn.execute("SELECT id FROM invites WHERE token_hash = ?", (_hash(token),)).fetchone()
        inv = self.invite(row["id"]) if row else None
        if inv is None or inv["revoked"]:
            raise InviteError("This invite link isn't valid. Ask for a new one.")
        if inv["expires_at"] <= now():
            raise InviteError("This invite link has expired. Ask for a new one.")
        if inv["uses"] >= inv["max_uses"]:
            raise InviteError("This invite link has already been used. Ask for a new one.")
        if email and inv["email"] and inv["email"] != email.strip().lower():
            raise InviteError(f"This invite is for {inv['email']}. Sign in with that Google account.")
        return inv

    def redeem(self, token: str, email: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Use an invite as `email`: create the user if new and add the membership.

        Using a link you've already used, for a campaign you're already in,
        changes nothing and doesn't count as a use.
        """
        row = self.conn.execute("SELECT id FROM invites WHERE token_hash = ?", (_hash(token),)).fetchone()
        inv = self.invite(row["id"]) if row else None
        user = self.user_by_email(email)
        if inv and user and inv["campaign_id"] and self.membership(inv["campaign_id"], user["id"]):
            return user, inv
        inv = self.check_invite(token, email)
        if inv["campaign_id"] is None and user is not None:
            return user, inv  # app-only invite, already has access
        user = user or self.create_user(email)
        if inv["campaign_id"] is not None:
            self.add_member(inv["campaign_id"], user["id"], inv["role"], inv["access"])
        with self.conn:
            self.conn.execute("UPDATE invites SET uses = uses + 1 WHERE id = ?", (inv["id"],))
        return user, inv


def at_least(access: str, needed: str) -> bool:
    return ACCESS_RANK[access] >= ACCESS_RANK[needed]
