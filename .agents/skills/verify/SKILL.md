---
name: verify
description: Start a throwaway copy of the whole Clash Lens stack (database, collector, worker, API, website, fake Clash/Google/Discord services) on the `wsl` machine and drive the website in a real browser from here to prove a change works the way a Clasher sees it. Use after changing website pages, account/crew/dashboard flows, or anything a Clasher sees, and whenever asked to "verify", "prove it in the app" or "check it in the browser".
---

# Verify Clash Lens in the real app

This skill proves behavior on the running product, not in unit tests. You copy the working tree to `wsl`, start a fresh stack there with the repo's own `./dev e2e-server`, open an SSH tunnel so `127.0.0.1:5173` here reaches it, drive the website with the repo's Playwright, keep the evidence here, and shut everything down.

It does not replace the end-to-end test that `CLAUDE.md` requires for calculations, jobs, boards and the Reset (`docs/testing.md`). Use both.

## Where things run and why

- **This machine (`ser5ver`)** is short on memory, has no Podman, and holds the checkout, `website/node_modules` (Playwright 1.62.1) and its Chromium. The browser runs here.
- **`wsl`** (Arch Linux, 12 GiB, rootless Podman 6.1) runs the stack. It is an agent playground: installing or upgrading there needs no permission. Its login shell is fish, so always send commands as `ssh wsl bash -s <<'EOF' ... EOF`.
- **Never** run this on the Fedora server (`rogue`); it is production.
- If `podman info` works on the machine you are on, you may skip SSH and the tunnel and run the `wsl` blocks locally from a copy of the repo. Everything else is the same.

Fixed paths:

| What | Where |
| --- | --- |
| Code copy the stack builds from | `wsl:~/clashlens-verify/` |
| Stack log, process ID, source fingerprint | `wsl:~/clashlens-verify-run/` |
| Tunnel control socket | `~/.cache/clashlens-verify-tunnel.sock` (here) |
| Evidence (kept after cleanup) | `~/clashlens-verify-evidence/<UTC time>-<feature>/` (here) |

**One run at a time.** The stack always publishes ports 5173, 8011 and 8012, and the copy lives at one path. If launch finds a running stack or busy ports, stop and report it. Never drive or tear down a stack you did not start.

## Launch

Run from the repository root (or your worktree). Measured on 14 October 2026: ready 153 seconds after start on a first build, 96 seconds with cached images. The running stack uses about 0.8 GiB on `wsl`.

1. **Refuse if busy, then copy the working tree** (tracked and new files, ignoring what `.gitignore` ignores, including uncommitted changes):

   ```sh
   ssh wsl bash -s <<'EOF'
   pid=$(cat ~/clashlens-verify-run/dev.pid 2>/dev/null) && kill -0 "$pid" 2>/dev/null && { echo "BUSY: stack $pid is running"; exit 1; }
   podman pod ps --format '{{.Name}}' | grep '^clashlens-dev-' && { echo "BUSY: a Clash Lens pod exists"; exit 1; }
   ss -ltn | grep -E ':(5173|8011|8012) ' && { echo "BUSY: stack ports in use on wsl"; exit 1; }
   echo free
   EOF
   ss -ltn | grep -E ':(5173|8011|8012) ' && echo "BUSY: ports in use here"
   rsync -a --delete --exclude=.git --filter=':- .gitignore' ./ wsl:clashlens-verify/
   { git rev-parse HEAD; git diff HEAD; git ls-files -o --exclude-standard; } | sha256sum | cut -c1-16 \
     | ssh wsl 'mkdir -p ~/clashlens-verify-run; cat > ~/clashlens-verify-run/source'
   ```

2. **Start a fresh stack.** `e2e-server` always starts from empty data (200 fake Clashers), and removes its pod and data volumes when it gets `SIGTERM`:

   ```sh
   ssh wsl bash -s <<'EOF'
   cd ~/clashlens-verify
   setsid nohup ./dev e2e-server > ~/clashlens-verify-run/dev.log 2>&1 < /dev/null &
   echo $! > ~/clashlens-verify-run/dev.pid
   EOF
   ```

