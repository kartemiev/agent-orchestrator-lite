 English | [Русский](securing-with-npm-and-cloudflare.ru.md)

# Securing a public deployment with Nginx Proxy Manager and Cloudflare Access

This guide turns a local `agent-orchestrator-lite` stack (bound to `127.0.0.1`)
into a public HTTPS deployment protected by **two independent layers**:

1. **Nginx Proxy Manager (NPM)** — terminates TLS, forwards traffic to the
   containers, enforces HTTP Basic Auth through an *Access List* and keeps the
   Streamlit WebSocket alive.
2. **Cloudflare Zero Trust / Access** — puts an identity gate (Email PIN / OTP)
   in front of the UI, while API and webhook paths stay reachable for
   automation.

The order matters: a request travels `client → Cloudflare edge → NPM → container`.
NPM must therefore never be reachable directly from the internet — restrict it to
the Cloudflare IP ranges or to a private/Tailscale interface.

---

## What you get

```
   browser / CI ──► ┌──────────────────────────────┐
                    │ Cloudflare edge (TLS, WAF)   │
                    │  Access app  ui.example.com  │  ← Email PIN / OTP
                    │  Access app  api.example.com │  ← or Service Token
                    └──────────────┬───────────────┘
                                   │ HTTPS (Full / strict)
                    ┌──────────────▼───────────────┐
                    │ Nginx Proxy Manager :443     │
                    │  Access List → Basic Auth    │
                    │  Websockets Support → ON     │
                    └───────┬──────────────┬───────┘
                            │              │
                 ┌──────────▼───┐   ┌──────▼─────────────┐
                 │ agent-ui     │   │ agent-orchestrator │
                 │ Streamlit    │   │ FastAPI Gateway    │
                 │ :8501        │   │ :8000 (host :8085) │
                 └──────────────┘   └────────────────────┘
```

Two hostnames are used on purpose:

| Hostname | Upstream (container:port) | Purpose |
|---|---|---|
| `ui.example.com` | `agent-ui:8501` | Streamlit Dashboard, protected by Access + Basic Auth |
| `api.example.com` | `agent-orchestrator:8000` | Gateway API (`/health`, `/api/v1/plan/direct`), protected by a service token and/or Basic Auth |

> Prefer a **separate subdomain per service** over path-based routing. Streamlit
> assumes it owns the web root; serving it from a sub-path requires
> `--server.baseUrlPath` and breaks static assets and the WebSocket endpoint.

---

## Prerequisites

- A domain whose DNS is managed by Cloudflare (nameservers pointed at
  Cloudflare).
- A Linux host with Docker and Docker Compose, and the stack from this
  repository running (`docker compose up -d`).
- Nginx Proxy Manager running (own container or host install) with the admin UI
  on port `81` reachable from your workstation.
- Ports `80` and `443` open on the host firewall for Cloudflare only.

Never publish the orchestrator ports directly. `temporal` (`7233`),
`temporal-ui` (`8233`) and `openhands` (`8000`) must stay private — they have no
authentication of their own.

## Step 1 — Let NPM reach the containers

By default every service in `docker-compose.yml` binds to
`${BIND_IP:-127.0.0.1}`, i.e. the loopback of the **Docker host**. A
bridge-network NPM container has its own loopback, so `127.0.0.1:8501` inside
NPM points at NPM itself. Choose one of the two options below.

### Option A (recommended) — shared Docker network

The compose project is named `agent-orchestrator-lite`, so its network is
`agent-orchestrator-lite_agent-net`:

```bash
docker network ls | grep agent-net
# agent-orchestrator-lite_agent-net   bridge   local
```

If NPM runs from its own `docker-compose.yml`, declare that network as external:

```yaml
services:
  app:
    image: 'jc21/nginx-proxy-manager:latest'
    restart: unless-stopped
    ports:
      - '80:80'
      - '443:443'
      - '81:81'
    volumes:
      - ./data:/data
      - ./letsencrypt:/etc/letsencrypt
    networks:
      - npm-net

networks:
  npm-net:
    name: agent-orchestrator-lite_agent-net
    external: true
```

