/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include "../BangOnProContracts.h"

#include <QJsonObject>
#include <QMetaType>
#include <QString>
#include <QStringList>
#include <QWidget>

class QComboBox;
class QFrame;
class QLabel;
class QPlainTextEdit;
class QProgressBar;
class QPushButton;
class QToolButton;
class QTreeWidget;
class QTreeWidgetItem;

Q_DECLARE_METATYPE(BangOnPro::TimelineCommandBatch)
Q_DECLARE_METATYPE(BangOnPro::TimelineCommandResponse)

namespace BangOnPro {

/**
 * @brief Collapsible AI Assistant panel for Bang On Pro.
 *
 * Designed to live inside a collapsible QDockWidget in Kdenlive's MainWindow
 * without altering or obstructing any conventional manual editing controls
 * (timeline, monitors, bins, audio mixer, effects stack).
 *
 * All proposed AI edits are presented as a structured Timeline Command API v1
 * diff tree with confidence indicators and explicit human approval controls
 * (Accept, Reject, Compare A/B, Refine, Undo).
 */
class AiAssistantDock : public QWidget
{
    Q_OBJECT

public:
    explicit AiAssistantDock(QWidget *parent = nullptr);
    ~AiAssistantDock() override = default;

    /** @brief Currently selected built-in or custom show profile identifier. */
    QString selectedProfileId() const;

    /** @brief Path to loaded custom Show Profile v1 JSON file, if any. */
    QString customProfilePath() const;

    /** @brief Programmatically select a show profile by id (e.g. "podcast_multicam", "gameshow_multicam"). */
    bool setSelectedProfileId(const QString &profileId);

    /** @brief Load and validate a Show Profile v1 JSON file from disk. */
    bool loadCustomProfileFromFile(const QString &filePath, QString *errorMessage = nullptr);

    /** @brief Apply an already-parsed Show Profile v1 JSON object. */
    bool applyCustomProfileJson(const QJsonObject &profileObj, const QString &sourcePath = QString(), QString *errorMessage = nullptr);

    /** @brief Current natural-language instruction text. */
    QString instructionText() const;

    /** @brief Set the natural-language instruction input text. */
    void setInstructionText(const QString &instruction);

    /** @brief List of full natural-language prompts bound to quick-action chips. */
    QStringList quickActionPrompts() const;

    /** @brief Trigger one of the quick-action chips by index, filling the prompt box. */
    void triggerQuickAction(int index);

    /** @brief Display a proposed Timeline Command API v1 batch in the diff tree. */
    void displayProposal(const TimelineCommandBatch &batch);

    /** @brief Clear the active proposal diff tree and reset pending state. */
    void clearProposal();

    /** @brief True if a proposed batch is awaiting approval or rejection. */
    bool hasPendingProposal() const;

    /** @brief Currently displayed proposed batch. */
    const TimelineCommandBatch &currentProposal() const;

    /** @brief Number of operations in the current proposal flagged below the review threshold. */
    int lowConfidenceOperationCount() const;

    /** @brief Confidence threshold below which operations receive a review warning flag. */
    double reviewConfidenceThreshold() const;
    void setReviewConfidenceThreshold(double threshold);

    /** @brief Update UI state and diff tree after TimelineCommandEngine executes a batch. */
    void applyExecutionResponse(const TimelineCommandResponse &response);

    /** @brief Update the displayed timeline revision counter. */
    void setTimelineRevision(qint64 revision);
    qint64 timelineRevision() const;

    /** @brief Enable or disable the Undo Last AI Batch button. */
    void setCanUndo(bool canUndo);
    bool canUndo() const;

    /** @brief Toggle A/B compare preview state programmatically. */
    void setCompareModeActive(bool active);
    bool isCompareModeActive() const;

    /** @brief Update busy/progress state and enable/disable Cancel button. */
    void setBusy(bool busy, const QString &statusText = QString(), int progressPercent = -1);
    bool isBusy() const;

    /** @brief Append a timestamped entry to the live Agent Activity Log. */
    void appendActivityLog(const QString &agent, const QString &message, const QString &level = QStringLiteral("INFO"));

