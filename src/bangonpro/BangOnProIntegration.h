/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include "BangOnProContracts.h"

#include <QJsonArray>
#include <QObject>
#include <QPointer>
#include <QString>
#include <QStringList>
#include <functional>
#include <memory>

class QAction;
class QDockWidget;
class QEvent;
class QMainWindow;
class QProcess;
class QTimer;
class EffectListWidget;

namespace BangOnPro {

class AgentControlCentre;
class AiAssistantDock;
class PremierePresetBrowser;
class RotoStudioDock;
class ShortcutPresetManager;
class SystemCheckDialog;
class TimelineCommandEngine;
class TimelineCommandServer;
class TranscriptEditorWidget;
class WorkspaceSwitcherBar;
struct WorkspaceDocks;
enum class WorkspaceMode;

/**
 * @brief Wires Bang On Pro modules into the Kdenlive MainWindow.
 *
 * - Timeline Command API v1 engine backed by the live Kdenlive timeline (single authorised writer)
 * - Local IPC server so out-of-process agents submit validated JSON batches (no simulated input)
 * - Studio Workspace Switcher Bar (9 workspaces + saved layouts) and Premiere Pro shortcut presets
 * - AI Assistant dock: proposal -> per-change review -> accept/reject/compare/refine/undo
 * - Editable Transcript dock linked to live timeline playhead & speakers
 * - Stage Two Rotoscoping, Masking & ArtCraft 2D Composition / Storyboard Studio dock
 * - Premiere preset browser: built-in starter library + .prfpset archive import + custom stacks
 * - Custom Show Profile editor & Explicit External AI Provider consent configuration
 *
 * The Python AI/preset/roto tooling runs as a separate process (QProcess, argument vector, no shell).
 */
class BangOnProIntegration : public QObject
{
    Q_OBJECT

public:
    BangOnProIntegration(QMainWindow *mainWindow, EffectListWidget *effectList, QObject *parent = nullptr);
    ~BangOnProIntegration() override;

    QDockWidget *aiDock() const { return m_aiDock; }
    QDockWidget *presetDock() const { return m_presetDock; }
    QDockWidget *transcriptDock() const { return m_transcriptDock; }
    QDockWidget *rotoDock() const { return m_rotoDock; }
    WorkspaceSwitcherBar *workspaceBar() const { return m_workspaceBar; }
    QString ipcSocketPath() const;

    /** Directory containing the `bangonpro` Python package (env BANGONPRO_PYTHONPATH overrides). */
    static QString locatePythonPackageRoot();
    static QString pythonExecutable();
    static QString dataDir();

protected:
    /** @brief While a timed session is open, input anywhere in the editor counts as editing time. */
    bool eventFilter(QObject *watched, QEvent *event) override;

private:
    void setupEngine();
    void setupAiDock();
    void setupAgentControlCentre();
    void setupTranscriptDock();
    void setupPresetBrowser();
    void setupRotoStudioDock();
    void setupWorkspaceBar();
    void setupShortcuts();
    void setupMenu();
    void loadPresetReportIfPresent();
    void loadBuiltinStarterPresetsIfNeeded();
    WorkspaceDocks collectWorkspaceDocks() const;
    QStringList collectProjectBinMediaPaths() const;

    void applyStudioWorkspace(WorkspaceMode mode);
    void openShortcutPresetsDialog();
    void openShowProfileEditor();
    void openExternalProviderSettings();
    void openModelManager();
    void clearTemporaryCaches();

