/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include <QElapsedTimer>
#include <QJsonArray>
#include <QJsonObject>
#include <QString>
#include <QStringList>
#include <QWidget>

class QButtonGroup;
class QCheckBox;
class QComboBox;
class QDoubleSpinBox;
class QLabel;
class QLineEdit;
class QListWidget;
class QPlainTextEdit;
class QProgressBar;
class QPushButton;
class QSpinBox;
class QTimer;
class QToolButton;
class QTreeWidget;
class QTreeWidgetItem;
class QVBoxLayout;

namespace BangOnPro {

/**
 * @brief The Production panel (Agent Control Centre).
 *
 * V1.6 makes the normal workflow one column an editor can read top to bottom:
 * sequence → what to make (First cut, Full edit, Social clips, Custom) → style and
 * reference videos → instructions → Start editing → live stages → Editing complete →
 * Open edited sequence. Everything an expert needs (modes, the 14 agents, accept /
 * reject / rerun, budgets, timing, history, draft files) stays one click away under
 * Advanced.
 *
 * Truthfulness rules: the stage list mirrors the stages the department records in
 * job.json; a percentage appears only when the running stage reported a measurable
 * fraction; a job recorded as running is shown as running only while a worker really
 * holds the job (see jobWorkerAlive), so a dead worker can never freeze the panel.
 */
class AgentControlCentre : public QWidget
{
    Q_OBJECT

public:
    explicit AgentControlCentre(QWidget *parent = nullptr);
    ~AgentControlCentre() override;

    static QStringList agentIds();
    static QStringList automationModes();
    static QStringList editTypes();
    /** @brief True while some process holds <jobDir>/worker.lock (flock probe; released by the kernel when a worker dies). */
    static bool jobWorkerAlive(const QString &jobDir);

    /** @brief Build the department CLI arguments ("run" sub-command onward) from the current UI state. */
    QStringList buildRunArguments(const QStringList &inputs) const;

    QString selectedMode() const;
    void setSelectedMode(const QString &mode);
    QString selectedProfileId() const;
    QString editType() const;
    void setEditType(const QString &type);
    void setBrief(const QString &brief);
    QString brief() const;
    /** @brief Agents the editor explicitly disabled for this job. */
    QStringList disabledAgents() const;
    void setAgentEnabled(const QString &agentId, bool enabled);
    QString selectedAgentId() const;
    QString revisionInstruction() const;
    void setRevisionInstruction(const QString &text);
    bool revisionSelectionOnly() const;
    void setRevisionSelectionOnly(bool on);
    void selectAgent(const QString &agentId);

    /** @brief Current project and its sequences (names in tab order); @p current is the open one. */
    void setProjectContext(const QString &projectName, const QStringList &sequenceNames, int current);
    QString projectContextText() const;

    /** @brief Render a job.json document (stages, agents, progress, reasoning, decisions, artifacts). */
    void loadJobStatus(const QJsonObject &jobDoc);
    void loadAgentSession(const QJsonObject &presence, const QJsonObject &agentEdits);
    QString agentSessionText() const;
    QString agentDetailText() const;
    /** @brief Start polling <jobDir>/job.json (and summary.md when the job finishes). */
    void setActiveJobDir(const QString &jobDir);
    QString activeJobDir() const;
    bool refreshFromDisk();
    /** @brief Reopen a job after a restart; liveness is probed, never assumed. */
    void recoverJob(const QString &jobDir, bool processAlive);
    void setBusy(bool busy, const QString &text = QString());
    /** @brief The integration's own department process for the active job is running (it may not hold the job lock yet). */
    void setWorkerProcessRunning(bool running);
    /** @brief Show a problem the department could not record in job.json (process failed to start, crashed, …). */
    void showTaskProblem(const QString &title, const QString &fix, const QString &detail = QString());

    QString jobStatus() const;
    int reviewItemCount() const;
    QString agentStatus(const QString &agentId) const;
    QString summaryText() const;
    /** @brief Stage list as "label=state" lines (state: pending, running, done, failed, stopped, skipped). */
    QStringList stageStates() const;
    QString statusText() const;
    QString reasoningText() const;
    QString problemText() const;
    bool progressIsMeasured() const;
    bool completionVisible() const;
    bool advancedExpanded() const;
    bool qcMarkersVisible() const;
    void setQcMarkersVisible(bool visible);
    void setAdvancedExpanded(bool expanded);

    bool queueSelected() const;
    void setQueueSelected(bool queued);
    void loadHistory(const QJsonObject &historyPayload);
    int historyCount() const;
    void addCustomProfile(const QString &profileId, const QString &displayName);

