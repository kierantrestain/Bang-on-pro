/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "BangOnProWorkspace.h"
#include "AiAssistantDock.h"

#include <QCoreApplication>
#include <QDockWidget>
#include <QList>
#include <QMainWindow>

namespace BangOnPro {

namespace {

QDockWidget *resolveDock(QMainWindow *mainWindow, QDockWidget *explicitDock, const char *objectName)
{
    if (explicitDock != nullptr) {
        return explicitDock;
    }
    if (mainWindow == nullptr) {
        return nullptr;
    }
    return mainWindow->findChild<QDockWidget *>(QString::fromLatin1(objectName));
}

} // namespace

KAboutData createAboutData()
{
    const QString displayName = QString::fromLatin1(kAppDisplayName);
    const QString version = QString::fromLatin1(kAppVersion);
    const QString shortDesc = QCoreApplication::translate("BangOnProWorkspace", "AI-Native Professional Video Editor built on Kdenlive & MLT");
    const QString copyright = QCoreApplication::translate(
        "BangOnProWorkspace", "Copyright © 2026 Bang On Pro Contributors. Built upon Kdenlive (Copyright © 2007–2024 Kdenlive authors).");
    const QString otherText =
        QCoreApplication::translate("BangOnProWorkspace", "Bang On Pro pairs full manual non-linear editing with human-in-the-loop AI production workflows, "
                                                          "Premiere preset compatibility, and Apple Silicon hardware optimization.");

    KAboutData aboutData(QStringLiteral("bangonpro"), displayName, version, shortDesc, KAboutLicense::GPL_V3, copyright, otherText,
                         QStringLiteral("https://bangonpro.dev"), QStringLiteral("https://github.com/kierantrestain/Bang-on-pro/issues"));

    aboutData.setOrganizationDomain(QByteArrayLiteral("bangonpro.dev"));
    aboutData.setDesktopFileName(QStringLiteral("dev.bangonpro.BangOnPro"));

    aboutData.addAuthor(QStringLiteral("Bang On Pro Engineering Team"),
                        QCoreApplication::translate("BangOnProWorkspace", "AI Timeline API, Production Pipeline, Premiere Engine & UI"), QString(),
                        QStringLiteral("https://bangonpro.dev"));

    aboutData.addCredit(QStringLiteral("Kdenlive / KDE Community"),
                        QCoreApplication::translate("BangOnProWorkspace", "Upstream open-source NLE foundation (release/24.08)"), QString(),
                        QStringLiteral("https://kdenlive.org"));
    aboutData.addCredit(QStringLiteral("MLT Framework"), QCoreApplication::translate("BangOnProWorkspace", "Open-source multimedia engine"), QString(),
                        QStringLiteral("https://mltframework.org"));
    aboutData.addCredit(QStringLiteral("FFmpeg"), QCoreApplication::translate("BangOnProWorkspace", "Media decoding, encoding, and VideoToolbox acceleration"),
                        QString(), QStringLiteral("https://ffmpeg.org"));

    aboutData.addComponent(QStringLiteral("Kdenlive Base"), QCoreApplication::translate("BangOnProWorkspace", "Upstream NLE Core"), QStringLiteral("24.08"),
                           QStringLiteral("https://kdenlive.org"));
    aboutData.addComponent(QStringLiteral("Bang On Pro Timeline API"), QCoreApplication::translate("BangOnProWorkspace", "Structured AI Command Contract"),
                           QString::fromLatin1(kTimelineApiVersion), QStringLiteral("https://bangonpro.dev"));

    return aboutData;
}

QString brandAccentHex()
{
    return QStringLiteral("#FF5A1F");
}

QString brandStyleSheet()
{
    return QStringLiteral("QWidget#bangOnProAiAssistant {"
                          "  background-color: #16181D;"
                          "  color: #E8ECF2;"
                          "}"
                          "QFrame#bangOnProHeader {"
                          "  background-color: #1F232B;"
                          "  border: 1px solid #2F3542;"
                          "  border-left: 4px solid %1;"
                          "  border-radius: 6px;"
                          "}"
                          "QLabel#bangOnProTitle {"
                          "  font-size: 14px;"
                          "  font-weight: 700;"
                          "  color: #FFFFFF;"
                          "}"
                          "QLabel#bangOnProBadge {"
                          "  background-color: %1;"
                          "  color: #FFFFFF;"
                          "  font-size: 10px;"
                          "  font-weight: 700;"
                          "  padding: 2px 6px;"
                          "  border-radius: 3px;"
                          "}"
                          "QLabel#bangOnProStatusBadge {"
                          "  background-color: #2A303C;"
                          "  color: #CFD6E3;"
                          "  font-size: 10px;"
                          "  font-weight: 600;"
                          "  padding: 2px 7px;"
                          "  border-radius: 9px;"
                          "}"
                          "QLabel#bangOnProStatusBadge[statusState=\"proposal\"] {"
                          "  background-color: #4D2A12;"
                          "  color: #FFB07C;"
                          "}"
                          "QLabel#bangOnProStatusBadge[statusState=\"ok\"] {"
                          "  background-color: #1A3E2A;"
                          "  color: #7AE098;"
                          "}"
                          "QLabel#bangOnProStatusBadge[statusState=\"busy\"] {"
                          "  background-color: #1C3554;"
                          "  color: #7CC0FF;"
                          "}"
                          "QLabel#bangOnProStatusBadge[statusState=\"error\"] {"
                          "  background-color: #541D24;"
                          "  color: #FF8E9B;"
                          "}"
                          "QLabel#bangOnProSubtitle, QLabel#bangOnProMutedLabel, QLabel#bangOnProRevision, QLabel#taskStatusLabel {"
                          "  color: #9AA4B5;"
                          "  font-size: 11px;"
                          "}"
                          "QLabel#bangOnProSectionLabel {"
                          "  color: #DCE2EC;"
                          "  font-weight: 600;"
                          "  font-size: 11px;"
                          "}"
                          "QLabel#proposalSummaryLabel {"
                          "  color: #FFB07C;"
                          "  font-size: 11px;"
                          "  font-weight: 600;"
                          "}"
                          "QComboBox#showProfileCombo, QPlainTextEdit#instructionInput, QPlainTextEdit#agentActivityLog, QTreeWidget#proposedDiffTree {"
                          "  background-color: #111318;"
                          "  color: #E8ECF2;"
                          "  border: 1px solid #2E3440;"
                          "  border-radius: 4px;"
                          "}"
                          "QTreeWidget#proposedDiffTree {"
                          "  alternate-background-color: #1A1D24;"
                          "}"
                          "QHeaderView::section {"
                          "  background-color: #222630;"
                          "  color: #C7CFDD;"
                          "  border: 0px;"
                          "  border-right: 1px solid #2E3440;"
                          "  padding: 4px 6px;"
                          "  font-size: 11px;"
                          "  font-weight: 600;"
                          "}"
                          "QPushButton {"
                          "  background-color: #262B36;"
                          "  color: #E8ECF2;"
                          "  border: 1px solid #394050;"
                          "  border-radius: 4px;"
                          "  padding: 5px 8px;"
                          "  font-size: 11px;"
                          "}"
                          "QPushButton:hover { background-color: #313847; }"
                          "QPushButton:disabled { background-color: #1D2027; color: #687182; border-color: #262B36; }"
                          "QPushButton[bangOnProChip=\"true\"] {"
                          "  background-color: #202530;"
                          "  border: 1px solid #353D4D;"
                          "  border-radius: 10px;"
                          "  padding: 4px 8px;"
                          "  font-size: 10px;"
                          "  text-align: left;"
                          "}"
                          "QPushButton[bangOnProChip=\"true\"]:hover {"
                          "  border-color: %1;"
                          "  color: #FFFFFF;"
                          "}"
                          "QPushButton[bangOnProPrimary=\"true\"] {"
                          "  background-color: %1;"
                          "  border-color: %1;"
                          "  color: #FFFFFF;"
                          "  font-weight: 700;"
                          "}"
                          "QPushButton[bangOnProPrimary=\"true\"]:hover { background-color: #FF713D; }"
                          "QPushButton[bangOnProAccept=\"true\"] {"
                          "  background-color: #217A4B;"
                          "  border-color: #2A965D;"
                          "  color: #FFFFFF;"
                          "  font-weight: 700;"
                          "}"
                          "QPushButton[bangOnProAccept=\"true\"]:hover { background-color: #28945B; }"
                          "QPushButton[bangOnProDanger=\"true\"]:hover { background-color: #6B232B; border-color: #993440; }"
                          "QPushButton#compareToggleButton:checked {"
                          "  background-color: #284C7A;"
                          "  border-color: #4D88D6;"
                          "  color: #FFFFFF;"
                          "  font-weight: 600;"
                          "}"
                          "QProgressBar#agentProgressBar {"
                          "  background-color: #111318;"
                          "  border: 1px solid #2E3440;"
                          "  border-radius: 3px;"
                          "  text-align: center;"
                          "  font-size: 10px;"
                          "  color: #E8ECF2;"
                          "}"
                          "QProgressBar#agentProgressBar::chunk {"
                          "  background-color: %1;"
                          "  border-radius: 2px;"
                          "}"
                          "QToolButton#activityLogToggleButton {"
                          "  color: #DCE2EC;"
                          "  font-weight: 600;"
                          "  font-size: 11px;"
                          "}")
        .arg(brandAccentHex());
}

QDockWidget *createAiAssistantDock(QMainWindow *mainWindow, AiAssistantDock *contentWidget, bool initiallyVisible)
{
    if (mainWindow == nullptr) {
        return nullptr;
    }

    auto *dock = new QDockWidget(QCoreApplication::translate("BangOnProWorkspace", "Bang On Pro — AI Assistant"), mainWindow);
    dock->setObjectName(QString::fromLatin1(kAiAssistantDockName));
    dock->setFeatures(QDockWidget::DockWidgetClosable | QDockWidget::DockWidgetMovable | QDockWidget::DockWidgetFloatable);
    dock->setAllowedAreas(Qt::RightDockWidgetArea | Qt::LeftDockWidgetArea);

    AiAssistantDock *widget = contentWidget != nullptr ? contentWidget : new AiAssistantDock(dock);
    dock->setWidget(widget);

    mainWindow->addDockWidget(Qt::RightDockWidgetArea, dock);
    dock->setVisible(initiallyVisible);
    return dock;
}

bool applyPremiereWorkspaceLayout(QMainWindow *mainWindow, const WorkspaceDocks &explicitDocks)
{
    if (mainWindow == nullptr) {
        return false;
    }

    QDockWidget *clipMonitor = resolveDock(mainWindow, explicitDocks.clipMonitor, kClipMonitorDockName);
    QDockWidget *projectMonitor = resolveDock(mainWindow, explicitDocks.projectMonitor, kProjectMonitorDockName);
    QDockWidget *projectBin = resolveDock(mainWindow, explicitDocks.projectBin, kProjectBinDockName);
    QDockWidget *effectList = resolveDock(mainWindow, explicitDocks.effectList, kEffectListDockName);
    QDockWidget *effectStack = resolveDock(mainWindow, explicitDocks.effectStack, kEffectStackDockName);
    QDockWidget *compositionList = resolveDock(mainWindow, explicitDocks.compositionList, kCompositionListDockName);
    QDockWidget *premiereBrowser = resolveDock(mainWindow, explicitDocks.premiereBrowser, kPremiereBrowserDockName);
    QDockWidget *audioMixer = resolveDock(mainWindow, explicitDocks.audioMixer, kAudioMixerDockName);
    QDockWidget *aiAssistant = resolveDock(mainWindow, explicitDocks.aiAssistant, kAiAssistantDockName);

    int arrangedCount = 0;

    mainWindow->setDockNestingEnabled(true);

    // Premiere-inspired corner ownership:
    // - Top area spans top-left to top-right for Effect Controls + Source & Programme Monitors
    // - Left dock area occupies bottom-left alongside the central Timeline (Project Bin + Effects/Premiere Browser)
    // - Right dock area hosts the collapsible AI Assistant cleanly without disturbing Timeline width
    mainWindow->setCorner(Qt::TopLeftCorner, Qt::TopDockWidgetArea);
    mainWindow->setCorner(Qt::TopRightCorner, Qt::RightDockWidgetArea);
    mainWindow->setCorner(Qt::BottomLeftCorner, Qt::LeftDockWidgetArea);
    mainWindow->setCorner(Qt::BottomRightCorner, Qt::RightDockWidgetArea);

    // 1. Top row: Effect Controls (optional top-left) + Source Monitor + Programme Monitor (top-right)
    QDockWidget *topAnchor = nullptr;
    if (effectStack != nullptr) {
        mainWindow->addDockWidget(Qt::TopDockWidgetArea, effectStack);
        effectStack->setFloating(false);
        effectStack->show();
        topAnchor = effectStack;
        ++arrangedCount;
    }

    if (clipMonitor != nullptr) {
        mainWindow->addDockWidget(Qt::TopDockWidgetArea, clipMonitor);
        clipMonitor->setFloating(false);
        clipMonitor->show();
        if (topAnchor != nullptr && topAnchor != clipMonitor) {
            mainWindow->splitDockWidget(topAnchor, clipMonitor, Qt::Horizontal);
        }
        topAnchor = clipMonitor;
        ++arrangedCount;
    }

    if (projectMonitor != nullptr) {
        mainWindow->addDockWidget(Qt::TopDockWidgetArea, projectMonitor);
        projectMonitor->setFloating(false);
        projectMonitor->show();
        if (topAnchor != nullptr && topAnchor != projectMonitor) {
            mainWindow->splitDockWidget(topAnchor, projectMonitor, Qt::Horizontal);
        }
        ++arrangedCount;
    }

    // 2. Bottom-left area: Project Bin tabbed with Effects, Compositions, and Premiere Browser
    QDockWidget *leftAnchor = nullptr;
    const QList<QDockWidget *> leftCandidates = {projectBin, effectList, premiereBrowser, compositionList};
    for (QDockWidget *dock : leftCandidates) {
        if (dock == nullptr) {
            continue;
        }
        mainWindow->addDockWidget(Qt::LeftDockWidgetArea, dock);
        dock->setFloating(false);
        dock->show();
        if (leftAnchor == nullptr) {
            leftAnchor = dock;
        } else if (dock != leftAnchor) {
            mainWindow->tabifyDockWidget(leftAnchor, dock);
        }
        ++arrangedCount;
    }
    if (projectBin != nullptr) {
        projectBin->raise();
    }

    // Optional Audio Mixer remains accessible alongside timeline / right side without replacing AI dock
    if (audioMixer != nullptr) {
        mainWindow->addDockWidget(Qt::RightDockWidgetArea, audioMixer);
        audioMixer->setFloating(false);
        ++arrangedCount;
    }

    // 3. Right area: Collapsible Bang On Pro AI Assistant dock
    if (aiAssistant != nullptr) {
        mainWindow->addDockWidget(Qt::RightDockWidgetArea, aiAssistant);
        aiAssistant->setFloating(false);
        if (audioMixer != nullptr && audioMixer != aiAssistant) {
            mainWindow->tabifyDockWidget(audioMixer, aiAssistant);
        }
        aiAssistant->show();
        aiAssistant->raise();
        mainWindow->resizeDocks({aiAssistant}, {380}, Qt::Horizontal);
        ++arrangedCount;
    }

    // Balance proportions if monitors & bin are present
    QList<QDockWidget *> topRowDocks;
    QList<int> topRowWidths;
    if (effectStack != nullptr) {
        topRowDocks.append(effectStack);
        topRowWidths.append(300);
    }
    if (clipMonitor != nullptr) {
        topRowDocks.append(clipMonitor);
        topRowWidths.append(420);
    }
    if (projectMonitor != nullptr) {
        topRowDocks.append(projectMonitor);
        topRowWidths.append(480);
    }
    if (!topRowDocks.isEmpty()) {
        mainWindow->resizeDocks(topRowDocks, topRowWidths, Qt::Horizontal);
    }
    if (leftAnchor != nullptr) {
        mainWindow->resizeDocks({leftAnchor}, {320}, Qt::Horizontal);
    }

    return arrangedCount > 0;
}

} // namespace BangOnPro
