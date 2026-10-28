#!/bin/sh
# Build the HAMA provider image locally (works from any folder).
#   sh build.sh                                  -> hama-provider:local and hama-provider:<version> for this machine
#   sh build.sh --platform linux/amd64           -> another architecture (needs Docker buildx + QEMU/binfmt for foreign CPUs)
#   sh build.sh --save                           -> also write dist/hama-provider-<version>-<arch>.tar (docker load -i …)
# Release images for 'docker pull' are built and pushed by GitHub Actions (.github/workflows/release.yml).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
PROVIDER=$(cd "$HERE/../.." && pwd)
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$PROVIDER/hama_provider/__init__.py")
PLATFORM=""
SAVE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --platform) PLATFORM="$2"; shift 2 ;;
    --save) SAVE=1; shift ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
done

if [ -n "$PLATFORM" ]; then
  docker buildx build --platform "$PLATFORM" --load --build-arg VERSION="$VERSION" \
    -f "$HERE/Dockerfile" -t hama-provider:local -t "hama-provider:$VERSION" "$PROVIDER"
else
  docker build --build-arg VERSION="$VERSION" -f "$HERE/Dockerfile" -t hama-provider:local -t "hama-provider:$VERSION" "$PROVIDER"
fi
echo "Built hama-provider:$VERSION (also tagged hama-provider:local). Use it with HAMA_IMAGE=hama-provider:local in .env."

if [ -n "$SAVE" ]; then
  ARCH=$(docker image inspect "hama-provider:$VERSION" --format '{{.Architecture}}')
  mkdir -p "$PROVIDER/../dist"
  OUT="$PROVIDER/../dist/hama-provider-$VERSION-$ARCH.tar"
  docker save -o "$OUT" "hama-provider:$VERSION"
  echo "Saved $OUT (load it elsewhere with: docker load -i $(basename "$OUT"))"
fi
