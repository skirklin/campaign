# Deploying to the internet

This runs the web UI at `https://campaign.kirkl.in` on the homelab cluster.

- **Hosting** follows the `werewolf` pattern: this repo publishes an image to
  GHCR, the cluster runs it with a persistent volume, and the homelab Caddy
  proxies a public hostname to it.
- **Its own namespace:** it runs in a `campaign` namespace, the "tenant"
  layout the homelab repo's `infra/EXTERNAL.md` describes. Homelab owns the
  envelope: the namespace, its quota and default limits, the `deployer`
  ServiceAccount CI uses, and the Caddy route
  (`infra/k8s/tenants/campaign.yaml`, `infra/k8s/caddy.yaml`). This repo
  owns what runs inside it: the Deployment, Service and PVC.
- **Sign-in** is Google, and invite-only. People join a campaign through an
  invite link, as a DM or a player, and the app's users and memberships
  live in its database, not in config.

```
browser ──https──▶ Caddy (homelab ns, Let's Encrypt) ──▶ campaign.campaign.svc:8000 ──▶ /data (PVC)
                                                          └─ Google sign-in, invite-only
GitHub Actions: push to main ─▶ test ─▶ ghcr.io/skirklin/campaign:sha-…
                                         └──────────▶ deploy (kubectl apply -k, as `deployer`)
```

## What's protected, and how

- **Everything requires a signed-in user.** That covers every page,
  `/api/*` and `/static/*`; only `/auth/*`, `/invite/*` and `/healthz` are
  open. A Google account that isn't a user and has no valid invite is
  turned away. Users are looked up on every request, so removing someone
  locks them out immediately.
- **The MCP endpoint takes bearer tokens instead.** `/mcp` and its OAuth
  endpoints (`/.well-known/oauth-*`, `/authorize`, `/token`, `/register`,
  `/revoke`) skip the session check, because apps call them from elsewhere.
  `/mcp` refuses anything without a valid access token issued for it, and
  the only way to get one is the consent page, which needs your session
  and shows which app and redirect host is asking. The page can't be
  framed. Tokens are stored hashed, expire, rotate, and are revoked when
  you disconnect the app or the user is removed.
- **Every campaign checks membership.** A campaign you don't belong to
  looks like it doesn't exist. Players see only what the party knows,
  including names and search results, and viewers can't change anything.
- **Invite links** expire (1-90 days, 7 by default) and have a use limit
  (1 by default). They can be tied to one Google account and revoked. Only
  a hash of each link is stored, so a link is shown once, when it's made.
- **The sign-in flow** is OpenID Connect with PKCE, state and nonce. The
  session is a signed `__Host-` cookie: HttpOnly, Secure, SameSite=Lax,
  lasting 30 days. Requests that change data from another site are refused.
- **It refuses to run unprotected.** The server won't listen on anything but
  localhost unless sign-in is configured or `--no-auth` is passed.
- **The container is locked down.** It runs as a non-root user with a
  read-only root filesystem and no service-account token. The `deployer`
  account that CI uses can manage Deployments, Services and PVCs and read
  pods and logs, in this namespace only. It can't read Secrets.

## One-time setup

### 1. Google OAuth client