If NPM is already running, attach it without recreating the container:

```bash
docker network connect agent-orchestrator-lite_agent-net nginx-proxy-manager
```

Now the upstreams are simply the service names: `agent-ui` → `8501` and
`agent-orchestrator` → `8000`. No host port has to be published at all, so
`BIND_IP=127.0.0.1` can stay as it is.

### Option B — publish on a private interface

Set `BIND_IP` to the host's LAN or Tailscale address in `.env`, restart the
stack, and point NPM at `<host-ip>:8501` / `<host-ip>:8085`:

```bash
# .env
BIND_IP=100.64.0.10      # example Tailscale address — never 0.0.0.0 on a public host
```

```bash
docker compose up -d
```

With `BIND_IP=0.0.0.0` the containers listen on every interface, so the host
firewall must drop everything except `80`/`443` coming from Cloudflare IP
ranges.

---

## Step 2 — Create the proxy hosts in NPM

`Hosts → Proxy Hosts → Add Proxy Host`, once per hostname.

### `ui.example.com` — Streamlit

**Details tab**

| Field | Value |
|---|---|
| Domain Names | `ui.example.com` |
| Scheme | `http` |
| Forward Hostname / IP | `agent-ui` (Option A) or host IP (Option B) |
| Forward Port | `8501` |
| Block Common Exploits | ON |
| **Websockets Support** | **ON** — see Step 5, the UI stalls without it |
| Access List | the list created in Step 3 |

**Custom locations** — not required for a dedicated subdomain. Leave empty.

**SSL tab** — select the certificate from Step 4, then:

| Field | Value |
|---|---|
| Force SSL | ON |
| HTTP/2 Support | ON |
| HSTS Enabled | ON (enable *HSTS Subdomains* only if the whole domain is HTTPS) |

### `api.example.com` — FastAPI Gateway

Same form, `Forward Port` = `8000`, **Websockets Support OFF** (the API is plain
HTTP). Keep `Block Common Exploits` ON.

> The host port `8085` is only the *external* mapping of the container's `8000`.
> Inside the Docker network always use `agent-orchestrator:8000`; `8085` will
> not resolve there.

### `temporal.example.com` — do not create it

The Temporal UI (`8233`) and the gRPC frontend (`7233`) have no authentication.
Reach them over SSH port-forwarding or Tailscale instead:

```bash
ssh -L 8233:127.0.0.1:8233 user@host
```

Keep `openhands` (`8000` on the host) private as well: it runs with
`DISABLE_AUTH=true` and would expose an unauthenticated agent runtime.

---

## Step 3 — Basic Auth via an NPM Access List

An Access List is a reusable set of credentials that NPM injects as
`auth_basic` into the generated vhost. It protects the panel even if Cloudflare
Access is misconfigured or bypassed through the origin IP.

1. Open the NPM admin UI (`http://<host>:81`) and sign in.
2. Go to **Access Lists → Add Access List**.
3. Fill in **Details**:
   - **Name**: `orchestrator-operators`
   - **Satisfy Any**: OFF (with *Any*, a request is allowed when *either* the IP
     rule *or* the credentials match; for a strict gate use *All* semantics).
4. Switch to the **Authorization** tab → **Add Authorization**:
   - **Username**: `operator` (do not use `admin`)
   - **Password**: generate a long random one, e.g.
     `openssl rand -base64 24`
   - Press **Save**. NPM stores a bcrypt hash, never the plaintext.
5. Optional but recommended — **Access** tab: add an *Allow* rule for your
   office/VPN/Tailscale CIDR and a `deny all` fallback.
6. Press **Save**, then open the `ui.example.com` proxy host →
   **Access List** → select `orchestrator-operators` → **Save**.

Verification — the origin must answer `401` without credentials:

```bash
curl -sI https://ui.example.com/ | head -n 5
# HTTP/2 401
# www-authenticate: Basic realm="Authorization Required"

curl -sI -u operator:'<password>' https://ui.example.com/ | head -n 3
# HTTP/2 200
```

