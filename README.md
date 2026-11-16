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

The browser has a built-in **virtual security key**. Any passkey it registers is saved to
`/var/lib/canvas-api/passkeys.json` (mode 0600) and loaded again on every start. Register
that key on your account once, and logins need no stored password and no phone:

- **Microsoft passkey**: sign-in uses "Sign-in options → Face, fingerprint, PIN or security
  key", which needs no username and no password.
- **Duo security key**: when Duo prompts, the service picks "Security key" and the virtual
  key answers. Microsoft still asks for a password first.

### One-time setup

1. Sign in once, passing your login in the request so it is used for that attempt only
   and never stored. Approve the Duo prompt on your phone.

   ```sh
   curl -X POST -H "Authorization: Bearer $CANVAS_API_KEY" -H 'content-type: application/json' \
        -d '{"username":"netid@tamu.edu","password":"..."}' $CANVAS_API/session/login
   curl -H "Authorization: Bearer $CANVAS_API_KEY" "$CANVAS_API/session/login?wait_seconds=90"
   ```
2. With `/browser/goto` and `/browser/screenshot`, open Microsoft's
   [Security info](https://mysignins.microsoft.com/security-info) page or Duo's device
   management portal. Add a security key or passkey there. The registration prompt goes to
   the virtual key, which answers it automatically.
3. Check that it was kept: `GET /passkey` lists the saved keys by `rpId`.

To revoke the box's access, delete that security key on the Microsoft or Duo page.

The keys are backed up automatically. Every time the service rewrites `passkeys.json`,
root merges it into `/var/backups/canvas-api/passkeys.json`. The backup is root-only and
add-only: it never removes a key, and it keeps each key's highest sign counter and known
`rpId`. On every service start, root merges that backup back into the live file. If the
live file is ever emptied or damaged, the key comes back on its own.

To retire a key for good, delete it on the Microsoft or Duo page first. Then remove it from
both files with `sudo` while the service is stopped.

### Login status

```sh
curl -X POST -H "Authorization: Bearer $CANVAS_API_KEY" $CANVAS_API/session/login
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
When it has expired, the service logs in again in the background with the passkey. If no
passkey is saved, it uses stored credentials instead. If a background attempt hits a phone
prompt, it stops and waits 6 hours before trying again.

### Stored credentials (optional)

You only need these if you use the Duo security-key path, which still needs a password,
or if you don't use a passkey. They go in the root-only `/etc/canvas-api/credentials.env`:

```sh
ssh -t <vps> sudo canvas-api-credentials
```

## Endpoints for apps and agents

These return compact JSON with plain-text bodies, absolute URLs, and the ids you need for
the next call. They're built on the same internal endpoints the Canvas web app itself
uses. Raw access is still available under `/api/...` (see below).

| Endpoint | What it returns |
| --- | --- |
| `GET /todo?days=14` | `due`: upcoming planner items (assignments, quizzes, discussions) with submitted/graded/missing flags. `missing`: overdue work. `announcements`: recent posts |
| `GET /courses` | Active courses with current scores (`?all=true` adds past ones) |
| `GET /grades` | Current score and grade per course |
| `GET /announcements?days=14&course_id=` | Announcements as text |
| `GET /assignments?course_id=&bucket=` | Assignments and quizzes across courses with your status. `bucket` is one of `upcoming`, `past`, `overdue`, `unsubmitted`, `ungraded`, `undated`, `future` |
| `GET /courses/{id}/assignments/{aid}` | Description text, rubric, allowed types and extensions, attempts, and your submission (files, comments, history) |
| `GET /courses/{id}/modules` | Modules and items. Each item's `content` is this API's endpoint for reading it |
| `GET /courses/{id}/pages/{slug}` | Page text and links (file links include a `download` path) |
| `GET /courses/{id}/discussions/{tid}` | Discussion prompt and threaded replies |
| `GET /files/{id}`, `GET /files/{id}/download` | File info and bytes |
| `GET /quizzes?course_id=` | Quizzes (Classic and New) with score, attempts, and a `review` link |
| `GET /courses/{id}/quizzes/{qid}/review?version=` | Your answers per question, with correctness and comments where the instructor shows them |
| `POST /courses/{id}/assignments/{aid}/submit` | Submit text, a URL, or files. **Previews unless `confirm` is true** |
| `POST /courses/{id}/assignments/{aid}/comment` | Comment on your submission (`confirm` required) |
| `POST /courses/{id}/discussions/{tid}/reply` | Reply to a discussion (`confirm` required) |
| `GET /browser/network?contains=` | Internal API calls the browser tab made. Use it to discover endpoints |

### Submitting

```sh
# preview (nothing is submitted): checks type, extensions, attempts, lock, and lateness
curl -H "$H" -F files=@report.pdf "$CANVAS_API/courses/123/assignments/456/submit"
# submit for real
curl -H "$H" -F files=@report.pdf -F confirm=true -F comment="Final version" \
     "$CANVAS_API/courses/123/assignments/456/submit"
# JSON works too: {"body": "<p>answer</p>", "confirm": true}, {"url": "https://..."},
# or {"files": [{"name": "a.pdf", "content_base64": "..."}]}
```

A preview returns `warnings`, for example when the submission is late or replaces an
earlier attempt. Problems such as a wrong file type, no attempts left, or a locked
assignment return `422` and nothing is sent.

### Quizzes

The quiz review works for Classic Quizzes, even in courses that hide the Quizzes tab.
Questions appear only when the instructor shows results; otherwise you get the score and
`results_visible: false`. New Quizzes (`kind: new_quiz`) report scores. Their review runs
in a separate Instructure app; to see what it calls, open the quiz with
`/browser/goto` and read `/browser/network`.

### Finding internal endpoints

```sh
curl -X DELETE -H "$H" $CANVAS_API/browser/network
curl -X POST -H "$H" -H 'content-type: application/json' -d '{"url":"/courses/123/grades","wait_until":"networkidle"}' $CANVAS_API/browser/goto
curl -H "$H" "$CANVAS_API/browser/network?contains=/api/"   # then call them via /api/...
```


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
