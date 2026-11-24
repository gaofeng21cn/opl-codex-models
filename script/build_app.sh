#!/bin/bash

set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
configuration="debug"
output_path="$project_root/dist/CodexModelManager.app"
signing_identity="-"
version="$(/usr/bin/plutil -extract version raw -o - "$project_root/product-version.json")"
build_number=""
explicit_build_number=0
universal=0

compute_monotonic_build_number() {
    local ver="$1"
    if [[ "$ver" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
        local major="${BASH_REMATCH[1]}"
        local minor="${BASH_REMATCH[2]}"
        local patch="${BASH_REMATCH[3]}"
        echo "$((10#$major * 10000 + 10#$minor * 100 + 10#$patch))"
    else
        echo "版本号必须是 X.Y.Z 格式，当前为: $ver" >&2
        exit 2
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --configuration)
            configuration="$2"
            shift 2
            ;;
        --output)
            output_path="$2"
            shift 2
            ;;
        --identity)
            signing_identity="$2"
            shift 2
            ;;
        --version)
            version="$2"
            shift 2
            ;;
        --build-number)
            build_number="$2"
            explicit_build_number=1
            shift 2
            ;;
        --universal)
            universal=1
            shift
            ;;
        *)
            echo "未知参数: $1" >&2
            exit 2
            ;;
    esac
done

cd "$project_root"
if (( ! explicit_build_number )); then
    build_number="$(compute_monotonic_build_number "$version")"
fi
if (( universal )); then
    /usr/bin/swift build -c "$configuration" --arch arm64 --arch x86_64
    binary_dir="$(/usr/bin/swift build -c "$configuration" --arch arm64 --arch x86_64 --show-bin-path)"
else
    /usr/bin/swift build -c "$configuration"
    binary_dir="$(/usr/bin/swift build -c "$configuration" --show-bin-path)"
fi

contents_path="$output_path/Contents"
macos_path="$contents_path/MacOS"
helpers_path="$contents_path/Helpers"
resources_path="$contents_path/Resources"
plist_path="$contents_path/Info.plist"
icon_source="$project_root/Resources/AppIcon.png"

if [[ ! -f "$icon_source" ]]; then
    echo "缺少应用图标: $icon_source" >&2
    exit 1
fi
icon_width="$(/usr/bin/sips -g pixelWidth "$icon_source" | /usr/bin/awk '/pixelWidth/ { print $2 }')"
icon_height="$(/usr/bin/sips -g pixelHeight "$icon_source" | /usr/bin/awk '/pixelHeight/ { print $2 }')"
if [[ "$icon_width" != "1024" || "$icon_height" != "1024" ]]; then
    echo "应用图标必须是 1024 x 1024，当前为 ${icon_width} x ${icon_height}。" >&2
    exit 1
fi

/bin/mkdir -p "$(/usr/bin/dirname "$output_path")"
/bin/rm -rf "$output_path"
/bin/mkdir -p "$macos_path" "$helpers_path" "$resources_path"
/usr/bin/ditto "$binary_dir/CodexModelManager" "$macos_path/CodexModelManager"
/usr/bin/ditto "$binary_dir/CodexModelSync" "$helpers_path/CodexModelSync"
/usr/bin/ditto "$project_root/LICENSE" "$resources_path/LICENSE"
/usr/bin/ditto "$project_root/THIRD_PARTY_NOTICES.txt" "$resources_path/THIRD_PARTY_NOTICES.txt"
/usr/bin/ditto "$project_root/Resources/en.lproj" "$resources_path/en.lproj"
/usr/bin/ditto "$project_root/Resources/zh_CN.lproj" "$resources_path/zh_CN.lproj"

icon_work_directory="$(/usr/bin/mktemp -d /tmp/codex-model-manager-icon.XXXXXX)"
trap '/bin/rm -rf -- "$icon_work_directory"' EXIT
iconset_path="$icon_work_directory/AppIcon.iconset"
/bin/mkdir -p "$iconset_path"

create_icon_size() {
    local size="$1"
    local output="$2"
    /usr/bin/sips -z "$size" "$size" "$icon_source" --out "$iconset_path/$output" >/dev/null
}

create_icon_size 16 icon_16x16.png
create_icon_size 32 icon_16x16@2x.png
create_icon_size 32 icon_32x32.png
create_icon_size 64 icon_32x32@2x.png
create_icon_size 128 icon_128x128.png
create_icon_size 256 icon_128x128@2x.png
create_icon_size 256 icon_256x256.png
create_icon_size 512 icon_256x256@2x.png
create_icon_size 512 icon_512x512.png
create_icon_size 1024 icon_512x512@2x.png
/usr/bin/iconutil -c icns "$iconset_path" -o "$resources_path/AppIcon.icns"

