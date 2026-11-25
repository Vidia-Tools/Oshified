# Deployment

## Oshified setup

Oshified uses the Open Studio runtime and a separate, pinned Oshified node
pack. It can run on a local ComfyUI installation with model weights on disk.
The hosted build below uses the public Vidia worker image as its base and
loads model weights from Backblaze B2 into the worker's local cache.

### Local development

1. Start ComfyUI v0.25.1 on `127.0.0.1:8188`. Install the Oshified
   node pack at the chosen Git commit and the three custom-node dependencies listed
   in `worker/scripts/package_oshified_release.py` in ComfyUI's
   `custom_nodes` directory. The Oshified pack also registers the inherited
   Vidia node IDs, so use a ComfyUI node directory with a single copy of the
   Vidia/Oshified pack. Install each package's requirements in ComfyUI's
   Python environment. Add the graph's model files to the matching categories in
   ComfyUI's `models` directory. Install FFmpeg, and for YouTube audio install
   [Deno](https://docs.deno.com/runtime/getting_started/installation/) 2.3 or later.
2. Use an app-server Python environment with Pillow, requests and
   websocket-client. Run the local app server from the repository root with operator-provided
   assets. Use absolute paths for a pose reference image and three
   videos at 16 fps: end card, corner watermark, and corner mask.

   ```bash
   COMFY_OUTPUT_DIR=/path/to/ComfyUI/output VIDIA_MODE=local \
   OSHIFIED_POSE_FILE=/path/to/pose.png \
   OSHIFIED_END_CARD_FILE=/path/to/end-card.mp4 \
   OSHIFIED_CORNER_WATERMARK_FILE=/path/to/corner.mp4 \
   OSHIFIED_CORNER_MASK_FILE=/path/to/corner-mask.mp4 \
   python worker/src/app_server.py --host 127.0.0.1 --port 8189
   ```

3. Start the wizard from another shell, using the same origin and port as
   shown. Set `OSHIFIED_LOCAL_FRONTEND_PORT` on the app server if you change the
   frontend port.

   ```bash
   cd frontend
   npm ci
   VITE_OSHIFIED_API_ORIGIN=http://127.0.0.1:8189 npm run dev -- --host 127.0.0.1 --port 3000 --strictPort
   ```

To offer optional examples in the avatar-to-real mode, set
`VITE_OSHIFIED_PRESETS_JSON` to an array of objects with `id` and `name`,
plus any of `imageUrl`, `previewAudio`, and `audioUrl`. Use media you can
share. Give assets same-origin static paths or HTTPS URLs that allow the
app's origin to fetch them. An `audioUrl` can also be a YouTube link.

Local generation runs on the workstation with its local model files and media.
Local jobs are available for the lifetime of the app-server process. The
loopback wizard requires only the local app server and ComfyUI.

To exercise the pipeline without private assets, install Pillow in the local
Python environment, then run `python worker/scripts/make_oshified_demo_media.py /tmp/oshified-demo`
with FFmpeg on `PATH`. Use its `pose.png`, `end-card.mp4`, `corner.mp4`, and
`corner-mask.mp4` as operator assets. Upload `avatar.png` or `natural.png`
and `voice-formant.wav` through the wizard. The generated media is synthetic
and tests the pipeline. The workflow still needs its model weights.

### Build the hosted worker image

Use Docker Buildx on a builder that supports `linux/amd64`. The helper loads
a locally tagged image. The build extends the Linux amd64 digest of
`federicobuilds/vidia-open-studio` pinned in
[`Dockerfile.model-store`](worker/Dockerfile.model-store) and installs the
Oshified node dependencies plus a pinned Deno runtime for YouTube. The
image includes workflow, worker and custom-node source. Model weights are
fetched and checked by the worker on demand from the operator's model store.

Prepare a reviewed release directory containing:

- `manifest.json`, a version 1 model manifest accepted by
  `worker/src/model_assets.py` with relative model paths, object keys, sizes,
  and SHA-256 checksums. The operator's model bucket must contain the objects
  named in this manifest.
- `nodes/` containing reviewed source snapshots of
  `comfyui-videohelpersuite`, `comfyui_controlnet_aux`, and
  `comfyui_essentials`. Retain the corresponding
  upstream source revisions and license notices with the release record.
  The helper adds the OFL notices for the verified Noto Sans Regular and
  Share Tech Mono font binaries. If a dependency snapshot changes its font,
  supply that font's matching `.OFL.txt` beside the `.ttf`.

From the app checkout, provide the separately checked out Oshified nodes
repository and its full release commit SHA:

```bash
python3 worker/scripts/package_oshified_release.py \
  --dependencies /path/to/reviewed-dependency-release \
  --node-repo /path/to/oshified-nodes \
  --node-revision "<full Oshified nodes commit SHA>" \
  --output /path/to/new-oshified-release-context
worker/scripts/build_oshified_worker.sh /path/to/new-oshified-release-context oshified-worker:local
```

The packaging helper exports the commit into the inherited
`vidia-open-studio-node` slot and writes `release-receipt.json` with the
commit, manifest hash, and package content hashes. Compare the receipt
against the one for the release you intend to rebuild. This Docker target
starts the worker and workflows baked from the app checkout. Record the app
commit, node commit, release receipt, and final image digest together when
cutting a release.

### Isolated hosted configuration

Use separate test resources for Cloudflare Pages, the Cloudflare Worker and
its private R2 buckets, the RunPod endpoint, and the Backblaze B2 model bucket.

- **Pages build** from `frontend/` with `npm ci` and `npm run build`. Serve
  `dist/`. Set `VITE_OSHIFIED_API_ORIGIN=https://api.example.com` and
  `VITE_OSHIFIED_TURNSTILE_SITE_KEY` in the build environment or in
  `frontend/.env.production`. Use your own Turnstile site key for the test
  frontend's hostname. Set `VITE_OSHIFIED_SOURCE_URL` to the HTTPS URL for
  the exact corresponding source of the hosted app. The wizard exposes it
  as a Source code link. These Vite values are public.
- **Cloudflare Worker** starts from `backend/wrangler.toml.example`. Set
  `OSHIFIED_API_ORIGIN=https://api.example.com` and configure your approved
  HTTPS media with `OSHIFIED_POSE_URL`, `OSHIFIED_END_CARD_URL`,
  `OSHIFIED_CORNER_WATERMARK_URL`, and `OSHIFIED_CORNER_MASK_URL`. Add the
  frontend to `ALLOWED_ORIGINS`, set `RUNPOD_BASIC_ENDPOINT_ID` to the isolated
  endpoint, and configure the `OSHIFIED_JOBS` Durable Object binding and
  migration plus the private `IMPORTS_BUCKET` and `EXPORTS_BUCKET` bindings.
  Set `JWT_SECRET`, `TURNSTILE_SECRET_KEY`, `RUNPOD_API_KEY`, and
  `RUNPOD_CALLBACK_SECRET` as Worker secrets. `OSHIFIED_JOB_LEASE_MINUTES`
  defaults to 45 and accepts whole minutes from 30 to 120. It covers queue
  time, worker startup, generation, and delivery of the final callback. A
  job retains its configured lease for a clip retry.
- **RunPod worker** uses the staged image digest. Start staging with a
  35-minute execution timeout and a 45-minute application lease. One private
  Montréal H200 cold run took 26.5 minutes before queue and image startup.
  Measure those intervals on the final hosted build and adjust both limits
  before cutover. Set
  `BACKEND_BASE=https://api.example.com` and the matching
  `RUNPOD_CALLBACK_SECRET`. Configure private output uploads with
  `S3_ENDPOINT_URL`, `S3_BUCKET_NAME`, `S3_ACCESS_KEY_ID`, and
  `S3_SECRET_ACCESS_KEY`. Set `S3_REGION_NAME` when required by the
  provider. Point this configuration to the same private exports bucket as
  the backend. The frontend accesses private outputs through the API.
- **Backblaze B2 model store** uses `MODEL_STORE_ENDPOINT`,
  `MODEL_STORE_REGION`, `MODEL_STORE_BUCKET`, `MODEL_STORE_KEY_ID`, and
  `MODEL_STORE_APP_KEY` on RunPod. Use a read-only application key scoped
  to the model bucket/prefix. Configure model-store credentials separately
  from private output-upload credentials.

Keep tokens, credentials, reference media, and model weights outside the
public application checkout. For source publication, review the complete
source set and accompanying node and dependency licenses, notices, and
redistribution permissions. Ship operator paths and configuration examples
with the source.

### Release verification and rollout

Qualify both guided modes using an uploaded image and audio in the isolated
environment. Test the YouTube input separately with an authorized test link.
Verify that a finished portrait reaches the wizard, its output feeds the
clip, and the final media plays and downloads. Exercise clip retry and
access controls. With B2, verify actual model preparation progress on a cold
worker and cache reuse on a warm worker.

Image publication, isolated infrastructure setup, and any production cutover
are separate deployment actions. Production traffic and network-volume
retirement each require their own approval. Record the previously serving
application, worker image, routing, and storage before a cutover so the
operator can restore them.

---

## Vidia Open Studio deployment reference

How to deploy your own hosted instance: Cloudflare Pages (frontend), a
Cloudflare Worker (backend), and a RunPod serverless endpoint (GPU worker).
This reflects the tested deployment contract of the maintainer's live stack.

Local mode (Docker Compose or bare metal, no cloud accounts) is covered in
`worker/README.md` and needs none of this. Note the local app server binds
`127.0.0.1` by default; pass `--host` to `app_server.py` to override.

## Architecture

- **Frontend**: Vite SPA deployed to Cloudflare Pages, git-connected. Build
  config comes from the committed `frontend/.env.production`.
- **Backend**: Cloudflare Worker (`backend/`), itty-router + Durable Objects
  (auth, logs, payments, run tracking, websockets, history) + a KV namespace
  (disposable-email blocklist + rate-limit counters) + two R2 buckets
  (`imports`, `exports`). Git-connected Workers Builds deploy on push.
- **GPU worker**: RunPod serverless endpoint running `worker/Dockerfile`.
  Self-updating: at every cold start the pod clones this repo's `main`
  (`worker/updater.sh` -> `bootstrap.sh` -> `worker/src/start.sh`), refreshing
  boot scripts, `worker/src/`, and `workflows/`. `start.sh` also clones the
  separate custom-node repo (Vidia-Open-Studio-Nodes) onto the network volume.
  Net effect: pushing to `main` deploys worker code instantly, no image rebuild.
- **Nodes repo**: `Vidia-Open-Studio-Nodes` (separate repo), cloned by
  `worker/src/start.sh` at boot (`VIDIA_NODE_REPO_URL` / `VIDIA_NODE_REPO_REF`).

## 1. Frontend (Cloudflare Pages)

Connect the repo to Pages (git-connected build):
- Build command: `npm run build` in `frontend/`
- Build output directory: `dist`

Configuration is the committed `frontend/.env.production` file. Replace its
values with your own before deploying (they are public, not secrets):

```
VITE_API_BASE=<your Worker URL>
VITE_TURNSTILE_SITE_KEY_LOGIN=<your Turnstile site key>
VITE_GA_MEASUREMENT_ID=<your GA4 id, or delete this line>
```

Important: Vite variables set in the Pages dashboard are NOT reliably baked
into git-connected builds. The committed `.env.production` file is the
supported mechanism. For local mode, delete the file (or use
`frontend/.env.local` with `VITE_API_BASE=http://127.0.0.1:8189`).

Other optional VITE_* switches are documented in the frontend section of
`.env.example`.

## 2. Backend (Cloudflare Worker)

The Worker deploys git-connected from `backend/` (Workers Builds runs
`npx wrangler deploy`). Alternatively `cd backend && wrangler deploy`.

### wrangler.toml vars to edit

Edit `backend/wrangler.toml` (see `backend/wrangler.toml.example`):
- `name`, `account_id`
- `ALLOWED_ORIGINS` (comma-separated; include your Pages origins)
- `ADMIN_EMAIL`, `APP_BASE_URL`, `EMAIL_FROM`
- `ACCOUNT_ID` (same Cloudflare account id, used for S3 presign)
- `IMPORTS_BUCKET_NAME` / `EXPORTS_BUCKET_NAME` and their `*_DOMAIN` values
- `RUNPOD_BASIC_ENDPOINT_ID` / `RUNPOD_STANDARD_ENDPOINT_ID` /
  `RUNPOD_PRO_ENDPOINT_ID` (your RunPod endpoint id; may all be the same)
- `VIDIA_PAYMENTS_ENABLED=false` (payments are decorative in v1),
  `STRIPE_PRICE_ID`, `CHECKOUT_RETURN_URL`

Bindings (Durable Objects, KV namespace, R2 buckets) also live in
`wrangler.toml`, not in env vars. Create the KV namespace
(`wrangler kv namespace create DISPOSABLE_EMAIL_DOMAINS`) and put its id in the
`kv_namespaces` block; the Durable Object classes migrate on first deploy.

### Required secrets

Set via `wrangler secret put <NAME>` in `backend/` (or run `./setup.sh` with a
filled root `.env`):

| Secret | Purpose |
|--------|---------|
| `JWT_SECRET` | Session token signing (long random string) |
| `RUNPOD_API_KEY` | Starts serverless jobs (RunPod console) |
| `RUNPOD_CALLBACK_SECRET` | Authenticates worker -> backend callbacks; MUST match the RunPod endpoint env var of the same name |
| `EMAIL_API_KEY` | Resend transactional email |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | R2 API token for presigned upload URLs |
| `TURNSTILE_SECRET_KEY_LOGIN` | Turnstile login widget |
| `TURNSTILE_SECRET_KEY_NEWSLETTER` | Turnstile newsletter widget |
| `MAILERLITE_API_KEY` | Newsletter / user sync (optional) |

## 3. R2 buckets

Create the two buckets:

```bash
wrangler r2 bucket create imports
wrangler r2 bucket create exports
```

Give each a public custom domain (the `*_BUCKET_DOMAIN` vars). The exports
bucket serves finished videos under a public prefix; set an object-lifecycle
expiry rule on it for retention (pure R2 config, no backend code involved).

### Required CORS rules

Browser uploads presign directly against R2, so BOTH buckets need CORS rules
for your Pages origins or uploads fail:

```json
[
  {
    "AllowedOrigins": ["https://<your-pages-site>.pages.dev"],
    "AllowedMethods": ["GET", "PUT", "POST", "HEAD", "DELETE"],
    "AllowedHeaders": [
      "Content-Type", "Content-Length", "Authorization",
      "x-amz-date", "x-amz-content-sha256"
    ],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
]
```

## 4. RunPod serverless endpoint

1. Build and push the worker image: `IMAGE=<registry>/vidia-worker:<tag> ./worker/deploy.sh`
2. Create a serverless endpoint pointing at the image, attach a network volume
   populated with models and nodes (see `worker/README.md` and
   `worker/scripts/fetch_models.py`).
3. Set **Execution Timeout** to `3600` seconds (long video generations).
4. Set endpoint env vars (worker block of `.env.example`):
   - `BACKEND_BASE` and `VIDIA_BACKEND_URL`: your Worker URL. MUST include the
     `https://` prefix; a bare hostname breaks all callbacks.
   - `RUNPOD_CALLBACK_SECRET`: MUST match the Worker secret of the same name.
   - `EXPORTS_DOMAIN`, `S3_PUBLIC_URL_PREFIX`, `S3_ENDPOINT_URL`,
     `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET_NAME=exports`,
     `S3_REGION_NAME=auto`
   - `DEPLOYMENT_MODE=serverless`
5. Put the endpoint id into `backend/wrangler.toml` (`RUNPOD_*_ENDPOINT_ID`)
   and redeploy the Worker.

### Cold-start self-update model

At every cold start the pod clones this repo's `main` and refreshes its boot
scripts, `worker/src/`, and `workflows/` (`updater.sh` -> `bootstrap.sh` ->
`start.sh`), then clones the Nodes repo. Push-to-main therefore deploys worker
code instantly on the next cold start. If a clone fails, the image-baked copies
are used.

For production stability, pin `VIDIA_DOCKER_REPO_REF` and `VIDIA_NODE_REPO_REF`
to a tag or commit SHA in the endpoint env instead of tracking `main`. Forks
set `VIDIA_DOCKER_REPO_URL` / `VIDIA_NODE_REPO_URL` to their own repos; private
forks can set `VIDIA_DOCKER_DEPLOY_KEY` / `VIDIA_NODE_DEPLOY_KEY` to an SSH key
path on the persistent volume.

## 5. Turnstile

Create a Turnstile widget in the Cloudflare dashboard for your Pages domain.
The public site key goes in `frontend/.env.production`
(`VITE_TURNSTILE_SITE_KEY_LOGIN`); the secret key goes in Worker secrets
(`TURNSTILE_SECRET_KEY_LOGIN`, and `TURNSTILE_SECRET_KEY_NEWSLETTER` for the
newsletter widget if used).

## 6. Local mode

No cloud accounts needed. See `worker/README.md` for Docker Compose and
bare-metal setup, and `setup.sh` for secret distribution when moving to hosted
mode later. The local app server (`worker/src/app_server.py`) binds
`127.0.0.1` by default; pass `--host 0.0.0.0` only if you need LAN access
(the Docker Compose setup does this internally for port mapping).

## Verification

- `node scripts/release-check.mjs` guards the release set against secret leaks.
- Smoke-test payloads live in `worker/test-payloads/` (`minimal.json` first,
  then `full-feature.json` with real file URLs).
- Known limitations and deferred items: see `KNOWN_ISSUES.md`.
