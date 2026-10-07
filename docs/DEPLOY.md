# Deploying Roadie on Render (free Docker web service)

The demo runs from the public repo as a Docker web service. The image holds code only: no Qloo data, no gallery, no
tests and no key. Private gallery files arrive as Secret Files and the Qloo key as an environment variable.

Free-instance facts (as reported by Render; check your dashboard): about 512 MB RAM and a fraction of a CPU, it
sleeps after 15 minutes without traffic (the first request afterwards takes a while; the page shows "Waking the
server"), and the filesystem is ephemeral. Consequences: the gallery files are copied in again at every start,
and the live monthly counter (`live_budget.json`) and the 24 hour cache reset when the instance restarts. The
per-day, per-hour and per-run limits still bound spend, and the Qloo key itself is capped by the organizers.

## Render steps

1. In the Render dashboard choose **New > Web Service** and connect the public Roadie repository (or **New > Blueprint**
   to use `render.yaml`, which sets the values below for you).
2. Choose the **Docker** runtime (Dockerfile at the repo root) and the **Free** plan.
3. Environment variables (Environment tab):

   | Variable | Value |
   |---|---|
   | `QLOO_API_KEY` | your hackathon key, typed into the dashboard only (the blueprint declares it with `sync: false`) |
   | `ROADIE_LIVE` | `1` |
   | `ROADIE_LIVE_UNTIL` | `2026-11-16` |
   | `ROADIE_GLOBAL_SEARCHES_PER_DAY` | `50` |
   | `ROADIE_TRUST_PROXY` | `1` (Render puts a proxy in front) |
   | `QLOO_BASE_URL` | `https://hackathon.api.qloo.com` |
   | `QLOO_TRUSTED_BASE_URL` | `https://hackathon.api.qloo.com` |

   Do not set `PORT`; Render sets it and the container listens on it (default 8000).
4. **Secret Files** (Environment tab): add the five gallery files, one per comedian, named `<slug>.json` where the slug
   matches `[a-z0-9_]{1,40}` (for example `my_comic.json`). The combined limit is 1 MB per service. Never commit
   these files. Assumption: for Docker services Render exposes Secret Files under `/etc/secrets/`. The container reads
   that folder by default; if your service shows them elsewhere set `ROADIE_SECRETS_DIR` to that path.
5. Deploy. The log shows one line: `roadie: gallery files copied: N` (a count, never file names).
6. Check `https://<your-service>.onrender.com/api/health`. Expect `status: ok`, `live_enabled: true`,
   a number for `live_budget_remaining` and `gallery_count: 5`. `gallery_count: 0` means the Secret Files were not found
   (the page then says the gallery is unavailable; nothing crashes). Then open `/` and the gallery.

The key is read from `QLOO_API_KEY` at run time only. It is never written to disk by the entrypoint, never printed and
never part of the image. Live mode ends on its own after `ROADIE_LIVE_UNTIL`; the gallery keeps working.

## Build and run the image locally

Run these from the repository root. `./secrets-demo` stands in for Render's Secret Files folder: put your private
`<slug>.json` files in it (outside git; the folder is not part of the repo). With the key already exported in your shell:

```
docker build -t roadie .

docker run --rm -p 8000:8000 \
  -e QLOO_API_KEY \
  -e ROADIE_LIVE=1 -e ROADIE_LIVE_UNTIL=2026-11-16 \
  -v "$PWD/secrets-demo:/etc/secrets:ro" \
  roadie
```

`-e QLOO_API_KEY` with no value passes the variable from your shell, so the key never appears in the command line or
shell history. Leave out `-e QLOO_API_KEY` and the live flags to run the gallery only. Then open
http://127.0.0.1:8000/ and check `curl http://127.0.0.1:8000/api/health` (the image has no curl; run that on your host).
To use a different port inside the container add `-e PORT=9000 -p 9000:9000`.

Do not type the key as a literal value on the command line, and do not bake a key into an image, an `ENV` line or a build argument.

## Image contents and entrypoint
The `Dockerfile` (Python 3.12 slim plus Node 22, with `@qloo/qloo-harness` 0.1.26 installed globally, the backend without
dev extras, `frontend/` and `scripts/`; no tests, data or keys) runs as a non-root user and starts
`scripts/docker-entrypoint.sh`. The entrypoint copies `<slug>.json` files (names matching `^[a-z0-9_]{1,40}\.json$` only,
copies not symlinks) from `$ROADIE_SECRETS_DIR` (default `/etc/secrets`) into `$ROADIE_DATA_DIR/gallery` (default
`/app/data/gallery`), prints only a count, then runs one uvicorn worker on `$PORT` (default 8000, `0.0.0.0`). With no
secrets folder it starts anyway; the gallery is then empty and the page says it is unavailable. The image has a
`HEALTHCHECK` (python, no curl) on `/api/health`. `render.yaml` is a free-plan Docker blueprint; `QLOO_API_KEY` is
`sync: false` (entered in the dashboard, never in the repo).

Assumption: Render exposes Secret Files to Docker services under `/etc/secrets/` (hence the default; override with
`ROADIE_SECRETS_DIR`).

## What was verified
- The image was built and run locally with a 0.5 CPU and 512 MB limit. A real live search and a full live plan ran
  inside it against the hackathon server. That live run took 133 seconds with the calls run serially; the pool of
  concurrent workers was added afterwards, and its effect on a small CPU has not been measured.
- `qloo config set base-url https://hackathon.api.qloo.com` runs non-interactively without a key and records `base_url`
  and `trusted_base_url` in the harness config. The harness reads `QLOO_BASE_URL` and `QLOO_TRUSTED_BASE_URL` and trusts
  the endpoint when both name the hackathon URL; the entrypoint sets both as overridable defaults and runs `config set`
  with the key removed from its environment, discarding its output. The package declares Node >= 22.19.
- Not yet verified: Render's Secret Files path (assumption above) and Render's proxy hop count (see Notes).
- The key is read from `QLOO_API_KEY` at run time only; it is never written to disk, printed or logged by Roadie.

## Notes
- `ROADIE_TRUST_PROXY=1` takes the visitor address from `X-Forwarded-For`, counting **from the right end**: a trusted
  proxy appends the address it saw, so the entries on the left are whatever the client sent and are never used.
  `ROADIE_TRUSTED_PROXY_HOPS` (default `1`) is how many proxies sit in front of the app: 1 reads the last entry, 2 the
  second from the end. A header with fewer entries than hops, or an entry that is not an IP address, falls back to the
  direct connection address. Only set it behind a proxy that appends to that header itself; directly exposed, a client
  could choose its own address and dodge the per-IP limits (the global daily and monthly limits still hold).
- **The hop count is unverified for Render's proxy.** `render.yaml` does not set it, so the default of 1 applies. After
  the first deployment, check it: temporarily log only the number of comma-separated entries in the header
  (`len(header.split(","))`), never the addresses, send one request from your own machine, and confirm the count is 1
  (so hop count 1 is right). If Render adds a hop the count is 2: set `ROADIE_TRUSTED_PROXY_HOPS=2`. Remove the
  temporary logging afterwards. If the check shows every visitor sharing one address, the hop count is too low or the
  header is missing; the per-IP limit then behaves as a second global limit, which is safe but strict.
- Live runs use `ROADIE_LIVE_WORKERS` concurrent Qloo calls (default 4, maximum 6). A serial run took 133 seconds in the
  0.5 CPU test; the speedup from workers on a small CPU has not been measured. Setting it to 1 makes runs serial; the
  4-per-second pace and the 20-call cap hold either way.
- The entrypoint log distinguishes three secrets-folder cases: `gallery files copied: N` (readable),
  `... the secrets folder is missing` or `... is empty`, and `the secrets folder exists but is not readable by this user`.
  The last one means the mount permissions do not let the container user (`roadie`) list the folder; fix the mode or owner of the secret files, not the app.
- One uvicorn worker keeps the memory use inside the free instance.
