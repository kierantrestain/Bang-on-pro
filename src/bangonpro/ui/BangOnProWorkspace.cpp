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
#include <QInputDialog>
#include <QLabel>
#include <QLineEdit>
#include <QList>
#include <QMainWindow>
#include <QMenu>
#include <QPointer>
#include <QPushButton>
#include <QResizeEvent>
#include <QScreen>
#include <QSettings>
#include <QTimer>
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
    QDockWidget *agentControlCentre = nullptr;
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
        const QList<QDockWidget *> candidates = {clipMonitor, projectMonitor, projectBin, effectList,     effectStack,  compositionList,   premiereBrowser,
                                                 audioMixer,  aiAssistant,    rotoStudio, transcriptDock, subtitleDock, colorScopes,       vectorscope,
                                                 waveform,    rgbParade,      histogram,  renderDock,     undoHistory,  agentControlCentre};
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
    d.agentControlCentre = resolveDock(mainWindow, explicitDocks.agentControlCentre, kAgentControlCentreDockName);
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

struct StudioColumnSpec {
    QList<QDockWidget *> docks;
    QDockWidget *preferred = nullptr;
    int width = 360;
};

void dockTopStudioColumns(QMainWindow *mainWindow, const QList<StudioColumnSpec> &columns, QList<QDockWidget *> &visibleDocks, int &arrangedCount)
{
    // V1.2 fix: QMainWindow::splitDockWidget() adds the second dock as a *tab* when the first dock is
    // already part of a tab group. The previous single-pass algorithm tabified each column before
    // splitting the next one, so every column collapsed into one tab group. We now (0) detach every
    // column dock from stale tab groups, (1) place & split all column anchors while untabbed, then
    // (2) tabify the remaining docks onto their column anchor.
    QList<QDockWidget *> colAnchors;
    QList<int> colWidths;
    QList<QList<QDockWidget *>> colMembers;

    for (const StudioColumnSpec &col : columns) {
        QList<QDockWidget *> members;
        for (QDockWidget *dock : col.docks) {
            if (dock != nullptr && !members.contains(dock)) {
                members.append(dock);
            }
        }
        if (members.isEmpty()) {
            continue;
        }
        for (QDockWidget *dock : members) {
            mainWindow->removeDockWidget(dock);
            dock->setAllowedAreas(Qt::AllDockWidgetAreas);
            dock->setFloating(false);
        }
        colMembers.append(members);
        colWidths.append(col.width);
    }

    // Screen-aware: never let side-by-side columns force the window wider than the available screen.
    // Columns whose minimum width would overflow are tabbed into the previous column instead.
    int available = mainWindow->width();
    if (QScreen *screen = mainWindow->screen()) {
        available = qMin(qMax(available, 800), screen->availableGeometry().width());
    }
    int used = 0;
    for (int i = 0; i < colMembers.size(); ++i) {
        int colMin = 0;
        for (QDockWidget *dock : std::as_const(colMembers[i])) {
            colMin = qMax(colMin, qMax(dock->minimumSizeHint().width(), dock->minimumWidth()));
        }
        if (i > 0 && used + colMin > available - 16) {
            colMembers[i - 1].append(colMembers[i]);
            colMembers.removeAt(i);
            colWidths.removeAt(i);
            --i;
            continue;
        }
        used += colMin;
    }

    QDockWidget *prevAnchor = nullptr;
    for (const QList<QDockWidget *> &members : std::as_const(colMembers)) {
        QDockWidget *anchor = members.first();
        mainWindow->addDockWidget(Qt::TopDockWidgetArea, anchor);
        anchor->show();
        if (prevAnchor != nullptr) {
            mainWindow->splitDockWidget(prevAnchor, anchor, Qt::Horizontal);
        }
        colAnchors.append(anchor);
        prevAnchor = anchor;
    }

    QList<QDockWidget *> preferred;
    for (const StudioColumnSpec &col : columns) {
        if (col.preferred != nullptr) {
            preferred.append(col.preferred);
        }
    }
    for (int colIdx = 0; colIdx < colMembers.size(); ++colIdx) {
        const QList<QDockWidget *> &members = colMembers.at(colIdx);
        QDockWidget *anchor = colAnchors.at(colIdx);
        QDockWidget *raiseTarget = anchor;
        for (QDockWidget *dock : members) {
            if (dock != anchor) {
                mainWindow->tabifyDockWidget(anchor, dock);
                dock->show();
            }
            if (preferred.contains(dock) && raiseTarget == anchor && !preferred.contains(anchor)) {
                raiseTarget = dock;
            }
            visibleDocks << dock;
            ++arrangedCount;
        }
        raiseTarget->raise();
    }

    if (!colAnchors.isEmpty()) {
        mainWindow->resizeDocks(colAnchors, colWidths, Qt::Horizontal);
        // Re-apply once the new dock layout has been processed (sizes requested before the layout settles are ignored).
        QList<QPointer<QDockWidget>> guarded;
        for (QDockWidget *a : std::as_const(colAnchors)) {
            guarded.append(QPointer<QDockWidget>(a));
        }
        QPointer<QMainWindow> win(mainWindow);
        QTimer::singleShot(0, mainWindow, [win, guarded, colWidths]() {
            if (!win) {
                return;
            }
            QList<QDockWidget *> docks;
            QList<int> widths;
            for (int i = 0; i < guarded.size(); ++i) {
                if (guarded.at(i)) {
                    docks.append(guarded.at(i).data());
                    widths.append(colWidths.at(i));
                }
            }
            win->resizeDocks(docks, widths, Qt::Horizontal);
        });
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
    // V1.2.1: single design system. The component layer lives in BangOnProTheme (globalApplicationStyleSheet);
    // this alias remains for API compatibility and widgets no longer install their own copies.
    return componentStyleSheet();
}

QDockWidget *createAiAssistantDock(QMainWindow *mainWindow, AiAssistantDock *contentWidget, bool initiallyVisible)
{
    if (mainWindow == nullptr) {
        return nullptr;
    }

    auto *dock = new QDockWidget(QCoreApplication::translate("BangOnProWorkspace", "AI Assistant"), mainWindow);
    dock->setObjectName(QString::fromLatin1(kAiAssistantDockName));
    dock->setFeatures(QDockWidget::DockWidgetClosable | QDockWidget::DockWidgetMovable | QDockWidget::DockWidgetFloatable);
    dock->setAllowedAreas(Qt::AllDockWidgetAreas);

    AiAssistantDock *widget = contentWidget != nullptr ? contentWidget : new AiAssistantDock(dock);
    // Scroll container: the dense assistant must never force the window taller than the screen (V1.2.1).
    dock->setWidget(makeScrollableDockContent(widget, dock));

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
        // Premiere Pro / DaVinci Resolve practical editing layout:
        // Project Bin & Unified Effects top-left, Source Monitor + Effect Controls top-center,
        // Program Monitor top-right, full-width Timeline bottom.
        // AI Assistant docked and collapsed/hidden so manual editing stays unobstructed.
        dockTopStudioColumns(mainWindow,
                             {
                                 {{d.projectBin, d.effectList, d.compositionList, d.undoHistory}, d.projectBin, 320},
                                 {{d.clipMonitor, d.effectStack}, d.clipMonitor != nullptr ? d.clipMonitor : d.effectStack, 520},
                                 {{d.projectMonitor}, d.projectMonitor, 600},
                             },
                             visibleDocks, arrangedCount);

        if (d.aiAssistant != nullptr) {
            d.aiAssistant->setAllowedAreas(Qt::AllDockWidgetAreas);
            mainWindow->addDockWidget(Qt::RightDockWidgetArea, d.aiAssistant);
            d.aiAssistant->setFloating(false);
            d.aiAssistant->hide(); // Collapsible on right edge; toggle via AI Panel pill
            ++arrangedCount;
        }
        break;
    }

    case WorkspaceMode::Multicam: {
        // Project Bin left, Dual/Program Monitor center, Audio Mixer + AI Assistant right, full-width Timeline bottom.
        dockTopStudioColumns(
            mainWindow,
            {
                {{d.projectBin, d.effectStack}, d.projectBin, 260},
                {{d.projectMonitor, d.clipMonitor}, d.projectMonitor, 640},
                {{d.audioMixer, d.aiAssistant}, d.audioMixer != nullptr ? d.audioMixer : d.aiAssistant, 520},
            },
            visibleDocks, arrangedCount);
        break;
    }

    case WorkspaceMode::AiFirstCut: {
        // V1.2.1: the production department gets a full-height right column beside the timeline (it is the
        // primary tool here); Project Bin and Program/Clip Monitor sit above the timeline.
        dockTopStudioColumns(mainWindow,
                             {
                                 {{d.projectBin, d.premiereBrowser}, d.projectBin, 280},
                                 {{d.projectMonitor, d.clipMonitor}, d.projectMonitor, 720},
                             },
                             visibleDocks, arrangedCount);
        QList<QDockWidget *> right;
        for (QDockWidget *dk : {d.agentControlCentre, d.aiAssistant, d.transcriptDock}) {
            if (dk != nullptr) {
                right.append(dk);
            }
        }
        if (!right.isEmpty()) {
            QDockWidget *anchor = right.first();
            for (QDockWidget *dk : std::as_const(right)) {
                mainWindow->removeDockWidget(dk);
                dk->setFloating(false);
            }
            mainWindow->addDockWidget(Qt::RightDockWidgetArea, anchor);
            anchor->show();
            for (QDockWidget *dk : std::as_const(right)) {
                if (dk != anchor) {
                    mainWindow->tabifyDockWidget(anchor, dk);
                    dk->show();
                }
                visibleDocks << dk;
                ++arrangedCount;
            }
            anchor->raise();
            QPointer<QMainWindow> win(mainWindow);
            QPointer<QDockWidget> guardedAnchor(anchor);
            QTimer::singleShot(0, mainWindow, [win, guardedAnchor]() {
                if (win && guardedAnchor) {
                    const int w = qBound(360, win->width() / 4, 480);
                    win->resizeDocks({guardedAnchor.data()}, {w}, Qt::Horizontal);
                }
            });
        }
        break;
    }

    case WorkspaceMode::Effects: {
        // Unified Effects Library & Bin left, Program/Clip Monitor center, Effect Controls (raised on top) & Premiere Presets right, full-width Timeline
        // bottom.
        QDockWidget *preferredLeft = d.effectList != nullptr ? d.effectList : d.projectBin;
        QDockWidget *preferredRight = d.effectStack != nullptr ? d.effectStack : d.premiereBrowser;
        dockTopStudioColumns(mainWindow,
                             {
                                 {{d.effectList, d.compositionList, d.projectBin}, preferredLeft, 300},
                                 {{d.projectMonitor, d.clipMonitor}, d.projectMonitor, 540},
                                 {{d.effectStack, d.premiereBrowser}, preferredRight, 580},
                             },
                             visibleDocks, arrangedCount);
        break;
    }

    case WorkspaceMode::Colour: {
        // Project Bin left, Program Monitor center, Color Scopes & Effect Stack right, full-width Timeline bottom.
        QList<QDockWidget *> rightDocks;
        rightDocks << d.effectStack;
        for (QDockWidget *scope : d.allColorScopeDocks()) {
            rightDocks << scope;
        }
        rightDocks << d.effectList;
        dockTopStudioColumns(
            mainWindow,
            {
                {{d.projectBin, d.premiereBrowser}, d.projectBin, 260},
                {{d.projectMonitor}, d.projectMonitor, 620},
                {rightDocks, d.effectStack != nullptr ? d.effectStack : (!rightDocks.isEmpty() ? rightDocks.first() : nullptr), 540},
            },
            visibleDocks, arrangedCount);
        break;
    }

    case WorkspaceMode::Audio: {
        // Effects & Audio presets left, Program Monitor center, Audio Mixer & Effect Stack right, full-width Timeline bottom.
        QDockWidget *preferredLeft = d.effectList != nullptr ? d.effectList : d.projectBin;
        dockTopStudioColumns(
            mainWindow,
            {
                {{d.effectList, d.premiereBrowser, d.projectBin}, preferredLeft, 260},
                {{d.projectMonitor}, d.projectMonitor, 580},
                {{d.audioMixer, d.effectStack}, d.audioMixer != nullptr ? d.audioMixer : d.effectStack, 580},
            },
            visibleDocks, arrangedCount);
        break;
    }

    case WorkspaceMode::Captions: {
        // Project Bin left, Program Monitor center, Transcript & Subtitles right, full-width Timeline bottom.
        QDockWidget *preferredRight = d.transcriptDock != nullptr ? d.transcriptDock : (d.subtitleDock != nullptr ? d.subtitleDock : d.aiAssistant);
        dockTopStudioColumns(
            mainWindow,
            {
                {{d.projectBin, d.effectStack}, d.projectBin, 260},
                {{d.projectMonitor}, d.projectMonitor, 560},
                {{d.transcriptDock, d.subtitleDock, d.aiAssistant}, preferredRight, 600},
            },
            visibleDocks, arrangedCount);
        break;
    }

    case WorkspaceMode::Export: {
        // Project Bin left, Program Monitor center, Render / AI QC right, full-width Timeline bottom.
        QDockWidget *preferredRight = d.renderDock != nullptr ? d.renderDock : d.aiAssistant;
        dockTopStudioColumns(
            mainWindow,
            {
                {{d.projectBin, d.undoHistory}, d.projectBin, 260},
                {{d.projectMonitor}, d.projectMonitor, 580},
                {{d.renderDock, d.aiAssistant}, preferredRight, 580},
            },
            visibleDocks, arrangedCount);
        break;
    }

    case WorkspaceMode::Rotoscoping: {
        // Project Bin left, Program Monitor center, Roto & 2D Studio + Effect Stack right, full-width Timeline bottom.
        dockTopStudioColumns(
            mainWindow,
            {
                {{d.projectBin, d.effectList}, d.projectBin, 240},
                {{d.projectMonitor, d.clipMonitor}, d.projectMonitor, 520},
                {{d.rotoStudio, d.effectStack}, d.rotoStudio, 660},
            },
            visibleDocks, arrangedCount);
        break;
    }
    }

    hideUnusedDocks(d, visibleDocks);

    // Hide any extra Kdenlive docks (e.g. Library, Speech Editor, Project Notes, Clip Properties)
    // that are not part of the active studio workspace so layouts fit comfortably on laptop displays.
    const QList<QDockWidget *> allMainDocks = mainWindow->findChildren<QDockWidget *>(QString(), Qt::FindDirectChildrenOnly);
    for (QDockWidget *dock : allMainDocks) {
        if (dock == nullptr || visibleDocks.contains(dock)) {
            continue;
        }
        if (dock->objectName().contains(QStringLiteral("timeline"), Qt::CaseInsensitive)) {
            continue;
        }
        dock->hide();
    }
    // Styled dock title bars paint accelerator markers literally ("C&lip Monitor"); keep titles clean.
    for (QDockWidget *dock : allMainDocks) {
        const QString title = dock->windowTitle();
        if (title.contains(QLatin1Char('&'))) {
            QString clean = title;
            clean.replace(QStringLiteral("&&"), QStringLiteral("\x01"));
            clean.remove(QLatin1Char('&'));
            clean.replace(QStringLiteral("\x01"), QStringLiteral("&"));
            dock->setWindowTitle(clean);
        }
    }
    // Keep the window on the current screen after every workspace change (restored layouts from larger displays).
    QTimer::singleShot(0, mainWindow, [mainWindow]() { fitWindowToScreen(mainWindow); });
    if (qEnvironmentVariableIsSet("BANGONPRO_LAYOUT_DEBUG")) {
        // Layout diagnostics: which visible docks force the main window wider/taller than the screen.
        QTimer::singleShot(0, mainWindow, [mainWindow]() {
            qInfo("[bangonpro-layout] window=%dx%d minHint=%dx%d", mainWindow->width(), mainWindow->height(), mainWindow->minimumSizeHint().width(),
                  mainWindow->minimumSizeHint().height());
            const QList<QDockWidget *> docks = mainWindow->findChildren<QDockWidget *>();
            for (QDockWidget *dk : docks) {
                if (dk->isVisible()) {
                    const QWidget *w = dk->widget();
                    qInfo("[bangonpro-layout] dock=%s area=%d size=%dx%d minHint=%dx%d min=%dx%d widgetMinHint=%dx%d", qPrintable(dk->objectName()),
                          int(mainWindow->dockWidgetArea(dk)), dk->width(), dk->height(), dk->minimumSizeHint().width(), dk->minimumSizeHint().height(),
                          dk->minimumWidth(), dk->minimumHeight(), w ? w->minimumSizeHint().width() : -1, w ? w->minimumSizeHint().height() : -1);
                }
            }
            if (QWidget *c = mainWindow->centralWidget()) {
                qInfo("[bangonpro-layout] central minHint=%dx%d", c->minimumSizeHint().width(), c->minimumSizeHint().height());
            }
        });
    }
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
    setFixedHeight(36);
    setAttribute(Qt::WA_StyledBackground, true);
    buildUi();
}

