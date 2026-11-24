/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "ui/BangOnProShortcutPresets.h"
#include "ui/BangOnProTheme.h"
#include "ui/BangOnProWorkspace.h"
#include "ui/ExternalProviderConsentDialog.h"
#include "ui/ShowProfileEditorDialog.h"
#include "ui/TranscriptEditorWidget.h"

#include <QAction>
#include <QApplication>
#include <QDockWidget>
#include <QJsonArray>
#include <QJsonObject>
#include <QMainWindow>
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

        ExternalProviderConsentDialog consentDlg(nullptr, QStringLiteral("BangOnProTest"), QStringLiteral("ConsentUnitTest"));
        // Default must be LocalOnly (free & offline, zero external transmission)
        QCOMPARE(consentDlg.currentSettings().mode, AiBackendMode::LocalOnly);
        QVERIFY(!consentDlg.currentSettings().isExternalTransmissionAllowed());
        QVERIFY(consentDlg.canAcceptConfiguration());

        // Switching to ExternalProvider without explicit consent MUST be blocked
        consentDlg.setBackendMode(AiBackendMode::ExternalProvider);
        consentDlg.setEndpointUrl(QStringLiteral("https://api.example.com/v1"));
        consentDlg.setModelName(QStringLiteral("gpt-4o-mini"));
        consentDlg.setExplicitConsentGranted(false);
        QVERIFY(!consentDlg.canAcceptConfiguration());
        QVERIFY(!consentDlg.currentSettings().isExternalTransmissionAllowed());

        // Granting explicit consent enables external mode
        consentDlg.setExplicitConsentGranted(true);
        QVERIFY(consentDlg.canAcceptConfiguration());
        QVERIFY(consentDlg.currentSettings().isExternalTransmissionAllowed());
    }
};

QTEST_MAIN(TestProWorkspace)
#include "test_pro_workspace.moc"
