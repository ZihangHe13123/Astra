#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  printf '%s\n' 'AstraMacComputerHelper can only be built on macOS.' >&2
  exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
package_root="$project_root/native/macos-computer-helper"
output_root="$project_root/.astra/bin"
if [[ "$#" -gt 0 ]]; then
  if [[ "$#" -ne 2 || "$1" != "--output-root" || "$2" != /* ]]; then
    printf '%s\n' 'Usage: build_macos_computer_helper.sh [--output-root /absolute/directory]' >&2
    exit 1
  fi
  output_root="$2"
fi
bundle_name="AstraMacComputerHelper.app"
output_bundle="$output_root/$bundle_name"
production_source="$package_root/Sources/AstraMacComputerHelper"
compatibility_source="$project_root/config/macos_computer_compatibility.json"

if rg -n 'CGWarpMouseCursorPosition|CGSyntheticInputPoster|legacyForegroundActionAllowed' "$production_source"; then
  printf '%s\n' 'Legacy global input capability remains in production source.' >&2
  exit 1
fi

# Global events are confined to the explicitly guarded foreground emitter.
if rg -n '\.post\(tap:' "$production_source" --glob '!CGForegroundInputPoster.swift'; then
  printf '%s\n' 'Global input outside the foreground emitter.' >&2
  exit 1
fi

python3 - "$project_root" "$compatibility_source" <<'PY'
import sys
from pathlib import Path

project_root, compatibility_source = map(Path, sys.argv[1:])
sys.path.insert(0, str(project_root))
from agent.runtime.macos_computer_compatibility import _load_cells

_load_cells(compatibility_source)
PY

swift build -c release --package-path "$package_root" --product AstraMacComputerHelper
swift build -c release --package-path "$package_root" --product AstraVirtualCursorSidecar
binary_directory="$(swift build -c release --package-path "$package_root" --show-bin-path)"
helper_binary="$binary_directory/AstraMacComputerHelper"
sidecar_binary="$binary_directory/AstraVirtualCursorSidecar"

if [[ ! -f "$helper_binary" ]]; then
  printf '%s\n' "Release helper binary is missing: $helper_binary" >&2
  exit 1
fi
if [[ ! -f "$sidecar_binary" ]]; then
  printf '%s\n' "Release cursor sidecar binary is missing: $sidecar_binary" >&2
  exit 1
fi

umask 077
mkdir -p "$output_root"
staging_bundle="$(mktemp -d "$output_root/.${bundle_name}.staging.XXXXXX")"
backup_bundle=""

cleanup() {
  if [[ -n "$staging_bundle" && -e "$staging_bundle" ]]; then
    rm -rf -- "$staging_bundle"
  fi
}
trap cleanup EXIT

chmod 700 "$staging_bundle"
mkdir -p "$staging_bundle/Contents/MacOS" "$staging_bundle/Contents/Resources"
chmod 700 "$staging_bundle/Contents" "$staging_bundle/Contents/MacOS" "$staging_bundle/Contents/Resources"
install -m 700 "$helper_binary" "$staging_bundle/Contents/MacOS/AstraMacComputerHelper"
install -m 700 "$sidecar_binary" "$staging_bundle/Contents/MacOS/AstraVirtualCursorSidecar"
compatibility_resource="$staging_bundle/Contents/Resources/macos_computer_compatibility.json"
install -m 600 "$compatibility_source" "$compatibility_resource"

python3 - "$project_root" "$compatibility_resource" <<'PY'
import sys
from pathlib import Path

project_root, compatibility_resource = map(Path, sys.argv[1:])
sys.path.insert(0, str(project_root))
from agent.runtime.macos_computer_compatibility import _load_cells

_load_cells(compatibility_resource)
PY

cmp "$compatibility_source" "$compatibility_resource"

for production_binary in "$staging_bundle/Contents/MacOS/AstraMacComputerHelper" "$staging_bundle/Contents/MacOS/AstraVirtualCursorSidecar"; do
  for forbidden_marker in 'ASTRA_COMPUTER_HELPER_TEST_MODE' 'ASTRA_MACOS_COMPUTER_E2E' '__test_' 'astra.button' 'astra.secure' 'Astra Computer Fixture' 'AstraMacComputerE2EHarness'; do
    if /usr/bin/grep -aFq -- "$forbidden_marker" "$production_binary"; then
      printf '%s\n' "Production executable unexpectedly contains test marker: $forbidden_marker ($production_binary)" >&2
      exit 1
    fi
  done
done

plist="$staging_bundle/Contents/Info.plist"
plutil -create xml1 "$plist"
plutil -replace CFBundleIdentifier -string 'com.astra.computer-helper' "$plist"
plutil -replace CFBundleExecutable -string 'AstraMacComputerHelper' "$plist"
plutil -replace CFBundlePackageType -string 'APPL' "$plist"
plutil -replace CFBundleShortVersionString -string '1.0' "$plist"
plutil -replace CFBundleVersion -string '1' "$plist"
plutil -replace LSUIElement -bool YES "$plist"
plutil -replace NSScreenCaptureUsageDescription -string 'Astra uses screen recording to capture an approved application window.' "$plist"
# The calendar mode runs as its own responsible process, so macOS shows these texts when it asks.
for calendar_key in NSCalendarsFullAccessUsageDescription NSCalendarsUsageDescription; do
  plutil -replace "$calendar_key" -string 'Astra reads your calendars and, when you approve, adds events to them.' "$plist"
done
for reminders_key in NSRemindersFullAccessUsageDescription NSRemindersUsageDescription; do
  plutil -replace "$reminders_key" -string 'Astra reads your reminders and, when you approve, adds or completes them.' "$plist"
done

# Record build-time provenance inside the signed bundle; never infer it from
# the caller's checkout when status is requested later.
python3 - "$project_root" "$compatibility_resource" "$staging_bundle/Contents/Resources/build-info.json" <<'BUILD_INFO_PY'
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

project_root, registry, output = map(Path, sys.argv[1:])
try:
    revision = subprocess.check_output(
        ["git", "-C", str(project_root), "rev-parse", "HEAD"], text=True,
        stderr=subprocess.DEVNULL,
    ).strip()
    dirty = bool(subprocess.check_output(
        ["git", "-C", str(project_root), "status", "--porcelain", "--untracked-files=normal"],
        text=True, stderr=subprocess.DEVNULL,
    ).strip())
except (OSError, subprocess.CalledProcessError):
    revision, dirty = "unknown", True
output.write_text(json.dumps({
    "helper_git_revision": revision,
    "helper_source_dirty": dirty,
    "compatibility_registry_sha256": hashlib.sha256(registry.read_bytes()).hexdigest(),
    "build_id": str(uuid.uuid4()),
}, sort_keys=True) + "\n", encoding="utf-8")
BUILD_INFO_PY

# Inspect mode markers without running a daemon or requesting permissions.
for appshot_mode in '--appshot-daemon' '--appshot-client-identity' '--appshot-broker-identity'; do
  if ! /usr/bin/grep -aFq -- "$appshot_mode" "$staging_bundle/Contents/MacOS/AstraMacComputerHelper"; then
    printf '%s\n' 'Production helper is missing an Appshot CLI mode.' >&2
    exit 1
  fi
done
if [[ "$(plutil -extract LSUIElement raw -o - "$plist")" != "true" ]]; then
  printf '%s\n' 'Production helper must remain LSUIElement.' >&2
  exit 1
fi

codesign --force --deep --sign - --identifier 'com.astra.computer-helper' "$staging_bundle"
codesign --verify --deep --strict "$staging_bundle"

helper_symbols="$(nm -u "$staging_bundle/Contents/MacOS/AstraMacComputerHelper")"
python3 "$project_root/scripts/check_macos_computer_helper_symbols.py" <<<"$helper_symbols"

if [[ -e "$output_bundle" ]]; then
  backup_bundle="$(mktemp -d "$output_root/.${bundle_name}.backup.XXXXXX")"
  rmdir "$backup_bundle"
  mv -- "$output_bundle" "$backup_bundle"
fi
mv -- "$staging_bundle" "$output_bundle"
staging_bundle=""

if [[ -n "$backup_bundle" ]]; then
  rm -rf -- "$backup_bundle"
fi

printf '%s\n' "$output_bundle"
