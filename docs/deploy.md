# Deploying to the internet

This runs the web UI at `https://campaign.kirkl.in` on the homelab cluster.

- **Hosting** follows the `werewolf` pattern: this repo publishes an image to
  GHCR, the cluster runs it with a persistent volume, and the homelab Caddy
  proxies a public hostname to it.
- **Its own namespace:** it gets a `campaign` namespace, the "tenant"
  layout `infra/EXTERNAL.md` describes.
- **Sign-in** is Google, limited to an allowlist of email addresses.

```
browser ──https──▶ Caddy (homelab ns, Let's Encrypt) ──▶ campaign.campaign.svc:8000 ──▶ /data (PVC)
                                                          └─ Google sign-in, email allowlist
GitHub Actions: push to main ─▶ test ─▶ ghcr.io/skirklin/campaign:sha-…
                tag v* / manual ─────────────────────────────▶ deploy (kubectl set image, as `deployer`)
```

## What's protected, and how

- **Everything requires a signed-in, allowed Google account.** That covers
  every page, `/api/*` and `/static/*`; only `/auth/*` and `/healthz` are
  open. The allowlist is checked on every request, so removing an address
  locks it out immediately.
- **The sign-in flow** is OpenID Connect with PKCE, state and nonce. The
  session is a signed `__Host-` cookie: HttpOnly, Secure, SameSite=Lax,
  lasting 30 days. Requests that change data from another site are refused.
- **It refuses to run unprotected.** The server won't listen on anything but
  localhost unless sign-in is configured or `--no-auth` is passed.
- **The container is locked down.** It runs as a non-root user with a
  read-only root filesystem and no service-account token. The `deployer`
  account that CI uses can only update this one Deployment's image and
  watch the rollout, in this namespace.

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

### 2. Cluster resources (needs admin credentials)

Settings live in two places:

- **In `deploy/k8s/deployment.yaml`**, committed, like your other apps'
  plain `env` values: `CAMPAIGN_AUTH`, `CAMPAIGN_PUBLIC_URL`,
  `GOOGLE_CLIENT_ID` and `CAMPAIGN_ALLOWED_EMAILS`. Replace the two
  `REPLACE_ME` values before the first apply. To change the allowlist
  later, commit the edit and deploy.
- **In the `campaign-secrets` Secret**, created by hand, for the three
  credentials:

  | Key | Value |
  |---|---|
  | `ANTHROPIC_API_KEY` | a key just for this app, so its spending is separate and it can be revoked on its own |
  | `GOOGLE_CLIENT_SECRET` | from step 1 |
  | `CAMPAIGN_SESSION_SECRET` | `openssl rand -hex 32`; changing it signs everyone out |

  The Secret is per namespace, so `campaign` can't share the homelab's
  `api-secrets`.

With admin credentials, apply the manifests:

```bash
kubectl apply -k deploy/k8s
```

Copy the template:

```bash
cp deploy/k8s/secrets.env.example deploy/k8s/secrets.env
```

Fill in `deploy/k8s/secrets.env` (it's gitignored), then create the Secret:

```bash
kubectl -n campaign create secret generic campaign-secrets --from-env-file=deploy/k8s/secrets.env
```

The pod starts once the Secret exists. To change a credential later, edit
the file and re-create the Secret:

```bash
kubectl -n campaign create secret generic campaign-secrets --from-env-file=deploy/k8s/secrets.env --dry-run=client -o yaml | kubectl apply -f -
```

Then restart:

```bash
kubectl -n campaign rollout restart deployment/campaign
```

### 3. The image

The first push to `main` publishes `ghcr.io/skirklin/campaign`. GHCR
packages start private: either make it public (the package's settings
page → Change visibility), as werewolf's is, or add an `imagePullSecret`
to the Deployment.

### 4. Caddy (in the homelab repo)

Add a site block to the Caddyfile, and a line to `infra/EXTERNAL.md`:

```
# EXTERNAL: deployed from skirklin/campaign into its own `campaign` namespace.
campaign.kirkl.in {
    reverse_proxy campaign.campaign.svc.cluster.local:8000
}
```

Then add a DNS record for `campaign.kirkl.in` pointing where the other
`*.kirkl.in` hosts do. Caddy gets the certificate itself.

### 5. GitHub (this repo → Settings)

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

## Releasing

```bash
git tag v0.1.0
```

```bash
git push origin v0.1.0
```

The Release workflow tests, builds and pushes the image, rolls the
Deployment to it by digest, and checks `/healthz` through the public URL.
Running the workflow by hand from the Actions tab deploys the current
`main`. Pushes to `main` only publish an image.

**Rollback:** run the workflow by hand on an older tag. Or point the
Deployment at an earlier image:

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
