/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "ui/AgentControlCentre.h"
#include "ui/ModelManagerDialog.h"

#include <QCheckBox>
#include <QComboBox>
#include <QCoreApplication>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLayout>
#include <QLineEdit>
#include <QListWidget>
#include <QPushButton>
#include <QSignalSpy>
#include <QSpinBox>
#include <QTemporaryDir>
#include <QTest>

#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>

using namespace BangOnPro;

namespace {
QJsonObject sampleJob(const QString &status)
{
    QJsonObject agents;
    const QStringList ids = AgentControlCentre::agentIds();
    for (const QString &id : ids) {
        QJsonObject a{{QStringLiteral("status"), QStringLiteral("completed")},
                      {QStringLiteral("active"), true},
                      {QStringLiteral("confidence"), 0.9},
                      {QStringLiteral("operation_count"), 3},
                      {QStringLiteral("summary"), QStringLiteral("done %1").arg(id)}};
        if (id == QLatin1String("captions")) {
            a.insert(QStringLiteral("neural_inference"), false);
            a.insert(QStringLiteral("review_items"), QJsonArray{QStringLiteral("Install a Whisper model")});
        }
        if (id == QLatin1String("rough_cut")) {
            a.insert(QStringLiteral("decision"), QStringLiteral("rejected"));
        }
        agents.insert(id, a);
    }
    return QJsonObject{{QStringLiteral("job_id"), QStringLiteral("job_test")},
                       {QStringLiteral("status"), status},
                       {QStringLiteral("progress"), 1.0},
                       {QStringLiteral("agents"), agents},
                       {QStringLiteral("budget"), QJsonObject{{QStringLiteral("spent_usd"), 0.0}}},
                       {QStringLiteral("artifacts"), QJsonObject{{QStringLiteral("kdenlive_project"), QStringLiteral("/tmp/none.kdenlive")}}}};
}
// Clicks need real geometry: show the panel at a narrow MacBook dock size, as an editor sees it.
void showAsDock(QWidget &w)
{
    w.resize(320, 1800);
    w.show();
    QVERIFY(QTest::qWaitForWindowExposed(&w));
}

// Lay out whatever just became visible (an expanded section, a finished job's view), then click like a user.
void click(QWidget *button)
{
    QVERIFY(button);
    for (int i = 0; i < 3; ++i) {
        QCoreApplication::processEvents();
        if (QWidget *top = button->window(); top && top->layout()) {
            top->layout()->activate();
        }
    }
    QTest::mouseClick(button, Qt::LeftButton);
}
} // namespace

class TestAgentControlCentre : public QObject
{
    Q_OBJECT
private Q_SLOTS:
    void listsFourteenAgentsAndFourModes()
    {
        QCOMPARE(AgentControlCentre::agentIds().size(), 14);
        QCOMPARE(AgentControlCentre::automationModes().size(), 4);
    }

    void compactControlCentreKeepsPrimaryActionAndStableIds()
    {
        AgentControlCentre acc;
        QVERIFY(acc.minimumWidth() <= 280); // fits a narrow MacBook dock column
        auto *start = acc.findChild<QPushButton *>(QStringLiteral("acc_start"));
        QVERIFY(start);
        QVERIFY(start->property("bangOnProPrimary").toBool());
        QVERIFY(acc.findChild<QPushButton *>(QStringLiteral("acc_review_draft")));
        QVERIFY(acc.findChild<QPushButton *>(QStringLiteral("acc_open_draft")));
        QVERIFY(acc.findChild<QPushButton *>(QStringLiteral("acc_promote")));
        QVERIFY(acc.findChild<QPushButton *>(QStringLiteral("acc_open_sequence")));
        // V1.6: the essential workflow is visible; the expert controls are one disclosure away.
        for (const QString &type : AgentControlCentre::editTypes()) {
            QVERIFY2(acc.findChild<QPushButton *>(QStringLiteral("acc_edit_%1").arg(type)), qPrintable(type));
        }
        QVERIFY(acc.findChild<QPushButton *>(QStringLiteral("acc_references")));
        QVERIFY(acc.findChild<QComboBox *>(QStringLiteral("acc_sequence")));
        QVERIFY(!acc.advancedExpanded());
        QVERIFY(acc.findChild<QWidget *>(QStringLiteral("acc_advanced"))->isHidden());
        QVERIFY(acc.findChild<QWidget *>(QStringLiteral("acc_agents"))->isVisibleTo(&acc) == false);
        QCOMPARE(start->text(), QStringLiteral("Start editing"));
    }

    void editTypesMapToModesAndCustomOpensAdvanced()
    {
        AgentControlCentre acc;
        showAsDock(acc);
        QCOMPARE(acc.editType(), QStringLiteral("full_edit"));
        QCOMPARE(acc.selectedMode(), QStringLiteral("full_production_draft"));
        click(acc.findChild<QPushButton *>(QStringLiteral("acc_edit_first_cut")));
        QCOMPARE(acc.selectedMode(), QStringLiteral("autonomous_first_cut"));
        QVERIFY(acc.buildRunArguments({}).join(QLatin1Char(' ')).contains(QStringLiteral("--edit-type first_cut")));
        click(acc.findChild<QPushButton *>(QStringLiteral("acc_edit_social_clips")));
        QCOMPARE(acc.selectedMode(), QStringLiteral("full_production_draft"));
        QVERIFY(!acc.advancedExpanded());
        click(acc.findChild<QPushButton *>(QStringLiteral("acc_edit_custom")));
        QVERIFY(acc.advancedExpanded()); // custom choices live under Advanced
        // Automatic style sends no --profile: the department picks one from the footage.
        QVERIFY(!acc.buildRunArguments({}).contains(QStringLiteral("--profile")));
    }

