#!/usr/bin/env bash

release_forbidden='(^|/)(\.signing|target|out|\.build|DerivedData|sparkle_tools|__pycache__|\.pytest_cache|\.mypy_cache|\.venv|\.venv-train)(/|$)|(^|/)\.DS_Store$|(^|/)[^/]+\.xcarchive(/|$)|\.(p12|p8|pem|key|cer|crt|der|mobileprovision|provisionprofile|dmg)$|\.keychain(-db)?$|\.bin\.gz$|libqidao_core\.a$|appcast\.xml$|exportOptions\.plist$|(^|/)release\.yml$|^QiDao/QiDao/Core(/|$)|(^|/)katago/katago(\.exe)?$'
release_max_bytes=$((50 * 1024 * 1024))
release_mac_user_path='/Users'
release_mac_user_path+='/[^/[:space:]]+'
release_pem_header='-----BEGIN'
release_pem_header+='( [A-Z0-9]+)? PRIVATE KEY-----'
