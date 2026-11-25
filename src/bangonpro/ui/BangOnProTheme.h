/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include <QColor>
#include <QIcon>
#include <QPalette>
#include <QString>

class QApplication;
class QWidget;

namespace BangOnPro {

/**
 * @brief Bang On Pro design tokens (V1.2.1 design system).
 *
 * Direction: a broadcast control gallery at night. Graphite surfaces from one
 * neutral ramp, one Bang On orange accent used rarely (primary action, playhead
 * family, selection), toned semantic colours that only carry meaning, and tally
 * indicators as the signature detail. Footage, timeline, waveforms and scopes
 * carry the colour; the chrome stays quiet.
 *
 * Layout follows a 4 px grid. Type uses the platform UI family at two weights
 * (400/500), with a mono partner for timecode-like figures.
 */
struct StudioPaletteTokens
{
    // Graphite neutral ramp (deepest -> raised)
    static constexpr const char *kWindowChrome = "#0F0F10";    // menu bar, workspace bar, separators
    static constexpr const char *kSunkenWell = "#111113";      // lists, inputs, monitor & timeline surrounds
    static constexpr const char *kPanelSurface = "#18181A";    // dock panels
    static constexpr const char *kElevatedSurface = "#222225"; // buttons, menus, raised rows
    static constexpr const char *kElevatedHover = "#2A2A2E";   // hover / active row
    static constexpr const char *kSelectedRow = "#2F2F34";     // selected list row (neutral)
    static constexpr const char *kBorderHairline = "#2A2A2D";  // primary text at ~8 %
    static constexpr const char *kBorderActive = "#3E3E44";    // control outlines, strong hairline
    // Three text roles + disabled
    static constexpr const char *kTextPrimary = "#EDEDED";
    static constexpr const char *kTextSecondary = "#A1A1A6";
    static constexpr const char *kTextMuted = "#85858B";
    static constexpr const char *kTextDisabled = "#5F5F66";
    // One accent (Bang On orange) and its darker selection shade
    static constexpr const char *kAccentPrimary = "#FF5A1F";
    static constexpr const char *kAccentPrimaryHover = "#FF6E3A";
    static constexpr const char *kAccentPressed = "#E64E16";
    static constexpr const char *kAccentSelection = "#B4441C"; // white text reaches 5.5:1
    static constexpr const char *kTextOnAccent = "#161616";
    // Semantic colours, toned to sit inside the ramp
    static constexpr const char *kStatusSuccess = "#4CC38A";
    static constexpr const char *kStatusWarning = "#D9A23A";
    static constexpr const char *kStatusDanger = "#E5484D";
};

/** @brief 4 px spacing grid, radii and type scale shared by every Bang On Pro surface. */
struct StudioMetrics
{
    static constexpr int kSpace1 = 4;
    static constexpr int kSpace2 = 8;
    static constexpr int kSpace3 = 12;
    static constexpr int kSpace4 = 16;
    static constexpr int kSpace6 = 24;
    static constexpr int kRadiusControl = 4;
    static constexpr int kRadiusGroup = 6;
    static constexpr int kControlHeight = 28;
    static constexpr int kRowHeight = 28;
    static constexpr int kFontMeta = 11;
    static constexpr int kFontBody = 12;
    static constexpr int kFontTitle = 13;
};

/** @brief Agent / job state shown as a tally indicator (signature detail). */
enum class TallyState { Idle, Running, Done, Review, Failed, Skipped };

/** @brief Builds the studio dark QPalette tuned for colour-critical NLE work. */
QPalette createBangOnProPalette();

/**
 * @brief Application-wide stylesheet: every native Qt control plus the reusable
 * Bang On Pro roles (buttons, text roles, tones, tally, banners, workspace bar).
 */
QString globalApplicationStyleSheet();

/** @brief The reusable component layer of the global stylesheet (roles and Bang On Pro widgets). */
QString componentStyleSheet();

/** @brief Applies Fusion, the studio palette and the global stylesheet to @p app. */
bool applyBangOnProTheme(QApplication *app);

/** @brief Assigns a button/component role: "primary", "secondary", "accept", "danger", "quiet". */
void setRole(QWidget *widget, const char *role);

/** @brief Assigns a text role: "primary", "secondary", "muted", "meta", "section", "title". */
void setTextRole(QWidget *widget, const char *role);

/** @brief Assigns a semantic tone: "success", "warning", "danger", "accent" or "" to clear. */
void setTone(QWidget *widget, const char *tone);

/** @brief Small square tally indicator icon in the semantic colour of @p state. */
QIcon tallyIcon(TallyState state);

/** @brief Semantic colour of a tally state. */
QColor tallyColor(TallyState state);

/**
 * @brief Wraps dense panel content in a frameless scroll container so docks can
 * shrink below the content's minimum size instead of forcing the main window
 * wider or taller than the screen.
 */
QWidget *makeScrollableDockContent(QWidget *content, QWidget *parent = nullptr);

/**
 * @brief Keeps a restored or resized top-level window inside the available
 * geometry of its current screen (restored layouts from larger displays).
 * @return true if the window was adjusted.
 */
bool fitWindowToScreen(QWidget *window);

} // namespace BangOnPro
