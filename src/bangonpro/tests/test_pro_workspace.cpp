/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "ui/BangOnProShortcutPresets.h"
#include "ui/BangOnProTheme.h"
#include "ui/BangOnProWorkspace.h"
#include "ui/ExternalProviderConsentDialog.h"
#include "ui/ModelManagerDialog.h"
#include "ui/ShowProfileEditorDialog.h"
#include "ui/TranscriptEditorWidget.h"

#include <QAction>
#include <QApplication>
#include <QDockWidget>
#include <QImage>
#include <QJsonArray>
#include <QJsonObject>
#include <QMainWindow>
#include <QScreen>
#include <QSignalSpy>
#include <QTemporaryDir>
#include <QtTest>

using namespace BangOnPro;

class TestProWorkspace : public QObject
{
    Q_OBJECT

private Q_SLOTS:
    void testStudioThemeAndPalette()
    {
        const QPalette pal = createBangOnProPalette();
        QCOMPARE(pal.color(QPalette::Window), QColor(QString::fromLatin1(StudioPaletteTokens::kPanelSurface)));
        QCOMPARE(pal.color(QPalette::Dark), QColor(QString::fromLatin1(StudioPaletteTokens::kWindowChrome)));
        QCOMPARE(pal.color(QPalette::Highlight), QColor(QString::fromLatin1(StudioPaletteTokens::kAccentSelection)));
        const QString qss = globalApplicationStyleSheet();
        QVERIFY(qss.contains(QStringLiteral("QDockWidget")));
        QVERIFY(qss.contains(QStringLiteral("#FF5A1F")));
        QVERIFY(applyBangOnProTheme(qApp));
    }

    void testDesignSystemSingleSourceAndTally()
    {
        // V1.2.1: one design system. The legacy brand stylesheet is the shared component layer.
        QCOMPARE(brandStyleSheet(), componentStyleSheet());
        const QString qss = globalApplicationStyleSheet();
        QVERIFY(qss.contains(componentStyleSheet()));
        QVERIFY(!qss.contains(QStringLiteral("{{")));
        QVERIFY(!qss.contains(QStringLiteral("font-weight: 700")));
        QVERIFY(qss.contains(QStringLiteral("bopRole=\"primary\"")));
        const QImage img = tallyIcon(TallyState::Done).pixmap(12, 12).toImage();
        QCOMPARE(img.pixelColor(6, 6).name(), QColor(QString::fromLatin1(StudioPaletteTokens::kStatusSuccess)).name());
        QCOMPARE(tallyColor(TallyState::Running), QColor(QString::fromLatin1(StudioPaletteTokens::kAccentPrimary)));
    }

    void testDenseDocksCanShrinkAndWindowFitsScreen()
    {
        // Regression: dense docks (AI Assistant min height 782 px, Premiere Presets min width 664 px)
        // forced the main window beyond 1920x1080. The shared scroll container lets them shrink.
        auto *dense = new QWidget;
        dense->setMinimumSize(900, 900);
        QWidget *scroll = makeScrollableDockContent(dense);
        QVERIFY(scroll->minimumSizeHint().width() <= 200);
        QVERIFY(scroll->minimumSizeHint().height() <= 200);
        delete scroll;

        QMainWindow mw;
        mw.resize(5000, 4000);
        mw.show();
        QVERIFY(QTest::qWaitForWindowExposed(&mw) || true);
        const QRect avail = mw.screen()->availableGeometry();
        fitWindowToScreen(&mw);
        QVERIFY(mw.width() <= avail.width());
        QVERIFY(mw.height() <= avail.height());
        QVERIFY(!fitWindowToScreen(&mw)); // idempotent once inside the screen
    }

    void testAiFirstCutGivesProductionColumnFullHeight()
    {
        QMainWindow mw;
        mw.resize(1440, 900);
        WorkspaceDocks docks;
        auto makeDock = [&mw](const char *name) {
            auto *d = new QDockWidget(QString::fromLatin1(name), &mw);
            d->setObjectName(QString::fromLatin1(name));
            d->setWidget(new QWidget(d));
            mw.addDockWidget(Qt::LeftDockWidgetArea, d);
            return d;
        };
        docks.projectMonitor = makeDock(kProjectMonitorDockName);
        docks.clipMonitor = makeDock(kClipMonitorDockName);
        docks.projectBin = makeDock(kProjectBinDockName);
        docks.aiAssistant = makeDock(kAiAssistantDockName);
        docks.transcriptDock = makeDock(kTranscriptDockName);
        docks.agentControlCentre = makeDock(kAgentControlCentreDockName);
        mw.setCentralWidget(new QWidget(&mw));
        mw.show();
        QVERIFY(applyWorkspace(&mw, WorkspaceMode::AiFirstCut, docks));
        QCOMPARE(mw.dockWidgetArea(docks.agentControlCentre), Qt::RightDockWidgetArea);
        QCOMPARE(mw.dockWidgetArea(docks.projectMonitor), Qt::TopDockWidgetArea);
        QVERIFY(mw.tabifiedDockWidgets(docks.agentControlCentre).contains(docks.aiAssistant));
        QCOMPARE(mw.corner(Qt::TopRightCorner), Qt::RightDockWidgetArea);
        // Dock titles never show accelerator markers in the styled title bar.
        docks.clipMonitor->setWindowTitle(QStringLiteral("C&lip Monitor"));
        docks.projectBin->setWindowTitle(QStringLiteral("Roto && 2D"));
        QVERIFY(applyWorkspace(&mw, WorkspaceMode::Edit, docks));
        QCOMPARE(docks.clipMonitor->windowTitle(), QStringLiteral("Clip Monitor"));
        QCOMPARE(docks.projectBin->windowTitle(), QStringLiteral("Roto & 2D"));
    }

