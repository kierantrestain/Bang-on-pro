/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "ui/AgentControlCentre.h"

#include <QDir>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QPushButton>
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
};

QTEST_MAIN(TestAgentControlCentre)
#include "test_agent_control_centre.moc"
