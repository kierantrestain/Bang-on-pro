/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include "../BangOnProContracts.h"

#include <KAboutData>
#include <QHash>
#include <QMetaType>
#include <QPointer>
#include <QString>
#include <QStringList>
#include <QVector>
#include <QWidget>

class QAbstractButton;
class QButtonGroup;
class QDockWidget;
class QMainWindow;
class QMenu;
class QPushButton;
class QToolButton;

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
inline constexpr const char *kAgentControlCentreDockName = "bangonpro_agent_control_centre_dock";
inline constexpr const char *kRotoStudioDockName = "bangonpro_roto_studio";
inline constexpr const char *kTranscriptDockName = "bangonpro_transcript_editor";
inline constexpr const char *kSubtitleDockName = "Subtitles";
inline constexpr const char *kColorScopesDockName = "bangonpro_color_scopes";
inline constexpr const char *kVectorscopeDockName = "vectorscope";
inline constexpr const char *kWaveformDockName = "waveform";
inline constexpr const char *kRgbParadeDockName = "rgb_parade";
inline constexpr const char *kHistogramDockName = "histogram";
inline constexpr const char *kRenderDockName = "bangonpro_render_qc";

/**
 * @brief Practical studio workspaces inspired by Premiere Pro and DaVinci Resolve.
 */
enum class WorkspaceMode {
    Edit,
    Multicam,
    AiFirstCut,
    Effects,
    Colour,
    Audio,
    Captions,
    Export,
    Rotoscoping
};

/** @brief Canonical ordered list of all built-in studio workspaces. */
QVector<WorkspaceMode> allWorkspaceModes();

/** @brief Stable machine-readable identifier for @p mode (e.g. "edit", "ai_first_cut", "roto"). */
QString workspaceModeToId(WorkspaceMode mode);

/** @brief Human-readable studio pill label for @p mode. */
QString workspaceModeDisplayName(WorkspaceMode mode);

/** @brief Parse a workspace ID string back to a WorkspaceMode. */
bool workspaceModeFromId(const QString &id, WorkspaceMode *outMode);

/**
 * @brief Explicit dock references for Bang On Pro studio workspace arrangement.
 *
 * Any member left as nullptr is automatically discovered via QMainWindow::findChild<QDockWidget *>()
 * using Kdenlive's and Bang On Pro's standard dock object names.
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
    QDockWidget *agentControlCentre = nullptr;
    QDockWidget *rotoStudio = nullptr;
    QDockWidget *transcriptDock = nullptr;
    QDockWidget *subtitleDock = nullptr;
    QDockWidget *colorScopes = nullptr;
    QDockWidget *renderDock = nullptr;
    QDockWidget *undoHistory = nullptr;
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
 * badges, quick-action chips, diff tree, workspace bar, and collapsible activity log)
 * harmonized seamlessly with BangOnProTheme.
 */
QString brandStyleSheet();

/**
 * @brief Wraps an AiAssistantDock widget inside a collapsible, movable, floatable QDockWidget
 * anchored on the right dock area of @p mainWindow.
 */
QDockWidget *createAiAssistantDock(QMainWindow *mainWindow, AiAssistantDock *contentWidget = nullptr, bool initiallyVisible = true);

/**
 * @brief Arranges Kdenlive's docks in a Premiere-inspired professional editing workspace.
 *
 * All manual Kdenlive panels remain fully accessible and unblocked.
 *
 * @return true if mainWindow was valid and at least one dock was arranged.
 */
bool applyPremiereWorkspaceLayout(QMainWindow *mainWindow, const WorkspaceDocks &explicitDocks = WorkspaceDocks());

/**
 * @brief Arranges Kdenlive and Bang On Pro docks for the requested studio @p mode.
 *
 * Covers all 8 core editorial workspaces plus Rotoscoping & 2D Studio.
 *
 * @return true if mainWindow was valid and at least one dock was arranged.
 */