    void stagesComeFromJobEventsAndProgressIsOnlyShownWhenMeasured()
    {
        QTemporaryDir tmp;
        AgentControlCentre acc;
        acc.setActiveJobDir(tmp.path());
        acc.setWorkerProcessRunning(true);
        QJsonObject job = sampleJob(QStringLiteral("running"));
        QJsonArray stages;
        const QStringList ids{QStringLiteral("analyse"), QStringLiteral("understand"), QStringLiteral("rough_cut"), QStringLiteral("audio"),
                              QStringLiteral("colour"),  QStringLiteral("graphics"),   QStringLiteral("qc"),        QStringLiteral("prepare")};
        for (const QString &id : ids) {
            const QString state =
                id == QLatin1String("analyse")
                    ? QStringLiteral("done")
                    : (id == QLatin1String("understand") ? QStringLiteral("running")
                                                         : (id == QLatin1String("colour") ? QStringLiteral("skipped") : QStringLiteral("pending")));
            stages.append(
                QJsonObject{{QStringLiteral("id"), id}, {QStringLiteral("state"), state}, {QStringLiteral("message"), QStringLiteral("Detecting pauses")}});
        }
        job.insert(QStringLiteral("stages"), stages);
        acc.loadJobStatus(job);
        QVERIFY(acc.stageStates().contains(QStringLiteral("Analysing footage=done")));
        QVERIFY(acc.stageStates().contains(QStringLiteral("Transcribing and understanding dialogue=running")));
        QVERIFY(acc.stageStates().contains(QStringLiteral("Colour correction=skipped")));
        QVERIFY(acc.statusText().contains(QStringLiteral("1 of 7 stages done")));
        QVERIFY(!acc.progressIsMeasured()); // no fraction reported: busy indicator, no invented percentage
        QJsonObject running = stages.at(1).toObject();
        running.insert(QStringLiteral("fraction"), 0.4);
        stages.replace(1, running);
        job.insert(QStringLiteral("stages"), stages);
        acc.loadJobStatus(job);
        QVERIFY(acc.progressIsMeasured());
        // Reasoning is reported exactly as the department recorded it.
        job.insert(QStringLiteral("reasoning"),
                   QJsonObject{{QStringLiteral("message"), QStringLiteral("Processed on this Mac: no subscription reasoning was needed")}});
        acc.loadJobStatus(job);
        QVERIFY(acc.reasoningText().contains(QStringLiteral("no subscription reasoning")));
    }

    void deadWorkerIsNeverShownAsRunning()
    {
        // The V1.5 freeze: job.json said "running" after its worker died; the panel waited for ever.
        QTemporaryDir tmp;
        QFile f(QDir(tmp.path()).filePath(QStringLiteral("job.json")));
        QVERIFY(f.open(QIODevice::WriteOnly));
        QJsonObject job = sampleJob(QStringLiteral("running"));
        job.insert(QStringLiteral("error_help"), QJsonObject{{QStringLiteral("title"), QStringLiteral("Production stopped before it finished")},
                                                             {QStringLiteral("fix"), QStringLiteral("Press Resume")}});
        f.write(QJsonDocument(job).toJson());
        f.close();
        AgentControlCentre acc;
        QSignalSpy lost(&acc, &AgentControlCentre::workerLost);
        acc.setActiveJobDir(tmp.path());
        QVERIFY(acc.refreshFromDisk());
        QCOMPARE(acc.jobStatus(), QStringLiteral("interrupted"));
        QCOMPARE(lost.count(), 1); // the integration persists the truth once
        QVERIFY(acc.findChild<QPushButton *>(QStringLiteral("acc_resume"))->isEnabled());
        QVERIFY(!acc.findChild<QPushButton *>(QStringLiteral("acc_cancel"))->isEnabled());
        QVERIFY(acc.problemText().contains(QStringLiteral("Production stopped before it finished")));
        QVERIFY(!acc.stageStates().join(QLatin1Char(',')).contains(QStringLiteral("=running")));
        // A live worker (holding worker.lock) is followed, not overridden.
        const QByteArray lockPath = QFile::encodeName(QDir(tmp.path()).filePath(QStringLiteral("worker.lock")));
        const int fd = ::open(lockPath.constData(), O_RDWR | O_CREAT, 0644);
        QVERIFY(fd >= 0 && ::flock(fd, LOCK_EX | LOCK_NB) == 0);
        QVERIFY(AgentControlCentre::jobWorkerAlive(tmp.path()));
        AgentControlCentre live;
        live.recoverJob(tmp.path(), AgentControlCentre::jobWorkerAlive(tmp.path()));
        QCOMPARE(live.jobStatus(), QStringLiteral("running"));
        ::flock(fd, LOCK_UN);
        ::close(fd);
        QVERIFY(!AgentControlCentre::jobWorkerAlive(tmp.path()));
    }

    void completionShowsOpenEditedSequence()
    {
        QTemporaryDir tmp;
        AgentControlCentre acc;
        showAsDock(acc);
        acc.setActiveJobDir(tmp.path());
        QJsonObject job = sampleJob(QStringLiteral("draft_ready"));
        job.insert(QStringLiteral("processing_elapsed_sec"), 92.0);
        acc.loadJobStatus(job);
        QVERIFY(acc.completionVisible());
        auto *open = acc.findChild<QPushButton *>(QStringLiteral("acc_open_sequence"));
        QVERIFY(open->isEnabled() && open->property("bangOnProPrimary").toBool());
        QCOMPARE(acc.findChild<QLabel *>(QStringLiteral("acc_done_title"))->text(), QStringLiteral("Editing complete"));
        QSignalSpy opened(&acc, &AgentControlCentre::openEditedSequenceRequested);
        click(open);
        QCOMPARE(opened.count(), 1);
        QSignalSpy qc(&acc, &AgentControlCentre::qcMarkersToggled);
        auto *qcBox = acc.findChild<QCheckBox *>(QStringLiteral("acc_qc_markers"));
        QVERIFY(!qcBox->isChecked()); // clean timeline by default
        qcBox->setChecked(true);
        QCOMPARE(qc.count(), 1);
    }