Apply the same Access List to `api.example.com` **only if** your automation can
send credentials. Otherwise use a Cloudflare service token (Step 8) and leave
the API host without Basic Auth, so `curl` in CI does not have to embed a
human password.

> Browsers cache Basic Auth credentials per realm and send them preemptively.
> Use a private window when testing two accounts, and always serve the realm
> over HTTPS — Basic Auth is base64, not encryption.

---

## Step 4 — Custom SSL certificate (StartSSL / Custom CRT + Key)

NPM can either issue a Let's Encrypt certificate or accept an existing PEM
pair. The custom path is the one to use when the certificate comes from
Cloudflare Origin CA, an internal CA, or a commercial/legacy issuer.

### 4.1 Obtain the pair

- **Cloudflare Origin CA (recommended when Cloudflare proxies the domain)**:
  Cloudflare dashboard → **SSL/TLS → Origin Server → Create Certificate**,
  hostnames `*.example.com` + `example.com`, validity 15 years. You get an
  origin certificate and a private key. It is trusted by the Cloudflare edge,
  so it works with **Full (strict)**.
- **Let's Encrypt (HTTP-01)** inside NPM — works only when port `80` reaches the
  origin directly; if Cloudflare's proxy is on, prefer **DNS-01** or the Origin
  CA above.
- **Existing corporate/legacy CRT** — concatenate the leaf and intermediates
  into one file (`fullchain.pem`).

> ⚠️ **About “StartSSL”.** Older NPM builds expose a *StartSSL* entry that
> refers to the StartCom CA. StartCom certificates have not been trusted by
> browsers since 2016, so use it only for an internal CA that your clients
> already trust. Modern NPM builds show a single **Custom** form, which is what
> the steps below use; the flow is identical for a legacy CRT.

### 4.2 Import into NPM

1. **SSL Certificates → Add SSL Certificate → Custom**.
2. **Name**: `example.com-wildcard`.
3. **Server Name**: `*.example.com` (or the exact FQDN; it must match the CN/SAN
   of the certificate).
4. **Certificate**: paste the **full chain** — leaf first, then intermediates:

   ```text
   -----BEGIN CERTIFICATE-----
   ...leaf...
   -----END CERTIFICATE-----
   -----BEGIN CERTIFICATE-----
   ...intermediate...
   -----END CERTIFICATE-----
   ```

5. **Private Key**: paste the unencrypted key:

   ```text
   -----BEGIN PRIVATE KEY-----
   ...
   -----END PRIVATE KEY-----
   ```

6. **Intermediate Certificate**: optional — only if you kept the chain out of
   the *Certificate* field.
7. **Save**. NPM writes the files under
   `/data/custom_ssl/npm-<id>/{fullchain.pem,privkey.pem}` and reloads nginx.
8. In each proxy host → **SSL** tab → pick the certificate → **Force SSL** ON →
   **Save**.

### 4.3 Verify

```bash
openssl s_client -connect ui.example.com:443 -servername ui.example.com \
  -showcerts </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer -dates
```

A mismatch between **Server Name** and the certificate CN/SAN makes nginx serve
the default vhost, which shows up as `ERR_CERT_COMMON_NAME_INVALID` or a
Cloudflare `526`.

> If Cloudflare's proxy is enabled, set **SSL/TLS → Overview → Full (strict)**
> and keep the origin certificate valid — *Flexible* combined with NPM's
> *Force SSL* produces an endless redirect loop.

---

## Step 5 — ⚠️ Enable `Websockets Support` for Streamlit

**This is the single most common reason a perfectly configured deployment looks
broken.** Streamlit does not poll over HTTP for live updates: the browser opens a
WebSocket to `/_stcore/stream` and keeps it open for the whole session.

If **Websockets Support** is OFF on the `ui.example.com` proxy host, NPM does not
forward the `Upgrade` / `Connection` headers, the handshake fails, and the page
loads but stays on *“Please wait…”* / *“Connecting…”* and finally shows
**“Connection error”**. Nothing appears in the container logs, because the
request never becomes a valid WebSocket — which is why the toggle is easy to
miss.

