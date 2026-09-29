#!/usr/bin/env bash
# Build a .deb from the PyInstaller bundle.
# Usage: build_deb.sh VERSION BUNDLE_DIR OUTPUT.deb
set -euo pipefail

version="$1"
bundle="$2"
output="$3"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root_dir="$(cd "$here/../.." && pwd)"
arch="$(dpkg --print-architecture)"
pkg="$(mktemp -d)"
trap 'rm -rf "$pkg"' EXIT

install -d "$pkg/DEBIAN" "$pkg/opt/parquetry" "$pkg/usr/bin" "$pkg/usr/share/mime/packages" \
  "$pkg/usr/share/applications" "$pkg/usr/share/icons/hicolor/scalable/apps"
cp -a "$bundle/." "$pkg/opt/parquetry/"
ln -s /opt/parquetry/parquetry "$pkg/usr/bin/parquetry"
ln -s /opt/parquetry/parquetry-gui "$pkg/usr/bin/parquetry-gui"
install -m 644 "$here/parquetry.desktop" "$pkg/usr/share/applications/parquetry.desktop"
install -m 644 "$here/parquetry-mime.xml" "$pkg/usr/share/mime/packages/parquetry.xml"
install -m 644 "$root_dir/src/parquetry/ui/icon.svg" "$pkg/usr/share/icons/hicolor/scalable/apps/parquetry.svg"

installed_size="$(du -sk "$pkg" | cut -f1)"
cat > "$pkg/DEBIAN/control" <<CONTROL
Package: parquetry
Version: ${version}
Section: science
Priority: optional
Architecture: ${arch}
Installed-Size: ${installed_size}
Maintainer: Parquetry developers <parquetry@users.noreply.github.com>
Homepage: https://github.com/loganrf/parquetry
Depends: libc6 (>= 2.35), libegl1, libgl1, libfontconfig1, libfreetype6, libxkbcommon0, libxkbcommon-x11-0, libdbus-1-3, libx11-xcb1, libxcb-cursor0
Description: Explore, aggregate and export Parquet files
 Parquetry is a desktop application and command line tool for exploring large
 Parquet files: plot parameters against time, compact data with aggregation,
 select time ranges and export them to CSV, and save the processing settings
 for automated batch processing with the "parquetry export" command.
CONTROL

dpkg-deb --root-owner-group --build "$pkg" "$output"
