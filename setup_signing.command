#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SIGNING_DIR="$PROJECT_DIR/.signing"
KEYCHAIN_PATH="$SIGNING_DIR/QiDaoLocal.keychain-db"
PASSWORD_FILE="$SIGNING_DIR/keychain-password"
IDENTITY_NAME="QiDao Local Code Signing"

echo "WARNING: this creates a persistent CA:TRUE/keyCertSign code-signing trust root"
echo "in a dedicated QiDao keychain and adds that keychain to your user search list."
echo "Read README.md for the exact cleanup procedure before continuing."

mkdir -p "$SIGNING_DIR"
chmod 700 "$SIGNING_DIR"

if [ ! -f "$PASSWORD_FILE" ]; then
    openssl rand -hex -out "$PASSWORD_FILE" 32
    chmod 600 "$PASSWORD_FILE"
fi
IFS= read -r QIDAO_KEYCHAIN_PASSWORD < "$PASSWORD_FILE"

if [ -f "$KEYCHAIN_PATH" ] && security find-identity -v -p codesigning "$KEYCHAIN_PATH" | grep -Fq "$IDENTITY_NAME"; then
    echo "QiDao local signing identity is ready."
    exit 0
fi

if [ -e "$KEYCHAIN_PATH" ]; then
    echo "签名钥匙串已存在但没有有效身份：$KEYCHAIN_PATH"
    echo "请先备份或移走它，再重新运行本脚本。"
    exit 1
fi

SIGNING_TEMP_DIR="$(mktemp -d /private/tmp/qidao-signing.XXXXXX)"
cleanup() {
    find "$SIGNING_TEMP_DIR" -type f -delete 2>/dev/null || true
    rmdir "$SIGNING_TEMP_DIR" 2>/dev/null || true
}
trap cleanup EXIT

PRIVATE_KEY="$SIGNING_TEMP_DIR/qidao-local.key.pem"
CERTIFICATE="$SIGNING_TEMP_DIR/qidao-local.cert.pem"
PKCS12_FILE="$SIGNING_TEMP_DIR/qidao-local.p12"

openssl req -new -newkey rsa:3072 -x509 -sha256 -days 3650 -nodes \
    -subj "/CN=$IDENTITY_NAME/O=QiDao Local Development/OU=macOS Code Signing" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,digitalSignature,keyCertSign" \
    -addext "extendedKeyUsage=critical,codeSigning" \
    -keyout "$PRIVATE_KEY" \
    -out "$CERTIFICATE"

export QIDAO_P12_PASSWORD="$QIDAO_KEYCHAIN_PASSWORD"
openssl pkcs12 -export \
    -legacy \
    -inkey "$PRIVATE_KEY" \
    -in "$CERTIFICATE" \
    -name "$IDENTITY_NAME" \
    -passout "env:QIDAO_P12_PASSWORD" \
    -out "$PKCS12_FILE"
unset QIDAO_P12_PASSWORD

security create-keychain -p "$QIDAO_KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"
security set-keychain-settings -lut 21600 "$KEYCHAIN_PATH"
security unlock-keychain -p "$QIDAO_KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"

# Trust evaluation only discovers certificates in the user's keychain search
# list. Preserve every existing keychain and add the QiDao keychain once.
CURRENT_KEYCHAINS=()
while IFS= read -r KEYCHAIN_LINE; do
    KEYCHAIN_ITEM="${KEYCHAIN_LINE//\"/}"
    KEYCHAIN_ITEM="${KEYCHAIN_ITEM#"${KEYCHAIN_ITEM%%[![:space:]]*}"}"
    if [ -n "$KEYCHAIN_ITEM" ] && [ "$KEYCHAIN_ITEM" != "$KEYCHAIN_PATH" ]; then
        CURRENT_KEYCHAINS+=("$KEYCHAIN_ITEM")
    fi
done < <(security list-keychains -d user)
security list-keychains -d user -s "$KEYCHAIN_PATH" "${CURRENT_KEYCHAINS[@]}"

security import "$PKCS12_FILE" \
    -k "$KEYCHAIN_PATH" \
    -P "$QIDAO_KEYCHAIN_PASSWORD" \
    -T /usr/bin/codesign
security set-key-partition-list \
    -S apple-tool:,apple: \
    -s \
    -k "$QIDAO_KEYCHAIN_PASSWORD" \
    "$KEYCHAIN_PATH"
security add-trusted-cert \
    -r trustRoot \
    -p codeSign \
    -k "$KEYCHAIN_PATH" \
    "$CERTIFICATE"

if ! security find-identity -v -p codesigning "$KEYCHAIN_PATH" | grep -Fq "$IDENTITY_NAME"; then
    echo "本地代码签名身份创建失败。"
    exit 1
fi

echo "Created local QiDao signing identity in $KEYCHAIN_PATH"