bool applyWorkspace(QMainWindow *mainWindow, WorkspaceMode mode, const WorkspaceDocks &explicitDocks = WorkspaceDocks());

/**
 * @brief Persists the current QMainWindow geometry and dock layout under @p name
 * using QSettings("BangOnPro", "Workspaces").
 */
bool saveWorkspaceState(QMainWindow *mainWindow, const QString &name);

/**
 * @brief Restores a previously saved QMainWindow geometry and dock layout named @p name.
 */
bool restoreWorkspaceState(QMainWindow *mainWindow, const QString &name);

/**
 * @brief Lists all user-saved custom workspace layout names in sorted order.
 */
QStringList savedCustomWorkspaces();

/**
 * @brief Deletes the saved layout state for @p name from QSettings.
 * @return true if a saved layout with @p name existed and was removed.
 */
bool resetWorkspaceState(const QString &name);

/**
 * @brief Sleek Premiere Pro / DaVinci Resolve-style top workspace switcher pill bar.
 *
 * Provides one-click workspace mode pills (Edit, Multicam, AI First Cut, Effects,
 * Colour, Audio, Captions, Roto & 2D, Export), a collapsible AI Panel toggle pill,
 * and a Save Layout / Saved Layouts control.
 */
class WorkspaceSwitcherBar : public QWidget
{
    Q_OBJECT

public:
    explicit WorkspaceSwitcherBar(QWidget *parent = nullptr);
    ~WorkspaceSwitcherBar() override = default;

    /** @brief Currently highlighted workspace mode pill. */
    WorkspaceMode activeWorkspace() const;

    /** @brief Update active workspace pill state (optionally emitting workspaceSelected). */
    void setActiveWorkspace(WorkspaceMode mode, bool emitSignal = false);

    /** @brief Current state of the collapsible AI Panel toggle pill. */
    bool isAiPanelVisible() const;

    /** @brief Synchronize the AI Panel toggle pill without/with emitting aiPanelToggled. */
    void setAiPanelVisible(bool visible, bool emitSignal = false);

    /** @brief Access the pill button corresponding to @p mode (for testing/tooltips). */
    QAbstractButton *workspaceButton(WorkspaceMode mode) const;

    /** @brief Rebuild the Saved Layouts popup menu from QSettings. */
    void refreshSavedLayoutsMenu();

    /**
     * @brief Convenience helper that wires workspaceSelected, aiPanelToggled,
     * and custom layout restore directly to @p mainWindow.
     */
    void attachToMainWindow(QMainWindow *mainWindow, const WorkspaceDocks &explicitDocks = WorkspaceDocks());

Q_SIGNALS:
    void workspaceSelected(BangOnPro::WorkspaceMode mode);
    void aiPanelToggled(bool visible);
    void saveLayoutRequested();
    void restoreCustomLayoutRequested(const QString &name);
    void resetCustomLayoutRequested(const QString &name);

private Q_SLOTS:
    void handleWorkspaceButtonClicked(int id);
    void handleAiPanelToggled(bool checked);
    void handleSaveLayoutClicked();

protected:
    void resizeEvent(QResizeEvent *event) override;

private:
    void buildUi();
    void updateResponsivePills();

    QButtonGroup *m_modeGroup = nullptr;
    QHash<int, QPushButton *> m_modeButtons;
    QToolButton *m_overflowButton = nullptr;
    QMenu *m_overflowMenu = nullptr;
    QPushButton *m_aiPanelToggleButton = nullptr;
    QToolButton *m_saveLayoutButton = nullptr;
    QMenu *m_savedLayoutsMenu = nullptr;
    WorkspaceMode m_activeMode = WorkspaceMode::Edit;
    QPointer<QMainWindow> m_attachedWindow;
    WorkspaceDocks m_attachedDocks;
};

} // namespace BangOnPro

Q_DECLARE_METATYPE(BangOnPro::WorkspaceMode)
