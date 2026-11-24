# SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
# SPDX-FileCopyrightText: 2020-2024 KDE Craft Contributors
# SPDX-License-Identifier: BSD-2-Clause
"""KDE Craft blueprint override for building Bang On Pro on Apple Silicon (arm64).

Usage inside an Apple Silicon KDE Craft (master / Qt 6 / macos-arm-clang) environment:

    1. Bootstrap KDE Craft via CraftMaster + packaging/macos/CraftConfig.ini:
       git clone --depth=1 https://invent.kde.org/packaging/craftmaster.git --branch=master /tmp/craftmaster
       python3 /tmp/craftmaster/CraftMaster.py \
           --config packaging/macos/CraftConfig.ini \
           --variables "Root=$HOME/CraftRoot" \
           --targets macos-arm-clang \
           --setup
       python3 /tmp/craftmaster/CraftMaster.py \
           --config packaging/macos/CraftConfig.ini \
           --variables "Root=$HOME/CraftRoot" \
           --targets macos-arm-clang \
           -c -i --options virtual.ignored=True --update craft

    2. Overlay this blueprint into Craft's blueprint tree:
       BLUEPRINT_DIR="$HOME/CraftRoot/macos-arm-clang/etc/blueprints/locations/craft-blueprints-kde/kde/kdemultimedia/bangonpro"
       mkdir -p "$BLUEPRINT_DIR"
       cp packaging/macos/craft_blueprint.py "$BLUEPRINT_DIR/bangonpro.py"

    3. Install binary-cached Qt6/KF6/MLT/FFmpeg dependencies and build:
       python3 /tmp/craftmaster/CraftMaster.py \
           --config packaging/macos/CraftConfig.ini \
           --variables "Root=$HOME/CraftRoot" \
           --targets macos-arm-clang \
           -c --install-deps kdenlive

Notes:
    - Uses Craft `master` blueprint paths (`libs/qt/qtbase`, etc.) compatible with
      KDE's official Qt 6 binary caches at https://files.kde.org/craft/Qt6/.
    - Targets Qt 6, KDE Frameworks 6 (KF6), MLT >= 7.22.0, FFmpeg (with Apple
      VideoToolbox hardware decode/encode), frei0r plugins, and Breeze icons on arm64.
    - DBus is disabled (-DUSE_DBUS=OFF) on macOS to avoid background dbus-daemon
      dependencies inside the standalone .app bundle.
    - This module degrades cleanly when imported outside KDE Craft (e.g. during
      static syntax checks or CI inspection on Linux) via lightweight fallback
      base classes.
"""

from pathlib import Path
import os

try:
    import info  # type: ignore[import-not-found]
    from Package.CMakePackageBase import CMakePackageBase  # type: ignore[import-not-found]
    from CraftCore import CraftCore  # type: ignore[import-not-found]
    from Packager.MacDMGPackager import MacDMGPackager  # type: ignore[import-not-found]

    _IN_CRAFT = True
except ImportError:
    _IN_CRAFT = False

    class _FallbackInfoBase:
        """Minimal stub enabling offline inspection and syntax checks outside Craft."""

        def __init__(self):
            self.targets = {}
            self.targetInstSrc = {}
            self.svnTargets = {}
            self.defaultTarget = ""
            self.description = ""
            self.webpage = ""
            self.displayName = ""
            self.runtimeDependencies = {}
            self.buildDependencies = {}

        def setTargets(self):
            pass

        def setDependencies(self):
            pass

    class _FallbackCMakePackageBase:
        """Minimal stub enabling offline inspection and syntax checks outside Craft."""

        def __init__(self, **kwargs):
            self.subinfo = None
            self.defines = {}
            self.ignoredPackages = []
            self.blacklist_file = []

        def createPackage(self):
            return True

    class info:  # noqa: N801 - matches KDE Craft API
        infoclass = _FallbackInfoBase

    CMakePackageBase = _FallbackCMakePackageBase
    CraftCore = None
    MacDMGPackager = None


BANGONPRO_VERSION = "1.0.0"
MACOS_DEPLOYMENT_TARGET = "13.0"
MIN_MLT_VERSION = "7.22.0"