In [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services:

1. **OAuth consent screen**: choose External, give the app a name, and add
   your address as a test user, or publish the app. It asks only for
   `openid email`, so publishing needs no verification.
2. **Credentials → Create credentials → OAuth client ID**: choose "Web
   application" and set the authorized redirect URI to
   `https://campaign.kirkl.in/auth/callback`.
3. Keep the client ID and secret for step 2.

### 2. The app's settings and credentials

Settings live in two places:

- **In `deploy/k8s/deployment.yaml`**, committed, like your other apps'
  plain `env` values: `CAMPAIGN_AUTH`, `CAMPAIGN_PUBLIC_URL` and
  `GOOGLE_CLIENT_ID`. Replace the `REPLACE_ME` client ID before the first
  deploy.
- **In the `campaign-secrets` Secret**, created by hand, for the three
  credentials:

  | Key | Value |
  |---|---|
  | `ANTHROPIC_API_KEY` | a key just for this app, so its spending is separate and it can be revoked on its own |
  | `GOOGLE_CLIENT_SECRET` | from step 1 |
  | `CAMPAIGN_SESSION_SECRET` | `openssl rand -hex 32`; changing it signs everyone out |

  The Secret is per namespace, so `campaign` can't share the homelab's
  `api-secrets`.

The `campaign` namespace comes from the homelab repo, so it must be deployed
there first. Then, on the VPS, copy the template:

```bash
cp deploy/k8s/secrets.env.example deploy/k8s/secrets.env
```

Fill in `deploy/k8s/secrets.env` (it's gitignored), then create the Secret:

```bash
kubectl -n campaign create secret generic campaign-secrets --from-env-file=deploy/k8s/secrets.env
```

Create it before the first deploy: the pod can't start without it. To change a credential later, edit
the file and re-create the Secret:

```bash
kubectl -n campaign create secret generic campaign-secrets --from-env-file=deploy/k8s/secrets.env --dry-run=client -o yaml | kubectl apply -f -
```

Then restart:

```bash
kubectl -n campaign rollout restart deployment/campaign
```

### 3. Make yourself the first admin

After the first deploy (steps 4-6, then [Releasing](#releasing)), add yourself from inside it:

```bash
kubectl -n campaign exec deploy/campaign -- campaign users add you@gmail.com --admin --claim-unowned
```

- **`--admin`** gives you the admin page (users, app invites).
- **`--claim-unowned`** makes you the owner of any campaign nobody owns
  yet, such as campaigns carried over from local use.

After that, everyone else arrives by invite (see
[Inviting people](#inviting-people)).

### 4. The image

The first push to `main` publishes `ghcr.io/skirklin/campaign`. GHCR
packages start private: either make it public (the package's settings
page → Change visibility), as werewolf's is, or add an `imagePullSecret`
to the Deployment.

### 5. Homelab

The homelab repo provides the namespace and its quota (one PVC of up to
2 GiB; ephemeral storage counted, with defaults from a LimitRange), the
`deployer` ServiceAccount and its token, and the Caddy route for
`campaign.kirkl.in` with a 200 MB upload cap. See `campaign` in its
`infra/EXTERNAL.md`. DNS needs nothing: the `*.kirkl.in` wildcard covers
it, and Caddy gets the certificate itself.

A manifest here that outgrows the envelope (a second PVC, a bigger one,
another kind of object the deployer can't create) needs a homelab change
first.

### 6. GitHub (this repo → Settings)

- **Environments → New environment `production`.** Optionally require your
  approval before each deploy.
- **Secrets** (on the environment, or the repo):
  - `KUBE_SERVER`: the API server URL. `https://k8s.kirkl.in` works: Caddy
    passes bearer tokens through, and the apiserver checks them.
  - `KUBE_TOKEN`: the deployer's token:

    ```bash
    kubectl -n campaign get secret deployer-token -o jsonpath='{.data.token}' | base64 -d
    ```
- **Variables:**
  - `DEPLOY_ENABLED` = `true`
  - `PUBLIC_URL` = `https://campaign.kirkl.in`

## Inviting people

- **To a campaign:** open the campaign's **Members** tab. Choose a role
  (DM sees everything; player sees what the party knows) and access
  (editor can change things; viewer can only look), then send the link.
  It's shown once. Owners can change anyone's role or access later,
  remove members, and revoke unused links; anyone can leave.
- **To the app:** the **Admin** page makes invites that grant access
  without a campaign, so someone can start their own. It also lists users,
  removes them (signing them out at once) and makes admins.
- **From the command line**, for when you're at a terminal:

  ```bash
  kubectl -n campaign exec deploy/campaign -- campaign invite -c curse-of-strahd --role player --access viewer
  ```

  Add `--email` to tie the link to one Google account, and `--days` or
  `--uses` to change its limits.

**Character sheets** belong to one campaign and to the person who made
them. Everyone in the campaign can read a sheet; its owner and the
campaign's DMs (with editor access) can change it. Copying a sheet to
another campaign needs edit rights on it and membership of the target.

## Releasing

**Merging to `main` deploys.** The Release workflow tests, builds and
pushes the image, applies `deploy/k8s` with the image pinned to that
digest, waits for the rollout, and checks `/healthz` through the public
URL. Manifest changes ship the same way. Running the workflow by hand from
the Actions tab redeploys; a `v*` tag also deploys, if you want to mark a
version.

**Rollback:** revert the commit on `main`, or run the workflow by hand on
an older tag. For something immediate, point the Deployment at an earlier
image:

```bash
kubectl -n campaign set image deployment/campaign campaign=ghcr.io/skirklin/campaign:sha-<commit>
```

## Data

- **Where it lives:** everything is on the `campaign-data` volume: the
  database, original uploads, and the snapshots taken before schema
  changes. Back up that volume the way the cluster backs up others.
- **What redeploys keep:** schema changes migrate in place, or start clean
  while keeping campaigns, character sheets, uploads and the digest cache.
  See the README.
- **One instance only:** the Deployment uses `Recreate` with one replica,
  because SQLite expects a single writer. Expect a few seconds of downtime
  per deploy.