void WorkspaceSwitcherBar::buildUi()
{
    auto *layout = new QHBoxLayout(this);
    layout->setContentsMargins(8, 0, 8, 0);
    layout->setSpacing(2);

    auto *brandTag = new QLabel(QStringLiteral("Bang On Pro"), this);
    brandTag->setObjectName(QStringLiteral("bangOnProBadge"));
    layout->addWidget(brandTag);
    layout->addSpacing(8);

    m_modeGroup = new QButtonGroup(this);
    m_modeGroup->setExclusive(true);

    const QVector<WorkspaceMode> modes = allWorkspaceModes();
    for (const WorkspaceMode mode : modes) {
        QString btnLabel = workspaceModeDisplayName(mode);
        btnLabel.replace(QLatin1Char('&'), QStringLiteral("&&"));
        auto *btn = new QPushButton(btnLabel, this);
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

    m_overflowButton = new QToolButton(this);
    m_overflowButton->setObjectName(QStringLiteral("workspaceOverflowButton"));
    m_overflowButton->setText(tr("More"));
    m_overflowButton->setPopupMode(QToolButton::InstantPopup);
    m_overflowButton->setCursor(Qt::PointingHandCursor);
    m_overflowButton->setToolTip(tr("Additional studio workspaces"));
    m_overflowMenu = new QMenu(m_overflowButton);
    m_overflowButton->setMenu(m_overflowMenu);
    m_overflowButton->setVisible(false);
    layout->addWidget(m_overflowButton);

    layout->addStretch(1);

    m_aiPanelToggleButton = new QPushButton(tr("AI panel"), this);
    m_aiPanelToggleButton->setObjectName(QStringLiteral("workspaceAiPanelToggle"));
    m_aiPanelToggleButton->setCheckable(true);
    m_aiPanelToggleButton->setChecked(false);
    m_aiPanelToggleButton->setCursor(Qt::PointingHandCursor);
    m_aiPanelToggleButton->setToolTip(tr("Show or collapse the Bang On Pro AI Assistant dock"));
    layout->addWidget(m_aiPanelToggleButton);

    layout->addSpacing(6);

    m_saveLayoutButton = new QToolButton(this);
    m_saveLayoutButton->setObjectName(QStringLiteral("workspaceSaveLayoutButton"));
    m_saveLayoutButton->setText(tr("Layout"));
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

void WorkspaceSwitcherBar::resizeEvent(QResizeEvent *event)
{
    QWidget::resizeEvent(event);
    updateResponsivePills();
}

void WorkspaceSwitcherBar::updateResponsivePills()
{
    if (!m_overflowButton || !m_overflowMenu || width() <= 0) {
        return;
    }
    const int reserved = 135 + m_aiPanelToggleButton->sizeHint().width() + m_saveLayoutButton->sizeHint().width() + 72;
    const int budget = qMax(180, width() - reserved);
    const QVector<WorkspaceMode> modes = allWorkspaceModes();
    int used = 0;
    m_overflowMenu->clear();
    bool hasOverflow = false;
    for (const WorkspaceMode mode : modes) {
        QPushButton *btn = m_modeButtons.value(static_cast<int>(mode));
        if (!btn) {
            continue;
        }
        const int w = btn->sizeHint().width() + 4;
        const bool isActive = (mode == m_activeMode);
        if (isActive || used + w <= budget) {
            btn->setVisible(true);
            used += w;
        } else {
            btn->setVisible(false);
            hasOverflow = true;
            QAction *act = m_overflowMenu->addAction(workspaceModeDisplayName(mode));
            connect(act, &QAction::triggered, this, [this, mode]() { handleWorkspaceButtonClicked(static_cast<int>(mode)); });
        }
    }
    m_overflowButton->setVisible(hasOverflow);
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
    // "Save Current Layout…" saves the window's current dock arrangement under a name the editor chooses
    // (it then appears under Restore / Reset Saved Layout).
    if (m_attachedWindow) {
        bool ok = false;
        const QString name = QInputDialog::getText(this, tr("Save Current Layout"), tr("Layout name:"), QLineEdit::Normal, QString(), &ok).trimmed();
        if (ok && !name.isEmpty() && saveWorkspaceState(m_attachedWindow.data(), name)) {
            refreshSavedLayoutsMenu();
        }
    }
    Q_EMIT saveLayoutRequested();
}

} // namespace BangOnPro