3. **Wait until ready.** Ready means the line `Clash Lens is ready at http://127.0.0.1:5173` is in the log: the database is migrated, the 200 fake Clashers are registered, `#2PP` has been collected, and the player page, fake Google and fake Discord all answer. A healthy `/healthz` alone is not ready. Poll with a background command or Monitor, not a foreground sleep:

   ```sh
   ssh wsl 'grep -q "Clash Lens is ready at" ~/clashlens-verify-run/dev.log && echo ready || tail -3 ~/clashlens-verify-run/dev.log'
   ```

   If the log ends with `dev: ...did not become ready`, read the readiness diagnostics above it, then run Cleanup.

4. **Open the tunnel** so this machine's `127.0.0.1:5173/8011/8012` reach the stack. The ports must match: sign-in redirects name `127.0.0.1:5173` and `127.0.0.1:8011`.

   ```sh
   ssh -M -S ~/.cache/clashlens-verify-tunnel.sock -fN -o ExitOnForwardFailure=yes \
     -L 5173:127.0.0.1:5173 -L 8011:127.0.0.1:8011 -L 8012:127.0.0.1:8012 wsl
   ```

## Doctor

Read-only. Run it after launch and whenever anything looks off. Every line must pass before you drive:

```sh
ssh wsl bash -s <<'EOF'
kill -0 "$(cat ~/clashlens-verify-run/dev.pid)" && echo "stack process: up"
podman pod ps --format '{{.Name}} {{.Status}}' | grep -- '-e2e '
podman ps --format '{{.Names}} {{.Status}}' | grep -- '-e2e-' | grep -v ' Up ' || echo "containers: all up"
echo "source: $(cat ~/clashlens-verify-run/source)"
EOF
echo "here:   $({ git rev-parse HEAD; git diff HEAD; git ls-files -o --exclude-standard; } | sha256sum | cut -c1-16)"
ssh -S ~/.cache/clashlens-verify-tunnel.sock -O check wsl
curl -fsS http://127.0.0.1:5173/healthz && echo
curl -fsS http://127.0.0.1:5173/players/%232PP | grep -c 'Synthetic Clasher 001'
```

- The pod line ends in `Running`, and every container shows `Up`.
- `source` equals `here`. If not, the stack runs other code than yours: Cleanup, then Launch again.
- The tunnel check says `Master running`, `/healthz` answers, and the player page count is at least 1.

## Drive

There is no driver tool. Write a throwaway CommonJS Playwright script in your scratchpad (never in the repo) and run it with the checkout's Playwright. Use the browser tests' handles: roles and accessible names, as in `website/tests/e2e/`.

```sh
NODE_PATH=/home/zubair/code/p/clashlens/website/node_modules node "$SCRATCH/drive.cjs"
```

Skeleton (`$SCRATCH/drive.cjs`):

```js
const { chromium, expect } = require("@playwright/test");
const out = process.env.OUT; // the evidence folder, see Evidence
(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ baseURL: "http://127.0.0.1:5173" });
  await context.tracing.start({ screenshots: true, snapshots: true });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  page.on("console", (m) => m.type() === "error" && errors.push(`console: ${m.text()}`));
  try {
    await page.goto("/players/%232PP");
    await expect(page.getByRole("heading", { name: "Synthetic Clasher 001" })).toBeVisible();
    await page.screenshot({ path: `${out}/01-player.png`, fullPage: true });
    require("fs").writeFileSync(`${out}/01-player.aria.txt`, await page.locator("main").ariaSnapshot());
  } finally {
    await context.tracing.stop({ path: `${out}/trace.zip` });
    require("fs").writeFileSync(`${out}/page-errors.txt`, errors.join("\n"));
    await browser.close();
  }
  if (errors.length) process.exit(1);
})().catch((e) => { console.error(e); process.exit(1); });
```

