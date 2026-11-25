#!/usr/bin/env bash
set -euo pipefail

if [ "$#" != 2 ]; then
    echo "Usage: $0 <model-release-directory> <local-image-tag>" >&2
    exit 2
fi

release_dir="$(cd "$1" && pwd)"
if [ ! -f "$release_dir/release-receipt.json" ]; then
    echo "Expected a context assembled by package_oshified_release.py" >&2
    exit 2
fi

app_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
worker_image='federicobuilds/vidia-open-studio@sha256:5ddc440dc770f96ef93d932f17c787cc31c3cbf4edc4a45bc7d9a6ca90793638'

docker buildx build --load --platform linux/amd64 --target oshified \
    --build-arg "WORKER_IMAGE=$worker_image" \
    --build-context "model_release=$release_dir" \
    --file "$app_dir/worker/Dockerfile.model-store" \
    --tag "$2" "$app_dir"
