/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "BangOnProWorkspace.h"
#include "AiAssistantDock.h"
#include "BangOnProTheme.h"

#include <QAction>
#include <QButtonGroup>
#include <QCoreApplication>
#include <QDateTime>
#include <QDockWidget>
#include <QHBoxLayout>
#include <QLabel>
#include <QList>
#include <QMainWindow>
#include <QMenu>
#include <QPushButton>
#include <QSettings>
#include <QToolButton>
#include <QUrl>
#include <utility>

namespace BangOnPro {

namespace {

constexpr const char *kWorkspaceSettingsOrg = "BangOnPro";
constexpr const char *kWorkspaceSettingsApp = "Workspaces";

QString workspaceKeyForName(const QString &name)
{
    const QByteArray encoded = QUrl::toPercentEncoding(name.trimmed());
    return QString::fromLatin1(encoded);
}

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

QDockWidget *resolveDockWithFallback(QMainWindow *mainWindow, QDockWidget *explicitDock, const char *primaryName, const char *fallbackName)
{
    if (QDockWidget *dock = resolveDock(mainWindow, explicitDock, primaryName)) {
        return dock;
    }
    return resolveDock(mainWindow, nullptr, fallbackName);
}

struct ResolvedDocks
{
    QDockWidget *clipMonitor = nullptr;
    QDockWidget *projectMonitor = nullptr;
    QDockWidget *projectBin = nullptr;
    QDockWidget *effectList = nullptr;
    QDockWidget *effectStack = nullptr;
    QDockWidget *compositionList = nullptr;
    QDockWidget *premiereBrowser = nullptr;
    QDockWidget *audioMixer = nullptr;
    QDockWidget *aiAssistant = nullptr;
    QDockWidget *rotoStudio = nullptr;
    QDockWidget *transcriptDock = nullptr;
    QDockWidget *subtitleDock = nullptr;
    QDockWidget *colorScopes = nullptr;
    QDockWidget *vectorscope = nullptr;
    QDockWidget *waveform = nullptr;
    QDockWidget *rgbParade = nullptr;
    QDockWidget *histogram = nullptr;
    QDockWidget *renderDock = nullptr;
    QDockWidget *undoHistory = nullptr;

    QList<QDockWidget *> allColorScopeDocks() const
    {
        QList<QDockWidget *> scopes;
        const QList<QDockWidget *> candidates = {colorScopes, vectorscope, waveform, rgbParade, histogram};
        for (QDockWidget *d : candidates) {
            if (d != nullptr && !scopes.contains(d)) {
                scopes.append(d);
            }
        }
        return scopes;
    }

