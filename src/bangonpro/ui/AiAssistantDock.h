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

    /** @brief Programmatically select a show profile by id (e.g. "podcast", "gameshow"). */
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

Q_SIGNALS:
    /** @brief Emitted when the user requests a new AI edit proposal. */
    void proposalRequested(const QString &instruction, const QString &profileId, const QString &customProfilePath);

    /** @brief Emitted when the user clicks Accept & Apply on the pending batch. */
    void batchApproveRequested(const BangOnPro::TimelineCommandBatch &batch);

    /** @brief Emitted when the user clicks Reject on the pending batch. */
    void batchRejectRequested(const QString &requestId);

    /** @brief Emitted when the user toggles A/B Compare mode (true = AI preview, false = original). */
    void compareModeToggled(bool previewEnabled);

    /** @brief Emitted when the user requests a refinement pass on the current proposal. */
    void refineRequested(const QString &requestId, const QString &refinementInstruction, const QString &profileId);

    /** @brief Emitted when the user clicks Undo Last AI Batch. */
    void undoRequested();

    /** @brief Emitted when the user clicks Cancel during an active agent task. */
    void taskCancelRequested();

    /** @brief Emitted when the active Show Profile changes. */
    void showProfileChanged(const QString &profileId, const QString &customProfilePath);

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
    QLabel *m_profileMetaLabel = nullptr;

    // Instruction + quick-action chips
    QPlainTextEdit *m_instructionInput = nullptr;
    QStringList m_quickPrompts;
    QVector<QPushButton *> m_quickChipButtons;

    // Proposal diff tree
    QLabel *m_proposalSummaryLabel = nullptr;
    QTreeWidget *m_diffTree = nullptr;

    // Action bar
    QPushButton *m_generateButton = nullptr;
    QPushButton *m_acceptButton = nullptr;
    QPushButton *m_rejectButton = nullptr;
    QPushButton *m_compareButton = nullptr;
    QPushButton *m_refineButton = nullptr;
    QPushButton *m_undoButton = nullptr;

    // Collapsible Activity Log & Task Progress
    QToolButton *m_activityToggleButton = nullptr;
    QWidget *m_activityContainer = nullptr;
    QLabel *m_taskStatusLabel = nullptr;
    QProgressBar *m_progressBar = nullptr;
    QPushButton *m_cancelButton = nullptr;
    QPlainTextEdit *m_activityLog = nullptr;

    // State
    TimelineCommandBatch m_currentBatch;
    bool m_hasPendingProposal = false;
    bool m_hasAppliedProposal = false;
    bool m_canUndo = false;
    bool m_isBusy = false;
    qint64 m_timelineRevision = 0;
    double m_reviewConfidenceThreshold = 0.72;
    QString m_customProfilePath;
    QString m_customProfileId;
    int m_lowConfidenceCount = 0;
};

} // namespace BangOnPro
