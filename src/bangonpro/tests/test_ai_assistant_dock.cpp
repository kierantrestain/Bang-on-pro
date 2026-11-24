/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "../ui/AiAssistantDock.h"
#include "../ui/BangOnProWorkspace.h"

#include <KAboutData>
#include <QDockWidget>
#include <QMainWindow>
#include <QPushButton>
#include <QSignalSpy>
#include <QtTest/QtTest>

using namespace BangOnPro;

class TestAiAssistantDock : public QObject
{
    Q_OBJECT

private Q_SLOTS:
    void testAboutDataAndBranding()
    {
        const KAboutData about = createAboutData();
        QCOMPARE(about.displayName(), QStringLiteral("Bang On Pro"));
        QVERIFY(about.shortDescription().contains(QStringLiteral("Kdenlive")));
        QCOMPARE(brandAccentHex(), QStringLiteral("#FF5A1F"));
        QVERIFY(brandStyleSheet().contains(QStringLiteral("#FF5A1F")));
    }

    void testProposalDisplayAndApprovalSignals()
    {
        AiAssistantDock dock;
        dock.setTimelineRevision(3);
        dock.setReviewConfidenceThreshold(0.75);

        TimelineCommandBatch batch;
        batch.requestId = QStringLiteral("req-test-1");
        batch.projectId = QStringLiteral("proj-1");
        batch.sequenceId = QStringLiteral("seq-1");
        batch.expectedRevision = 3;
        batch.transactionLabel = QStringLiteral("Podcast Rough Cut");

        TimelineOperation op1;
        op1.opId = QStringLiteral("op-1");
        op1.type = QStringLiteral("sync_clips");
        op1.trackId = QStringLiteral("V1");
        op1.confidence = 0.95;
        op1.rationale = QStringLiteral("Audio GCC-PHAT peak lock");

        TimelineOperation op2;
        op2.opId = QStringLiteral("op-2");
        op2.type = QStringLiteral("select_camera");
        op2.trackId = QStringLiteral("V2");
        op2.confidence = 0.55;
        op2.rationale = QStringLiteral("Ambiguous cross-talk reaction shot");

        batch.operations = {op1, op2};
        dock.displayProposal(batch);

        QSignalSpy approveSpy(&dock, &AiAssistantDock::batchApproveRequested);
        auto *acceptBtn = dock.findChild<QPushButton *>(QStringLiteral("acceptProposalButton"));
        QVERIFY(acceptBtn != nullptr);
        QVERIFY(acceptBtn->isEnabled());
        acceptBtn->click();
        QCOMPARE(approveSpy.count(), 1);

        QSignalSpy compareSpy(&dock, &AiAssistantDock::compareModeToggled);
        auto *compareBtn = dock.findChild<QPushButton *>(QStringLiteral("compareToggleButton"));
        QVERIFY(compareBtn != nullptr);
        compareBtn->click();
        QCOMPARE(compareSpy.count(), 1);
    }

    void testPremiereWorkspaceLayout()
    {
        QMainWindow mw;
        auto *clipDock = new QDockWidget(QStringLiteral("Clip Monitor"), &mw);
        clipDock->setObjectName(QStringLiteral("clip_monitor"));
        auto *projDock = new QDockWidget(QStringLiteral("Project Monitor"), &mw);
        projDock->setObjectName(QStringLiteral("project_monitor"));
        auto *binDock = new QDockWidget(QStringLiteral("Project Bin"), &mw);
        binDock->setObjectName(QStringLiteral("project_bin"));
        mw.addDockWidget(Qt::TopDockWidgetArea, clipDock);
        mw.addDockWidget(Qt::TopDockWidgetArea, projDock);
        mw.addDockWidget(Qt::LeftDockWidgetArea, binDock);

        QDockWidget *aiDock = createAiAssistantDock(&mw);
        QVERIFY(aiDock != nullptr);
        QVERIFY(applyPremiereWorkspaceLayout(&mw));
        QCOMPARE(mw.dockWidgetArea(aiDock), Qt::RightDockWidgetArea);
    }

    void testPerChangeCheckboxesAndBinToggle()
    {
        AiAssistantDock dock;
        dock.setTimelineRevision(7);
        dock.setReviewConfidenceThreshold(0.75);

        QVERIFY(dock.useProjectBinMedia());
        dock.setUseProjectBinMedia(false);
        QVERIFY(!dock.useProjectBinMedia());
        dock.setUseProjectBinMedia(true);
        QVERIFY(dock.useProjectBinMedia());

        TimelineCommandBatch batch;
        batch.requestId = QStringLiteral("req-partial-1");
        batch.projectId = QStringLiteral("proj-1");
        batch.sequenceId = QStringLiteral("seq-1");
        batch.expectedRevision = 7;
        batch.transactionLabel = QStringLiteral("Selective Approval Batch");

        TimelineOperation highConf;
        highConf.opId = QStringLiteral("op-high");
        highConf.type = QStringLiteral("sync_clips");
        highConf.confidence = 0.96;

        TimelineOperation lowConf;
        lowConf.opId = QStringLiteral("op-low");
        lowConf.type = QStringLiteral("select_camera");
        lowConf.confidence = 0.42;

        batch.operations = {highConf, lowConf};
        dock.displayProposal(batch);

        QCOMPARE(dock.selectedOperationIds().size(), 2);
        QCOMPARE(dock.lowConfidenceOperationCount(), 1);

        // Uncheck low-confidence items and verify filtered approval emits only high-confidence op
        dock.uncheckLowConfidenceOperations();
        QCOMPARE(dock.selectedOperationIds(), QStringList{QStringLiteral("op-high")});

        QSignalSpy approveSpy(&dock, &AiAssistantDock::batchApproveRequested);
        auto *acceptBtn = dock.findChild<QPushButton *>(QStringLiteral("acceptProposalButton"));
        QVERIFY(acceptBtn != nullptr);
        acceptBtn->click();
        QCOMPARE(approveSpy.count(), 1);
        const auto emittedBatch = approveSpy.takeFirst().at(0).value<TimelineCommandBatch>();
        QCOMPARE(emittedBatch.operations.size(), 1);
        QCOMPARE(emittedBatch.operations.first().opId, QStringLiteral("op-high"));
    }
};

QTEST_MAIN(TestAiAssistantDock)
#include "test_ai_assistant_dock.moc"