    void buildsRunArgumentsFromUi()
    {
        AgentControlCentre acc;
        acc.setSelectedMode(QStringLiteral("full_production_draft"));
        acc.setBrief(QStringLiteral("Tight cut with captions"));
        acc.setAgentEnabled(QStringLiteral("vfx"), false);
        acc.setAgentEnabled(QStringLiteral("executive"), false); // locked: cannot be disabled
        const QStringList args = acc.buildRunArguments({QStringLiteral("/m/cam1.mp4")});
        QCOMPARE(args.first(), QStringLiteral("run"));
        QCOMPARE(args.at(1), QStringLiteral("Tight cut with captions"));
        QVERIFY(args.contains(QStringLiteral("full_production_draft")));
        QVERIFY(!args.contains(QStringLiteral("--profile"))); // "Automatic": the department matches the footage
        const int d = args.indexOf(QStringLiteral("--disable"));
        QVERIFY(d > 0);
        QCOMPARE(args.at(d + 1), QStringLiteral("vfx"));
        QVERIFY(!acc.disabledAgents().contains(QStringLiteral("executive")));
        QCOMPARE(args.last(), QStringLiteral("/m/cam1.mp4"));
    }

    void rendersJobStatusAndReviewItems()
    {
        AgentControlCentre acc;
        acc.loadJobStatus(sampleJob(QStringLiteral("draft_ready")));
        QCOMPARE(acc.jobStatus(), QStringLiteral("draft_ready"));
        QVERIFY(acc.agentStatus(QStringLiteral("rough_cut")).contains(QStringLiteral("rejected")));
        QCOMPARE(acc.reviewItemCount(), 1);
    }

    void showsConnectedReasoningClientAndAgentActivity()
    {
        // V1.4: the MCP adapter's presence and task files appear in the existing panel, nothing new.
        AgentControlCentre acc;
        acc.loadJobStatus(sampleJob(QStringLiteral("draft_ready")));
        const int before = acc.reviewItemCount();
        QJsonObject presence{{QStringLiteral("client"), QStringLiteral("Claude Code")},
                             {QStringLiteral("status"), QStringLiteral("connected")},
                             {QStringLiteral("activity"), QStringLiteral("switching cameras")},
                             {QStringLiteral("agent"), QStringLiteral("rough_cut")}};
        QJsonObject issue{{QStringLiteral("agent"), QStringLiteral("rough_cut")}, {QStringLiteral("message"), QStringLiteral("Picture gap at 12.0s")}};
        QJsonObject task{{QStringLiteral("unresolved"), QJsonArray{issue}}};
        QJsonObject edits{{QStringLiteral("entries"), QJsonArray{QJsonObject{{QStringLiteral("entry_id"), QStringLiteral("e1")}}}},
                          {QStringLiteral("tasks"), QJsonObject{{QStringLiteral("t1"), task}}}};
        acc.loadAgentSession(presence, edits);
        QVERIFY(acc.agentSessionText().contains(QStringLiteral("Claude Code connected")));
        QVERIFY(acc.agentSessionText().contains(QStringLiteral("switching cameras")));
        QVERIFY(acc.agentSessionText().contains(QStringLiteral("1 agent edit")));
        QCOMPARE(acc.reviewItemCount(), before + 1);
    }

    void showsReasoningProvidersFailoverAndLearning()
    {
        // V1.5: app-driven provider order and switches plus department learning, in the same header; approvals in Review.
        AgentControlCentre acc;
        acc.loadJobStatus(sampleJob(QStringLiteral("draft_ready")));
        const int before = acc.reviewItemCount();
        QJsonObject pending{{QStringLiteral("agent"), QStringLiteral("graphics")}, {QStringLiteral("message"), QStringLiteral("Strategy v2 awaits your approval")}};
        QJsonObject presence{{QStringLiteral("client"), QStringLiteral("Codex")},
                             {QStringLiteral("status"), QStringLiteral("connected")},
                             {QStringLiteral("providers"), QStringLiteral("Reasoning: Claude Code (waiting) > OpenAI Codex · active OpenAI Codex")},
                             {QStringLiteral("failover"), QStringLiteral("claude usage_exhausted > codex")},
                             {QStringLiteral("learning"), QStringLiteral("Learning: 1 lesson(s), 1 skill(s), 1 strategy update(s)")},
                             {QStringLiteral("learning_review"), QJsonArray{pending}}};
        acc.loadAgentSession(presence, QJsonObject());
        QVERIFY(acc.agentSessionText().contains(QStringLiteral("Codex connected")));
        QVERIFY(acc.agentDetailText().contains(QStringLiteral("active OpenAI Codex")));
        QVERIFY(acc.agentDetailText().contains(QStringLiteral("Switched provider: claude usage_exhausted > codex")));
        QVERIFY(acc.agentDetailText().contains(QStringLiteral("Learning: 1 lesson(s)")));
        QCOMPARE(acc.reviewItemCount(), before + 1);
    }

    void pollsJobFileAndEmitsFinishedWithSummary()
    {
        QTemporaryDir tmp;
        QVERIFY(tmp.isValid());
        QFile f(QDir(tmp.path()).filePath(QStringLiteral("job.json")));
        QVERIFY(f.open(QIODevice::WriteOnly));
        f.write(QJsonDocument(sampleJob(QStringLiteral("draft_ready"))).toJson());
        f.close();
        QFile s(QDir(tmp.path()).filePath(QStringLiteral("summary.md")));
        QVERIFY(s.open(QIODevice::WriteOnly));
        s.write("# Production summary - job_test\n");
        s.close();
        AgentControlCentre acc;
        QSignalSpy finished(&acc, &AgentControlCentre::jobFinished);
        acc.setActiveJobDir(tmp.path());
        QVERIFY(acc.refreshFromDisk());
        QCOMPARE(finished.count(), 1);
        QVERIFY(acc.summaryText().startsWith(QStringLiteral("# Production summary")));
        QVERIFY(acc.refreshFromDisk());
        QCOMPARE(finished.count(), 1); // emitted once per terminal status
    }

