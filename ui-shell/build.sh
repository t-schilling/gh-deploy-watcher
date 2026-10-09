#!/usr/bin/env bash
# Builds build/GhDeployWatcher.app (ad-hoc signed). Needs only the Command Line Tools.
# GH_DEPLOY_WATCHER_BUILD_DIR overrides the output directory.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "${here}/.." && pwd)"
out="${GH_DEPLOY_WATCHER_BUILD_DIR:-${repo}/build}"
name="GhDeployWatcher"
app="${out}/${name}.app"

cd "${here}"
swift build -c release >&2
bin_dir="$(swift build -c release --show-bin-path)"

rm -rf "${app}"
mkdir -p "${app}/Contents/MacOS"
cp "${bin_dir}/GhDeployWatcherUI" "${app}/Contents/MacOS/${name}"

cat > "${app}/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>dev.gh-deploy-watcher.ui</string>
    <key>CFBundleName</key><string>GhDeployWatcher</string>
    <key>CFBundleDisplayName</key><string>gh-deploy-watcher</string>
    <key>CFBundleExecutable</key><string>GhDeployWatcher</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>LSMinimumSystemVersion</key><string>12.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsLocalNetworking</key><true/>
    </dict>
</dict>
</plist>
PLIST

codesign --force --sign - "${app}" >&2
echo "${app}"