/usr/bin/plutil -create xml1 "$plist_path"
/usr/bin/plutil -insert CFBundleDevelopmentRegion -string "zh_CN" "$plist_path"
# Preserve the filesystem name; localized metadata supplies the visible product name.
/usr/bin/plutil -insert CFBundleDisplayName -string "CodexModelManager" "$plist_path"
/usr/bin/plutil -insert CFBundleExecutable -string "CodexModelManager" "$plist_path"
/usr/bin/plutil -insert CFBundleIconFile -string "AppIcon" "$plist_path"
/usr/bin/plutil -insert CFBundleIdentifier -string "com.onepersonlab.codex-model-manager" "$plist_path"
/usr/bin/plutil -insert CFBundleInfoDictionaryVersion -string "6.0" "$plist_path"
/usr/bin/plutil -insert CFBundleName -string "CodexModelManager" "$plist_path"
/usr/bin/plutil -insert CFBundlePackageType -string "APPL" "$plist_path"
/usr/bin/plutil -insert CFBundleShortVersionString -string "$version" "$plist_path"
/usr/bin/plutil -insert CFBundleVersion -string "$build_number" "$plist_path"
/usr/bin/plutil -insert LSMinimumSystemVersion -string "14.0" "$plist_path"
/usr/bin/plutil -insert NSHighResolutionCapable -bool true "$plist_path"
/usr/bin/plutil -insert SUAutomaticallyUpdate -bool false "$plist_path"
/usr/bin/plutil -insert SUEnableAutomaticChecks -bool true "$plist_path"
/usr/bin/plutil -insert SUFeedURL -string "https://github.com/gaofeng21cn/opl-codex-models/releases/latest/download/appcast.xml" "$plist_path"
/usr/bin/plutil -insert SUPublicEDKey -string "SR+6mkq8nrhuOn5fydmkWDw/DYPXHXOZkCeSqAYNw0o=" "$plist_path"
/usr/bin/plutil -insert SUScheduledCheckInterval -integer 21600 "$plist_path"

/usr/bin/xattr -cr "$output_path"

shared_embed_script="${OPL_MACOS_SPARKLE_SCRIPTS_DIR:-}/embed-and-sign-sparkle.sh"
if [[ -n "${OPL_MACOS_SPARKLE_SCRIPTS_DIR:-}" && -x "$shared_embed_script" ]]; then
    embed_args=(
        --app "$output_path"
        --signing-identity "$signing_identity"
        --artifacts-dir "$project_root/.build/artifacts"
        --expected-feed-url "https://github.com/gaofeng21cn/opl-codex-models/releases/latest/download/appcast.xml"
    )
    if (( universal )); then
        embed_args+=(--archs "arm64 x86_64")
    fi
    "$shared_embed_script" "${embed_args[@]}"
else
    sparkle_framework_src="$project_root/.build/artifacts/sparkle/Sparkle/Sparkle.xcframework/macos-arm64_x86_64/Sparkle.framework"
    if [[ ! -d "$sparkle_framework_src" ]]; then
        sparkle_framework_src="$(/usr/bin/find "$project_root/.build/artifacts" -path '*/Sparkle.xcframework/macos-arm64_x86_64/Sparkle.framework' -type d -print -quit 2>/dev/null || true)"
    fi
    if [[ -z "$sparkle_framework_src" || ! -d "$sparkle_framework_src" ]]; then
        echo "缺少 Sparkle.framework（请先运行 swift package resolve）" >&2
        exit 1
    fi
    frameworks_path="$contents_path/Frameworks"
    sparkle_dest="$frameworks_path/Sparkle.framework"
    sparkle_ver="$sparkle_dest/Versions/B"
    /bin/mkdir -p "$frameworks_path"
    /bin/rm -rf "$sparkle_dest"
    /usr/bin/ditto "$sparkle_framework_src" "$sparkle_dest"
    if (( universal )); then
        /usr/bin/lipo "$sparkle_ver/Sparkle" -verify_arch arm64
        /usr/bin/lipo "$sparkle_ver/Sparkle" -verify_arch x86_64
    fi
    sparkle_sign_args=(--force --sign "$signing_identity")
    if [[ "$signing_identity" != "-" ]]; then
        sparkle_sign_args+=(--options runtime --timestamp)
    fi
    /usr/bin/codesign "${sparkle_sign_args[@]}" "$sparkle_ver/XPCServices/Installer.xpc"
    /usr/bin/codesign "${sparkle_sign_args[@]}" --preserve-metadata=entitlements "$sparkle_ver/XPCServices/Downloader.xpc"
    /usr/bin/codesign "${sparkle_sign_args[@]}" "$sparkle_ver/Autoupdate"
    /usr/bin/codesign "${sparkle_sign_args[@]}" "$sparkle_ver/Updater.app"
    /usr/bin/codesign "${sparkle_sign_args[@]}" "$sparkle_dest"
fi

if [[ "$signing_identity" == "-" ]]; then
    /usr/bin/codesign --force --sign - "$helpers_path/CodexModelSync"
    /usr/bin/codesign --force --sign - "$output_path"
else
    /usr/bin/codesign --force --options runtime --timestamp --sign "$signing_identity" "$helpers_path/CodexModelSync"
    /usr/bin/codesign --force --options runtime --timestamp --sign "$signing_identity" "$output_path"
fi

/usr/bin/plutil -lint "$plist_path"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$output_path"
echo "$output_path"