    void primaryActionHandsOverToReviewWhenDraftReady()
    {
        // One view at a time: Start in setup, Open edited sequence once the edit exists.
        QTemporaryDir tmp;
        AgentControlCentre acc;
        showAsDock(acc);
        auto *start = acc.findChild<QPushButton *>(QStringLiteral("acc_start"));
        auto *done = acc.findChild<QWidget *>(QStringLiteral("acc_done"));
        QVERIFY(start->property("bangOnProPrimary").toBool());
        QVERIFY(done->isHidden());
        acc.setActiveJobDir(tmp.path());
        acc.loadJobStatus(sampleJob(QStringLiteral("draft_ready")));
        QVERIFY(!done->isHidden());
        QVERIFY(acc.agentStatus(QStringLiteral("colourist")).startsWith(QStringLiteral("Done")));
        // Setup collapses while reviewing; "New production" detaches and brings it back.
        auto *reviewTitle = acc.findChild<QLabel *>(QStringLiteral("acc_review_title"));
        QVERIFY(reviewTitle && reviewTitle->text().contains(QStringLiteral("1")));
        auto *setup = acc.findChild<QWidget *>(QStringLiteral("acc_setup"));
        auto *newJob = acc.findChild<QPushButton *>(QStringLiteral("acc_new_job"));
        QVERIFY(setup && newJob);
        QVERIFY(setup->isHidden());
        QSignalSpy newProd(&acc, &AgentControlCentre::newProductionRequested);
        click(newJob);
        QVERIFY(!setup->isHidden());
        QVERIFY(acc.activeJobDir().isEmpty());
        QCOMPARE(newProd.count(), 1);
    }

    void recoversInterruptedJob()
    {
        QTemporaryDir tmp;
        QFile f(QDir(tmp.path()).filePath(QStringLiteral("job.json")));
        QVERIFY(f.open(QIODevice::WriteOnly));
        f.write(QJsonDocument(sampleJob(QStringLiteral("running"))).toJson());
        f.close();
        AgentControlCentre acc;
        acc.recoverJob(tmp.path(), false);
        QCOMPARE(acc.jobStatus(), QStringLiteral("interrupted"));
        auto *resume = acc.findChild<QPushButton *>(QStringLiteral("acc_resume"));
        QVERIFY(resume && resume->isEnabled());
    }

    void revisionControlsEmitInstructionAndSelection()
    {
        QTemporaryDir tmp;
        AgentControlCentre acc;
        showAsDock(acc);
        acc.setActiveJobDir(tmp.path());
        auto *edit = acc.findChild<QLineEdit *>(QStringLiteral("acc_revise_text"));
        auto *revise = acc.findChild<QPushButton *>(QStringLiteral("acc_revise"));
        auto *undo = acc.findChild<QPushButton *>(QStringLiteral("acc_revise_undo"));
        auto *selection = acc.findChild<QCheckBox *>(QStringLiteral("acc_revise_selection"));
        QVERIFY(edit && revise && undo && selection);
        acc.loadJobStatus(sampleJob(QStringLiteral("running")));
        QVERIFY(!revise->isEnabled()); // nothing to revise while a job runs
        acc.loadJobStatus(sampleJob(QStringLiteral("draft_ready")));
        QVERIFY(revise->isEnabled());
        QVERIFY(!undo->isEnabled()); // no revision yet
        QSignalSpy spy(&acc, &AgentControlCentre::reviseDraftRequested);
        acc.setRevisionInstruction(QStringLiteral("Tighten the opening but keep the natural conversation"));
        acc.setRevisionSelectionOnly(true);
        click(revise);
        QCOMPARE(spy.count(), 1);
        QCOMPARE(spy.first().at(1).toString(), QStringLiteral("Tighten the opening but keep the natural conversation"));
        QCOMPARE(spy.first().at(2).toBool(), true);
        QJsonObject job = sampleJob(QStringLiteral("draft_ready"));
        job.insert(QStringLiteral("revisions"), QJsonArray{QJsonObject{{QStringLiteral("revision"), 1}}});
        job.insert(QStringLiteral("last_revision"), QJsonObject{{QStringLiteral("message"), QStringLiteral("Select the section on the timeline")}});
        acc.loadJobStatus(job);
        QVERIFY(undo->isEnabled());
        QSignalSpy undoSpy(&acc, &AgentControlCentre::undoRevisionRequested);
        click(undo);
        QCOMPARE(undoSpy.count(), 1);
    }

    void agentActionsEmitSignals()
    {
        QTemporaryDir tmp;
        AgentControlCentre acc;
        showAsDock(acc);
        acc.setActiveJobDir(tmp.path());
        acc.loadJobStatus(sampleJob(QStringLiteral("draft_ready")));
        acc.selectAgent(QStringLiteral("colourist"));
        QSignalSpy decide(&acc, &AgentControlCentre::agentDecisionRequested);
        QSignalSpy rerun(&acc, &AgentControlCentre::rerunAgentRequested);
        QSignalSpy cancel(&acc, &AgentControlCentre::cancelJobRequested);
        auto *reject = acc.findChild<QPushButton *>(QStringLiteral("acc_reject"));
        auto *rerunBtn = acc.findChild<QPushButton *>(QStringLiteral("acc_rerun"));
        auto *cancelBtn = acc.findChild<QPushButton *>(QStringLiteral("acc_cancel"));
        QVERIFY(reject && rerunBtn && cancelBtn);
        QVERIFY(reject->isEnabled());
        QVERIFY(!cancelBtn->isEnabled()); // nothing running
        click(reject);
        click(rerunBtn);
        QCOMPARE(decide.count(), 1);
        QCOMPARE(decide.first().at(1).toString(), QStringLiteral("colourist"));
        QCOMPARE(decide.first().at(2).toBool(), false);
        QCOMPARE(rerun.count(), 1);
        acc.setBusy(true, QStringLiteral("running"));
        QVERIFY(cancelBtn->isEnabled());
        QVERIFY(!reject->isEnabled());
        click(cancelBtn);
        QCOMPARE(cancel.count(), 1);
    }