    void startProposal(const QString &instruction, const QString &profileId, const QString &customProfilePath);
    // V1.2 AI Production Department (Agent Control Centre)
    void startDepartmentJob(const QStringList &runArguments);
    void runDepartmentCommand(const QStringList &subcommandArgs, const QString &jobDir, const QString &description);
    void stageDepartmentDraft(const QString &jobDir);
    void promoteDepartmentDraft(const QString &jobDir);
    static QString departmentJobsRoot();
    // V1.2.2 production readiness: storage folder, system check, diagnostics, queue, timed sessions
    static QString storageRoot();
    static bool storageRootChosen();
    static QString queueRoot();
    static QString measurementRoot();
    bool prepareJobArguments(QStringList &args);
    void openSystemCheck(bool firstLaunch);
    void runReadinessInto(SystemCheckDialog *dialog);
    void exportDiagnostics();
    void queueDepartmentJob(const QStringList &runArguments);
    void ensureQueueWorker();
    void pollQueue();
    void refreshHistory();
    void runQuickDepartmentCommand(const QStringList &args, const std::function<void(const QJsonObject &)> &onDone = {});
    void startTiming(const QString &kind, const QString &deliverable);
    void loadCustomProfiles();
    void handleProposalFinished(int exitCode, int exitStatus);
    void approveBatch(const TimelineCommandBatch &batch);
    void approveBatchSelection(const QString &requestId, const QStringList &selectedOpIds, bool rebaseToCurrentRevision);
    void regenerateProposal(const QString &requestId);
    void rejectBatch(const QString &requestId);
    void toggleCompareMode(bool previewEnabled);
    void undoLast();
    void cancelTask();
    void openLastGeneratedProject();

    void seekTimelineToFrame(int frame);
    void applyTranscriptEdits(const QJsonObject &editedTranscriptJson);

    void commitRotoMaskToTimeline(const QJsonObject &rotoSessionJson);
    /** @brief Show the project monitor's current frame on the Roto canvas (masks are drawn on real footage). */
    bool refreshRotoFrame();
    /** @brief Segment the current frame from the AI mask's points (``roto_studio.cli segment``) and keep its matte for commit. */
    void runRotoSegmentation();
    void renderAndImport2DComposite(const QJsonObject &canvasJson, bool placeOnTimeline);
    void exportStoryboardToTimeline(const QJsonObject &storyboardJson);

    void importPresetArchive(const QString &zipPath);
    void applyPreset(const QString &presetId, const QJsonArray &mltFilters);
    void applyCustomStack(const QString &stackName, const QJsonArray &combinedMltFilters);
    void installAndApplyEffectXml(const QString &fileSlug, const QString &effectDisplayName, const QString &xml);

    QProcess *startPython(const QStringList &args);

    QMainWindow *m_mainWindow;
    QPointer<EffectListWidget> m_effectList;
    std::unique_ptr<TimelineCommandEngine> m_engine;
    std::unique_ptr<TimelineCommandServer> m_server;
    std::unique_ptr<ShortcutPresetManager> m_shortcutManager;
    AiAssistantDock *m_aiWidget{nullptr};
    QDockWidget *m_aiDock{nullptr};
    AgentControlCentre *m_agentCentre{nullptr};
    QDockWidget *m_agentDock{nullptr};
    QPointer<QProcess> m_deptProcess;
    QPointer<QProcess> m_queueWorker;
    QTimer *m_queuePollTimer{nullptr};
    QString m_queueRunningJobDir;
    QString m_timingSession;
    qint64 m_deptBaseRevision{0};
    QString m_deptBaseProjectId;
    QString m_deptBaseSequenceId;
    TranscriptEditorWidget *m_transcriptWidget{nullptr};
    QDockWidget *m_transcriptDock{nullptr};
    PremierePresetBrowser *m_presetWidget{nullptr};
    QDockWidget *m_presetDock{nullptr};
    RotoStudioDock *m_rotoWidget{nullptr};
    QDockWidget *m_rotoDock{nullptr};
    QPointer<QProcess> m_rotoProcess;
    QString m_rotoMattePath;   // matte from the last AI segmentation; committed as a native shape (luma) mask
    QString m_rotoMatteMaskId; // the AI mask it belongs to
    WorkspaceSwitcherBar *m_workspaceBar{nullptr};
    QPointer<QAction> m_openGeneratedProjectAction;
    QPointer<QProcess> m_aiProcess;
    QString m_aiOutputDir;
    QString m_lastKdenliveProjectPath;
    QString m_lastTranscriptPath;
    QStringList m_lastInputs;
    QString m_lastProfile;
    QString m_lastInstruction;
    QString m_lastCustomProfilePath;
    qint64 m_proposalBaseRevision{0};
    QString m_proposalBaseProjectId;
    QString m_proposalBaseSequenceId;
    QString m_activeProposalRequestId;
};

} // namespace BangOnPro
