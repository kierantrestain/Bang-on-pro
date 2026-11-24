"""Bang On Pro reusable Show Profiles package.

SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from bangonpro.show_profiles.loader import (
    ShowProfileValidationError,
    create_custom_profile,
    get_effective_profile_settings,
    get_profile_fps,
    get_profiles_dir,
    get_show_profile_schema_path,
    list_show_profiles,
    load_show_profile,
    resolve_show_profile_path,
    save_custom_profile,
    validate_show_profile,
)

__all__ = [
    "ShowProfileValidationError",
    "create_custom_profile",
    "get_effective_profile_settings",
    "get_profile_fps",
    "get_profiles_dir",
    "get_show_profile_schema_path",
    "list_show_profiles",
    "load_show_profile",
    "resolve_show_profile_path",
    "save_custom_profile",
    "validate_show_profile",
]