    QList<QDockWidget *> allKnownDocks() const
    {
        QList<QDockWidget *> all;
        const QList<QDockWidget *> candidates = {clipMonitor, projectMonitor, projectBin, effectList,  effectStack, compositionList, premiereBrowser,
                                                 audioMixer,  aiAssistant,    rotoStudio, transcriptDock, subtitleDock, colorScopes,     vectorscope,
                                                 waveform,    rgbParade,      histogram,  renderDock,  undoHistory};
        for (QDockWidget *d : candidates) {
            if (d != nullptr && !all.contains(d)) {
                all.append(d);
            }
        }
        return all;
    }
};

ResolvedDocks resolveAllDocks(QMainWindow *mainWindow, const WorkspaceDocks &explicitDocks)
{
    ResolvedDocks d;
    d.clipMonitor = resolveDock(mainWindow, explicitDocks.clipMonitor, kClipMonitorDockName);
    d.projectMonitor = resolveDock(mainWindow, explicitDocks.projectMonitor, kProjectMonitorDockName);
    d.projectBin = resolveDock(mainWindow, explicitDocks.projectBin, kProjectBinDockName);
    d.effectList = resolveDock(mainWindow, explicitDocks.effectList, kEffectListDockName);
    d.effectStack = resolveDock(mainWindow, explicitDocks.effectStack, kEffectStackDockName);
    d.compositionList = resolveDock(mainWindow, explicitDocks.compositionList, kCompositionListDockName);
    d.premiereBrowser = resolveDock(mainWindow, explicitDocks.premiereBrowser, kPremiereBrowserDockName);
    d.audioMixer = resolveDock(mainWindow, explicitDocks.audioMixer, kAudioMixerDockName);
    d.aiAssistant = resolveDock(mainWindow, explicitDocks.aiAssistant, kAiAssistantDockName);
    d.rotoStudio = resolveDock(mainWindow, explicitDocks.rotoStudio, kRotoStudioDockName);
    d.transcriptDock = resolveDockWithFallback(mainWindow, explicitDocks.transcriptDock, kTranscriptDockName, "textedit");
    d.subtitleDock = resolveDockWithFallback(mainWindow, explicitDocks.subtitleDock, kSubtitleDockName, "bangonpro_subtitles");
    d.colorScopes = resolveDock(mainWindow, explicitDocks.colorScopes, kColorScopesDockName);
    d.vectorscope = resolveDock(mainWindow, nullptr, kVectorscopeDockName);
    d.waveform = resolveDock(mainWindow, nullptr, kWaveformDockName);
    d.rgbParade = resolveDock(mainWindow, nullptr, kRgbParadeDockName);
    d.histogram = resolveDock(mainWindow, nullptr, kHistogramDockName);
    d.renderDock = resolveDockWithFallback(mainWindow, explicitDocks.renderDock, kRenderDockName, "render_widget");
    d.undoHistory = resolveDock(mainWindow, explicitDocks.undoHistory, kUndoHistoryDockName);
    return d;
}

void dockIntoTabbedArea(QMainWindow *mainWindow, Qt::DockWidgetArea area, const QList<QDockWidget *> &docks, QDockWidget *raiseTarget, int &arrangedCount,
                        QDockWidget **outAnchor = nullptr)
{
    QDockWidget *anchor = nullptr;
    for (QDockWidget *dock : docks) {
        if (dock == nullptr) {
            continue;
        }
        mainWindow->addDockWidget(area, dock);
        dock->setFloating(false);
        dock->show();
        if (anchor == nullptr) {
            anchor = dock;
        } else if (dock != anchor) {
            mainWindow->tabifyDockWidget(anchor, dock);
        }
        ++arrangedCount;
    }
    if (raiseTarget != nullptr) {
        raiseTarget->raise();
    } else if (anchor != nullptr) {
        anchor->raise();
    }
    if (outAnchor != nullptr) {
        *outAnchor = anchor;
    }
}

void dockHorizontalRow(QMainWindow *mainWindow, Qt::DockWidgetArea area, const QList<QDockWidget *> &docks, int &arrangedCount)
{
    QDockWidget *prev = nullptr;
    for (QDockWidget *dock : docks) {
        if (dock == nullptr) {
            continue;
        }
        mainWindow->addDockWidget(area, dock);
        dock->setFloating(false);
        dock->show();
        if (prev != nullptr && prev != dock) {
            mainWindow->splitDockWidget(prev, dock, Qt::Horizontal);
        }
        prev = dock;
        ++arrangedCount;
    }
}

void hideUnusedDocks(const ResolvedDocks &all, const QList<QDockWidget *> &keepVisible)
{
    for (QDockWidget *dock : all.allKnownDocks()) {
        if (dock != nullptr && !keepVisible.contains(dock)) {
            dock->hide();
        }
    }
}

} // namespace

QVector<WorkspaceMode> allWorkspaceModes()
{
    return {
        WorkspaceMode::Edit,   WorkspaceMode::Multicam, WorkspaceMode::AiFirstCut,  WorkspaceMode::Effects,     WorkspaceMode::Colour,
        WorkspaceMode::Audio,  WorkspaceMode::Captions, WorkspaceMode::Rotoscoping, WorkspaceMode::Export,
    };
}

QString workspaceModeToId(WorkspaceMode mode)
{
    switch (mode) {
    case WorkspaceMode::Edit:
        return QStringLiteral("edit");
    case WorkspaceMode::Multicam:
        return QStringLiteral("multicam");
    case WorkspaceMode::AiFirstCut:
        return QStringLiteral("ai_first_cut");
    case WorkspaceMode::Effects:
        return QStringLiteral("effects");
    case WorkspaceMode::Colour:
        return QStringLiteral("colour");
    case WorkspaceMode::Audio:
        return QStringLiteral("audio");
    case WorkspaceMode::Captions:
        return QStringLiteral("captions");
    case WorkspaceMode::Export:
        return QStringLiteral("export");
    case WorkspaceMode::Rotoscoping:
        return QStringLiteral("rotoscoping");
    }
    return QStringLiteral("edit");
}

QString workspaceModeDisplayName(WorkspaceMode mode)
{
    switch (mode) {
    case WorkspaceMode::Edit:
        return QCoreApplication::translate("BangOnProWorkspace", "Edit");
    case WorkspaceMode::Multicam:
        return QCoreApplication::translate("BangOnProWorkspace", "Multicam");
    case WorkspaceMode::AiFirstCut:
        return QCoreApplication::translate("BangOnProWorkspace", "AI First Cut");
    case WorkspaceMode::Effects:
        return QCoreApplication::translate("BangOnProWorkspace", "Effects");
    case WorkspaceMode::Colour:
        return QCoreApplication::translate("BangOnProWorkspace", "Colour");
    case WorkspaceMode::Audio:
        return QCoreApplication::translate("BangOnProWorkspace", "Audio");
    case WorkspaceMode::Captions:
        return QCoreApplication::translate("BangOnProWorkspace", "Captions");
    case WorkspaceMode::Export:
        return QCoreApplication::translate("BangOnProWorkspace", "Export");
    case WorkspaceMode::Rotoscoping:
        return QCoreApplication::translate("BangOnProWorkspace", "Roto & 2D");
    }
    return QCoreApplication::translate("BangOnProWorkspace", "Edit");
}

bool workspaceModeFromId(const QString &id, WorkspaceMode *outMode)
{
    const QString key = id.trimmed().toLower();
    WorkspaceMode parsed = WorkspaceMode::Edit;
    if (key == QStringLiteral("edit")) {
        parsed = WorkspaceMode::Edit;
    } else if (key == QStringLiteral("multicam")) {
        parsed = WorkspaceMode::Multicam;
    } else if (key == QStringLiteral("ai_first_cut") || key == QStringLiteral("aifirstcut") || key == QStringLiteral("ai")) {
        parsed = WorkspaceMode::AiFirstCut;
    } else if (key == QStringLiteral("effects")) {
        parsed = WorkspaceMode::Effects;
    } else if (key == QStringLiteral("colour") || key == QStringLiteral("color")) {
        parsed = WorkspaceMode::Colour;
    } else if (key == QStringLiteral("audio")) {
        parsed = WorkspaceMode::Audio;
    } else if (key == QStringLiteral("captions") || key == QStringLiteral("subtitles")) {
        parsed = WorkspaceMode::Captions;
    } else if (key == QStringLiteral("export") || key == QStringLiteral("deliver")) {
        parsed = WorkspaceMode::Export;
    } else if (key == QStringLiteral("rotoscoping") || key == QStringLiteral("roto") || key == QStringLiteral("roto_2d")) {
        parsed = WorkspaceMode::Rotoscoping;
    } else {
        return false;
    }
    if (outMode != nullptr) {
        *outMode = parsed;
    }
    return true;
}

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
    return QString::fromLatin1(StudioPaletteTokens::kAccentPrimary);
}

