# Offline deployment

On an internet-connected build host with the same CPU architecture:

```bash
./scripts/offline-pack.sh artifacts/offline
```

The resulting folder contains pinned/container-built images, application source/configuration, an installer, and SHA-256 hashes. Scan and approve the bundle before transferring it through the customer's controlled media process. No `.env`, API key, customer file, database dump, PBIX, or build cache is included.

On the isolated Linux server, run `./install.sh`, copy `env.template` to `application/.env`, generate/insert site-specific secrets, validate the configuration, and start Compose. Cloud AI remains unavailable; deterministic processing and Superset continue normally. A local AI model can be added only as a separately approved offline image and uncommitted LiteLLM overlay.

For upgrades, build a new versioned bundle and follow the backup/rehearsal process in `operations.md`. Retain the previous image archive until rollback acceptance is complete.