class subinfo(info.infoclass):  # noqa: N801 - Craft requires lowercase `subinfo`
    def setTargets(self):
        self.description = (
            "Bang On Pro — AI-native professional video editor built on "
            "Kdenlive (release/24.08), Qt 6, KDE Frameworks 6, and MLT 7."
        )
        self.displayName = "Bang On Pro"
        self.webpage = "https://github.com/kierantrestain/Bang-on-pro"

        self.svnTargets["main"] = (
            "https://github.com/kierantrestain/Bang-on-pro.git|main|"
        )
        self.svnTargets["integration"] = (
            "https://github.com/kierantrestain/Bang-on-pro.git|bangonpro/integration|"
        )
        self.defaultTarget = "main"

    def setDependencies(self):
        self.runtimeDependencies["virtual/base"] = None
        self.buildDependencies["kde/frameworks/extra-cmake-modules"] = None
        self.buildDependencies["dev-utils/ninja"] = None
        self.buildDependencies["dev-utils/pkg-config"] = None

        # Qt 6 core stack (Craft `master` unified libs/qt/* layout)
        self.runtimeDependencies["libs/qt/qtbase"] = None
        self.runtimeDependencies["libs/qt/qtdeclarative"] = None
        self.runtimeDependencies["libs/qt/qtmultimedia"] = None
        self.runtimeDependencies["libs/qt/qtnetworkauth"] = None
        self.runtimeDependencies["libs/qt/qtsvg"] = None
        self.runtimeDependencies["libs/qt/qtimageformats"] = None

        # KDE Frameworks 6 & Breeze theme matching KDE's official kdenlive.py blueprint
        kf6_modules = [
            "kde/frameworks/tier1/breeze-icons",
            "kde/frameworks/tier1/karchive",
            "kde/frameworks/tier1/kcodecs",
            "kde/frameworks/tier1/kcolorscheme",
            "kde/frameworks/tier1/kconfig",
            "kde/frameworks/tier1/kcoreaddons",
            "kde/frameworks/tier1/kguiaddons",
            "kde/frameworks/tier1/ki18n",
            "kde/frameworks/tier1/kimageformats",
            "kde/frameworks/tier1/kitemviews",
            "kde/frameworks/tier1/kplotting",
            "kde/frameworks/tier1/solid",
            "kde/frameworks/tier1/kwidgetsaddons",
            "kde/frameworks/tier2/kcompletion",
            "kde/frameworks/tier2/kcrash",
            "kde/frameworks/tier2/kfilemetadata",
            "kde/frameworks/tier2/kjobwidgets",
            "kde/frameworks/tier2/knotifications",
            "kde/frameworks/tier2/kpackage",
            "kde/frameworks/tier3/kbookmarks",
            "kde/frameworks/tier3/kconfigwidgets",
            "kde/frameworks/tier3/kdeclarative",
            "kde/frameworks/tier3/kiconthemes",
            "kde/frameworks/tier3/kio",
            "kde/frameworks/tier3/knewstuff",
            "kde/frameworks/tier3/knotifyconfig",
            "kde/frameworks/tier3/kservice",
            "kde/frameworks/tier3/ktextwidgets",
            "kde/frameworks/tier3/kxmlgui",
            "kde/frameworks/tier3/purpose",
            "kde/frameworks/tier4/frameworkintegration",
            "kde/plasma/breeze",
        ]
        for mod in kf6_modules:
            self.runtimeDependencies[mod] = None

        # Multimedia / MLT / FFmpeg / effects stack
        self.runtimeDependencies["libs/ffmpeg"] = None
        self.runtimeDependencies["libs/mlt"] = None
        self.runtimeDependencies["libs/frei0r-plugins"] = None
        self.runtimeDependencies["libs/rubberband"] = None
        self.runtimeDependencies["libs/libvpx"] = None
        self.runtimeDependencies["libs/x264"] = None
        self.runtimeDependencies["libs/x265"] = None


class Package(CMakePackageBase):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        configure_args = [
            "-DQT_MAJOR_VERSION=6",
            "-DBUILD_WITH_QT6=ON",
            "-DUSE_DBUS=OFF",
            "-DBUILD_TESTING=OFF",
            "-DRELEASE_BUILD=ON",
            "-DKDE_INSTALL_USE_QT_SYS_PATHS=OFF",
            "-DCMAKE_OSX_ARCHITECTURES=arm64",
            f"-DCMAKE_OSX_DEPLOYMENT_TARGET={MACOS_DEPLOYMENT_TARGET}",
        ]
        if _IN_CRAFT:
            self.subinfo.options.configure.args += configure_args
            self.subinfo.options.dynamic.setDefault("buildTests", False)
            # Ensure Craft packages Bang On Pro as a native macOS .dmg
            self.defines["appname"] = "Bang On Pro"
            self.defines["productname"] = "Bang On Pro"
            self.defines["shortcuts"] = [
                {"name": "Bang On Pro", "target": "bin/kdenlive"}
            ]
            self.defines["icon"] = os.path.join(
                self.sourceDir(), "data", "icons", "128-apps-kdenlive.png"
            )
            self.defines["mac_entitlements"] = os.path.join(
                self.sourceDir(), "packaging", "macos", "entitlements.plist"
            )
            self.defines["mac_info_plist"] = os.path.join(
                self.sourceDir(), "packaging", "macos", "Info.plist.in"
            )
            self.ignoredPackages.append("binary/mysql")
            self.ignoredPackages.append("libs/dbus")

    def createPackage(self):
        if _IN_CRAFT and CraftCore is not None:
            CraftCore.log.info(
                f"Packaging Bang On Pro {BANGONPRO_VERSION} for Apple Silicon (arm64)"
            )
            self.defines["version"] = BANGONPRO_VERSION
            # Embed Bang On Pro Python runtime & schemas inside the macOS bundle
            src_root = Path(self.sourceDir())
            app_resources = (
                Path(self.archiveDir())
                / "Applications"
                / "KDE"
                / "kdenlive.app"
                / "Contents"
                / "Resources"
            )
            if (src_root / "bangonpro").is_dir() and app_resources.parent.exists():
                import shutil

                shutil.copytree(
                    src_root / "bangonpro",
                    app_resources / "bangonpro",
                    dirs_exist_ok=True,
                )
        return super().createPackage()