    // ---- V1.2.2 controls -------------------------------------------------
    void queueToggleQueuesInsteadOfStarting()
    {
        AgentControlCentre acc;
        showAsDock(acc);
        auto *queue = acc.findChild<QCheckBox *>(QStringLiteral("acc_queue"));
        auto *start = acc.findChild<QPushButton *>(QStringLiteral("acc_start"));
        QVERIFY(queue && start);
        QVERIFY(!queue->isChecked());
        QSignalSpy started(&acc, &AgentControlCentre::startJobRequested);
        QSignalSpy queued(&acc, &AgentControlCentre::queueJobRequested);
        acc.setQueueSelected(true);
        QCOMPARE(start->text(), QStringLiteral("Add to queue"));
        QVERIFY(start->property("bangOnProPrimary").toBool()); // still the one primary action
        acc.setBusy(true, QStringLiteral("running"));
        QVERIFY(start->isEnabled()); // productions can be queued while one runs
        click(start);
        QCOMPARE(queued.count(), 1);
        QCOMPARE(started.count(), 0);
        QCOMPARE(queued.first().at(0).toStringList().first(), QStringLiteral("run"));
        acc.setQueueSelected(false);
        QVERIFY(!start->isEnabled()); // but not started
        QCOMPARE(start->text(), QStringLiteral("Start editing"));
    }

    void historyShowsQueueThenJobsAndOpensJobs()
    {
        QTemporaryDir tmp;
        QFile f(QDir(tmp.path()).filePath(QStringLiteral("job.json")));
        QVERIFY(f.open(QIODevice::WriteOnly));
        f.write(QJsonDocument(sampleJob(QStringLiteral("draft_ready"))).toJson());
        f.close();
        const QJsonObject running{{QStringLiteral("queue_id"), QStringLiteral("q_1")},
                                  {QStringLiteral("label"), QStringLiteral("Episode 13")},
                                  {QStringLiteral("status"), QStringLiteral("queued")},
                                  {QStringLiteral("job_dir"), QStringLiteral("/nonexistent/job_q1")}};
        const QJsonObject done{{QStringLiteral("queue_id"), QStringLiteral("q_0")}, {QStringLiteral("status"), QStringLiteral("completed")}};
        const QJsonObject job{{QStringLiteral("job_id"), QStringLiteral("job_test")},
                              {QStringLiteral("job_dir"), tmp.path()},
                              {QStringLiteral("status"), QStringLiteral("draft_ready")},
                              {QStringLiteral("episode_title"), QStringLiteral("Episode 12")},
                              {QStringLiteral("processing_elapsed_sec"), 92.0}};
        AgentControlCentre acc;
        acc.loadHistory(QJsonObject{{QStringLiteral("queue"), QJsonArray{running, done}}, {QStringLiteral("jobs"), QJsonArray{job}}});
        QCOMPARE(acc.historyCount(), 2); // finished queue entries are shown through their job
        auto *list = acc.findChild<QListWidget *>(QStringLiteral("acc_history"));
        QVERIFY(list);
        QVERIFY(list->item(0)->text().startsWith(QStringLiteral("Episode 13 · Queued")));
        QVERIFY(list->item(1)->text().contains(QStringLiteral("Editing complete")));
        QVERIFY(list->item(1)->toolTip().contains(QStringLiteral("machine 1:32")));
        auto *title = acc.findChild<QLabel *>(QStringLiteral("acc_history_title"));
        QVERIFY(title && title->text().endsWith(QStringLiteral("2")));
        QSignalSpy opened(&acc, &AgentControlCentre::historyJobActivated);
        Q_EMIT list->itemActivated(list->item(1));
        Q_EMIT list->itemActivated(list->item(0)); // queued: no job folder yet, nothing to open
        QCOMPARE(opened.count(), 1);
        QCOMPARE(opened.first().at(0).toString(), tmp.path());
    }

    void timingSessionRecordsThrottledActivityAndPause()
    {
        AgentControlCentre acc;
        showAsDock(acc);
        acc.setAdvancedExpanded(true); // timing lives under Advanced
        auto *startTiming = acc.findChild<QPushButton *>(QStringLiteral("acc_timing_start"));
        auto *pause = acc.findChild<QPushButton *>(QStringLiteral("acc_timing_pause"));
        auto *finish = acc.findChild<QPushButton *>(QStringLiteral("acc_timing_finish"));
        auto *rating = acc.findChild<QSpinBox *>(QStringLiteral("acc_timing_rating"));
        QVERIFY(startTiming && pause && finish && rating);
        QVERIFY(!pause->isVisibleTo(&acc) && !finish->isVisibleTo(&acc)); // one quiet control until timing starts
        QSignalSpy startReq(&acc, &AgentControlCentre::timingStartRequested);
        acc.findChild<QLineEdit *>(QStringLiteral("acc_episode"))->setText(QStringLiteral("Episode 12"));
        click(startTiming);
        QCOMPARE(startReq.count(), 1);
        QCOMPARE(startReq.first().at(0).toString(), QStringLiteral("agent_assisted"));
        QCOMPARE(startReq.first().at(1).toString(), QStringLiteral("Episode 12"));
        QSignalSpy activity(&acc, &AgentControlCentre::measurementActivity);
        acc.noteActivity(QStringLiteral("review")); // not confirmed yet: nothing recorded
        QCOMPARE(activity.count(), 0);
        acc.setTimingActive(true);
        QVERIFY(pause->isVisibleTo(&acc) && finish->isVisibleTo(&acc) && !startTiming->isVisibleTo(&acc));
        acc.noteActivity(QStringLiteral("review"));
        acc.noteActivity(QStringLiteral("review")); // throttled
        acc.noteActivity(QStringLiteral("correction"));
        QCOMPARE(activity.count(), 2);
        QCOMPARE(activity.at(1).at(0).toString(), QStringLiteral("correction"));
        // Rejecting an agent is corrective work.
        QTemporaryDir tmp;
        acc.setActiveJobDir(tmp.path());
        acc.loadJobStatus(sampleJob(QStringLiteral("draft_ready")));
        acc.selectAgent(QStringLiteral("graphics"));
        acc.setActivityHeartbeatSec(0);
        click(acc.findChild<QPushButton *>(QStringLiteral("acc_promote")));
        QCOMPARE(activity.last().at(0).toString(), QStringLiteral("approval"));
        QSignalSpy paused(&acc, &AgentControlCentre::timingPauseToggled);
        click(pause);
        QCOMPARE(paused.count(), 1);
        QVERIFY(paused.first().at(0).toBool());
        acc.setTimingActive(true, true);
        QCOMPARE(pause->text(), QStringLiteral("Resume"));
        const int before = activity.count();
        acc.noteActivity(QStringLiteral("editing")); // paused: not recorded
        QCOMPARE(activity.count(), before);
        QSignalSpy finished(&acc, &AgentControlCentre::timingFinishRequested);
        rating->setValue(5);
        click(finish);
        QCOMPARE(finished.count(), 1);
        QCOMPARE(finished.first().at(0).toInt(), 5);
        acc.setTimingActive(false);
        QVERIFY(!acc.timingActive() && startTiming->isVisibleTo(&acc));
    }