- **Sign in** with the fake Google: go to `/signup`, click the link `Sign up with Google`. A new identity lands on `/account/setup`; fill `Username` and `Display name`, click `Create account`, which opens `/account/verify-player`. On `/login`, `Sign in with Google` for a new identity shows `No account found` instead. Each browser context is a fresh visitor. For a second Clash Lens account, rewrite the authorize request's `login_hint` to another `fixture-google-subject-NNNN` (four digits), as `website/tests/e2e/account-isolation.spec.ts` does.
- **Link a player:** on `/account/verify-player`, fill `Player tag` and `API token` with `VERIFY-` plus the tag without `#` (for `#2PP`: `VERIFY-2PP`), click `Link player`. The fake game allows only a few verify calls a second; retry for up to 30 seconds.
- **Players:** `#2PP` is `Synthetic Clasher 001`. The stack collects for real while it runs, so numbers change between page loads; assert labels and relationships, not exact trophies.
- **Database side effects** (read-only):

  ```sh
  ssh wsl bash -s <<'EOF'
  P=$(podman pod ps --format '{{.Name}}' | grep -- '-e2e$')
  podman exec "$P-postgres" psql -U clashlens -d clashlens -c "SELECT count(*) FROM players"
  EOF
  ```

Feature recipes live in [`features/`](features/README.md). Read the index first.

## Evidence

Put each run's proof in `OUT=~/clashlens-verify-evidence/$(date -u +%Y%m%dT%H%MZ)-<feature>` (create it first). It is outside the repo and outside anything Cleanup removes.

Keep, at minimum:

- A screenshot and an accessibility snapshot after each action that matters, numbered in order, so the action and its result are both visible.
- `trace.zip` (open with `npx playwright show-trace`) and `page-errors.txt`. A page with a JavaScript error has not passed, even if it looks right.
- For anything that saves data: a second, read-only view of it (reload the page, or the `psql` query above), saved as text.
- `stack.txt`: the source fingerprint and the last 100 lines of the worker and website logs, taken **before** Cleanup:

  ```sh
  ssh wsl bash -s <<'EOF' > "$OUT/stack.txt"
  P=$(podman pod ps --format '{{.Name}}' | grep -- '-e2e$')
  echo "source $(cat ~/clashlens-verify-run/source)"
  for s in worker website api; do echo "[$s]"; podman logs --tail 100 "$P-$s" 2>&1; done
  EOF
  ```

Proof rules:

- Go through the pages the way a Clasher does. Do not call the private API, edit the database, or use routes the browser tests only use as shortcuts.
- The fake Clash, Google, Discord and archive services are the only fakes allowed; they stand where production talks to outside services.
- When a recipe's starting state is missing (for example, a feature needs a Season of history the 200 fake Clashers do not have), report the step as failed with the reason. Do not weaken the recipe to make it pass.

## Cleanup

Always run this, including after a failed launch or drive. It stops only what Launch started, and leaves the evidence folder alone.

```sh
ssh -S ~/.cache/clashlens-verify-tunnel.sock -O exit wsl 2>/dev/null
ssh wsl bash -s <<'EOF'
pid=$(cat ~/clashlens-verify-run/dev.pid 2>/dev/null) || exit 0
kill -TERM "$pid" 2>/dev/null
for i in $(seq 1 90); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
kill -0 "$pid" 2>/dev/null && echo "still running: $pid" || rm -f ~/clashlens-verify-run/dev.pid
podman pod ps --format '{{.Name}}' | grep -- '-e2e$' || echo "pod removed"
podman volume ls --format '{{.Name}}' | grep -- '-e2e-' || echo "volumes removed"
EOF
ls "$OUT"
```

Expect `pod removed`, `volumes removed`, and your evidence files listed. The built images stay on `wsl` on purpose, so the next launch is faster. The code copy in `~/clashlens-verify/` stays too; the next Launch overwrites it.
