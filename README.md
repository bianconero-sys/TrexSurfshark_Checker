# Surfshark Validator — Web Edition

A web dashboard that validates Surfshark cookies and reports the **latest purchased
active product**, with live results and a browser-built ZIP export. Deployable on
Render.com. Web layer by Trex.

## Validation approach (fast, no Playwright)
Auth is cookie-only. The flow mirrors a real browser without the browser:
1. **Preflight** `GET https://my.surfshark.com/account` — the server exchanges the
   `_ssrtk` refresh-token cookie for a fresh access session (Set-Cookie). Without
   this the JSON API returns `401 Invalid JWT Token`.
2. `GET /account/p_api/v1/payment/orders` — every order; each `order.subscription`
   has the product `name`, `status`, `expiresAt`, `recurring`, and billing frequency.
3. `GET /account/p_api/v1/identity/altid/profile` — the primary email.

**Latest purchased active product** = among subscriptions with `status == active`
and `expiresAt` in the future, the one with the most recent `createdAt`.

## Features
- **Dashboard** cards: Checked, Paid Hits, Free/Expired, Invalid, Error, Duplicate.
- **Single Check** + **Batch** (ZIP, multiple files, or pasted text). Cookies as
  Netscape `.txt`, JSON array, or `k=v; k2=v2`. Candidate key: `_ssli` / `_ssrtk`.
- **Email deduplication**; repeated emails flagged and skipped on export.
- **Live Results**: Status, Product, Email, Expires, Recurring. Filters:
  All / Valid / Paid / Free / Invalid / Error / Duplicate.
- **Settings → Export Fields** — every toggle maps to a real field: Email, Product,
  Status, Expires, Recurring, Frequency, Date, Source File, Validation, Reason.
  The Cookie block is always included.
- **Client-side ZIP export** (JSZip) via the native share sheet / blob download —
  reliable on mobile, no server round-trip. Active subs → a folder named after the
  product; expired → `/free/`. Invalid and duplicates are not exported.
- **Proxy support** (http / socks4 / socks5) with **retry + proxy rotation on
  transient errors** (1–8 attempts). Valid/invalid are never retried.
- Futuristic liquid-glass UI, dark / light theme toggle.

## Run locally
```
pip install -r requirements.txt
python app.py            # http://localhost:5000
```

## Deploy on Render.com
Push this folder to a GitHub repo (keep `app.py` and `dashboard.html` together),
then New → Blueprint on Render (uses `render.yaml`). `SECRET_KEY` is auto-generated.

From a cloud host, add residential/mobile proxies in Settings → Proxies (the
`cf_clearance` cookie is IP-bound, so datacenter IPs get Cloudflare-challenged).
For educational / personal use only.