    void failedAgentShowsPlainFixAndCustomProfilesAreSelectable()
    {
        QJsonObject job = sampleJob(QStringLiteral("completed_with_errors"));
        QJsonObject agents = job.value(QStringLiteral("agents")).toObject();
        QJsonObject dist = agents.value(QStringLiteral("distribution")).toObject();
        dist.insert(QStringLiteral("status"), QStringLiteral("failed"));
        dist.insert(QStringLiteral("error"), QStringLiteral("RuntimeError: melt render failed with exit code 1"));
        dist.insert(QStringLiteral("help"),
                    QJsonObject{{QStringLiteral("title"), QStringLiteral("Export failed")}, {QStringLiteral("fix"), QStringLiteral("Rerun the Distribution agent.")}});
        agents.insert(QStringLiteral("distribution"), dist);
        job.insert(QStringLiteral("agents"), agents);
        job.insert(QStringLiteral("processing_elapsed_sec"), 75.0);
        AgentControlCentre acc;
        acc.loadJobStatus(job);
        auto *review = acc.findChild<QListWidget *>(QStringLiteral("acc_review"));
        bool found = false;
        for (int i = 0; i < review->count(); ++i) {
            if (review->item(i)->text().contains(QStringLiteral("Export failed. Rerun the Distribution agent."))) {
                found = review->item(i)->toolTip().contains(QStringLiteral("exit code 1"));
            }
        }
        QVERIFY(found);
        QVERIFY(acc.statusText().contains(QStringLiteral("1:15 on this Mac")));
        acc.addCustomProfile(QStringLiteral("weekly_chat"), QStringLiteral("Weekly chat"));
        acc.addCustomProfile(QStringLiteral("weekly_chat"), QStringLiteral("Weekly chat")); // no duplicates
        auto *profiles = acc.findChild<QComboBox *>(QStringLiteral("acc_profile"));
        QCOMPARE(profiles->findData(QStringLiteral("weekly_chat")), profiles->count() - 1);
        profiles->setCurrentIndex(profiles->count() - 1);
        QVERIFY(acc.buildRunArguments({}).contains(QStringLiteral("weekly_chat")));
    }

    void systemCheckShowsFixesAndHandsOverPrimaryAction()
    {
        const auto item = [](const char *id, const char *status, const char *fix, const char *action) {
            return QJsonObject{{QStringLiteral("id"), QString::fromLatin1(id)},
                               {QStringLiteral("title"), QString::fromLatin1(id)},
                               {QStringLiteral("status"), QString::fromLatin1(status)},
                               {QStringLiteral("detail"), QStringLiteral("detail")},
                               {QStringLiteral("fix"), QString::fromLatin1(fix)},
                               {QStringLiteral("fix_action"), QString::fromLatin1(action)}};
        };
        QJsonObject blocked{{QStringLiteral("overall"), QStringLiteral("blocked")},
                            {QStringLiteral("summary"), QJsonObject{{QStringLiteral("error"), 1}}},
                            {QStringLiteral("paths"), QJsonObject{{QStringLiteral("job_root"), QStringLiteral("/Volumes/SSD/jobs")}}},
                            {QStringLiteral("items"),
                             QJsonArray{item("tool_ffmpeg", "error", "Reinstall Bang On Pro from the DMG.", "run_system_check"),
                                        item("model_speech", "warning", "Install Whisper tiny in Model Manager.", "open_model_manager"),
                                        item("cache", "ok", "", "")}}};
        SystemCheckDialog dlg;
        QVERIFY(!dlg.loadReport(QJsonObject{}));
        QVERIFY(dlg.loadReport(blocked));
        QCOMPARE(dlg.itemCount(), 3);
        QCOMPARE(dlg.overallStatus(), QStringLiteral("blocked"));
        QVERIFY(dlg.itemText(QStringLiteral("tool_ffmpeg")).contains(QStringLiteral("Reinstall Bang On Pro")));
        QCOMPARE(dlg.storageFolder(), QStringLiteral("/Volumes/SSD/jobs"));
        auto *recheck = dlg.findChild<QPushButton *>(QStringLiteral("sc_recheck"));
        auto *done = dlg.findChild<QPushButton *>(QStringLiteral("sc_done"));
        QVERIFY(recheck && done);
        QVERIFY(recheck->property("bangOnProPrimary").toBool() && !done->property("bangOnProPrimary").toBool());
        QSignalSpy recheckReq(&dlg, &SystemCheckDialog::recheckRequested);
        QSignalSpy models(&dlg, &SystemCheckDialog::modelManagerRequested);
        QSignalSpy cache(&dlg, &SystemCheckDialog::cacheActionRequested);
        QSignalSpy diag(&dlg, &SystemCheckDialog::exportDiagnosticsRequested);
        auto *list = dlg.findChild<QListWidget *>(QStringLiteral("sc_items"));
        Q_EMIT list->itemActivated(list->item(1)); // double-click the speech-model warning
        QCOMPARE(models.count(), 1);
        QTest::mouseClick(recheck, Qt::LeftButton);
        QCOMPARE(recheckReq.count(), 1);
        QTest::mouseClick(dlg.findChild<QPushButton *>(QStringLiteral("sc_cache_prune")), Qt::LeftButton);
        QTest::mouseClick(dlg.findChild<QPushButton *>(QStringLiteral("sc_cache_clear")), Qt::LeftButton);
        QCOMPARE(cache.count(), 2);
        QCOMPARE(cache.at(0).at(0).toString(), QStringLiteral("prune"));
        QTest::mouseClick(dlg.findChild<QPushButton *>(QStringLiteral("sc_diagnostics")), Qt::LeftButton);
        QCOMPARE(diag.count(), 1);
        dlg.setBusy(true);
        QVERIFY(!recheck->isEnabled());
        blocked.insert(QStringLiteral("overall"), QStringLiteral("ready"));
        QVERIFY(dlg.loadReport(blocked));
        QVERIFY(recheck->isEnabled());
        QVERIFY(done->property("bangOnProPrimary").toBool() && !recheck->property("bangOnProPrimary").toBool());
    }