    void setTimingActive(bool active, bool paused = false);
    bool timingActive() const;
    bool timingPaused() const;
    QString timingDeliverable() const;
    void noteActivity(const QString &category);
    void setActivityHeartbeatSec(int seconds);

Q_SIGNALS:
    void providerSettingsRequested();
    void referenceVideosRequested();
    void sequenceSelected(int index);
    void newSequenceRequested();
    void startJobRequested(const QStringList &runArguments);
    void queueJobRequested(const QStringList &runArguments);
    void historyJobActivated(const QString &jobDir);
    void queueEntryActionRequested(const QString &queueId, const QString &action);
    void timingStartRequested(const QString &kind, const QString &deliverable);
    void timingPauseToggled(bool paused);
    void timingFinishRequested(int qualityRating);
    void measurementActivity(const QString &category);
    void cancelJobRequested(const QString &jobDir);
    void resumeJobRequested(const QString &jobDir);
    void rerunAgentRequested(const QString &jobDir, const QString &agentId);
    void reviseDraftRequested(const QString &jobDir, const QString &instruction, bool selectionOnly);
    void undoRevisionRequested(const QString &jobDir);
    void agentDecisionRequested(const QString &jobDir, const QString &agentId, bool accept);
    void reviewDraftInTimelineRequested(const QString &jobDir);
    /** @brief Put the edited sequence into the current project (or raise it if it is already there). */
    void openEditedSequenceRequested(const QString &jobDir);
    void exportRequested();
    /** @brief Show or hide the Quality Control markers on the edited sequence (hidden by default). */
    void qcMarkersToggled(const QString &jobDir, bool visible);
    void openDraftProjectRequested(const QString &kdenliveProjectPath);
    void promoteDraftRequested(const QString &jobDir);
    void jobFinished(const QString &jobDir, const QString &status);
    /** @brief job.json says running but no worker holds the job: the integration persists the truth (department status). */
    void workerLost(const QString &jobDir);

private:
    void buildUi();
    QWidget *buildSetup();
    QWidget *buildRun();
    QWidget *buildDone();
    QWidget *buildAdvanced();
    void buildTimingRow(QVBoxLayout *root, QWidget *parent);
    void updateTimingLabel();
    void updateActionStates();
    void applyEditType(const QString &type);
    void renderStages(const QJsonArray &stages);
    void renderProblem(const QJsonObject &doc);
    QTreeWidgetItem *agentItem(const QString &agentId) const;
    bool isRunningStatus() const;

    // context
    QLabel *m_contextLabel{nullptr};
    QComboBox *m_sequenceCombo{nullptr};
    QPushButton *m_newSequenceBtn{nullptr};
    // setup
    QWidget *m_setupSection{nullptr};
    QButtonGroup *m_editTypeGroup{nullptr};
    QComboBox *m_profileCombo{nullptr};
    QPushButton *m_referencesBtn{nullptr};
    QPlainTextEdit *m_briefEdit{nullptr};
    QPushButton *m_startBtn{nullptr};
    QLabel *m_setupHint{nullptr};
    // run
    QWidget *m_runSection{nullptr};
    QListWidget *m_stageList{nullptr};
    QLabel *m_statusLabel{nullptr};
    QProgressBar *m_progress{nullptr};
    QLabel *m_reasoningLabel{nullptr};
    QLabel *m_problemLabel{nullptr};
    QPushButton *m_cancelBtn{nullptr};
    QPushButton *m_resumeBtn{nullptr};
    // done
    QWidget *m_doneSection{nullptr};
    QLabel *m_doneTitle{nullptr};
    QLabel *m_doneDetail{nullptr};
    QPushButton *m_openSequenceBtn{nullptr};
    QPushButton *m_exportBtn{nullptr};
    QPushButton *m_newJobBtn{nullptr};
    QCheckBox *m_qcMarkersCheck{nullptr};
    QLineEdit *m_reviseEdit{nullptr};
    QCheckBox *m_reviseSelection{nullptr};
    QPushButton *m_reviseBtn{nullptr};
    QPushButton *m_undoRevisionBtn{nullptr};
    QLabel *m_reviewTitle{nullptr};
    QListWidget *m_reviewList{nullptr};
    // advanced
    QToolButton *m_advancedToggle{nullptr};
    QWidget *m_advancedSection{nullptr};
    QComboBox *m_modeCombo{nullptr};
    QLineEdit *m_seriesEdit{nullptr};
    QLineEdit *m_episodeEdit{nullptr};
    QCheckBox *m_exportCheck{nullptr};
    QDoubleSpinBox *m_budgetSpin{nullptr};
    QCheckBox *m_queueCheck{nullptr};
    QLabel *m_jobMetaLabel{nullptr};
    QTreeWidget *m_agentTree{nullptr};
    QPushButton *m_acceptBtn{nullptr};
    QPushButton *m_rejectBtn{nullptr};
    QPushButton *m_rerunBtn{nullptr};
    QPlainTextEdit *m_summaryView{nullptr};
    QPushButton *m_reviewBtn{nullptr};
    QPushButton *m_openDraftBtn{nullptr};
    QPushButton *m_promoteBtn{nullptr};
    QLabel *m_historyTitle{nullptr};
    QListWidget *m_historyList{nullptr};

    QTimer *m_pollTimer{nullptr};
    QString m_jobDir;
    QString m_agentSessionText;
    QString m_agentDetailText;
    QString m_jobStatus;
    QString m_draftProject;
    QString m_lastStatusSeen;
    QString m_editType;
    int m_revisionCount{0};
    bool m_workerProcessRunning{false};
    bool m_progressMeasured{false};
    int m_deadPolls{0};
    bool m_lostReported{false};
    bool m_hasDraft{false};
    bool m_viewingFinished{false};

    // timed sessions (Advanced)
    QWidget *m_timingRow{nullptr};
    QComboBox *m_timingKind{nullptr};
    QPushButton *m_timingStartBtn{nullptr};
    QLabel *m_timingLabel{nullptr};
    QPushButton *m_timingPauseBtn{nullptr};
    QSpinBox *m_timingRating{nullptr};
    QPushButton *m_timingFinishBtn{nullptr};
    QTimer *m_timingClock{nullptr};
    QElapsedTimer m_timingElapsed;
    qint64 m_timingAccumulatedMs{0};
    bool m_timingActive{false};
    bool m_timingPaused{false};
    QString m_lastActivityCategory;
    QElapsedTimer m_lastActivityAt;
    int m_activityHeartbeatSec{30};
};

} // namespace BangOnPro
