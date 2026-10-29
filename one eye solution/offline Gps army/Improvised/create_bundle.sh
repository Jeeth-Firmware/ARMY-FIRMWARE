#!/bin/bash

set -e

# Check version argument
if [ -z "$1" ]; then
  echo "Usage: $0 vX.X.X"
  exit 1
fi

VERSION=$1
BUNDLE_DIR="bundle-$VERSION"
ZIP_NAME="$BUNDLE_DIR.zip"

echo "📦 Creating bundle for version: $VERSION"

# 1. Create version folder
mkdir -p "$BUNDLE_DIR"

# 2. Copy all necessary files
cp -r facial_tracking "$BUNDLE_DIR/"
cp -r ota "$BUNDLE_DIR/"
cp -r systemd "$BUNDLE_DIR/"
cp -r setup "$BUNDLE_DIR/"
cp *.py "$BUNDLE_DIR/"
cp requirements.txt "$BUNDLE_DIR/"

# 3. Create zip file
zip -r "$ZIP_NAME" "$BUNDLE_DIR"

# 4. Clean up
rm -rf "$BUNDLE_DIR"

echo "✅ Bundle created: $ZIP_NAME"