    // ---- V1.7 project isolation, lifecycle controls, setup options, telemetry ----
    void resetForProjectClearsActiveJobHistoryAndReturnsToSetup()
    {
        QTemporaryDir tmp;
        AgentControlCentre acc;
        showAsDock(acc);
        acc.setProjectContext(QStringLiteral("Project A"), {QStringLiteral("Seq A1")}, 0);
        acc.setActiveJobDir(tmp.path());
        acc.loadJobStatus(sampleJob(QStringLiteral("draft_ready")));
        acc.setQcMarkersVisible(true);
        const QJsonObject job{{QStringLiteral("job_id"), QStringLiteral("job_a")},
                              {QStringLiteral("job_dir"), tmp.path()},
                              {QStringLiteral("status"), QStringLiteral("draft_ready")}};
        acc.loadHistory(QJsonObject{{QStringLiteral("jobs"), QJsonArray{job}}});
        QVERIFY(acc.completionVisible());
        QCOMPARE(acc.historyCount(), 1);
        QVERIFY(acc.reviewItemCount() > 0);
        QVERIFY(acc.qcMarkersVisible());

        acc.resetForProject(QStringLiteral("uuid-proj-b"), QStringLiteral("Project B"), {QStringLiteral("Seq B1"), QStringLiteral("Seq B2")}, 1);

        QCOMPARE(acc.activeProjectUuid(), QStringLiteral("uuid-proj-b"));
        QVERIFY(acc.activeJobDir().isEmpty());
        QVERIFY(acc.jobStatus().isEmpty());
        QCOMPARE(acc.historyCount(), 0);
        QCOMPARE(acc.reviewItemCount(), 0);
        QVERIFY(!acc.qcMarkersVisible());
        QVERIFY(acc.projectContextText().contains(QStringLiteral("Project B")));
        QVERIFY(acc.projectContextText().contains(QStringLiteral("Seq B2")));
        QVERIFY(!acc.completionVisible());
        QVERIFY(!acc.findChild<QWidget *>(QStringLiteral("acc_setup"))->isHidden());
        QVERIFY(acc.findChild<QWidget *>(QStringLiteral("acc_done"))->isHidden());
        QVERIFY(acc.findChild<QWidget *>(QStringLiteral("acc_run"))->isHidden());
    }

    void lifecycleButtonsPauseRetryDiscardAndNewFromRun()
    {
        QTemporaryDir tmp;
        AgentControlCentre acc;
        showAsDock(acc);
        acc.setActiveJobDir(tmp.path());
        acc.setWorkerProcessRunning(true);
        acc.loadJobStatus(sampleJob(QStringLiteral("running")));

        auto *pauseBtn = acc.findChild<QPushButton *>(QStringLiteral("acc_pause"));
        auto *retryBtn = acc.findChild<QPushButton *>(QStringLiteral("acc_retry"));
        auto *discardBtn = acc.findChild<QPushButton *>(QStringLiteral("acc_discard"));
        auto *newFromRun = acc.findChild<QPushButton *>(QStringLiteral("acc_new_from_run"));
        auto *resumeBtn = acc.findChild<QPushButton *>(QStringLiteral("acc_resume"));
        QVERIFY(pauseBtn && retryBtn && discardBtn && newFromRun && resumeBtn);

        QVERIFY(!pauseBtn->isHidden() && pauseBtn->isEnabled());
        QVERIFY(retryBtn->isHidden());
        QVERIFY(discardBtn->isHidden());
        QVERIFY(newFromRun->isHidden());

        QSignalSpy pauseSpy(&acc, &AgentControlCentre::pauseJobRequested);
        click(pauseBtn);
        QCOMPARE(pauseSpy.count(), 1);
        QCOMPARE(pauseSpy.first().at(0).toString(), tmp.path());

        // Paused state: Resume, Discard, and New production available; Pause hidden.
        acc.setWorkerProcessRunning(false);
        acc.loadJobStatus(sampleJob(QStringLiteral("paused")));
        QVERIFY(pauseBtn->isHidden());
        QVERIFY(!resumeBtn->isHidden() && resumeBtn->isEnabled());
        QVERIFY(retryBtn->isHidden());
        QVERIFY(!discardBtn->isHidden() && discardBtn->isEnabled());
        QVERIFY(!newFromRun->isHidden() && newFromRun->isEnabled());
        QVERIFY(acc.statusText().contains(QStringLiteral("Paused")));

        // Failed state: Retry, Discard, Resume, and New production all available.
        acc.loadJobStatus(sampleJob(QStringLiteral("failed")));
        QVERIFY(!retryBtn->isHidden() && retryBtn->isEnabled());
        QVERIFY(!discardBtn->isHidden() && discardBtn->isEnabled());
        QVERIFY(!newFromRun->isHidden() && newFromRun->isEnabled());

        QSignalSpy retrySpy(&acc, &AgentControlCentre::retryJobRequested);
        QSignalSpy discardSpy(&acc, &AgentControlCentre::discardJobRequested);
        QSignalSpy newProdSpy(&acc, &AgentControlCentre::newProductionRequested);
        click(retryBtn);
        click(discardBtn);
        QCOMPARE(retrySpy.count(), 1);
        QCOMPARE(retrySpy.first().at(0).toString(), tmp.path());
        QCOMPARE(discardSpy.count(), 1);
        QCOMPARE(discardSpy.first().at(0).toString(), tmp.path());

        // New production from run view never traps the user on a failed run.
        click(newFromRun);
        QCOMPARE(newProdSpy.count(), 1);
        QVERIFY(acc.activeJobDir().isEmpty());
        QVERIFY(!acc.findChild<QWidget *>(QStringLiteral("acc_setup"))->isHidden());
    }

