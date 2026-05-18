# API Keys Management

**tags:** security, api, en
**source:** https://wiki.example.com/security/api-keys
**anchor:** api-keys

---

API keys are issued per-tenant and rotate every 90 days. Lost keys can
be regenerated from the admin dashboard.

> Treat API keys as secrets. Do not commit them to version control.
> Use environment variables or a secret manager.

The key format is `sk_<env>_<32hex>`, where `<env>` is `live` or `test`.