    void testAllNineStudioWorkspacesAndPersistence()
    {
        QMainWindow mw;
        mw.resize(1400, 900);
        WorkspaceDocks docks;
        auto makeDock = [&mw](const char *name) {
            auto *d = new QDockWidget(QString::fromLatin1(name), &mw);
            d->setObjectName(QString::fromLatin1(name));
            return d;
        };
        docks.clipMonitor = makeDock(kClipMonitorDockName);
        docks.projectMonitor = makeDock(kProjectMonitorDockName);
        docks.projectBin = makeDock(kProjectBinDockName);
        docks.effectList = makeDock(kEffectListDockName);
        docks.effectStack = makeDock(kEffectStackDockName);
        docks.premiereBrowser = makeDock(kPremiereBrowserDockName);
        docks.audioMixer = makeDock(kAudioMixerDockName);
        docks.aiAssistant = makeDock(kAiAssistantDockName);
        docks.rotoStudio = makeDock(kRotoStudioDockName);
        docks.transcriptDock = makeDock(kTranscriptDockName);

        QCOMPARE(allWorkspaceModes().size(), 9);
        for (const WorkspaceMode mode : allWorkspaceModes()) {
            QVERIFY(applyWorkspace(&mw, mode, docks));
            const QString id = workspaceModeToId(mode);
            WorkspaceMode parsed = WorkspaceMode::Edit;
            QVERIFY(workspaceModeFromId(id, &parsed));
            QCOMPARE(parsed, mode);
        }

        // Save, list, restore, and reset custom workspace
        const QString customName = QStringLiteral("UnitTestCustomStudio");
        QVERIFY(saveWorkspaceState(&mw, customName));
        QVERIFY(savedCustomWorkspaces().contains(customName));
        QVERIFY(restoreWorkspaceState(&mw, customName));
        QVERIFY(resetWorkspaceState(customName));
    }

    void testWorkspaceSwitcherBarSignals()
    {
        WorkspaceSwitcherBar bar;
        QSignalSpy modeSpy(&bar, &WorkspaceSwitcherBar::workspaceSelected);
        QSignalSpy aiSpy(&bar, &WorkspaceSwitcherBar::aiPanelToggled);
        bar.setActiveWorkspace(WorkspaceMode::Rotoscoping, true);
        QCOMPARE(modeSpy.count(), 1);
        QCOMPARE(bar.activeWorkspace(), WorkspaceMode::Rotoscoping);
        const bool nextVis = !bar.isAiPanelVisible();
        bar.setAiPanelVisible(nextVis, true);
        QCOMPARE(aiSpy.count(), 1);
        QCOMPARE(bar.isAiPanelVisible(), nextVis);
    }

    void testShortcutPresetsAndOverrides()
    {
        ShortcutPresetManager mgr(QStringLiteral("BangOnProTest"), QStringLiteral("ShortcutPresetsUnitTest"));
        QVERIFY(mgr.availablePresetIds().contains(QString::fromLatin1(kShortcutPresetPremiere)));
        QVERIFY(mgr.availablePresetIds().contains(QString::fromLatin1(kShortcutPresetResolve)));
        QVERIFY(mgr.availablePresetIds().contains(QString::fromLatin1(kShortcutPresetKdenlive)));

        QMainWindow mw;
        auto *razorAct = new QAction(QStringLiteral("Razor Tool"), &mw);
        razorAct->setObjectName(QStringLiteral("razor_tool"));
        auto *selectAct = new QAction(QStringLiteral("Select Tool"), &mw);
        selectAct->setObjectName(QStringLiteral("select_tool"));
        mw.addAction(razorAct);
        mw.addAction(selectAct);

        mgr.resetPresetToDefaults(QString::fromLatin1(kShortcutPresetPremiere));
        const int applied = mgr.applyPreset(&mw, QString::fromLatin1(kShortcutPresetPremiere));
        QVERIFY(applied >= 2);
        QCOMPARE(razorAct->shortcut(), QKeySequence(QStringLiteral("C")));
        QCOMPARE(selectAct->shortcut(), QKeySequence(QStringLiteral("V")));

        // Custom override round-trip
        mgr.setCustomOverride(QString::fromLatin1(kShortcutPresetPremiere), QStringLiteral("razor_tool"), QKeySequence(QStringLiteral("B")));
        mgr.applyPreset(&mw, QString::fromLatin1(kShortcutPresetPremiere));
        QCOMPARE(razorAct->shortcut(), QKeySequence(QStringLiteral("B")));
        mgr.resetPresetToDefaults(QString::fromLatin1(kShortcutPresetPremiere));
    }