    /** @brief Clear all entries in the Agent Activity Log. */
    void clearActivityLog();

    /** @brief Full text currently in the Agent Activity Log. */
    QString activityLogText() const;

    /** @brief Expand or collapse the Agent Activity Log section. */
    void setActivityLogExpanded(bool expanded);
    bool isActivityLogExpanded() const;

    /** @brief Accessor for the proposed-changes diff tree (for inspection/testing). */
    QTreeWidget *diffTreeWidget() const;

    /** @brief Return operation IDs currently checked for acceptance in the diff tree. */
    QStringList selectedOperationIds() const;

    /** @brief Check or uncheck all top-level proposed operations. */
    void setAllOperationsChecked(bool checked);

    /** @brief Uncheck all operations flagged below the review confidence threshold. */
    void uncheckLowConfidenceOperations();

    /** @brief True when the user prefers sourcing media directly from the open Project Bin. */
    bool useProjectBinMedia() const;
    void setUseProjectBinMedia(bool useBin);

    /** @brief Update the compact storage/cache summary label inside the dock. */
    void setStorageSummaryText(const QString &summary);

    /** @brief Show or clear an explicit stale/conflict banner with Rebase & Regenerate actions. */
    void showStaleProposalWarning(const QString &summary, bool canRebase);
    void clearStaleProposalWarning();
    bool isStaleProposalWarningVisible() const;

    /** @brief Show or clear a clarification prompt when an instruction is ambiguous or out-of-scope. */
    void showClarificationPrompt(const QString &question);
    void clearClarificationPrompt();
    QString clarificationPromptText() const;

    /** @brief Display read-only isolated A/B preview simulation diff (live timeline untouched). */
    void setIsolatedPreviewSummary(const QJsonObject &previewJson);
    QString isolatedPreviewSummaryText() const;

Q_SIGNALS:
    /** @brief Emitted when the user requests a new AI edit proposal. */
    void proposalRequested(const QString &instruction, const QString &profileId, const QString &customProfilePath);

    /** @brief Emitted when the user clicks Accept & Apply on the pending batch (filtered to checked ops). */
    void batchApproveRequested(const BangOnPro::TimelineCommandBatch &batch);

    /** @brief Emitted alongside batchApproveRequested with exact request_id and selected op_ids. */
    void batchApproveSelectionRequested(const QString &requestId, const QStringList &selectedOpIds);

    /** @brief Emitted when the user explicitly confirms Rebase & Apply on a stale proposal. */
    void batchRebaseApproveRequested(const QString &requestId, const QStringList &selectedOpIds);

    /** @brief Emitted when the user clicks Regenerate Proposal after a stale/context conflict. */
    void regenerateProposalRequested(const QString &requestId);

    /** @brief Emitted when the user toggles individual operation checkboxes in the diff tree. */
    void selectedOperationIdsChanged(const QStringList &opIds);

    /** @brief Emitted when the user clicks Reject on the pending batch. */
    void batchRejectRequested(const QString &requestId);

    /** @brief Emitted when the user toggles A/B Compare mode (true = isolated AI preview, false = original). */
    void compareModeToggled(bool previewEnabled);

    /** @brief Emitted when the user requests a refinement pass on the current proposal. */
    void refineRequested(const QString &requestId, const QString &refinementInstruction, const QString &profileId);

    /** @brief Emitted when the user clicks Undo Last AI Batch. */
    void undoRequested();

    /** @brief Emitted when the user clicks Cancel during an active agent task. */
    void taskCancelRequested();

    /** @brief Emitted when the active Show Profile changes. */
    void showProfileChanged(const QString &profileId, const QString &customProfilePath);

    /** @brief Emitted when the user clicks Edit Profile to open ShowProfileEditorDialog. */
    void editProfileRequested();

    /** @brief Emitted when the user clicks AI Providers & Subscriptions to open ExternalProviderConsentDialog. */
    void providerSettingsRequested();

