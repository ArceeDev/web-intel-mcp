# Web Intel MCP

Agent-ready web intelligence as a pay-per-call MCP server. Licensing and credits are handled by
[KeyFlint](https://keyflint.dev) (offline Ed25519 license check + credit ledger), used here only as the billing backend. Failed fetches are refunded
automatically; invalid input is rejected before any debit.

| Tool | Credits | Returns |
|---|---|---|
| `read_url` | 1 | Main content as clean Markdown (nav, footer, cookie banners removed) |
| `page_metadata` | 1 | Title, description, canonical, lang, robots, Open Graph, JSON-LD types, H1-H3, image alt stats |
| `extract_links` | 1 | Deduplicated internal / external links with anchor text |
| `tech_stack` | 2 | Frameworks, CMS, analytics, payments, CDN/hosting, security headers |
| `seo_audit` | 3 | 0-100 score, A-F grade, 16 weighted checks, top 5 fixes |
| `compare_seo` | 3 x URLs | Ranks 2-5 pages (you vs competitors); only fetched URLs are billed |
| `pricing`, `credits_balance` | free | Price list, remaining credits |

Prices are overridable: `WEB_INTEL_PRICES="seo_audit=5,read_url=2"`.

## Install

All you need is your KeyFlint license (`LIC-...`). The gateway URL, its public key and the checkout link have defaults.

**Claude Desktop, one click:** download `web-intel-mcp-X.Y.Z.mcpb` from the [latest release](https://github.com/ArceeDev/web-intel-mcp/releases/latest), double-click it and paste your license. No terminal, no JSON.

**Claude Code:**

```bash
claude mcp add web-intel -e KEYFLINT_LICENSE=LIC-... -- uvx web-intel-mcp
```

**Cursor, Windsurf, Claude Desktop (JSON):**

```json
{
  "mcpServers": {
    "web-intel": {
      "command": "uvx",
      "args": ["web-intel-mcp"],
      "env": { "KEYFLINT_LICENSE": "LIC-..." }
    }
  }
}
```

Optional: `KEYFLINT_URL`, `KEYFLINT_PUBLIC_KEYS` (fetched from the gateway and cached in `~/.keyflint/keyring.json` when unset), `KEYFLINT_CHECKOUT_URL`.

## Hosted mode (sellers)

Paid logic stays on your server. One process bills many buyers; each
request carries the buyer's license.

```bash
docker build -t web-intel .
docker run -p 8080:8080 \
  web-intel
```

Buyers connect to `https://<host>/mcp` (Streamable HTTP, stateless) with
`Authorization: Bearer LIC-...` (or `X-License-Key`). Missing or forged licenses get `-32060`.

## Billing flow per call

1. URL validated (http/https only). Bad input: error, nothing charged.
2. Offline Ed25519 license check (`keyflint-verify`), cached 60 s.
3. `POST /api/v1/quota/deduct` with a fresh idempotency key. Out of credits: `-32062` with the checkout link.
4. Fetch + analysis. If anything fails: `POST /api/v1/quota/refund` (same key), the client gets `isError`.

## Safety

Requests to private, loopback and link-local addresses are refused (checked on every redirect hop),
responses are capped at 3 MB (`WEB_INTEL_MAX_BYTES`), 15 s timeout (`WEB_INTEL_TIMEOUT`), max 5 redirects.
`WEB_INTEL_ALLOW_PRIVATE=1` disables the address check (tests only).

## Tests

End-to-end with a fake KeyFlint quota gateway, locally signed licenses and a local test site
(no network needed):

```bash
pip install -e ".[test]"
python -m pytest -q
```

## License

Business Source License 1.1. Source available; production use needs a KeyFlint license (or must be non-commercial). Each version becomes Apache 2.0 four years after release. See [LICENSE](LICENSE).