    void testTranscriptEditorSeekEditAndCut()
    {
        TranscriptEditorWidget editor;
        QJsonObject doc;
        doc.insert(QStringLiteral("fps"), 25.0);
        QJsonArray segs;
        QJsonObject s1;
        s1.insert(QStringLiteral("segment_id"), QStringLiteral("seg_1"));
        s1.insert(QStringLiteral("speaker_id"), QStringLiteral("Host"));
        s1.insert(QStringLiteral("start_sec"), 1.0);
        s1.insert(QStringLiteral("end_sec"), 3.0);
        s1.insert(QStringLiteral("text"), QStringLiteral("Welcome to Bang On Pro"));
        s1.insert(QStringLiteral("confidence"), 0.94);
        segs.append(s1);
        QJsonObject s2;
        s2.insert(QStringLiteral("segment_id"), QStringLiteral("seg_2"));
        s2.insert(QStringLiteral("speaker_id"), QStringLiteral("Guest"));
        s2.insert(QStringLiteral("start_sec"), 2.5);
        s2.insert(QStringLiteral("end_sec"), 5.0);
        s2.insert(QStringLiteral("text"), QStringLiteral("Overlapping response"));
        s2.insert(QStringLiteral("confidence"), 0.65);
        segs.append(s2);
        doc.insert(QStringLiteral("segments"), segs);

        QVERIFY(editor.loadFromJson(doc));
        QCOMPARE(editor.segmentCount(), 2);
        QCOMPARE(editor.overlapSegmentCount(), 2);
        QCOMPARE(editor.renameSpeaker(QStringLiteral("Host"), QStringLiteral("Kieran")), 1);
        QVERIFY(editor.setSegmentText(QStringLiteral("seg_1"), QStringLiteral("Welcome to Bang On Pro v1.0")));
        QVERIFY(editor.setSegmentCut(QStringLiteral("seg_2"), true));
        QCOMPARE(editor.cutSegmentCount(), 1);

        const QJsonObject exported = editor.exportEditedTranscriptJson();
        QCOMPARE(exported.value(QStringLiteral("segments")).toArray().size(), 2);
    }

    void testShowProfileEditorAndConsentDialog()
    {
        QTemporaryDir tmp;
        QVERIFY(tmp.isValid());

        ShowProfileEditorDialog profDlg;
        profDlg.setProfileId(QStringLiteral("custom_podcast_pro"));
        profDlg.setDisplayName(QStringLiteral("Custom Podcast Pro"));
        profDlg.setPacingRules(2.0, 10.0, 5.0);
        profDlg.setAudioRules(-16.0, true, true, -20.0);
        profDlg.setGraphicsRules(true, true, 4.0, QStringLiteral("#FF5A1F"));
        QVERIFY(profDlg.isValid());
        const QString outPath = tmp.filePath(QStringLiteral("custom_profile.json"));
        QVERIFY(profDlg.saveProfileToFile(outPath));

        ExternalProviderConsentDialog consentDlg(nullptr, {}, QStringLiteral("BangOnProTest"), QStringLiteral("ConsentUnitTest"));
        QCOMPARE(consentDlg.windowTitle(), QStringLiteral("AI Providers & Subscriptions"));
        for (const QString &provider : {QStringLiteral("claude"), QStringLiteral("codex"), QStringLiteral("grok"), QStringLiteral("agy")}) {
            QVERIFY(consentDlg.findChild<QWidget *>(QStringLiteral("subscriptionProvider_") + provider));
        }
        QVERIFY(!consentDlg.findChild<QWidget *>(QStringLiteral("externalEndpointUrlEdit")));
        QVERIFY(!consentDlg.findChild<QWidget *>(QStringLiteral("externalModelNameEdit")));
        // Default must be LocalOnly (free & offline, zero external transmission)
        QCOMPARE(consentDlg.currentSettings().mode, AiBackendMode::LocalOnly);
        QVERIFY(!consentDlg.currentSettings().isExternalTransmissionAllowed());
        QVERIFY(consentDlg.canAcceptConfiguration());

        // Switching to ExternalProvider without explicit consent MUST be blocked
        consentDlg.setBackendMode(AiBackendMode::ExternalProvider);
        consentDlg.setExplicitConsentGranted(false);
        QVERIFY(!consentDlg.canAcceptConfiguration());
        QVERIFY(!consentDlg.currentSettings().isExternalTransmissionAllowed());

        // Granting explicit consent enables external mode
        consentDlg.setExplicitConsentGranted(true);
        QVERIFY(consentDlg.canAcceptConfiguration());
        QVERIFY(consentDlg.currentSettings().isExternalTransmissionAllowed());
    }

