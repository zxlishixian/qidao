#!/usr/bin/env bash
set -eu

source_root=$(cd "$(dirname "$0")/../.." && pwd)
test_root=$(mktemp -d /private/tmp/qidao-release-audits.XXXXXX)
trap 'rm -rf "$test_root"' EXIT

new_repo() {
    repo=$test_root/$1
    git init -q -b main "$repo"
    git -C "$repo" config user.email test@example.invalid
    git -C "$repo" config user.name 'Release Audit Test'
    mkdir -p "$repo/scripts" "$repo/vision/models"
    cp "$source_root/scripts/verify_repository.sh" "$repo/scripts/"
    cp "$source_root/scripts/verify_history.sh" "$repo/scripts/"
    if [ -f "$source_root/scripts/release_audit_rules.sh" ]; then
        cp "$source_root/scripts/release_audit_rules.sh" "$repo/scripts/"
    fi
    cp "$source_root/vision/models/board_locator.onnx" "$repo/vision/models/"
    cp "$source_root/vision/models/intersection_classifier.onnx" "$repo/vision/models/"
    cp "$source_root/vision/models/vision_models.json" "$repo/vision/models/"
    printf 'safe\n' > "$repo/README.md"
    git -C "$repo" add README.md scripts vision/models
    git -C "$repo" commit -qm initial
}

expect_repository_pass() {
    description=$1
    if ! output=$(cd "$repo" && scripts/verify_repository.sh 2>&1); then
        printf 'FAIL (%s): expected repository pass\n%s\n' "$description" "$output" >&2
        exit 1
    fi
    printf 'PASS: %s\n' "$description"
}

expect_repository_fail() {
    description=$1
    expected=$2
    if output=$(cd "$repo" && scripts/verify_repository.sh 2>&1); then
        printf 'FAIL (%s): expected repository rejection\n%s\n' "$description" "$output" >&2
        exit 1
    fi
    if ! printf '%s\n' "$output" | grep -F -q -- "$expected"; then
        printf 'FAIL (%s): missing diagnostic %s\n%s\n' "$description" "$expected" "$output" >&2
        exit 1
    fi
    printf 'PASS: %s\n' "$description"
}

expect_history_pass() {
    description=$1
    if ! output=$(cd "$repo" && scripts/verify_history.sh 2>&1); then
        printf 'FAIL (%s): expected history pass\n%s\n' "$description" "$output" >&2
        exit 1
    fi
    printf 'PASS: %s\n' "$description"
}

expect_history_fail() {
    description=$1
    expected=$2
    if output=$(cd "$repo" && scripts/verify_history.sh 2>&1); then
        printf 'FAIL (%s): expected history rejection\n%s\n' "$description" "$output" >&2
        exit 1
    fi
    if ! printf '%s\n' "$output" | grep -F -q -- "$expected"; then
        printf 'FAIL (%s): missing diagnostic %s\n%s\n' "$description" "$expected" "$output" >&2
        exit 1
    fi
    printf 'PASS: %s\n' "$description"
}

stage_fixture() {
    relative=$1
    mkdir -p "$(dirname "$repo/$relative")"
    printf 'fixture\n' > "$repo/$relative"
    git -C "$repo" add -- "$relative"
}

new_repo clean_repository
expect_repository_pass 'clean repository and exact model set'

fixture_number=0
for relative in \
    profiles/test.mobileprovision \
    profiles/test.provisionprofile \
    certificates/test.cer \
    certificates/test.crt \
    certificates/test.der \
    katago/katago \
    katago/katago.exe \
    qidao-core/out/generated.swift \
    QiDao/QiDao/Core/generated.swift \
    nested/.DS_Store \
    nested/.venv/data \
    nested/__pycache__/data \
    exportOptions.plist \
    release.xcarchive/data \
    weights.bin.gz
do
    fixture_number=$((fixture_number + 1))
    new_repo "forbidden_$fixture_number"
    stage_fixture "$relative"
    expect_repository_fail "repository rejects $relative" '禁止发布的文件'
done

new_repo repository_gitlink
gitlink_object=$(git -C "$repo" rev-parse HEAD)
git -C "$repo" update-index --add --cacheinfo "160000,$gitlink_object,vendor/dependency"
expect_repository_fail 'repository rejects a gitlink' 'gitlink/submodule'

