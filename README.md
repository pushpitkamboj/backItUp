# BackItUp

BackItUp is a free, Apache-2.0 hosted GitHub backup and recovery application. People use one web dashboard. BackItUp runs all capture and recovery work on its servers; it has no user-facing CLI or customer-installed worker.

Every snapshot is stored in BackItUp Cloud, using an operator-managed Cloudflare R2 bucket. Users manage backup and recovery only through the dashboard; they never connect a bucket or provide storage credentials.

## What the current implementation does

- Captures bare Git data without checking out or running repository code, then verifies refs and object integrity.
- Packages a signed, versioned `.tar.zst` snapshot; publishes the completion marker last.
- Captures selected GitHub metadata to JSON archives, with explicit coverage status for unsupported or unavailable categories.
- Uses a PostgreSQL queue with leases, heartbeats, retries, recovery priority, workspace concurrency, quota reservations, retention, and interruption reconciliation.
- Restores Git refs into a new private GitHub repository after destination preflight; GitHub Actions remains disabled. Metadata remains a download in V1.
- Provides a React dashboard for login, onboarding, repository selection, storage checks, schedules, history, snapshot downloads, recovery review, progress, reports, and notification activity.

The GitHub, Supabase, and R2 integrations need real operator credentials before they can be exercised. The core archive/restore and object-publication tests run entirely locally.

## Local development

Requirements: Python 3.12+, Node 22+, Docker, Git, and optionally `git-lfs` for LFS backups.

```sh
uv sync --all-groups
npm --prefix frontend ci
cp .env.example .env
uv run python scripts/bootstrap_dev.py
docker compose up -d postgres
uv run alembic upgrade head
```

Start each trusted server process in a separate terminal:

```sh
uv run uvicorn backitup.api:app --app-dir backend --host 127.0.0.1 --port 8000
uv run python -m backitup.scheduler
BACKITUP_WORKER_NAME=local-worker uv run python -m backitup.worker
npm --prefix frontend run dev
```

Open `http://127.0.0.1:5173`. The API health endpoint is `http://127.0.0.1:8000/health` and interactive API documentation is at `http://127.0.0.1:8000/docs`.

Alternatively, after creating `.env` and the local `secrets/` files, run `docker compose up --build`. Run the migration once with `docker compose run --rm api alembic upgrade head`.

`scripts/bootstrap_dev.py` creates only local development keys with mode `0600`; it never overwrites existing key files. It is an operator/developer bootstrap step, not a user backup CLI.

> Upgrading from a pre-managed-storage build: the schema migration refuses to run if customer-managed profiles remain, rather than making their snapshots unrecoverable. Migrate those snapshots into BackItUp Cloud first; do not bypass the migration guard.

## Configure live integrations

Copy `.env.example` to `.env` and set the values below. Keep `.env` and `secrets/` out of Git.

1. Create a Supabase project. Enable GitHub OAuth under Auth, set the callback URL to `https://YOUR_DASHBOARD/auth/callback`, and fill `BACKITUP_SUPABASE_URL`, `BACKITUP_SUPABASE_PUBLIC_URL`, `BACKITUP_SUPABASE_PUBLISHABLE_KEY`, and `BACKITUP_SUPABASE_JWKS_URL`. Use the publishable key (`sb_publishable_...`) in the dashboard and backend. Do not put the Supabase secret key in this application; it bypasses RLS and is not needed.
2. Create a read-only **source GitHub App**. Configure its OAuth callback as `https://YOUR_API/api/v1/github/source/callback`. Supply its ID, slug, client credentials, webhook secret, and private key at `secrets/source-app.pem`. Install it only on selected repositories.
3. Create a separate **recovery GitHub App** with its callback at `https://YOUR_API/api/v1/github/recovery/callback`, a private key at `secrets/recovery-app.pem`, and only the permissions needed to create a private repository and write contents/workflows. It must be installed on the destination account or organization.
4. Configure the BackItUp Cloud R2 account, bucket, and scoped access key values. These are required in production and remain server-side.

GitHub must be able to reach `BACKITUP_PUBLIC_URL`; `localhost` is only sufficient for dashboard layout work. Use a temporary HTTPS tunnel or a deployed staging URL for OAuth callbacks and webhooks.

## Recovery behavior

| Captured data | V1 result |
| --- | --- |
| Git history, ordinary branches, and tags | Restored with original object IDs; destination refs are compared exactly. |
| Default branch | Restored when it exists in the snapshot. |
| LFS data | Uploaded and verified if captured and the destination authorizes it. |
| Workflow files | Restored in Git while GitHub Actions remains disabled. |
| Issues, PRs, comments, reviews, labels, milestones, releases, and settings | Downloadable metadata archives. |
| Wiki and release assets | Downloadable archives when captured. |

Recovery always targets a fresh private repository. A collision, a missing permission, an unexpected destination ref, or a changed checkpoint stops the run with an actionable failure instead of overwriting data.

## Verification

```sh
uv run ruff check backend scripts tests
uv run pytest -q
npm --prefix frontend run build
```

The local test suite covers Git capture/package/extract/ref restoration, hostile archive-path rejection, completion-marker ordering, manifest signatures, and corruption detection. Live GitHub and cloud-storage tests are intentionally opt-in and remain unverified until disposable credentials are configured.

## Security boundaries

- Application tables use the private `backitup` PostgreSQL schema; browsers only call FastAPI.
- Supabase access tokens are validated server-side. Every tenant resource is filtered by workspace ownership.
- GitHub authorization tokens are Fernet-encrypted before persistence; the encryption key is outside the database. BackItUp Cloud credentials stay in the service secret store and are never persisted per workspace.
- Object paths, providers, archive members, redirects, Git remotes, and artifact sizes are validated. Error messages redact provider output and credentials.
- BackItUp Cloud enforces 5 GB per workspace and 100 GB globally, including in-flight reservations.

Infrastructure provisioning and deployment are deliberately outside this repository.
