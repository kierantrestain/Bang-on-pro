/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include "../BangOnProContracts.h"

#include <KAboutData>
#include <QString>

class QDockWidget;
class QMainWindow;

namespace BangOnPro {

class AiAssistantDock;

inline constexpr const char *kClipMonitorDockName = "clip_monitor";
inline constexpr const char *kProjectMonitorDockName = "project_monitor";
inline constexpr const char *kProjectBinDockName = "project_bin";
inline constexpr const char *kEffectListDockName = "effect_list";
inline constexpr const char *kEffectStackDockName = "effect_stack";
inline constexpr const char *kCompositionListDockName = "transition_list";
inline constexpr const char *kAudioMixerDockName = "mixer";
inline constexpr const char *kUndoHistoryDockName = "undo_history";
inline constexpr const char *kPremiereBrowserDockName = "bangonpro_premiere_presets";
inline constexpr const char *kAiAssistantDockName = "bangonpro_ai_assistant";

/**
 * @brief Explicit dock references for Premiere-style workspace layout arrangement.
 *
 * Any member left as nullptr is automatically discovered via QMainWindow::findChild<QDockWidget *>()
 * using Kdenlive's standard dock object names.
 */
struct WorkspaceDocks
{
    QDockWidget *clipMonitor = nullptr;
    QDockWidget *projectMonitor = nullptr;
    QDockWidget *projectBin = nullptr;
    QDockWidget *effectList = nullptr;
    QDockWidget *effectStack = nullptr;
    QDockWidget *compositionList = nullptr;
    QDockWidget *premiereBrowser = nullptr;
    QDockWidget *audioMixer = nullptr;
    QDockWidget *aiAssistant = nullptr;
};

/**
 * @brief Creates the Bang On Pro application metadata (KAboutData) preserving full
 * upstream attribution to Kdenlive, MLT, and FFmpeg.
 */
KAboutData createAboutData();

/**
 * @brief Primary Bang On Pro dark-mode accent hex color ("#FF5A1F").
 */
QString brandAccentHex();

/**
 * @brief Returns scoped stylesheet styling for Bang On Pro UI surfaces (AI Assistant dock,
 * badges, quick-action chips, diff tree, and collapsible activity log) without altering
 * Kdenlive's native manual editing widgets.
 */
QString brandStyleSheet();

/**
 * @brief Wraps an AiAssistantDock widget inside a collapsible, movable, floatable QDockWidget
 * anchored on the right dock area of @p mainWindow.
 */
QDockWidget *createAiAssistantDock(QMainWindow *mainWindow, AiAssistantDock *contentWidget = nullptr, bool initiallyVisible = true);

/**
 * @brief Arranges Kdenlive's docks in a Premiere-inspired professional editing workspace:
 * - Source (Clip) & Programme (Project) monitors top-right (side-by-side)
 * - Effect Controls top-left (when present)
 * - Project Bin + Effects / Compositions / Premiere Browser tabbed bottom-left
 * - Timeline central widget spanning the bottom
 * - Collapsible Bang On Pro AI Assistant dock on the right
 *
 * All manual Kdenlive panels remain fully accessible and unblocked.
 *
 * @return true if mainWindow was valid and at least one dock was arranged.
 */
bool applyPremiereWorkspaceLayout(QMainWindow *mainWindow, const WorkspaceDocks &explicitDocks = WorkspaceDocks());

} // namespace BangOnPro
