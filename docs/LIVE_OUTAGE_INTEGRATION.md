# Live observed outages

The dashboard's outage numbers are forecast products.  They must remain
visually and semantically separate from observed outages.

PowerOutage.us offers a live API and embeddable maps, but its standard API
terms require an API key, prohibit exposing that key in public code, restrict
redistribution/public availability of API content, and forbid scraping.  Do
not call it directly from this GitHub Pages site and do not scrape its map.

Before enabling the observed layer, obtain written PowerOutage.us permission
for this dashboard's intended public/private display and an API credential.
Then deploy a server-side proxy (for example a Cloudflare Worker or AWS
Lambda) that holds the key, refreshes at the licensed cadence, and returns a
small normalized response with `observed_at_utc`, geography, and actual
customers out.  The static site should only read that proxy response; no
credential belongs in `site/` or Git history.

Until then, `site/data/live-outage-status.json` explicitly says the observed
layer is unavailable rather than presenting forecast values as live data.
