# Antonagents docs (Mintlify)

Source for **docs.antonagents.com**, built with [Mintlify](https://mintlify.com).

## Structure

```
docs-site/
├── docs.json                 # Mintlify config: theme, nav, tabs, API spec
├── introduction.mdx          # Docs home
├── quickstart.mdx
├── self-hosting.mdx
├── concepts/                 # agents, routines, connectors, skills, data-sources
├── guides/                   # build-a-routine, chat-and-files, using-the-api
├── api-reference/
│   ├── introduction.mdx
│   └── openapi.json          # /v1 spec — Mintlify auto-generates the API pages
├── logo/                     # light.svg / dark.svg wordmarks
└── favicon.svg
```

## Preview locally

```bash
npm i -g mint          # Mintlify CLI
cd docs-site
mint dev               # serves at http://localhost:3000
```

## Deploy

1. Push this repo to GitHub (already at `antonagents/antonagents`).
2. In the [Mintlify dashboard](https://dashboard.mintlify.com), connect the repo
   and set the docs directory to `docs-site`.
3. Every push to the default branch auto-deploys.

## Custom domain

Point **docs.antonagents.com** at Mintlify:

1. In the Mintlify dashboard → **Settings → Custom domain**, add
   `docs.antonagents.com`.
2. Add the **CNAME** record Mintlify gives you at your DNS provider.

This is independent of the app's domain, so it does **not** affect the existing
`app.over-watch.in` link.

## Regenerating the API reference

The API pages are generated from `api-reference/openapi.json`, a `/v1`-filtered
copy of the app's live OpenAPI spec. To refresh it after API changes:

```bash
# from the app instance
curl -s http://127.0.0.1:8080/openapi.json > /tmp/openapi_full.json
# then re-run the filter step (keep only /v1 paths, add the bearer auth scheme,
# set the server URL) and overwrite api-reference/openapi.json
```

## Notes

- Content is plain MDX — edit the `.mdx` files directly.
- Replace `logo/*.svg` and `favicon.svg` with final brand art when ready.
- Update the base URL in `api-reference/*` and `docs.json` after the
  `antonagents.com` domain cutover.
