#!/bin/sh
# Release-host helper. Uses an existing local Developer ID and keychain profile.
set -eu
if [ "$(uname -s)" != Darwin ]; then echo 'Requires an actual macOS release host.' >&2; exit 1; fi
APP=${1:?Usage: sign_and_notarize.sh /absolute/path/ReferenceLibrary.app [--outer-only]}
MODE=${2:-all}
IDENTITY=${DEVELOPER_ID_APPLICATION:?Set the existing local Developer ID Application identity name.}
PROFILE=${NOTARY_KEYCHAIN_PROFILE:?Set the existing local notarytool keychain profile name.}
case "$APP" in /*.app) ;; *) echo 'The app must be an absolute .app path.' >&2; exit 1 ;; esac
case "$MODE" in all|--outer-only) ;; *) echo 'Unknown signing mode.' >&2; exit 1 ;; esac
HELPER="$(dirname "$0")/refresh_signed_manifests.py"
if [ "$MODE" = --outer-only ]; then
    # A repeat outer seal must preserve already-signed nested Program bytes.
    python3 "$HELPER" --app "$APP" --verify --require-refreshed
else
    python3 "$HELPER" --app "$APP" --verify
    find "$APP" -type f -print | while IFS= read -r path; do
        case "$(/usr/bin/file -b "$path")" in *Mach-O*) /usr/bin/codesign --force --timestamp --options runtime --sign "$IDENTITY" "$path" ;; esac
    done
    # Refresh changed native bytes before outer codesign seals Program resources.
    python3 "$HELPER" --app "$APP"
fi
# Do not use --deep here: signing the outer app must not rewrite nested bytes.
/usr/bin/codesign --force --timestamp --options runtime --sign "$IDENTITY" "$APP"
python3 "$HELPER" --app "$APP" --verify --require-refreshed
/usr/bin/codesign --verify --deep --strict --verbose=2 "$APP"
# The submission ZIP is a generated scratch artifact, outside the release
# inventory root; a sibling ZIP inside app.parent would invalidate that inventory.
NOTARY_TEMP=$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/reference-library-notary.XXXXXX")
ARCHIVE="$NOTARY_TEMP/notarization.zip"
trap '/bin/rm -f "$ARCHIVE"; /bin/rmdir "$NOTARY_TEMP"' 0
APP_PARENT=$(cd "$(dirname "$APP")" && pwd -P)
NOTARY_PARENT=$(cd "$NOTARY_TEMP" && pwd -P)
case "$NOTARY_PARENT/" in "$APP_PARENT/"*) echo 'Notary scratch must be outside the release bundle parent.' >&2; exit 1 ;; esac
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP" "$ARCHIVE"
/usr/bin/xcrun notarytool submit "$ARCHIVE" --keychain-profile "$PROFILE" --wait
/usr/bin/xcrun stapler staple "$APP"
python3 "$HELPER" --app "$APP" --verify --require-refreshed
/usr/bin/xcrun stapler validate "$APP"
/usr/sbin/spctl --assess --type execute --verbose=2 "$APP"
echo 'Recreate the final distribution archive from this stapled app and record its new SHA-256.'
