<img src="assets/icon.svg" width="64" height="64" alt="">

# canvas-api

An HTTP API for [Canvas LMS](https://canvas.tamu.edu) for schools that don't hand out
API tokens. A headless Chromium stays logged in to Canvas through single sign-on
(Microsoft Entra + MFA). Your requests go through that session, so the whole
[Canvas REST API](https://canvas.instructure.com/doc/api/) and GraphQL work as you.
You can also drive the browser directly.

It runs as a NixOS service (`nixosModules.default`). Expose it only on a private network
such as Tailscale (`openFirewallOnInterfaces = [ "tailscale0" ]`).
Interactive docs: `/docs`. OpenAPI schema: `/openapi.json`.

## Authentication

Send the service key with every request except `/health`:

```sh
export CANVAS_API=http://<vps-tailscale-name>:8787
export CANVAS_API_KEY=$(ssh <vps> canvas-api-key)
curl -H "Authorization: Bearer $CANVAS_API_KEY" $CANVAS_API/session
```

`X-API-Key: <key>` also works. The key is generated on first start at
`/var/lib/canvas-api/api-key`. Add more keys there, one per line, to give each app
its own key. To rotate a key, edit or delete that file; deleting it makes the
service generate a new key on restart.

## Logging in

Your NetID credentials live in the root-only `/etc/canvas-api/credentials.env`.
They never go into Git or the Nix store. Set them once:

```sh
ssh -t <vps> sudo canvas-api-credentials
```

Then start a login and approve it on your phone:

```sh
curl -X POST -H "Authorization: Bearer $CANVAS_API_KEY" $CANVAS_API/session/login
# {"state":"awaiting_mfa","mfa_number":"42","message":"Approve the sign-in in Microsoft Authenticator and enter 42", ...}
curl -H "Authorization: Bearer $CANVAS_API_KEY" "$CANVAS_API/session/login?wait_seconds=60"
# {"state":"logged_in", ...}
```

| `state` | Meaning |
| --- | --- |
| `running` | Going through the SSO pages |
| `awaiting_mfa` | Approve on your phone; enter `mfa_number` if one is shown |
| `awaiting_code` | `POST /session/login/code {"code":"123456"}` |
| `logged_in` | Done |
| `failed` | Wrong password, or a background re-login stopped at MFA |
| `needs_attention` | Unexpected page. Check `GET /browser/screenshot` and finish with the `/browser/*` endpoints |

The login answers "Stay signed in?" with yes, and the browser profile persists in
`/var/lib/canvas-api/profile`. Every 10 minutes the service checks the Canvas session.
When the session expires, the service tries to log in again in the background, and
Microsoft usually skips MFA for a remembered browser. If MFA is required, the
background attempt stops and waits for you. It won't try again for 6 hours, so you
won't get random phone prompts. Run `POST /session/login` yourself when that happens.

For fully unattended logins, add an "Authenticator app" sign-in method on your account
in Microsoft's [Security info](https://mysignins.microsoft.com/security-info) page. Choose
"different authenticator app" to see the secret key, and paste it as the TOTP secret
in `canvas-api-credentials`. The service then types the 6-digit codes itself.

## Canvas API

`/api/...` forwards to `https://canvas.tamu.edu/api/...` with your session and the CSRF
token. Requests use the same paths, query strings, and bodies as the Canvas docs.

```sh
H="Authorization: Bearer $CANVAS_API_KEY"
curl -H "$H" "$CANVAS_API/api/v1/users/self"
curl -H "$H" "$CANVAS_API/api/v1/courses?enrollment_state=active&all_pages=true"
curl -H "$H" "$CANVAS_API/api/v1/users/self/todo"
curl -H "$H" "$CANVAS_API/api/v1/courses/123/assignments?include[]=submission&all_pages=true"
curl -H "$H" -X POST "$CANVAS_API/api/graphql" -H 'content-type: application/json' \
     -d '{"query":"{ allCourses { _id name } }"}'
```

- `all_pages=true` follows `Link: rel="next"` and returns one merged JSON array. It
  stops after `max_pages` pages (default 50); `X-Truncated: true` means more pages remain.
- Without `all_pages`, the `Link` headers are rewritten to point back at this service.
- If the Canvas session is gone, you get `401 {"error":"canvas_session_expired"}`.
- `GET /web/<path>` fetches any other Canvas URL with the session, such as an HTML page,
  `/web/files/123/download`, or `/web/courses/1/modules`. Redirects are followed.

## Browser control

The service has one tab, which the login also uses.

| Endpoint | Body / query |
| --- | --- |
| `GET /browser` | current `url`, `title` |
| `POST /browser/goto` | `{"url": "/courses/123"}` |
| `GET /browser/screenshot` | `?full_page=true`, returns a PNG |
| `GET /browser/content` | `?format=text\|html&selector=main` |
| `POST /browser/click` | `{"selector": "text=Submit"}` |
| `POST /browser/fill` | `{"selector": "#q", "value": "hello"}` |
| `POST /browser/press` | `{"key": "Enter", "selector": "#q"}` |
| `POST /browser/wait` | `{"selector": ".done", "state": "visible"}` |
| `POST /browser/evaluate` | `{"expression": "() => document.title"}` |

Selectors use [Playwright syntax](https://playwright.dev/docs/locators).

## For agents

Point an agent at `$CANVAS_API/openapi.json` and give it the key. The fastest path to
any data is `/api/v1/...` with `all_pages=true`. Use `/browser/*` only for things the
REST API can't do.

## Development

```sh
nix develop          # Linux; includes Chromium for Playwright
pytest
CANVAS_API_STATE_DIR=./state python -m canvas_api   # serves on 127.0.0.1:8787
```

Settings come from environment variables (see `canvas_api/config.py`): `CANVAS_BASE_URL`,
`CANVAS_API_STATE_DIR`, `CANVAS_API_KEY_FILE`, `CANVAS_API_CREDENTIALS_FILE`,
`CANVAS_API_HOST`, `CANVAS_API_PORT`, `CANVAS_API_KEEPALIVE_SECONDS`, and
`CANVAS_API_LOGIN_TIMEOUT_SECONDS`.

## NixOS

```nix
inputs.canvas-api.url = "github:Bean0-0/canvas-api";
# in the system modules:
canvas-api.nixosModules.default
{ services.canvas-api = { enable = true; openFirewallOnInterfaces = [ "tailscale0" ]; keyReaders = [ "admin" ]; }; }
```

The service runs as an unprivileged, sandboxed system user with a 2 GB memory cap.
