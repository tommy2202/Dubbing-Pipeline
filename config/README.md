### Configuration split (public vs secrets)

This repo uses **exactly two Python config sources** under `config/`:

- `config/public_config.py` (**committed**): non-sensitive settings with safe defaults
- `config/secret_config.py` (**committed, contains no secrets**): loads secret values from env / `.env.secrets`

A single merged access point lives at `config/settings.py`:

- `from config.settings import SETTINGS`

### Env files

- **`.env`**: optional non-sensitive overrides (do not put secrets here)
- **`.env.secrets`**: local secrets (never commit)

### Quickstart

Create `.env` from the example:

```bash
cp .env.example .env
```

Create `.env.secrets` (required for real deployments; tests/dev can run with dev defaults):

```bash
cp .env.secrets.example .env.secrets
# edit values
```

### Secret validation

By default, the repo preserves current behavior by allowing dev-insecure secret defaults
to keep local dev + tests working.

To enforce “real secrets required”, set:

```bash
export STRICT_SECRETS=1
```

Then missing/unsafe secrets (e.g. default `JWT_SECRET`) will raise a clear error at startup.