new_repo extra_model
printf 'not an allowed model\n' > "$repo/vision/models/extra.onnx"
git -C "$repo" add vision/models/extra.onnx
expect_repository_fail 'repository rejects an extra ONNX model' '只能包含两份指定 ONNX'

new_repo missing_model_metadata
python3 - "$repo/vision/models/vision_models.json" <<'PY'
import json
import pathlib
import sys

manifest_path = pathlib.Path(sys.argv[1])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
manifest.pop("license", None)
manifest.pop("trainingScript", None)
manifest.pop("syntheticData", None)
manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
PY
git -C "$repo" add vision/models/vision_models.json
expect_repository_fail 'repository requires model provenance metadata' '模型验收失败'

new_repo history_unusual_name
mkdir -p "$repo/odd
directory"
printf 'ordinary content\n' > "$repo/odd
directory/file	name.txt"
git -C "$repo" add -- "odd
directory/file	name.txt"
git -C "$repo" commit -qm 'add unusual safe name'
expect_history_pass 'history audit is self-safe and NUL-safe'

new_repo historical_forbidden_name
stage_fixture 'profiles/test.mobileprovision'
git -C "$repo" commit -qm 'add forbidden filename'
git -C "$repo" rm -q -- profiles/test.mobileprovision
git -C "$repo" commit -qm 'remove forbidden filename'
expect_history_fail 'history rejects a forbidden ancestor filename' '禁止发布的历史文件'

new_repo historical_large_blob
dd if=/dev/zero of="$repo/large.bin" bs=1048576 count=50 2>/dev/null
git -C "$repo" add large.bin
git -C "$repo" commit -qm 'add exact-limit blob'
git -C "$repo" rm -q large.bin
git -C "$repo" commit -qm 'remove exact-limit blob'
expect_history_fail 'history rejects a 50 MiB ancestor blob' '达到或超过 50 MiB'

new_repo historical_macos_path
mac_user_path='/Users'
mac_user_path+='/example-name/private/model.bin'
printf '%s\n' "$mac_user_path" > "$repo/config.txt"
git -C "$repo" add config.txt
git -C "$repo" commit -qm 'add private macOS path'
printf 'clean\n' > "$repo/config.txt"
git -C "$repo" commit -qam 'remove private macOS path'
expect_history_fail 'history rejects a generic macOS user path' '本机绝对路径'

new_repo historical_private_key
pem_header='-----BEGIN'
pem_header+=' OPENSSH PRIVATE KEY-----'
printf '%s\n' "$pem_header" > "$repo/credentials.txt"
git -C "$repo" add credentials.txt
git -C "$repo" commit -qm 'add private-key header'
printf 'clean\n' > "$repo/credentials.txt"
git -C "$repo" commit -qam 'remove private-key header'
expect_history_fail 'history rejects a private-key PEM header' '私钥 PEM 头'

new_repo forbidden_gitlink_path_order
gitlink_object=$(git -C "$repo" rev-parse HEAD)
git -C "$repo" update-index --add --cacheinfo "160000,$gitlink_object,qidao-core/out/dependency"
git -C "$repo" commit -qm 'add forbidden-path gitlink'
expect_history_fail 'history checks forbidden paths before object type' '禁止发布的历史文件'

new_repo history_gitlink
gitlink_object=$(git -C "$repo" rev-parse HEAD)
git -C "$repo" update-index --add --cacheinfo "160000,$gitlink_object,vendor/dependency"
git -C "$repo" commit -qm 'add gitlink'
expect_history_fail 'history explicitly rejects a gitlink' 'gitlink/submodule'

new_repo head_only_not_all
git -C "$repo" switch -qc local-audit
mac_user_path='/Users'
mac_user_path+='/branch-only/private'
printf '%s\n' "$mac_user_path" > "$repo/branch-only.txt"
git -C "$repo" add branch-only.txt
git -C "$repo" commit -qm 'add private path on non-HEAD branch'
git -C "$repo" switch -q main
expect_history_pass 'history intentionally excludes non-HEAD branches'

printf 'ALL RELEASE AUDIT TESTS PASSED\n'
