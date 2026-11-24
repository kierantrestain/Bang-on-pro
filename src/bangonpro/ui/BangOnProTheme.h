/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include <QColor>
#include <QPalette>
#include <QString>

class QApplication;

namespace BangOnPro {

/**
 * @brief Bang On Pro Studio Dark design tokens.
 *
 * Combines Premiere Pro's practical editing hierarchy, DaVinci Resolve's
 * refined studio polish, and ArtCraft's crisp near-black surface contrast.
 */
struct StudioPaletteTokens
{
    static constexpr const char *kWindowChrome = "#0B0D11";
    static constexpr const char *kPanelSurface = "#12151B";
    static constexpr const char *kElevatedSurface = "#181C24";
    static constexpr const char *kElevatedHover = "#222733";
    static constexpr const char *kSunkenWell = "#080A0D";
    static constexpr const char *kBorderHairline = "#272D39";
    static constexpr const char *kBorderActive = "#3A4356";
    static constexpr const char *kTextPrimary = "#E8ECF2";
    static constexpr const char *kTextSecondary = "#96A0B2";
    static constexpr const char *kTextDisabled = "#5C6578";
    static constexpr const char *kAccentPrimary = "#FF5A1F";
    static constexpr const char *kAccentPrimaryHover = "#FF733F";
    static constexpr const char *kAccentSelection = "#2D81FF";
    static constexpr const char *kStatusSuccess = "#22A06B";
    static constexpr const char *kStatusWarning = "#E5A00D";
    static constexpr const char *kStatusDanger = "#E04F5F";
};

/**
 * @brief Builds the studio dark QPalette tuned for color-critical NLE work.
 */
QPalette createBangOnProPalette();

/**
 * @brief Returns the application-wide QSS stylesheet styling every native Qt
 * control (docks, splitters, menus, toolbars, tabs, trees, inputs, sliders,
 * scrollbars, buttons, dialogs, and Bang On Pro workspace controls).
 */
QString globalApplicationStyleSheet();

/**
 * @brief Applies the Fusion base style, Bang On Pro studio dark QPalette, and
 * global application stylesheet cleanly to @p app.
 *
 * Safe to invoke on both Qt 5.15 and Qt 6.x.
 *
 * @return true if @p app was non-null and the theme was applied.
 */
bool applyBangOnProTheme(QApplication *app);

} // namespace BangOnPro
