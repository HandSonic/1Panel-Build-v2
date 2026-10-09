#!/usr/bin/env bash
# Keep validated architecture artifacts even when a sibling fails; never publish.
set -euo pipefail
: "${BUILD_VERSION:?}" "${BUILD_ARCHES:?}" "${CNB_COMMIT:?}" "${CNB_BUILD_ID:?}" "${CNB_PIPELINE_ID:?}"
rm -f -- cnb-verified-artifacts
source build-inputs.env
BUILD_ARCHES=$(python3 -c 'import sys;sys.path.insert(0,"scripts");from validate_artifacts import architectures;print(" ".join(architectures(sys.argv[1])))' "$BUILD_ARCHES")
[[ ! -e dist && ! -e cnb-shards && ! -e cnb-verified-artifacts ]]
export BUILD_REPOSITORY_COMMIT="$CNB_COMMIT"
docker build -f Dockerfile --target prepared-builder \
  --build-arg VERSION="$BUILD_VERSION" --build-arg SOURCE_COMMIT="$SOURCE_COMMIT" \
  --build-arg INSTALLER_REF="$INSTALLER_REF" --build-arg GO_VERSION="$GO_VERSION" \
  --build-arg NODE_VERSION="$NODE_VERSION" --build-arg BUILD_REPOSITORY_COMMIT="$CNB_COMMIT" \
  -t "1panel-prepared:$CNB_COMMIT" .
mkdir dist cnb-shards
accepted=()
failed=0
for arch in $BUILD_ARCHES; do
  mkdir "cnb-shards/$arch"
  if docker run --rm -e "TARGET_ARCHES=$arch" \
      -v "$PWD/cnb-shards/$arch:/out" --entrypoint /bin/bash "1panel-prepared:$CNB_COMMIT" \
      -euo pipefail -c 'cd /opt/1Panel; bash /opt/build-tools/scripts/build_release.sh; cp dist/* /out/' \
      && python3 scripts/validate_artifacts.py "cnb-shards/$arch" "$BUILD_VERSION" "$arch"; then
    cp "cnb-shards/$arch/1panel-$BUILD_VERSION-linux-$arch.tar.gz"* dist/
    accepted+=("$arch")
    printf '%s / %s: verified\n' "$BUILD_VERSION" "$arch"
  else
    printf '%s / %s: failed; artifact omitted; see this branch output above\n' "$BUILD_VERSION" "$arch" >&2
    failed=1
  fi
done
if ((${#accepted[@]})); then
  python3 scripts/package_release.py finalize . "$BUILD_VERSION" "${accepted[@]}"
  cp build-inputs.env dist/build-inputs.env
  python3 scripts/validate_artifacts.py dist "$BUILD_VERSION" "${accepted[*]}"
  printf '%s:%s:%s' "$CNB_BUILD_ID" "$CNB_PIPELINE_ID" "$CNB_COMMIT" > cnb-verified-artifacts
else
  echo 'No validated architecture output is available.' >&2
  exit 1
fi
# A partial run remains failed. CNB failStages preserves the validated subset.
exit "$failed"