Checklist:

1. `Hosts → Proxy Hosts → ui.example.com → Edit`.
2. **Details** tab → **Websockets Support** → **ON**.
3. **Save** (nginx reloads automatically; no container restart needed).
4. Hard-reload the dashboard (`Ctrl+Shift+R`).

Verify at the nginx level — the generated vhost must contain the upgrade
headers:

```bash
docker exec nginx-proxy-manager sh -c \
  'grep -n -A2 "Upgrade" /data/nginx/proxy_host/*.conf'
# proxy_set_header Upgrade $http_upgrade;
# proxy_set_header Connection $http_connection;
```

Verify the handshake end to end (expect `101 Switching Protocols`):

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Connection: Upgrade' \
  -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: x3JJHMbDL1EzLkh9GBhXDw==' \
  https://ui.example.com/_stcore/stream
# 101
```

`400` or `200` here means the upgrade headers are still being stripped. Also
check in the browser: **DevTools → Network → WS** — the `_stcore/stream` entry
must be `101`, and **Console** must stay free of reconnect warnings.

Notes:

- Cloudflare proxies WebSockets transparently; there is no Cloudflare-side
  toggle to enable.
- Do not put a caching rule on `ui.example.com/_stcore/*` — a cached or buffered
  stream breaks the session even with the toggle on.
- The API host (`api.example.com`) does not need WebSockets; leave it OFF.

---

## Step 6 — Cloudflare Zero Trust: Email PIN / OTP login

1. Cloudflare dashboard → **Zero Trust**. On first use, pick a **team name**
   (this becomes `<team>.cloudflareaccess.com`) and the free plan.
2. **Settings → Authentication → Login methods**. **One-time PIN** is enabled by
   default and needs no external IdP — Cloudflare emails a PIN to the address
   you type on the login page. Add Google/GitHub SSO here later if you want.
3. **Settings → Custom pages** (optional): put your team name and a contact on
   the Access login page so operators know whom to ask.
4. **Settings → WARP authentication** / device enrolment is not needed for a
   browser-only deployment.

> OTP proves *who* the user is, nothing more. It does not replace Basic Auth
> from Step 3: if Cloudflare Access is ever bypassed via the origin IP, NPM still
> asks for credentials.

---

## Step 7 — Access Application for the UI (Allow policy)

1. **Access → Applications → Add an application → Self-hosted**.
2. **Application configuration**:
   - **Application name**: `agent-orchestrator UI`
   - **Session Duration**: `8 hours` (or `24 hours` for a long-running sprint;
     shorter is safer because the session cookie is the only thing standing
     between the internet and a panel that can launch agents).
3. **Public hostname**:
   - **Subdomain**: `ui`
   - **Domain**: `example.com`
   - **Path**: leave empty (the whole host is protected)
4. **Access policies → Add a policy**:
   - **Policy name**: `operators`
   - **Action**: `Allow`
   - **Configure rules → Include → Emails** — list the operator addresses, or use
     **Emails ending in** `@example.com` for a whole company domain.
   - Optional: **Require → Country** to cut off everything outside your region.
5. **Save**. Access now answers unauthenticated requests with a `302` to
   `https://<team>.cloudflareaccess.com/cdn-cgi/access/login/...`.

Repeat for a second application if the API host should also be identity-gated
(see the next step for why it usually should not be OTP-gated).

---

## Step 8 — Bypass rules for `/api/*` and `/webhook/*`

Automation cannot type a PIN. Two mechanisms exist, and they should be used for
different paths:

| Path | Mechanism | Why |
|---|---|---|
| `/api/*` (e.g. `POST /api/v1/plan/direct`) | **Service Token** (preferred) or **Bypass** | CI/curl must call the gateway without a human |
| `/webhook/*` (GitHub, GitLab, monitoring) | **Bypass** + HMAC signature check in the app | The sender has no Cloudflare identity |

### 8.1 Why the path needs its own Access application

Access policies are scoped to an **application**, not to a path inside one, so a
bypass is expressed as a separate application for the path. Cloudflare evaluates
the most specific matching application first, so `api.example.com/api` wins over
`api.example.com`.

1. **Access → Applications → Add an application → Self-hosted**.
2. **Application name**: `agent-orchestrator API`.
3. **Public hostname**: subdomain `api`, domain `example.com`,
   **Path**: `api` (Cloudflare path matching is a prefix, `/api/v1/plan/direct`
   is covered).
4. **Access policies → Add a policy**:
   - **Policy name**: `service-token-only`
   - **Action**: `Service Auth`
   - **Include → Service Token** → the token created in 8.2
5. **Save**.

### 8.2 Create a service token (recommended for `/api/*`)

1. **Access → Service Auth → Service Tokens → Create Service Token**.
2. **Name**: `orchestrator-ci`, **Duration**: `1 year` (rotate before it
   expires).
3. Copy the **Client ID** and **Client Secret** — the secret is shown once.
4. Store them in the CI secret store, never in the repository:

```bash
export CF_ACCESS_CLIENT_ID='<client-id>.access'
export CF_ACCESS_CLIENT_SECRET='<client-secret>'

curl -s https://api.example.com/health \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET"
# {"status":"ok"}
```

Requests without the headers get `302`/`403` from Cloudflare and never reach
FastAPI.

### 8.3 Pure bypass (for senders that cannot send headers)

For GitHub webhooks or third-party callers, replace the policy action in 8.1
with:

- **Action**: `Bypass`
- **Include → Everyone**

The application is then not identity-checked at all, so **the application itself
must authenticate the caller**. For GitHub that means verifying
`X-Hub-Signature-256` (HMAC-SHA256 of the raw body with the webhook secret) and
rejecting anything that does not match. Keep NPM Basic Auth on that host as a
second lock, or better, restrict the host to the sender's published IP ranges.

> ⚠️ **Scope check.** `agent-orchestrator-lite` exposes only `GET /health` and
> `POST /api/v1/plan/direct`; the GitHub webhook was removed from the Lite
> version. Create the `/webhook/*` bypass **only** if you run the full version or
> your own receiver — an unused bypass rule is an unnecessary hole. Never bypass
> more than the exact path prefix you need, and never bypass `/` on the UI host:
> that host controls agent execution.

### 8.4 Order of checks

```
Cloudflare Access (OTP for ui.example.com, token/bypass for api.example.com)
        ↓
NPM Access List (Basic Auth, per proxy host)
        ↓
FastAPI/Streamlit
```

Both layers are independent. Removing one does not disable the other, which is
the whole point of the design.

---

## Step 9 — End-to-end verification

Run the matrix top to bottom; each row isolates one layer.

| # | Request | Expected result | Layer proven |
|---|---|---|---|
| 1 | `curl -sI https://ui.example.com/` | `302` → `<team>.cloudflareaccess.com` | Cloudflare Access |
| 2 | Same URL with a valid `CF_Authorization` cookie | `401` + `www-authenticate: Basic` | NPM Access List |
| 3 | Same URL with the cookie **and** `-u operator:...` | `200` HTML | full chain |
| 4 | `curl -s https://api.example.com/health` | `302`/`403` | API is not public |
| 5 | Same with `CF-Access-Client-Id`/`-Secret` | `{"status":"ok"}` | service token |
| 6 | WebSocket probe on `/_stcore/stream` | `101` | Websockets Support |
| 7 | `openssl s_client ... -servername ui.example.com` | issuer = your CA, dates valid | custom SSL |

Launch a plan through the protected API:

```bash
curl -s -X POST https://api.example.com/api/v1/plan/direct \
  -H "Content-Type: application/json" \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  -d '{
        "raw_json": "{\"tasks\":[{\"id\":\"step_1\",\"title\":\"Hello\",\"prompt\":\"Create hello.txt containing hi\",\"depends_on\":[],\"engine\":\"openhands\"}]}",
        "auto_approve": true
      }'
# {"status":"triggered","workflow_id":"direct-plan-...","mode":"direct_json_plan",...}
```

If step 1 returns `200` straight away, the Access application is not matching
the hostname (check the public hostname, not just the policy). If step 2 returns
`200` without credentials, the Access List is not attached to the proxy host.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| UI loads but hangs on “Please wait…” / “Connection error”, container logs clean | `Websockets Support` is OFF in NPM | Step 5 — enable the toggle, save, hard-reload |
| `502 Bad Gateway` from NPM | Wrong upstream: `127.0.0.1:8501` while NPM is containerised, or `8085` used inside the Docker network | Use `agent-ui:8501` / `agent-orchestrator:8000`, or Option B |
| `522` / `524` from Cloudflare | Origin unreachable, firewall blocks Cloudflare IPs, or the request takes longer than 100 s | Open `80`/`443` to Cloudflare ranges; keep long agent calls on `/api/*` with a raised proxy timeout |
| `526` invalid SSL certificate | Cloudflare **Full (strict)** but NPM serves a self-signed/expired cert | Import a Cloudflare Origin CA cert (Step 4.1) or switch to Full temporarily |
| Infinite redirect (`ERR_TOO_MANY_REDIRECTS`) | Cloudflare **Flexible** + NPM **Force SSL** | Set SSL mode to **Full (strict)** |
| `403` with a Cloudflare Access page | Email not in the Allow policy, or the OTP expired | Step 7, re-check the policy's *Include* rule |
| Basic Auth prompt loops | Wrong credentials cached, or `Satisfy Any` with a mismatched IP rule | Retest in a private window, set `Satisfy Any` OFF |
| `400` on the `/_stcore/stream` probe | Upgrade headers stripped (WebSockets off) or a proxy/WAF rule rewriting the path | Step 5, then check Cloudflare rules for `_stcore` |
| Cloudflare Access login page never appears | DNS record is not proxied (grey cloud) or Access app hostname typo | Enable the orange cloud, verify the public hostname |
| `curl` works, browser gets `401` on every request | Cloudflare strips/ignores `Authorization` on some plans, or the browser cached a stale realm | Prefer the service token for automation; clear the realm's saved credentials |

Useful logs:

```bash
docker logs --tail 100 nginx-proxy-manager
docker logs --tail 100 agent-orchestrator-lite
docker logs --tail 100 agent-ui
```

---

## Security checklist

- [ ] `BIND_IP` is `127.0.0.1` (Option A) or a private/Tailscale address (Option B) — never `0.0.0.0` on a public host.
- [ ] NPM's admin UI (port `81`) is not exposed publicly; change the default `admin@example.com` / `changeme` immediately.
- [ ] `ui.example.com` is behind Cloudflare Access **and** an NPM Access List.
- [ ] `api.example.com` requires a service token (or a bypass + app-level signature check); `/api/v1/plan/direct` can start agents and must never be anonymous.
- [ ] Only `/api/*` (and `/webhook/*` if actually used) are bypassed — never `/`.
- [ ] Ports `7233`, `8233`, `8000` are not published to the internet.
- [ ] TLS is **Full (strict)**; the origin certificate is valid and tracked in a calendar for renewal.
- [ ] `Websockets Support` is ON for the Streamlit host and absent from cache rules.
- [ ] `GITHUB_TOKEN`, `LLM_API_KEY` and `OPENHANDS_SESSION_API_KEY` live only in `.env`/secret storage, never in a plan, a URL or a log.
- [ ] Cloudflare WAF rate limiting is on for `/api/*` (a plan launch is expensive).
- [ ] Access sessions expire (8–24 h) and service tokens have an expiry date.

---

## Related documentation

- [README.md](../../README.md) — quick start, ports and the JSON plan format.
- [OpenHands and LLM provider setup](../openhands-setup.md) — the `openhands`
  container and provider keys.
- [AGENTS.md](../../AGENTS.md) — the `ExecutionPlan` specification.
- [`docker-compose.yml`](../../docker-compose.yml) and
  [`.env.example`](../../.env.example) — ports, `BIND_IP`, service names.
- [Русская версия](securing-with-npm-and-cloudflare.ru.md)