    void testShortcutConflictDetectionAndModelManagerDialog()
    {
        ShortcutPresetManager mgr(QStringLiteral("BangOnProTest"), QStringLiteral("ShortcutConflictsUnitTest"));
        for (const QString &presetId : mgr.availablePresetIds()) {
            mgr.resetPresetToDefaults(presetId);
            const auto conflicts = mgr.detectShortcutConflicts(presetId, true);
            QVERIFY2(conflicts.isEmpty(), qPrintable(QStringLiteral("Unexpected default shortcut conflict in preset %1").arg(presetId)));
        }

        // Inject an intentional duplicate custom override and verify conflict detection catches it
        mgr.setCustomOverride(QString::fromLatin1(kShortcutPresetPremiere), QStringLiteral("razor_tool"), QKeySequence(QStringLiteral("V")));
        const auto injected = mgr.detectShortcutConflicts(QString::fromLatin1(kShortcutPresetPremiere), true);
        QVERIFY(!injected.isEmpty());
        QVERIFY(injected.first().actionIds.contains(QStringLiteral("razor_tool")));
        QVERIFY(injected.first().actionIds.contains(QStringLiteral("select_tool")));
        mgr.resetPresetToDefaults(QString::fromLatin1(kShortcutPresetPremiere));

        // Verify ModelManagerDialog parses hardware & honest neural-vs-fallback model catalog JSON
        ModelManagerDialog modelDlg(QStringLiteral("python3"), QStringLiteral("/tmp"));
        QJsonObject report;
        report.insert(QStringLiteral("model_dir"), QStringLiteral("/tmp/models"));
        report.insert(QStringLiteral("total_mb"), 75.5);
        report.insert(QStringLiteral("installed_count"), 1);
        report.insert(QStringLiteral("ready_count"), 0);
        report.insert(QStringLiteral("hardware"), QJsonObject{{QStringLiteral("os_name"), QStringLiteral("Darwin")},
                                                              {QStringLiteral("arch"), QStringLiteral("arm64")},
                                                              {QStringLiteral("is_apple_silicon"), true},
                                                              {QStringLiteral("total_ram_gb"), 16.0},
                                                              {QStringLiteral("ram_tier"), QStringLiteral("medium_16gb")},
                                                              {QStringLiteral("mps_available"), true},
                                                              {QStringLiteral("coreml_available"), true}});
        QJsonObject m1;
        m1.insert(QStringLiteral("model_id"), QStringLiteral("faster-whisper-small"));
        m1.insert(QStringLiteral("display_name"), QStringLiteral("Faster-Whisper Small"));
        m1.insert(QStringLiteral("family"), QStringLiteral("asr_whisper"));
        m1.insert(QStringLiteral("runtime_backend"), QStringLiteral("faster_whisper"));
        m1.insert(QStringLiteral("license_spdx"), QStringLiteral("MIT"));
        m1.insert(QStringLiteral("disk_size_mb"), 244);
        m1.insert(QStringLiteral("disk_usage_bytes"), 79167488);
        m1.insert(QStringLiteral("backend_package_installed"), false);
        m1.insert(QStringLiteral("model_available_on_disk"), true);
        m1.insert(QStringLiteral("status"), QStringLiteral("backend_missing"));
        m1.insert(QStringLiteral("hardware_compatible"), true);
        m1.insert(QStringLiteral("recommended"), true);
        report.insert(QStringLiteral("models"), QJsonArray{m1});

        QVERIFY(modelDlg.loadStatusJson(report));
        QCOMPARE(modelDlg.modelCount(), 1);
        QVERIFY(modelDlg.hardwareSummaryText().contains(QStringLiteral("Apple Silicon")));
        QVERIFY(modelDlg.selectModelById(QStringLiteral("faster-whisper-small")));
        QCOMPARE(modelDlg.selectedModelId(), QStringLiteral("faster-whisper-small"));
    }
};

QTEST_MAIN(TestProWorkspace)
#include "test_pro_workspace.moc"