QString brandStyleSheet()
{
    const QString chrome = QString::fromLatin1(StudioPaletteTokens::kWindowChrome);
    const QString panel = QString::fromLatin1(StudioPaletteTokens::kPanelSurface);
    const QString elevated = QString::fromLatin1(StudioPaletteTokens::kElevatedSurface);
    const QString hover = QString::fromLatin1(StudioPaletteTokens::kElevatedHover);
    const QString well = QString::fromLatin1(StudioPaletteTokens::kSunkenWell);
    const QString hairline = QString::fromLatin1(StudioPaletteTokens::kBorderHairline);
    const QString activeBorder = QString::fromLatin1(StudioPaletteTokens::kBorderActive);
    const QString text = QString::fromLatin1(StudioPaletteTokens::kTextPrimary);
    const QString textMuted = QString::fromLatin1(StudioPaletteTokens::kTextSecondary);
    const QString accent = brandAccentHex();
    const QString selection = QString::fromLatin1(StudioPaletteTokens::kAccentSelection);
    const QString green = QString::fromLatin1(StudioPaletteTokens::kStatusSuccess);
    const QString amber = QString::fromLatin1(StudioPaletteTokens::kStatusWarning);
    const QString red = QString::fromLatin1(StudioPaletteTokens::kStatusDanger);

    QString qss;
    qss.reserve(4096);

    qss += QStringLiteral(
               "QWidget#bangOnProAiAssistant, QWidget#bangOnProTranscriptEditor {"
               "  background-color: %1;"
               "  color: %2;"
               "}"
               "QFrame#bangOnProHeader {"
               "  background-color: %3;"
               "  border: 1px solid %4;"
               "  border-left: 3px solid %5;"
               "  border-radius: 5px;"
               "}"
               "QLabel#bangOnProTitle {"
               "  font-size: 13px;"
               "  font-weight: 700;"
               "  color: #FFFFFF;"
               "}"
               "QLabel#bangOnProBadge {"
               "  background-color: %5;"
               "  color: #FFFFFF;"
               "  font-size: 10px;"
               "  font-weight: 700;"
               "  padding: 2px 6px;"
               "  border-radius: 3px;"
               "}"
               "QLabel#bangOnProStatusBadge {"
               "  background-color: %6;"
               "  color: %2;"
               "  font-size: 10px;"
               "  font-weight: 600;"
               "  padding: 2px 7px;"
               "  border-radius: 9px;"
               "}"
               "QLabel#bangOnProStatusBadge[statusState=\"proposal\"] { background-color: #4A2810; color: #FFB07C; }"
               "QLabel#bangOnProStatusBadge[statusState=\"ok\"] { background-color: #133B2A; color: #6CE5AD; }"
               "QLabel#bangOnProStatusBadge[statusState=\"busy\"] { background-color: #163055; color: #7BB8FF; }"
               "QLabel#bangOnProStatusBadge[statusState=\"error\"] { background-color: #4D1B22; color: #FF8E9B; }"
               "QLabel#bangOnProSubtitle, QLabel#bangOnProMutedLabel, QLabel#bangOnProRevision, QLabel#taskStatusLabel {"
               "  color: %7;"
               "  font-size: 11px;"
               "}"
               "QLabel#bangOnProSectionLabel {"
               "  color: %2;"
               "  font-weight: 600;"
               "  font-size: 11px;"
               "}"
               "QLabel#proposalSummaryLabel {"
               "  color: %8;"
               "  font-size: 11px;"
               "  font-weight: 600;"
               "}")
               .arg(panel, text, elevated, hairline, accent, hover, textMuted, amber);

    qss += QStringLiteral(
               "QComboBox#showProfileCombo, QPlainTextEdit#instructionInput, QPlainTextEdit#agentActivityLog, QTreeWidget#proposedDiffTree {"
               "  background-color: %1;"
               "  color: %2;"
               "  border: 1px solid %3;"
               "  border-radius: 4px;"
               "}"
               "QTreeWidget#proposedDiffTree { alternate-background-color: %4; }"
               "QHeaderView::section {"
               "  background-color: %5;"
               "  color: %6;"
               "  border: 0px;"
               "  border-right: 1px solid %3;"
               "  border-bottom: 1px solid %3;"
               "  padding: 4px 6px;"
               "  font-size: 11px;"
               "  font-weight: 600;"
               "}"
               "QPushButton {"
               "  background-color: %5;"
               "  color: %2;"
               "  border: 1px solid %3;"
               "  border-radius: 4px;"
               "  padding: 5px 8px;"
               "  font-size: 11px;"
               "}"
               "QPushButton:hover { background-color: %7; border-color: %8; }"
               "QPushButton:disabled { background-color: %4; color: #5C6578; border-color: %3; }"
               "QPushButton[bangOnProChip=\"true\"] {"
               "  background-color: %5;"
               "  border: 1px solid %3;"
               "  border-radius: 10px;"
               "  padding: 4px 8px;"
               "  font-size: 10px;"
               "  text-align: left;"
               "}"
               "QPushButton[bangOnProChip=\"true\"]:hover { border-color: %9; color: #FFFFFF; }")
               .arg(well, text, hairline, panel, elevated, textMuted, hover, activeBorder, accent);

    qss += QStringLiteral(
               "QPushButton[bangOnProPrimary=\"true\"] {"
               "  background-color: %1;"
               "  border-color: %1;"
               "  color: #FFFFFF;"
               "  font-weight: 700;"
               "}"
               "QPushButton[bangOnProPrimary=\"true\"]:hover { background-color: #FF733F; }"
               "QPushButton[bangOnProAccept=\"true\"] {"
               "  background-color: %2;"
               "  border-color: %2;"
               "  color: #FFFFFF;"
               "  font-weight: 700;"
               "}"
               "QPushButton[bangOnProAccept=\"true\"]:hover { background-color: #28BA7C; }"
               "QPushButton[bangOnProDanger=\"true\"]:hover { background-color: %3; border-color: %3; color: #FFFFFF; }"
               "QPushButton#compareToggleButton:checked {"
               "  background-color: #1A3A66;"
               "  border-color: %4;"
               "  color: #FFFFFF;"
               "  font-weight: 600;"
               "}"
               "QProgressBar#agentProgressBar {"
               "  background-color: %5;"
               "  border: 1px solid %6;"
               "  border-radius: 3px;"
               "  text-align: center;"
               "  font-size: 10px;"
               "  color: %7;"
               "}"
               "QProgressBar#agentProgressBar::chunk { background-color: %1; border-radius: 2px; }"
               "QToolButton#activityLogToggleButton { color: %7; font-weight: 600; font-size: 11px; }")
               .arg(accent, green, red, selection, well, hairline, text);

    qss += QStringLiteral(
               "QWidget#bangOnProWorkspaceSwitcher {"
               "  background-color: %1;"
               "  border-bottom: 1px solid %2;"
               "}"
               "QPushButton[bangOnProWorkspacePill=\"true\"] {"
               "  background-color: transparent;"
               "  color: %3;"
               "  border: 0px;"
               "  border-bottom: 2px solid transparent;"
               "  border-radius: 0px;"
               "  padding: 5px 10px;"
               "  font-size: 11px;"
               "  font-weight: 600;"
               "}"
               "QPushButton[bangOnProWorkspacePill=\"true\"]:hover { color: %4; background-color: %5; }"
               "QPushButton[bangOnProWorkspacePill=\"true\"]:checked {"
               "  color: #FFFFFF;"
               "  background-color: %6;"
               "  border-bottom: 2px solid %7;"
               "}"
               "QPushButton#workspaceAiPanelToggle {"
               "  background-color: %5;"
               "  color: %4;"
               "  border: 1px solid %2;"
               "  border-radius: 11px;"
               "  padding: 3px 10px;"
               "  font-size: 11px;"
               "  font-weight: 600;"
               "}"
               "QPushButton#workspaceAiPanelToggle:checked {"
               "  background-color: #3D1F13;"
               "  border-color: %7;"
               "  color: #FFB07C;"
               "}")
               .arg(chrome, hairline, textMuted, text, elevated, panel, accent);

    return qss;
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

    const ResolvedDocks d = resolveAllDocks(mainWindow, explicitDocks);
    int arrangedCount = 0;

    mainWindow->setDockNestingEnabled(true);
    mainWindow->setCorner(Qt::TopLeftCorner, Qt::TopDockWidgetArea);
    mainWindow->setCorner(Qt::TopRightCorner, Qt::RightDockWidgetArea);
    mainWindow->setCorner(Qt::BottomLeftCorner, Qt::LeftDockWidgetArea);
    mainWindow->setCorner(Qt::BottomRightCorner, Qt::RightDockWidgetArea);

    // 1. Top row: Effect Controls (optional top-left) + Source Monitor + Programme Monitor
    dockHorizontalRow(mainWindow, Qt::TopDockWidgetArea, {d.effectStack, d.clipMonitor, d.projectMonitor}, arrangedCount);

    // 2. Bottom-left: Project Bin tabbed with Effects, Premiere Browser, Compositions
    QDockWidget *leftAnchor = nullptr;
    dockIntoTabbedArea(mainWindow, Qt::LeftDockWidgetArea, {d.projectBin, d.effectList, d.premiereBrowser, d.compositionList}, d.projectBin, arrangedCount,
                       &leftAnchor);

    // 3. Right area: Optional Audio Mixer + Collapsible Bang On Pro AI Assistant dock
    if (d.audioMixer != nullptr) {
        mainWindow->addDockWidget(Qt::RightDockWidgetArea, d.audioMixer);
        d.audioMixer->setFloating(false);
        ++arrangedCount;
    }

    if (d.aiAssistant != nullptr) {
        mainWindow->addDockWidget(Qt::RightDockWidgetArea, d.aiAssistant);
        d.aiAssistant->setFloating(false);
        if (d.audioMixer != nullptr && d.audioMixer != d.aiAssistant) {
            mainWindow->tabifyDockWidget(d.audioMixer, d.aiAssistant);
        }
        d.aiAssistant->show();
        d.aiAssistant->raise();
        mainWindow->resizeDocks({d.aiAssistant}, {380}, Qt::Horizontal);
        ++arrangedCount;
    }

    // Balance proportions
    QList<QDockWidget *> topRowDocks;
    QList<int> topRowWidths;
    if (d.effectStack != nullptr) {
        topRowDocks.append(d.effectStack);
        topRowWidths.append(300);
    }
    if (d.clipMonitor != nullptr) {
        topRowDocks.append(d.clipMonitor);
        topRowWidths.append(420);
    }
    if (d.projectMonitor != nullptr) {
        topRowDocks.append(d.projectMonitor);
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

bool applyWorkspace(QMainWindow *mainWindow, WorkspaceMode mode, const WorkspaceDocks &explicitDocks)
{
    if (mainWindow == nullptr) {
        return false;
    }

    const ResolvedDocks d = resolveAllDocks(mainWindow, explicitDocks);
    int arrangedCount = 0;

    mainWindow->setDockNestingEnabled(true);
    mainWindow->setCorner(Qt::TopLeftCorner, Qt::TopDockWidgetArea);
    mainWindow->setCorner(Qt::TopRightCorner, Qt::RightDockWidgetArea);
    mainWindow->setCorner(Qt::BottomLeftCorner, Qt::LeftDockWidgetArea);
    mainWindow->setCorner(Qt::BottomRightCorner, Qt::RightDockWidgetArea);

    QList<QDockWidget *> visibleDocks;

    switch (mode) {
    case WorkspaceMode::Edit: {
        // Premiere Pro practical editing layout:
        // Source & Program Monitors top, Project Bin + Effects bottom-left, Timeline bottom-center.
        // AI Assistant docked on right edge and collapsed/hidden so manual editing stays unobstructed.
        dockHorizontalRow(mainWindow, Qt::TopDockWidgetArea, {d.effectStack, d.clipMonitor, d.projectMonitor}, arrangedCount);
        visibleDocks << d.effectStack << d.clipMonitor << d.projectMonitor;

        QDockWidget *leftAnchor = nullptr;
        dockIntoTabbedArea(mainWindow, Qt::LeftDockWidgetArea, {d.projectBin, d.effectList, d.premiereBrowser, d.compositionList, d.undoHistory}, d.projectBin,
                           arrangedCount, &leftAnchor);
        visibleDocks << d.projectBin << d.effectList << d.premiereBrowser << d.compositionList << d.undoHistory;

        if (d.aiAssistant != nullptr) {
            mainWindow->addDockWidget(Qt::RightDockWidgetArea, d.aiAssistant);
            d.aiAssistant->setFloating(false);
            d.aiAssistant->hide(); // Collapsible on right edge; toggle via AI Panel pill
            ++arrangedCount;
        }

        if (leftAnchor != nullptr) {
            mainWindow->resizeDocks({leftAnchor}, {320}, Qt::Horizontal);
        }
        break;
    }

    case WorkspaceMode::Multicam: {
        // Source/Multicam + Program Monitor top, Audio Mixer + Timeline prominent, AI Assistant compact tab.
        dockHorizontalRow(mainWindow, Qt::TopDockWidgetArea, {d.clipMonitor, d.projectMonitor}, arrangedCount);
        visibleDocks << d.clipMonitor << d.projectMonitor;

        QDockWidget *leftAnchor = nullptr;
        dockIntoTabbedArea(mainWindow, Qt::LeftDockWidgetArea, {d.projectBin, d.effectStack}, d.projectBin, arrangedCount, &leftAnchor);
        visibleDocks << d.projectBin << d.effectStack;

        QDockWidget *rightAnchor = nullptr;
        dockIntoTabbedArea(mainWindow, Qt::RightDockWidgetArea, {d.audioMixer, d.aiAssistant}, d.audioMixer, arrangedCount, &rightAnchor);
        visibleDocks << d.audioMixer << d.aiAssistant;

        if (leftAnchor != nullptr) {
            mainWindow->resizeDocks({leftAnchor}, {280}, Qt::Horizontal);
        }
        if (rightAnchor != nullptr) {
            mainWindow->resizeDocks({rightAnchor}, {340}, Qt::Horizontal);
        }
        break;
    }

    case WorkspaceMode::AiFirstCut: {
        // Monitors top, AI Assistant + Transcript prominent right, Project Bin left, Timeline bottom.
        dockHorizontalRow(mainWindow, Qt::TopDockWidgetArea, {d.clipMonitor, d.projectMonitor}, arrangedCount);
        visibleDocks << d.clipMonitor << d.projectMonitor;

        QDockWidget *leftAnchor = nullptr;
        dockIntoTabbedArea(mainWindow, Qt::LeftDockWidgetArea, {d.projectBin, d.premiereBrowser}, d.projectBin, arrangedCount, &leftAnchor);
        visibleDocks << d.projectBin << d.premiereBrowser;

        QDockWidget *rightAnchor = nullptr;
        if (d.aiAssistant != nullptr && d.transcriptDock != nullptr && d.aiAssistant != d.transcriptDock) {
            mainWindow->addDockWidget(Qt::RightDockWidgetArea, d.aiAssistant);
            d.aiAssistant->setFloating(false);
            d.aiAssistant->show();
            mainWindow->addDockWidget(Qt::RightDockWidgetArea, d.transcriptDock);
            d.transcriptDock->setFloating(false);
            d.transcriptDock->show();
            mainWindow->splitDockWidget(d.aiAssistant, d.transcriptDock, Qt::Vertical);
            rightAnchor = d.aiAssistant;
            arrangedCount += 2;
        } else {
            dockIntoTabbedArea(mainWindow, Qt::RightDockWidgetArea, {d.aiAssistant, d.transcriptDock}, d.aiAssistant, arrangedCount, &rightAnchor);
        }
        visibleDocks << d.aiAssistant << d.transcriptDock;

        if (leftAnchor != nullptr) {
            mainWindow->resizeDocks({leftAnchor}, {280}, Qt::Horizontal);
        }
        if (rightAnchor != nullptr) {
            mainWindow->resizeDocks({rightAnchor}, {420}, Qt::Horizontal);
        }
        break;
    }

    case WorkspaceMode::Effects: {
        // Effects + Premiere Presets Browser left, Effect Controls (Inspector) + Program Monitor top, Timeline bottom.
        dockHorizontalRow(mainWindow, Qt::TopDockWidgetArea, {d.effectStack, d.projectMonitor}, arrangedCount);
        visibleDocks << d.effectStack << d.projectMonitor;
        if (d.clipMonitor != nullptr && d.projectMonitor != nullptr && d.clipMonitor != d.projectMonitor) {
            mainWindow->addDockWidget(Qt::TopDockWidgetArea, d.clipMonitor);
            d.clipMonitor->setFloating(false);
            d.clipMonitor->show();
            mainWindow->tabifyDockWidget(d.projectMonitor, d.clipMonitor);
            d.projectMonitor->raise();
            visibleDocks << d.clipMonitor;
            ++arrangedCount;
        }

        QDockWidget *leftAnchor = nullptr;
        QDockWidget *preferredLeft = d.effectList != nullptr ? d.effectList : d.premiereBrowser;
        dockIntoTabbedArea(mainWindow, Qt::LeftDockWidgetArea, {d.effectList, d.premiereBrowser, d.compositionList, d.projectBin}, preferredLeft, arrangedCount,
                           &leftAnchor);
        visibleDocks << d.effectList << d.premiereBrowser << d.compositionList << d.projectBin;

        if (leftAnchor != nullptr) {
            mainWindow->resizeDocks({leftAnchor}, {340}, Qt::Horizontal);
        }
        break;
    }

    case WorkspaceMode::Colour: {
        // Program Monitor + Vectorscope/RGB Parade/Waveform/Histogram Scopes top, Effect Stack/Colour tools right, Timeline bottom.
        const QList<QDockWidget *> scopes = d.allColorScopeDocks();
        QDockWidget *scopeAnchor = nullptr;
        if (d.projectMonitor != nullptr) {
            mainWindow->addDockWidget(Qt::TopDockWidgetArea, d.projectMonitor);
            d.projectMonitor->setFloating(false);
            d.projectMonitor->show();
            visibleDocks << d.projectMonitor;
            ++arrangedCount;
        }
        for (QDockWidget *scope : scopes) {
            mainWindow->addDockWidget(Qt::TopDockWidgetArea, scope);
            scope->setFloating(false);
            scope->show();
            if (scopeAnchor == nullptr) {
                scopeAnchor = scope;
                if (d.projectMonitor != nullptr && d.projectMonitor != scopeAnchor) {
                    mainWindow->splitDockWidget(d.projectMonitor, scopeAnchor, Qt::Horizontal);
                }
            } else if (scope != scopeAnchor) {
                mainWindow->tabifyDockWidget(scopeAnchor, scope);
            }
            visibleDocks << scope;
            ++arrangedCount;
        }
        if (scopeAnchor != nullptr) {
            scopeAnchor->raise();
        }

        QDockWidget *rightAnchor = nullptr;
        dockIntoTabbedArea(mainWindow, Qt::RightDockWidgetArea, {d.effectStack, d.effectList}, d.effectStack, arrangedCount, &rightAnchor);
        visibleDocks << d.effectStack << d.effectList;

        QDockWidget *leftAnchor = nullptr;
        dockIntoTabbedArea(mainWindow, Qt::LeftDockWidgetArea, {d.projectBin, d.premiereBrowser}, d.projectBin, arrangedCount, &leftAnchor);
        visibleDocks << d.projectBin << d.premiereBrowser;

        if (rightAnchor != nullptr) {
            mainWindow->resizeDocks({rightAnchor}, {360}, Qt::Horizontal);
        }
        break;
    }

    case WorkspaceMode::Audio: {
        // Audio Mixer prominent right, Effects/Audio presets left, Program Monitor top, Timeline bottom.
        dockHorizontalRow(mainWindow, Qt::TopDockWidgetArea, {d.projectMonitor, d.effectStack}, arrangedCount);
        visibleDocks << d.projectMonitor << d.effectStack;

        QDockWidget *leftAnchor = nullptr;
        QDockWidget *preferredLeft = d.effectList != nullptr ? d.effectList : d.projectBin;
        dockIntoTabbedArea(mainWindow, Qt::LeftDockWidgetArea, {d.effectList, d.premiereBrowser, d.projectBin}, preferredLeft, arrangedCount, &leftAnchor);
        visibleDocks << d.effectList << d.premiereBrowser << d.projectBin;

        QDockWidget *rightAnchor = nullptr;
        dockIntoTabbedArea(mainWindow, Qt::RightDockWidgetArea, {d.audioMixer}, d.audioMixer, arrangedCount, &rightAnchor);
        visibleDocks << d.audioMixer;

        if (rightAnchor != nullptr) {
            mainWindow->resizeDocks({rightAnchor}, {420}, Qt::Horizontal);
        }
        break;
    }

    case WorkspaceMode::Captions: {
        // Transcript & Subtitles Editor prominent alongside Program Monitor + Timeline.
        dockHorizontalRow(mainWindow, Qt::TopDockWidgetArea, {d.projectMonitor}, arrangedCount);
        visibleDocks << d.projectMonitor;

        QDockWidget *leftAnchor = nullptr;
        dockIntoTabbedArea(mainWindow, Qt::LeftDockWidgetArea, {d.projectBin, d.effectStack}, d.projectBin, arrangedCount, &leftAnchor);
        visibleDocks << d.projectBin << d.effectStack;

        QDockWidget *rightAnchor = nullptr;
        if (d.transcriptDock != nullptr && d.subtitleDock != nullptr && d.transcriptDock != d.subtitleDock) {
            mainWindow->addDockWidget(Qt::RightDockWidgetArea, d.transcriptDock);
            d.transcriptDock->setFloating(false);
            d.transcriptDock->show();
            mainWindow->addDockWidget(Qt::RightDockWidgetArea, d.subtitleDock);
            d.subtitleDock->setFloating(false);
            d.subtitleDock->show();
            mainWindow->splitDockWidget(d.transcriptDock, d.subtitleDock, Qt::Vertical);
            rightAnchor = d.transcriptDock;
            arrangedCount += 2;
        } else {
            dockIntoTabbedArea(mainWindow, Qt::RightDockWidgetArea, {d.transcriptDock, d.subtitleDock, d.aiAssistant},
                               d.transcriptDock != nullptr ? d.transcriptDock : d.subtitleDock, arrangedCount, &rightAnchor);
        }
        visibleDocks << d.transcriptDock << d.subtitleDock;
        if (d.transcriptDock == nullptr && d.subtitleDock == nullptr && d.aiAssistant != nullptr) {
            visibleDocks << d.aiAssistant;
        }

        if (rightAnchor != nullptr) {
            mainWindow->resizeDocks({rightAnchor}, {440}, Qt::Horizontal);
        }
        break;
    }

    case WorkspaceMode::Export: {
        // Program Monitor + AI QC / Export Controls + Timeline.
        dockHorizontalRow(mainWindow, Qt::TopDockWidgetArea, {d.projectMonitor}, arrangedCount);
        visibleDocks << d.projectMonitor;

        QDockWidget *leftAnchor = nullptr;
        dockIntoTabbedArea(mainWindow, Qt::LeftDockWidgetArea, {d.projectBin, d.undoHistory}, d.projectBin, arrangedCount, &leftAnchor);
        visibleDocks << d.projectBin << d.undoHistory;

        QDockWidget *rightAnchor = nullptr;
        QDockWidget *preferredRight = d.renderDock != nullptr ? d.renderDock : d.aiAssistant;
        dockIntoTabbedArea(mainWindow, Qt::RightDockWidgetArea, {d.renderDock, d.aiAssistant}, preferredRight, arrangedCount, &rightAnchor);
        visibleDocks << d.renderDock << d.aiAssistant;

        if (rightAnchor != nullptr) {
            mainWindow->resizeDocks({rightAnchor}, {420}, Qt::Horizontal);
        }
        break;
    }

    case WorkspaceMode::Rotoscoping: {
        // Roto & 2D Studio Dock prominent alongside Program Monitor + Project Bin + Timeline.
        dockHorizontalRow(mainWindow, Qt::TopDockWidgetArea, {d.projectMonitor}, arrangedCount);
        visibleDocks << d.projectMonitor;

        QDockWidget *leftAnchor = nullptr;
        dockIntoTabbedArea(mainWindow, Qt::LeftDockWidgetArea, {d.projectBin, d.effectList}, d.projectBin, arrangedCount, &leftAnchor);
        visibleDocks << d.projectBin << d.effectList;

        QDockWidget *rightAnchor = nullptr;
        dockIntoTabbedArea(mainWindow, Qt::RightDockWidgetArea, {d.rotoStudio, d.effectStack}, d.rotoStudio, arrangedCount, &rightAnchor);
        visibleDocks << d.rotoStudio << d.effectStack;

        if (rightAnchor != nullptr) {
            mainWindow->resizeDocks({rightAnchor}, {420}, Qt::Horizontal);
        }
        break;
    }
    }

    hideUnusedDocks(d, visibleDocks);
    return arrangedCount > 0;
}

bool saveWorkspaceState(QMainWindow *mainWindow, const QString &name)
{
    const QString trimmed = name.trimmed();
    if (mainWindow == nullptr || trimmed.isEmpty()) {
        return false;
    }

    const QString key = workspaceKeyForName(trimmed);
    QSettings settings(QString::fromLatin1(kWorkspaceSettingsOrg), QString::fromLatin1(kWorkspaceSettingsApp));
    settings.beginGroup(QStringLiteral("custom/%1").arg(key));
    settings.setValue(QStringLiteral("display_name"), trimmed);
    settings.setValue(QStringLiteral("geometry"), mainWindow->saveGeometry());
    settings.setValue(QStringLiteral("state"), mainWindow->saveState());
    settings.setValue(QStringLiteral("saved_at_utc"), QDateTime::currentDateTimeUtc().toString(Qt::ISODate));
    settings.endGroup();
    settings.sync();
    return settings.status() == QSettings::NoError;
}

bool restoreWorkspaceState(QMainWindow *mainWindow, const QString &name)
{
    const QString trimmed = name.trimmed();
    if (mainWindow == nullptr || trimmed.isEmpty()) {
        return false;
    }

    const QString key = workspaceKeyForName(trimmed);
    QSettings settings(QString::fromLatin1(kWorkspaceSettingsOrg), QString::fromLatin1(kWorkspaceSettingsApp));
    settings.beginGroup(QStringLiteral("custom/%1").arg(key));
    const QByteArray geometry = settings.value(QStringLiteral("geometry")).toByteArray();
    const QByteArray state = settings.value(QStringLiteral("state")).toByteArray();
    settings.endGroup();

    if (geometry.isEmpty() && state.isEmpty()) {
        return false;
    }

    bool ok = true;
    if (!geometry.isEmpty()) {
        ok = mainWindow->restoreGeometry(geometry) && ok;
    }
    if (!state.isEmpty()) {
        ok = mainWindow->restoreState(state) && ok;
    }
    return ok;
}

QStringList savedCustomWorkspaces()
{
    QStringList names;
    QSettings settings(QString::fromLatin1(kWorkspaceSettingsOrg), QString::fromLatin1(kWorkspaceSettingsApp));
    settings.beginGroup(QStringLiteral("custom"));
    const QStringList groups = settings.childGroups();
    for (const QString &group : groups) {
        const QString displayName = settings.value(QStringLiteral("%1/display_name").arg(group)).toString();
        if (!displayName.isEmpty()) {
            names.append(displayName);
        }
    }
    settings.endGroup();
    names.sort(Qt::CaseInsensitive);
    return names;
}

bool resetWorkspaceState(const QString &name)
{
    const QString trimmed = name.trimmed();
    if (trimmed.isEmpty()) {
        return false;
    }

    const QString key = workspaceKeyForName(trimmed);
    QSettings settings(QString::fromLatin1(kWorkspaceSettingsOrg), QString::fromLatin1(kWorkspaceSettingsApp));
    settings.beginGroup(QStringLiteral("custom"));
    const bool existed = settings.childGroups().contains(key);
    if (existed) {
        settings.remove(key);
    }
    settings.endGroup();
    settings.sync();
    return existed;
}

WorkspaceSwitcherBar::WorkspaceSwitcherBar(QWidget *parent)
    : QWidget(parent)
{
    qRegisterMetaType<BangOnPro::WorkspaceMode>("BangOnPro::WorkspaceMode");
    setObjectName(QStringLiteral("bangOnProWorkspaceSwitcher"));
    setFixedHeight(34);
    buildUi();
    setStyleSheet(brandStyleSheet());
}

void WorkspaceSwitcherBar::buildUi()
{
    auto *layout = new QHBoxLayout(this);
    layout->setContentsMargins(10, 0, 10, 0);
    layout->setSpacing(2);

    auto *brandTag = new QLabel(QStringLiteral("BANG ON PRO"), this);
    brandTag->setObjectName(QStringLiteral("bangOnProBadge"));
    layout->addWidget(brandTag);
    layout->addSpacing(8);

    m_modeGroup = new QButtonGroup(this);
    m_modeGroup->setExclusive(true);

    const QVector<WorkspaceMode> modes = allWorkspaceModes();
    for (const WorkspaceMode mode : modes) {
        auto *btn = new QPushButton(workspaceModeDisplayName(mode), this);
        btn->setObjectName(QStringLiteral("workspacePill_%1").arg(workspaceModeToId(mode)));
        btn->setCheckable(true);
        btn->setFlat(true);
        btn->setProperty("bangOnProWorkspacePill", true);
        btn->setCursor(Qt::PointingHandCursor);
        const int id = static_cast<int>(mode);
        m_modeGroup->addButton(btn, id);
        m_modeButtons.insert(id, btn);
        layout->addWidget(btn);
    }

    if (QPushButton *editBtn = m_modeButtons.value(static_cast<int>(WorkspaceMode::Edit))) {
        editBtn->setChecked(true);
    }

    layout->addStretch(1);

    m_aiPanelToggleButton = new QPushButton(tr("AI Panel"), this);
    m_aiPanelToggleButton->setObjectName(QStringLiteral("workspaceAiPanelToggle"));
    m_aiPanelToggleButton->setCheckable(true);
    m_aiPanelToggleButton->setChecked(false);
    m_aiPanelToggleButton->setCursor(Qt::PointingHandCursor);
    m_aiPanelToggleButton->setToolTip(tr("Show or collapse the Bang On Pro AI Assistant dock"));
    layout->addWidget(m_aiPanelToggleButton);

    layout->addSpacing(6);

    m_saveLayoutButton = new QToolButton(this);
    m_saveLayoutButton->setObjectName(QStringLiteral("workspaceSaveLayoutButton"));
    m_saveLayoutButton->setText(tr("Save Layout"));
    m_saveLayoutButton->setPopupMode(QToolButton::MenuButtonPopup);
    m_saveLayoutButton->setCursor(Qt::PointingHandCursor);
    m_saveLayoutButton->setToolTip(tr("Save current panel layout or restore a saved custom workspace"));

    m_savedLayoutsMenu = new QMenu(m_saveLayoutButton);
    m_saveLayoutButton->setMenu(m_savedLayoutsMenu);
    refreshSavedLayoutsMenu();
    layout->addWidget(m_saveLayoutButton);

#if QT_VERSION >= QT_VERSION_CHECK(5, 15, 0)
    connect(m_modeGroup, &QButtonGroup::idClicked, this, &WorkspaceSwitcherBar::handleWorkspaceButtonClicked);
#else
    connect(m_modeGroup, QOverload<int>::of(&QButtonGroup::buttonClicked), this, &WorkspaceSwitcherBar::handleWorkspaceButtonClicked);
#endif
    connect(m_aiPanelToggleButton, &QPushButton::toggled, this, &WorkspaceSwitcherBar::handleAiPanelToggled);
    connect(m_saveLayoutButton, &QToolButton::clicked, this, &WorkspaceSwitcherBar::handleSaveLayoutClicked);
}

WorkspaceMode WorkspaceSwitcherBar::activeWorkspace() const
{
    return m_activeMode;
}

void WorkspaceSwitcherBar::setActiveWorkspace(WorkspaceMode mode, bool emitSignal)
{
    m_activeMode = mode;
    if (QPushButton *btn = m_modeButtons.value(static_cast<int>(mode))) {
        m_modeGroup->blockSignals(true);
        btn->setChecked(true);
        m_modeGroup->blockSignals(false);
    }
    if (emitSignal) {
        Q_EMIT workspaceSelected(mode);
    }
}

bool WorkspaceSwitcherBar::isAiPanelVisible() const
{
    return m_aiPanelToggleButton->isChecked();
}

void WorkspaceSwitcherBar::setAiPanelVisible(bool visible, bool emitSignal)
{
    if (!emitSignal) {
        m_aiPanelToggleButton->blockSignals(true);
    }
    m_aiPanelToggleButton->setChecked(visible);
    if (!emitSignal) {
        m_aiPanelToggleButton->blockSignals(false);
    }
}

QAbstractButton *WorkspaceSwitcherBar::workspaceButton(WorkspaceMode mode) const
{
    return m_modeButtons.value(static_cast<int>(mode), nullptr);
}

void WorkspaceSwitcherBar::refreshSavedLayoutsMenu()
{
    if (m_savedLayoutsMenu == nullptr) {
        return;
    }
    m_savedLayoutsMenu->clear();

    QAction *saveNow = m_savedLayoutsMenu->addAction(tr("Save Current Layout…"));
    connect(saveNow, &QAction::triggered, this, &WorkspaceSwitcherBar::handleSaveLayoutClicked);

    const QStringList saved = savedCustomWorkspaces();
    if (!saved.isEmpty()) {
        m_savedLayoutsMenu->addSeparator();
        for (const QString &name : saved) {
            QAction *restoreAct = m_savedLayoutsMenu->addAction(tr("Restore: %1").arg(name));
            connect(restoreAct, &QAction::triggered, this, [this, name]() {
                if (m_attachedWindow) {
                    restoreWorkspaceState(m_attachedWindow.data(), name);
                }
                Q_EMIT restoreCustomLayoutRequested(name);
            });
        }
        m_savedLayoutsMenu->addSeparator();
        QMenu *resetMenu = m_savedLayoutsMenu->addMenu(tr("Reset Saved Layout"));
        for (const QString &name : saved) {
            QAction *resetAct = resetMenu->addAction(name);
            connect(resetAct, &QAction::triggered, this, [this, name]() {
                resetWorkspaceState(name);
                refreshSavedLayoutsMenu();
                Q_EMIT resetCustomLayoutRequested(name);
            });
        }
    }
}

void WorkspaceSwitcherBar::attachToMainWindow(QMainWindow *mainWindow, const WorkspaceDocks &explicitDocks)
{
    m_attachedWindow = mainWindow;
    m_attachedDocks = explicitDocks;

    if (mainWindow == nullptr) {
        return;
    }

    QDockWidget *aiDock = resolveDock(mainWindow, explicitDocks.aiAssistant, kAiAssistantDockName);
    if (aiDock != nullptr) {
        setAiPanelVisible(aiDock->isVisible(), false);
        connect(aiDock, &QDockWidget::visibilityChanged, this, [this](bool visible) { setAiPanelVisible(visible, false); });
    }
}

void WorkspaceSwitcherBar::handleWorkspaceButtonClicked(int id)
{
    const auto mode = static_cast<WorkspaceMode>(id);
    m_activeMode = mode;
    if (m_attachedWindow) {
        applyWorkspace(m_attachedWindow.data(), mode, m_attachedDocks);
        if (QDockWidget *aiDock = resolveDock(m_attachedWindow.data(), m_attachedDocks.aiAssistant, kAiAssistantDockName)) {
            setAiPanelVisible(aiDock->isVisible(), false);
        }
    }
    Q_EMIT workspaceSelected(mode);
}

void WorkspaceSwitcherBar::handleAiPanelToggled(bool checked)
{
    if (m_attachedWindow) {
        if (QDockWidget *aiDock = resolveDock(m_attachedWindow.data(), m_attachedDocks.aiAssistant, kAiAssistantDockName)) {
            aiDock->setVisible(checked);
            if (checked) {
                aiDock->raise();
            }
        }
    }
    Q_EMIT aiPanelToggled(checked);
}

void WorkspaceSwitcherBar::handleSaveLayoutClicked()
{
    Q_EMIT saveLayoutRequested();
}

} // namespace BangOnPro
