# Domain Listing Watch

Public dashboard for a .si domain portfolio: Dynadot marketplace prices, listings and nameservers for 1,352 names.

- `check.py` runs every 6 hours in GitHub Actions (`.github/workflows/check.yml`). It reads each name's Dynadot listing through the official API (read-only key stored as the `DYNADOT_API_KEY` repository secret) and its public nameservers via dns.google, then commits any changes.
- `index.html` is the dashboard, served by GitHub Pages. It reads `domains.json`, `changes.json` and `checks.json`.
- A name leaving Dynadot or getting new nameservers is a signal to check, not proof of a sale.
