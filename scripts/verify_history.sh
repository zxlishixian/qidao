#!/usr/bin/env bash
set -eu

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=release_audit_rules.sh
source "$script_dir/release_audit_rules.sh"

while IFS= read -r commit; do
    while IFS= read -r -d '' entry; do
        metadata=${entry%%$'\t'*}
        file=${entry#*$'\t'}
        read -r _ type object <<< "$metadata"

        if [[ $file =~ $release_forbidden ]]; then
            printf '检测到禁止发布的历史文件（%s）：%q\n' "$commit" "$file" >&2
            exit 1
        fi

        if [ "$type" = commit ]; then
            printf '历史包含 gitlink/submodule（%s）：%q\n' "$commit" "$file" >&2
            exit 1
        fi
        [ "$type" = blob ] || continue

        size=$(git cat-file -s "$object")
        if [ "$size" -ge "$release_max_bytes" ]; then
            printf '历史文件达到或超过 50 MiB（%s）：%q\n' "$commit" "$file" >&2
            exit 1
        fi

        if git cat-file blob "$object" | LC_ALL=C grep -aE -q -- "$release_mac_user_path"; then
            printf '历史文件包含本机绝对路径（%s）：%q\n' "$commit" "$file" >&2
            exit 1
        fi

        if git cat-file blob "$object" | LC_ALL=C grep -aE -q -- "$release_pem_header"; then
            printf '历史文件包含私钥 PEM 头（%s）：%q\n' "$commit" "$file" >&2
            exit 1
        fi
    done < <(git ls-tree -r -z --full-tree "$commit")
done < <(git rev-list HEAD)

echo '历史验收通过'
