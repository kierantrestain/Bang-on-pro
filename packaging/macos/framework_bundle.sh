# SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
# SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
#
# Framework bundling helpers for build_macos_arm64.sh, exercised directly by
# tests/bangonpro/test_packaging_target_selection.py.
#
# The Mach-O relocator copies every framework a bundled binary links against.
# Qt/KF frameworks from Craft are plain framework bundles and are copied whole.
# A Homebrew Python.framework is not: next to the Python library it carries a
# whole second Python installation (bin/, lib/python3.x with absolute symlinks
# back into /opt/homebrew, include/, Resources/Python.app). codesign rejects it
# ("bundle format unrecognized, invalid, or unsuitable") and its host symlinks
# would leak build-machine paths. The bundled runtime already ships its own
# standard library under Contents/Resources/python-env/lib/pythonX.Y, so only the
# library itself is needed: it is bundled as a minimal, well-formed framework.

# bangonpro_framework_needs_minimal_copy FRAMEWORK_BASENAME
bangonpro_framework_needs_minimal_copy() {
    [[ "$1" == "Python.framework" ]]
}

# bangonpro_bundle_minimal_framework SRC_FRAMEWORK_DIR DEST_FRAMEWORK_DIR SUBPATH
# SUBPATH is the linked path inside the framework, e.g. Python.framework/Versions/3.12/Python.
# Creates DEST/Versions/<v>/{<binary>,Resources/Info.plist}, Versions/Current -> <v> and
# relative top-level <binary> and Resources links. Returns non-zero when the linked
# binary is missing or the framework is not versioned.
bangonpro_bundle_minimal_framework() {
    local src="$1" dest="$2" sub="$3"
    local rel="${sub#*/}"                 # Versions/3.12/Python
    [[ "${rel}" == Versions/*/* ]] || return 1
    local ver_dir="${rel%/*}"             # Versions/3.12
    local ver="${ver_dir#Versions/}"      # 3.12
    local bin_name="${rel##*/}"           # Python
    [[ -f "${src}/${rel}" ]] || return 1

    rm -rf "${dest}"
    mkdir -p "${dest}/${ver_dir}/Resources"
    cp -L -f "${src}/${rel}" "${dest}/${rel}"
    chmod 755 "${dest}/${rel}"
    if [[ -f "${src}/${ver_dir}/Resources/Info.plist" ]]; then
        cp -L -f "${src}/${ver_dir}/Resources/Info.plist" "${dest}/${ver_dir}/Resources/Info.plist"
    else
        cat > "${dest}/${ver_dir}/Resources/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>${bin_name}</string>
    <key>CFBundleIdentifier</key><string>org.bangonpro.bundled.${bin_name}</string>
    <key>CFBundlePackageType</key><string>FMWK</string>
    <key>CFBundleShortVersionString</key><string>${ver}</string>
    <key>CFBundleVersion</key><string>${ver}</string>
</dict>
</plist>
EOF
    fi
    ln -sfn "${ver}" "${dest}/Versions/Current"
    ln -sfn "Versions/Current/${bin_name}" "${dest}/${bin_name}"
    ln -sfn "Versions/Current/Resources" "${dest}/Resources"
}