    /** @brief Emitted when the user clicks Manage Models to open ModelManagerDialog. */
    void modelManagerRequested();

    /** @brief Emitted when the user clicks Clear Cache in the Storage & Models footer. */
    void clearCacheRequested();

private Q_SLOTS:
    void handleProfileSelectionChanged(int index);
    void handleLoadCustomProfileClicked();
    void handleGenerateClicked();
    void handleAcceptClicked();
    void handleRejectClicked();
    void handleCompareToggled(bool checked);
    void handleRefineClicked();
    void handleUndoClicked();
    void handleCancelClicked();
    void handleActivityToggle(bool expanded);
    void handleDiffItemChanged(QTreeWidgetItem *item, int column);

private:
    void buildUi();
    void populateDefaultProfiles();
    void updateActionButtonStates();
    void setStatusBadge(const QString &text, const QString &stateKey);
    static QString formatFrameRange(const FrameRange &range);
    static QString formatOperationTarget(const TimelineOperation &op);
    static QString formatJsonValueSummary(const QJsonValue &value);

    // Header / Branding
    QFrame *m_headerFrame = nullptr;
    QLabel *m_brandTitleLabel = nullptr;
    QLabel *m_brandBadgeLabel = nullptr;
    QLabel *m_statusBadgeLabel = nullptr;
    QLabel *m_revisionLabel = nullptr;

    // Show Profile selector
    QComboBox *m_profileCombo = nullptr;
    QPushButton *m_loadProfileButton = nullptr;
    QPushButton *m_editProfileButton = nullptr;
    QPushButton *m_providerSettingsButton = nullptr;
    QLabel *m_profileMetaLabel = nullptr;

    // Instruction + quick-action chips
    QPlainTextEdit *m_instructionInput = nullptr;
    QStringList m_quickPrompts;
    QVector<QPushButton *> m_quickChipButtons;
    QPushButton *m_useBinMediaButton = nullptr;

    // Clarification, Stale Proposal & Isolated Preview Banners
    QFrame *m_clarificationBanner = nullptr;
    QLabel *m_clarificationLabel = nullptr;
    QFrame *m_staleBanner = nullptr;
    QLabel *m_staleLabel = nullptr;
    QPushButton *m_rebaseApplyButton = nullptr;
    QPushButton *m_regenerateButton = nullptr;
    QLabel *m_isolatedPreviewLabel = nullptr;

    // Proposal diff tree & per-change selection helpers
    QLabel *m_proposalSummaryLabel = nullptr;
    QTreeWidget *m_diffTree = nullptr;
    QPushButton *m_selectAllOpsButton = nullptr;
    QPushButton *m_uncheckLowConfButton = nullptr;

    // Action bar
    QPushButton *m_generateButton = nullptr;
    QPushButton *m_acceptButton = nullptr;
    QPushButton *m_rejectButton = nullptr;
    QPushButton *m_compareButton = nullptr;
    QPushButton *m_refineButton = nullptr;
    QPushButton *m_undoButton = nullptr;

    // Collapsible Activity Log, Task Progress & Storage strip
    QToolButton *m_activityToggleButton = nullptr;
    QWidget *m_activityContainer = nullptr;
    QLabel *m_taskStatusLabel = nullptr;
    QProgressBar *m_progressBar = nullptr;
    QPushButton *m_cancelButton = nullptr;
    QPlainTextEdit *m_activityLog = nullptr;
    QLabel *m_storageSummaryLabel = nullptr;
    QPushButton *m_manageModelsButton = nullptr;
    QPushButton *m_clearCacheButton = nullptr;

    // State
    TimelineCommandBatch m_currentBatch;
    bool m_hasPendingProposal = false;
    bool m_hasAppliedProposal = false;
    bool m_canUndo = false;
    bool m_isBusy = false;
    bool m_updatingDiffChecks = false;
    qint64 m_timelineRevision = 0;
    double m_reviewConfidenceThreshold = 0.72;
    QString m_customProfilePath;
    QString m_customProfileId;
    int m_lowConfidenceCount = 0;
};

} // namespace BangOnPro
