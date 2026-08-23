# Feature: Teams & RBAC (multi-user orgs)

> Status: **in progress** (design locked 2026-08-04). Step 1 (schema + migration)
> under implementation. Supersedes the one-user-per-org model.

## Summary

Today an org is a **personal workspace**: signup mints a fresh org and the user is
its sole `admin` (11 orgs, 11 users, strictly 1:1). There is no membership table, no
invites, and `role` is never enforced.

This feature turns orgs into **teams**: multiple users share one org, invited by
email, with **Admin / Member** roles. A user can belong to **multiple orgs** (their
personal workspace *and* any teams they join), with an **active org** carried in the
session and a header **org switcher** that only appears once a user is in 2+ orgs.

## Decisions (locked)

- **Sharing model — team assets shared, personal stays personal.**
  - **Org-shared** (any member sees/uses): routines, data sources, skills, alerts.
  - **Personal** (per-user): connectors (personal OAuth), chat threads, notifications.
  - A **routine acts through its creator's** connectors; a **chat acts through the
    current user's** connectors. Data sources are **org-shared** (any member's
    chat/routine can use any org database).
- **Roles — Admin / Member** (two tiers).
  - **Admin**: invite/remove members, change roles, manage every org asset, delete org.
  - **Member**: create/run/use org assets, manage own connectors and chats.
- **Multi-org membership** (Slack/Linear model), built so **solo users see no extra
  UX** — the org switcher is hidden until a user belongs to more than one org.

## Scoping policy

| Resource | Scope | View / Use | Create | Edit / Delete |
|---|---|---|---|---|
| Routines (`agents` kind=routine) | org | any member | any member | creator **or** admin |
| Data sources (`db_connections`) | org | any member | any member | creator **or** admin |
| Skills | org | any member | any member | creator **or** admin |
| Alerts (`alert_rules`) | org (follow their agent) | — | with agent | with agent |
| Connectors (`connections`) | personal (`user_id`) | owner only | each member connects own | owner only |
| Chat threads (`agents` kind=chat) | personal (`owner_id`) | owner only | each member | owner only |
| Notifications | personal | owner only | — | owner only |

### Run identity (whose accounts a run acts through)
- **Routine** → its **creator** (`owner_id`). The runner already provisions
  `active_providers(owner_id)`, so a shared routine run by anyone uses the creator's
  connectors. No change.
- **Chat** → the **current user** (a chat's `owner_id` is that user). No change.
- **Data sources** → **org-scoped**. Requires flipping chat data-source provisioning
  from owner-scoped (`list_owner_db_connections`) to **org-scoped** in `runner.py`.
  This also fixes the current inconsistency (connectors personal, data sources org).

## Schema

```sql
CREATE TABLE memberships (
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  org_id     INTEGER NOT NULL REFERENCES orgs(id)  ON DELETE CASCADE,
  role       TEXT NOT NULL DEFAULT 'member',   -- 'admin' | 'member'
  created_at TEXT NOT NULL,
  PRIMARY KEY (user_id, org_id)
);

CREATE TABLE invitations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id      INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  email       TEXT NOT NULL,
  role        TEXT NOT NULL DEFAULT 'member',
  token       TEXT NOT NULL UNIQUE,             -- random, single-use
  invited_by  INTEGER REFERENCES users(id),
  status      TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | revoked
  created_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  accepted_at TEXT
);
```
- `sessions` gains `active_org_id INTEGER` (which org the user is currently acting in).
- `users.org_id` stays as the user's **personal/default** org (fallback); access is
  decided by `memberships`.
- **Migration/backfill:** insert `memberships(user_id, users.org_id, 'admin')` for
  every existing user. Day-one behavior is unchanged — each workspace is a 1-member
  org whose sole user is admin.

## Endpoints (later steps)

- `GET /api/org` · `GET /api/org/members` — roster + your role
- `GET /api/orgs` (orgs I'm in) · `POST /api/org/switch` (set active org)
- `POST /api/org/invitations` *(admin)* → token link · `DELETE /api/org/invitations/{id}` *(admin)*
- `GET /api/invitations/{token}` · `POST /api/invitations/{token}/accept`
- `DELETE /api/org/members/{uid}` · `PATCH /api/org/members/{uid}` *(admin)*

Enforcement: `current_user` resolves **active org + role** from `memberships`;
`_org_id(user)` returns the active org; `require_admin` guards member management;
edit/delete of org assets is **creator-or-admin**.

## UI (later steps)

- Header **org switcher** (only when in 2+ orgs).
- **Members** page: roster + roles; admins get invite form + remove/role controls.
- **Invite accept** flow: link → register/login → membership created → land in org.
- Chats stay filtered to `owner_id = you`; Routines/Data/Skills become org-wide.

## Edge cases

- **Member leaves:** personal connectors + chats go with them; org assets they
  created stay (owned by the org). A routine they created runs on *their* (now gone)
  connectors → flag it "needs reconnection" for an admin to reassign owner.
- **Last admin** can't leave or be demoted.
- **Invite tokens:** random, single-use, 14-day expiry.

## Build sequence

1. **Schema + migration + DB helpers + backfill** *(this step)* — non-breaking; new
   tables populated, `current_user` unchanged.
2. Session active-org + `current_user`/`_org_id` rewrite + `require_admin`.
3. Data-source scoping flip (personal → org) in `runner.py`.
4. Member/invitation endpoints.
5. UI: org switcher + Members page + invite accept.
6. Verify: two users in one org — shared routine runs through creator's connectors,
   chats stay private, member blocked from admin ops.
