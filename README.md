# status.concrete.ch

Public uptime board for Concrete App GmbH. A GitHub Actions job checks each
component every five minutes and publishes one JSON file. GitHub Pages serves
the site. There is no separate server and no paid status vendor.

## How a check works

`components.yml` is the only place components are defined. The monitor
workflow (`.github/workflows/monitor.yml`) runs on a five-minute cron and on
manual dispatch. For each HTTP component it:

1. sends a GET and follows redirects
2. records the final status code and the latency
3. marks it **up** (2xx, or a code listed in `up_statuses`, in under 3 seconds),
   **degraded** (slower than 3 seconds, a 4xx, or a 2xx missing `expect_body`),
   or **down** (timeout, connection error, or 5xx)
4. retries once before a down result is kept

A down component opens an incident. The incident closes, with start, end, and
duration, when the component is up or degraded again.

Third-party rows read public status APIs (Atlassian Statuspage, or the
Firebase and Google Cloud incident feeds). They are shown on the page and do
not change the banner. The banner is computed only from Concrete's own checks:
all up is "Alle Systeme funktionieren", any degraded check or a single
non-website outage is "Teilweise Störung", and the website down or two or more
components down is "Grössere Störung".

## Where the data lives

Results are not committed to `main`. Each run rewrites `status.json` and
force-pushes it as the single commit on the `status-data` branch, so the
repository does not grow by one commit per check. The file itself keeps:

- the current status of every component
- a daily uptime percentage for 90 days
- raw checks for the last 48 hours
- incidents, dropped 90 days after they close

The page reads that file from
`https://raw.githubusercontent.com/concrete-app/status/status-data/status.json`
and refetches it every 60 seconds. It also polls `version.json` every 60
seconds and when the tab becomes visible, and reloads itself when a new Pages
deploy changes the version.

## Add a component

Edit `components.yml`. An HTTP check:

```yaml
- id: beispiel
  name: Beispiel
  description: Kurze Bezeichnung
  url: https://beispiel.concrete.ch/
  expect_body: Beispiel
```

`id` must be unique. `expect_body` is an optional substring that a healthy
response must contain. `up_statuses` overrides which status codes count as
healthy (the ÖREB function answers 400 when called without an EGRID, and that
is the healthy response). `slow_ms` overrides the 3 second degraded threshold.

An upstream status page:

```yaml
- id: github
  name: GitHub
  provider: statuspage          # or firebase, or gcp
  counts_overall: false
  status_url: https://www.githubstatus.com/api/v2/status.json
  link: https://www.githubstatus.com
```

`firebase` and `gcp` also need a `products` list. Google Cloud incidents are
ignored unless they affect `europe-west4`, `europe-west6`, or are global.
Only `https` URLs are accepted, and private or link-local addresses are rejected.

Push the change to `main`. The next monitor run picks it up. To check
immediately, run the Monitor workflow with "Run workflow".

These production names are intentionally not checked yet:

| Address | Why |
| --- | --- |
| `wissensdatenbank.concrete.ch` | Named in `packages/entitlements/src/hosts.ts`, but it has no DNS. The live host is `knowledgebase.concrete.ch`. |
| `assistent.concrete.ch` | No DNS. `modules.ts` lists no environment where a backend exists. |
| `widget.concrete.ch` | No DNS. The chat widget is beta-only and is not a public product host. |
| `concrete.ch/launcher/` | The route exists in concrete-core. Production currently serves the marketing homepage for that path. Anmeldung (`/login/`) is the live hub check. |
| `app.concrete.ch` | CNAME to `planerhub-d731e.web.app`. It serves the same site as `concrete.ch`. |
| Fastly | Fronts `concrete.ch` (Firebase Hosting). `fastlystatus.com` returns 403 to this monitor. |
| Cloudflare | Not used. DNS is at Openprovider. |

## DNS

At Openprovider, on the `concrete.ch` zone (nameservers `ns1.openprovider.nl`,
`ns2.openprovider.be`, `ns3.openprovider.eu`):

| Field | Value |
| --- | --- |
| Type | CNAME |
| Name / host | `status` |
| Value / target | `concrete-app.github.io` |
| TTL | 3600, or the panel default |

Zone-file form:

```
status.concrete.ch.   CNAME   concrete-app.github.io.
```

GitHub Pages serves project sites from `https://<org>.github.io/<repo>` until
the custom domain is attached. The `site/CNAME` file sets the custom domain to
`status.concrete.ch` on every Pages deploy. After the CNAME resolves, turn on
**Enforce HTTPS** in the repository Pages settings if the certificate is ready
and the API step below has not already done it. Do not point the record at
`concrete-app.github.io/status`.

## Repository settings a person has to confirm

The workflows request the permissions they need. GitHub still has a few
one-time switches:

1. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
   The Pages workflow uses `actions/configure-pages`, `actions/upload-pages-artifact`,
   and `actions/deploy-pages`.
2. **Settings → Pages → Custom domain** `status.concrete.ch`, then **Enforce HTTPS**
   once the certificate has been issued. Creating the DNS record above is required
   before the certificate can be issued.
3. **Settings → Actions → General → Workflow permissions: Read and write.**
   The monitor force-pushes `status-data`. The workflow token needs contents
   write, and `status-data` must not be a protected branch.
4. Scheduled runs start only after this workflow is on the default branch.
   Until then, run **Monitor** and **Pages** by hand from the Actions tab.

If the Pages API is available, the same settings can be applied with:

```bash
gh api --method POST repos/concrete-app/status/pages -f build_type=workflow
gh api --method PUT repos/concrete-app/status/pages \
  -f build_type=workflow \
  -f cname='status.concrete.ch' \
  -F https_enforced=true
```

`https_enforced=true` fails until DNS points at GitHub and a certificate exists.
Set the CNAME first, wait for the certificate, then enforce HTTPS.

## Local check

```bash
python3 -m pip install pyyaml
python3 -m unittest test_statuslib
# from scripts/, or:
(cd scripts && python3 -m unittest test_statuslib)

python3 scripts/monitor.py --out site/status.json
python3 -m http.server 4173 --directory site
```

Open `http://127.0.0.1:4173/`. On localhost the page reads `site/status.json`.
`--push` updates the `status-data` branch.
