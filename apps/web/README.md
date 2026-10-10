# Loci web

React/Vite frontend for public object browse, spatial annotations, linked video, transcripts, citations and sharing. Follow the root README for the supported seeded Docker demonstration on localhost:8080.

For frontend development use Node 22, `npm ci --ignore-scripts`, and `npm run dev`. Vite binds loopback by default. Configure VITE_API_BASE_URL to point to your local API, then use `npm test`, `npm run build -- --outDir dist-ci`, and `npm run test:browser`. The browser regression uses synthetic public response fixtures; `npm run test:demo` checks the actual running seeded demo. See the root CONTRIBUTING.md for full test requirements.

The production build writes `/sitemap.xml` with the public homepage and object browser, and adds its URL to `robots.txt`. The default `VITE_PUBLIC_SITE_URL` is `http://localhost:8080` for the local demo; set it to the deployed site origin when building a public deployment. The generic sitemap contains only those two public entry points.
