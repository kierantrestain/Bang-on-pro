/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "ui/AgentControlCentre.h"

#include <QCheckBox>
#include <QComboBox>
#include <QDir>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QPushButton>
#include <QSpinBox>
#include <QSignalSpy>
#include <QTemporaryDir>
#include <QTest>

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
        QCOMPARE(acc.minimumWidth(), 280);
        auto *start = acc.findChild<QPushButton *>(QStringLiteral("acc_start"));
        QVERIFY(start);
        QVERIFY(start->property("bangOnProPrimary").toBool());
        QVERIFY(acc.findChild<QPushButton *>(QStringLiteral("acc_review_draft")));
        QVERIFY(acc.findChild<QPushButton *>(QStringLiteral("acc_open_draft")));
        QVERIFY(acc.findChild<QPushButton *>(QStringLiteral("acc_promote")));
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
        QVERIFY(args.contains(QStringLiteral("podcast_multicam")));
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
        // One primary action at a time: Start before a run, Review once a draft exists.
        QTemporaryDir tmp;
        AgentControlCentre acc;
        auto *start = acc.findChild<QPushButton *>(QStringLiteral("acc_start"));
        auto *review = acc.findChild<QPushButton *>(QStringLiteral("acc_review_draft"));
        QVERIFY(start->property("bangOnProPrimary").toBool());
        QVERIFY(!review->property("bangOnProPrimary").toBool());
        acc.setActiveJobDir(tmp.path());
        acc.loadJobStatus(sampleJob(QStringLiteral("draft_ready")));
        QVERIFY(review->property("bangOnProPrimary").toBool());
        QVERIFY(!start->property("bangOnProPrimary").toBool());
        QVERIFY(acc.agentStatus(QStringLiteral("colourist")).startsWith(QStringLiteral("Done")));
        // Setup collapses while reviewing; "New production" brings it back.
        auto *setup = acc.findChild<QWidget *>(QStringLiteral("acc_setup"));
        auto *newJob = acc.findChild<QPushButton *>(QStringLiteral("acc_new_job"));
        QVERIFY(setup && newJob);
        QVERIFY(setup->isHidden());
        QTest::mouseClick(newJob, Qt::LeftButton);
        QVERIFY(!setup->isHidden());
        auto *reviewTitle = acc.findChild<QLabel *>(QStringLiteral("acc_review_title"));
        QVERIFY(reviewTitle && reviewTitle->text().contains(QStringLiteral("1")));
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

    void agentActionsEmitSignals()
    {
        QTemporaryDir tmp;
        AgentControlCentre acc;
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
        QTest::mouseClick(reject, Qt::LeftButton);
        QTest::mouseClick(rerunBtn, Qt::LeftButton);
        QCOMPARE(decide.count(), 1);
        QCOMPARE(decide.first().at(1).toString(), QStringLiteral("colourist"));
        QCOMPARE(decide.first().at(2).toBool(), false);
        QCOMPARE(rerun.count(), 1);
        acc.setBusy(true, QStringLiteral("running"));
        QVERIFY(cancelBtn->isEnabled());
        QVERIFY(!reject->isEnabled());
        QTest::mouseClick(cancelBtn, Qt::LeftButton);
        QCOMPARE(cancel.count(), 1);
    }

    // ---- V1.2.2 controls -------------------------------------------------
    void queueToggleQueuesInsteadOfStarting()
    {
        AgentControlCentre acc;
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
        QTest::mouseClick(start, Qt::LeftButton);
        QCOMPARE(queued.count(), 1);
        QCOMPARE(started.count(), 0);
        QCOMPARE(queued.first().at(0).toStringList().first(), QStringLiteral("run"));
        acc.setQueueSelected(false);
        QVERIFY(!start->isEnabled()); // but not started
        QCOMPARE(start->text(), QStringLiteral("Start production"));
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
        QVERIFY(list->item(1)->text().contains(QStringLiteral("Draft ready")));
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
        auto *startTiming = acc.findChild<QPushButton *>(QStringLiteral("acc_timing_start"));
        auto *pause = acc.findChild<QPushButton *>(QStringLiteral("acc_timing_pause"));
        auto *finish = acc.findChild<QPushButton *>(QStringLiteral("acc_timing_finish"));
        auto *rating = acc.findChild<QSpinBox *>(QStringLiteral("acc_timing_rating"));
        QVERIFY(startTiming && pause && finish && rating);
        QVERIFY(!pause->isVisibleTo(&acc) && !finish->isVisibleTo(&acc)); // one quiet control until timing starts
        QSignalSpy startReq(&acc, &AgentControlCentre::timingStartRequested);
        acc.findChild<QLineEdit *>(QStringLiteral("acc_episode"))->setText(QStringLiteral("Episode 12"));
        QTest::mouseClick(startTiming, Qt::LeftButton);
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
        QTest::mouseClick(acc.findChild<QPushButton *>(QStringLiteral("acc_promote")), Qt::LeftButton);
        QCOMPARE(activity.last().at(0).toString(), QStringLiteral("approval"));
        QSignalSpy paused(&acc, &AgentControlCentre::timingPauseToggled);
        QTest::mouseClick(pause, Qt::LeftButton);
        QCOMPARE(paused.count(), 1);
        QVERIFY(paused.first().at(0).toBool());
        acc.setTimingActive(true, true);
        QCOMPARE(pause->text(), QStringLiteral("Resume"));
        const int before = activity.count();
        acc.noteActivity(QStringLiteral("editing")); // paused: not recorded
        QCOMPARE(activity.count(), before);
        QSignalSpy finished(&acc, &AgentControlCentre::timingFinishRequested);
        rating->setValue(5);
        QTest::mouseClick(finish, Qt::LeftButton);
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
        QVERIFY(acc.findChild<QLabel *>(QStringLiteral("acc_status"))->text().contains(QStringLiteral("machine 1:15")));
        acc.addCustomProfile(QStringLiteral("weekly_chat"), QStringLiteral("Weekly chat"));
        acc.addCustomProfile(QStringLiteral("weekly_chat"), QStringLiteral("Weekly chat")); // no duplicates
        auto *profiles = acc.findChild<QComboBox *>(QStringLiteral("acc_profile"));
        QCOMPARE(profiles->findData(QStringLiteral("weekly_chat")), profiles->count() - 1);
        profiles->setCurrentIndex(profiles->count() - 1);
        QVERIFY(acc.buildRunArguments({}).contains(QStringLiteral("weekly_chat")));
    }
};

QTEST_MAIN(TestAgentControlCentre)
#include "test_agent_control_centre.moc"
