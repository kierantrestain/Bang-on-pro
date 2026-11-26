/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include <QElapsedTimer>
#include <QJsonObject>
#include <QString>
#include <QStringList>
#include <QWidget>

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
class QTreeWidget;
class QTreeWidgetItem;
class QVBoxLayout;

namespace BangOnPro {

/**
 * @brief Agent Control Centre: drives the V1.2 AI Production Department.
 *
 * Configure the automation mode, production profile, series and brief; enable or
 * disable specialist agents per job; watch live progress, confidence, cost and
 * neural/fallback status per agent (polled from the job's job.json); and accept,
 * reject or rerun individual agents. Drafts stay isolated until the editor sends
 * them to the timeline for review or promotes them (replacing an edit needs approval).
 */
class AgentControlCentre : public QWidget
{
    Q_OBJECT

public:
    explicit AgentControlCentre(QWidget *parent = nullptr);
    ~AgentControlCentre() override;

    static QStringList agentIds();
    static QStringList automationModes();

    /** @brief Build the department CLI arguments ("run" sub-command onward) from the current UI state. */
    QStringList buildRunArguments(const QStringList &inputs) const;

    QString selectedMode() const;
    void setSelectedMode(const QString &mode);
    QString selectedProfileId() const;
    void setBrief(const QString &brief);
    QString brief() const;
    /** @brief Agents the editor explicitly disabled for this job. */
    QStringList disabledAgents() const;
    void setAgentEnabled(const QString &agentId, bool enabled);
    QString selectedAgentId() const;
    void selectAgent(const QString &agentId);

    /** @brief Render a job.json document (agents, progress, decisions, artifacts). */
    void loadJobStatus(const QJsonObject &jobDoc);
    /** @brief Start polling <jobDir>/job.json (and summary.md when the job finishes). */
    void setActiveJobDir(const QString &jobDir);
    QString activeJobDir() const;
    bool refreshFromDisk();
    /** @brief Job recovery: reopen a job after restart; a "running" job with no live process is shown as interrupted. */
    void recoverJob(const QString &jobDir, bool processAlive);
    void setBusy(bool busy, const QString &text = QString());

    QString jobStatus() const;
    int reviewItemCount() const;
    QString agentStatus(const QString &agentId) const;
    QString summaryText() const;

    /** @brief V1.2.2: true when Start adds the production to the queue instead of running it now. */
    bool queueSelected() const;
    void setQueueSelected(bool queued);
    /** @brief Render the output of the department `history` command ({"jobs": [...], "queue": [...]}). */
    void loadHistory(const QJsonObject &historyPayload);
    int historyCount() const;
    /** @brief Adds a custom show profile (saved by the profile editor) to the profile list. */
    void addCustomProfile(const QString &profileId, const QString &displayName);

    /** @brief Timed editing session (measurement): reflect the state the integration confirmed. */
    void setTimingActive(bool active, bool paused = false);
    bool timingActive() const;
    bool timingPaused() const;
    /** @brief Deliverable name used for timing: episode title, else series, else the job id. */
    QString timingDeliverable() const;
    /**
     * @brief Records editor activity for the open timing session (setup, editing, review, correction,
     * approval). Emitted at most once per category change or heartbeat interval, never while paused.
     */
    void noteActivity(const QString &category);
    void setActivityHeartbeatSec(int seconds);

Q_SIGNALS:
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
    void agentDecisionRequested(const QString &jobDir, const QString &agentId, bool accept);
    void reviewDraftInTimelineRequested(const QString &jobDir);
    void openDraftProjectRequested(const QString &kdenliveProjectPath);
    void promoteDraftRequested(const QString &jobDir);
    void jobFinished(const QString &jobDir, const QString &status);

private:
    void buildUi();
    void updateActionStates();
    QTreeWidgetItem *agentItem(const QString &agentId) const;

    QComboBox *m_modeCombo{nullptr};
    QComboBox *m_profileCombo{nullptr};
    QLineEdit *m_seriesEdit{nullptr};
    QLineEdit *m_episodeEdit{nullptr};
    QPlainTextEdit *m_briefEdit{nullptr};
    QCheckBox *m_exportCheck{nullptr};
    QDoubleSpinBox *m_budgetSpin{nullptr};
    QPushButton *m_startBtn{nullptr};
    QPushButton *m_cancelBtn{nullptr};
    QPushButton *m_resumeBtn{nullptr};
    QPushButton *m_rerunBtn{nullptr};
    QPushButton *m_acceptBtn{nullptr};
    QPushButton *m_rejectBtn{nullptr};
    QPushButton *m_reviewBtn{nullptr};
    QPushButton *m_openDraftBtn{nullptr};
    QPushButton *m_promoteBtn{nullptr};
    QTreeWidget *m_agentTree{nullptr};
    QProgressBar *m_progress{nullptr};
    QLabel *m_statusLabel{nullptr};
    QLabel *m_jobMetaLabel{nullptr};
    QLabel *m_reviewTitle{nullptr};
    QWidget *m_setupSection{nullptr};
    QPushButton *m_newJobBtn{nullptr};
    bool m_setupAutoCollapsed{false};
    QListWidget *m_reviewList{nullptr};
    QPlainTextEdit *m_summaryView{nullptr};
    QTimer *m_pollTimer{nullptr};
    QString m_jobDir;
    QString m_jobStatus;
    QString m_draftProject;
    QString m_lastStatusSeen;
    bool m_interrupted{false};

    // V1.2.2: queue, history and timed sessions
    void buildTimingRow(QVBoxLayout *root);
    void updateTimingLabel();
    QCheckBox *m_queueCheck{nullptr};
    QLabel *m_historyTitle{nullptr};
    QListWidget *m_historyList{nullptr};
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
