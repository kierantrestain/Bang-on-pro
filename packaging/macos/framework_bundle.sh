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
    # A framework-build python3 is a launcher stub that re-execs the real interpreter in
    # Versions/<v>/Resources/Python.app (macOS run 46078752183: posix_spawn ... Python.app failed
    # without it). Bundle that app and point its interpreter at this framework's own library.
    if [[ -d "${src}/${ver_dir}/Resources/Python.app" ]]; then
        cp -R "${src}/${ver_dir}/Resources/Python.app" "${dest}/${ver_dir}/Resources/Python.app"
        rm -rf "${dest}/${ver_dir}/Resources/Python.app/Contents/_CodeSignature"
        chmod -R u+w "${dest}/${ver_dir}/Resources/Python.app"
        local interp="${dest}/${ver_dir}/Resources/Python.app/Contents/MacOS/${bin_name}"
        if [[ -f "${interp}" ]] && command -v otool >/dev/null 2>&1 && command -v install_name_tool >/dev/null 2>&1; then
            local dep
            while IFS= read -r dep; do
                if [[ "${dep}" == *"/${sub}" || "${dep}" == "@rpath/${sub}" ]]; then
                    install_name_tool -change "${dep}" "@loader_path/../../../../${bin_name}" "${interp}"
                fi
            done < <(otool -L "${interp}" | bangonpro_otool_deps_from_text)
            # Homebrew builds the interpreter with LC_RPATH /opt/homebrew/lib (macOS run 46086140335:
            # leaked LC_RPATH); it now links only its framework's library by @loader_path, so every
            # absolute search path is removed.
            local rp
            while IFS= read -r rp; do
                if [[ "${rp}" == /* ]]; then
                    install_name_tool -delete_rpath "${rp}" "${interp}"
                fi
            done < <(otool -l "${interp}" | awk '/cmd LC_RPATH/{f=1; next} f && / path /{print $2; f=0}')
        fi
    fi
    ln -sfn "${ver}" "${dest}/Versions/Current"
    ln -sfn "Versions/Current/${bin_name}" "${dest}/${bin_name}"
    ln -sfn "Versions/Current/Resources" "${dest}/Resources"
}

# bangonpro_otool_deps_from_text
# Reads `otool -L` output on stdin and prints each load command path, one per line, in full.
# The bundle is "Bang On Pro.app": splitting on whitespace (awk '{print $1}') truncated every
# in-bundle path at the first space, so such references could be neither resolved nor rewritten.
bangonpro_otool_deps_from_text() {
    local line
    while IFS= read -r line; do
        # "<file>:" / "<file> (architecture arm64):" headers are unindented; load commands are indented
        [[ "${line}" == [[:space:]]* ]] || continue
        line="${line#"${line%%[![:space:]]*}"}"
        [[ -n "${line}" ]] || continue
        line="${line% (compatibility version*}"
        line="${line% (current version*}"
        printf '%s\n' "${line}"
    done
}

# bangonpro_relpath FROM_DIR TO_PATH -> relative path from directory FROM_DIR to TO_PATH (both absolute).
bangonpro_relpath() {
    local from="${1%/}" to="$2" common up=""
    common="${from}"
    while [[ "${to}" != "${common}/"* && "${common}" != "/" && -n "${common}" ]]; do
        common="$(dirname "${common}")"
        up="../${up}"
    done
    if [[ "${common}" == "/" ]]; then
        printf '%s%s\n' "${up}" "${to#/}"
    else
        printf '%s%s\n' "${up}" "${to#"${common}/"}"
    fi
}