    void setupControlsMediaSourceOutputFormatAndTargetDuration()
    {
        AgentControlCentre acc;
        QVERIFY(acc.minimumWidth() <= 280);
        auto *mediaSrc = acc.findChild<QComboBox *>(QStringLiteral("acc_media_source"));
        auto *outFmt = acc.findChild<QComboBox *>(QStringLiteral("acc_output_format"));
        auto *targetDur = acc.findChild<QSpinBox *>(QStringLiteral("acc_target_duration"));
        QVERIFY(mediaSrc && outFmt && targetDur);

        // Default selections append none of the optional V1.7 CLI flags.
        QCOMPARE(acc.selectedMediaSource(), QStringLiteral("sequence"));
        QCOMPARE(acc.selectedOutputFormat(), QStringLiteral("match"));
        QCOMPARE(acc.targetDurationMinutes(), 0);
        const QStringList defaultArgs = acc.buildRunArguments({QStringLiteral("/m/a.mp4")});
        QVERIFY(!defaultArgs.contains(QStringLiteral("--aspect-ratio")));
        QVERIFY(!defaultArgs.contains(QStringLiteral("--target-duration-sec")));
        QVERIFY(!defaultArgs.contains(QStringLiteral("--media-source")));

        // Explicit selections emit corresponding CLI flags before --inputs.
        acc.setSelectedMediaSource(QStringLiteral("selected_bin"));
        acc.setSelectedOutputFormat(QStringLiteral("9:16"));
        acc.setTargetDurationMinutes(5);
        const QStringList args = acc.buildRunArguments({QStringLiteral("/m/a.mp4")});
        const int arIdx = args.indexOf(QStringLiteral("--aspect-ratio"));
        QVERIFY(arIdx > 0);
        QCOMPARE(args.at(arIdx + 1), QStringLiteral("9:16"));
        const int tdIdx = args.indexOf(QStringLiteral("--target-duration-sec"));
        QVERIFY(tdIdx > 0);
        QCOMPARE(args.at(tdIdx + 1), QStringLiteral("300"));
        const int msIdx = args.indexOf(QStringLiteral("--media-source"));
        QVERIFY(msIdx > 0);
        QCOMPARE(args.at(msIdx + 1), QStringLiteral("selected_bin"));
        QCOMPARE(args.last(), QStringLiteral("/m/a.mp4"));
    }

    void liveTelemetryElapsedResourceAndComputeTierBadge()
    {
        QTemporaryDir tmp;
        AgentControlCentre acc;
        acc.setActiveJobDir(tmp.path());
        acc.setWorkerProcessRunning(true);

        // 1. Local deterministic + live elapsed + resource usage
        QJsonObject detJob = sampleJob(QStringLiteral("running"));
        detJob.insert(QStringLiteral("started_at_utc"), QDateTime::currentDateTimeUtc().addSecs(-65).toString(Qt::ISODateWithMs));
        detJob.insert(QStringLiteral("reasoning"), QJsonObject{{QStringLiteral("mode"), QStringLiteral("local")}, {QStringLiteral("calls"), 0}});
        detJob.insert(QStringLiteral("resource_usage"), QJsonObject{{QStringLiteral("rss_mb"), 184.4}, {QStringLiteral("cpu_time_sec"), 12.6}});
        acc.loadJobStatus(detJob);
        QCOMPARE(acc.engineBadgeText(), QStringLiteral("Local deterministic"));
        QVERIFY(acc.resourceText().contains(QStringLiteral("RAM 184 MB")));
        QVERIFY(acc.resourceText().contains(QStringLiteral("CPU 12.6s")));
        QVERIFY(acc.elapsedText().contains(QLatin1Char(':')));

        // 2. Local neural
        QJsonObject neuralJob = detJob;
        QJsonObject agents = neuralJob.value(QStringLiteral("agents")).toObject();
        QJsonObject ei = agents.value(QStringLiteral("editorial_intelligence")).toObject();
        ei.insert(QStringLiteral("neural_inference"), true);
        agents.insert(QStringLiteral("editorial_intelligence"), ei);
        neuralJob.insert(QStringLiteral("agents"), agents);
        acc.loadJobStatus(neuralJob);
        QCOMPARE(acc.engineBadgeText(), QStringLiteral("Local neural"));

        // 3. Subscription AI + finished final duration
        QJsonObject subJob = sampleJob(QStringLiteral("draft_ready"));
        subJob.insert(QStringLiteral("processing_elapsed_sec"), 92.0);
        subJob.insert(QStringLiteral("reasoning"), QJsonObject{{QStringLiteral("mode"), QStringLiteral("subscription")}, {QStringLiteral("calls"), 2}});
        acc.setWorkerProcessRunning(false);
        acc.loadJobStatus(subJob);
        QCOMPARE(acc.engineBadgeText(), QStringLiteral("Subscription AI"));
        QCOMPARE(acc.elapsedText(), QStringLiteral("1:32"));
    }
};

QTEST_MAIN(TestAgentControlCentre)
#include "test_agent_control_centre.moc"
