/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "BangOnProIntegration.h"

#include "effects_browser/PremierePresetBrowser.h"
#include "roto_ui/RotoStudioDock.h"
#include "timeline_api/KdenliveTimelineBridge.h"
#include "timeline_api/TimelineCommandEngine.h"
#include "timeline_api/TimelineCommandServer.h"
#include "ui/AgentControlCentre.h"
#include "ui/AiAssistantDock.h"
#include "ui/BangOnProShortcutPresets.h"
#include "ui/BangOnProTheme.h"
#include "ui/BangOnProWorkspace.h"
#include "ui/ExternalProviderConsentDialog.h"
#include "ui/ModelManagerDialog.h"
#include "ui/ShowProfileEditorDialog.h"
#include "ui/TranscriptEditorWidget.h"

#include "bin/projectclip.h"
#include "bin/projectitemmodel.h"
#include "core.h"
#include "doc/kdenlivedoc.h"
#include "effects/effectlist/view/effectlistwidget.hpp"
#include "mainwindow.h"
#include "monitor/monitor.h"
#include "monitor/monitormanager.h"
#include "monitor/monitorproxy.h"
#include "project/projectmanager.h"
#include "timeline2/view/timelinecontroller.h"
#include "timeline2/view/timelinewidget.h"

#include <KActionCollection>
#include <QAction>
#include <QApplication>
#include <QCoreApplication>
#include <QDateTime>
#include <QDir>
#include <QDockWidget>
#include <QEvent>
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QInputDialog>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMainWindow>
#include <QMenu>
#include <QMenuBar>
#include <QMessageBox>
#include <QProcess>
#include <QProcessEnvironment>
#include <QPushButton>
#include <QRegularExpression>
#include <QScrollArea>
#include <QSettings>
#include <QStandardPaths>
#include <QTimer>
#include <QToolBar>
#include <QUrl>
#include <QUuid>
#include <QXmlStreamWriter>

#ifdef Q_OS_UNIX
#include <cerrno>
#include <csignal>
#include <sys/types.h>
#endif

#ifndef BANGONPRO_SOURCE_DIR
#define BANGONPRO_SOURCE_DIR ""
#endif

namespace BangOnPro {

namespace {
QJsonObject readJsonFile(const QString &path, QString *err)
{
    QFile f(path);
    if (!f.open(QIODevice::ReadOnly)) {
        if (err) {
            *err = QStringLiteral("Cannot open %1").arg(path);
        }
        return {};
    }
    QJsonParseError pe;
    const QJsonDocument doc = QJsonDocument::fromJson(f.readAll(), &pe);
    if (pe.error != QJsonParseError::NoError || !doc.isObject()) {
        if (err) {
            *err = QStringLiteral("Invalid JSON in %1: %2").arg(path, pe.errorString());
        }
        return {};
    }
    return doc.object();
}

// V1.2.2: Bang On Pro's own settings (storage folder, first launch, open timing session).
const QString kSettingsOrg = QStringLiteral("BangOnPro");
const QString kSettingsApp = QStringLiteral("BangOnPro");

QString customProfilesDir()
{
    // Must match bangonpro/show_profiles/loader.py default_custom_profiles_dir().
    const QString env = qEnvironmentVariable("BANGONPRO_PROFILES_DIR");
    return env.isEmpty() ? QDir::homePath() + QStringLiteral("/.local/share/bangonpro/profiles") : env;
}

/**
 * True when a live Bang On Pro process owns (is running) the production in jobDir.
 * Every run holds <job>/run.lock and removes it only after recording a final status, so "running"
 * without a lock, or with a lock whose process is gone (or whose pid now belongs to something else), is not running.
 */
bool jobOwnerAlive(const QString &jobDir)
{
#ifdef Q_OS_UNIX
    const QJsonObject lock = readJsonFile(QDir(jobDir).filePath(QStringLiteral("run.lock")), nullptr);
    const qint64 pid = lock.value(QStringLiteral("pid")).toVariant().toLongLong();
    if (pid <= 0) {
        return false;
    }
    if (::kill(static_cast<pid_t>(pid), 0) != 0 && errno != EPERM) {
        return false;
    }
    QProcess ps;
    ps.start(QStringLiteral("ps"), {QStringLiteral("-p"), QString::number(pid), QStringLiteral("-o"), QStringLiteral("command=")});
    if (!ps.waitForFinished(3000)) {
        ps.kill();
        return true; // cannot tell: trust the live pid
    }
    return ps.exitCode() == 0 && QString::fromLocal8Bit(ps.readAllStandardOutput()).contains(QLatin1String("bangonpro"));
#else
    Q_UNUSED(jobDir)
    return false;
#endif
}

QString recordedJobStatus(const QString &jobDir)
{
    return readJsonFile(QDir(jobDir).filePath(QStringLiteral("job.json")), nullptr).value(QStringLiteral("status")).toString();
}

/** Ask a production process to stop cleanly (it saves the job as interrupted), then make sure it is gone. */
void stopProductionProcess(QProcess *proc)
{
    if (!proc || proc->state() == QProcess::NotRunning) {
        return;
    }
    proc->terminate();
    if (!proc->waitForFinished(5000)) {
        proc->kill();
        proc->waitForFinished(2000);
    }
}

bool queueHasPendingWork(const QJsonObject &queueDoc, QString *runningJobDir = nullptr, QString *runningLabel = nullptr)
{
    bool pending = false;
    for (const QJsonValue &v : queueDoc.value(QStringLiteral("entries")).toArray()) {
        const QJsonObject e = v.toObject();
        const QString status = e.value(QStringLiteral("status")).toString();
        if (status == QLatin1String("running") && runningJobDir) {
            *runningJobDir = e.value(QStringLiteral("job_dir")).toString();
            if (runningLabel) {
                *runningLabel = e.value(QStringLiteral("label")).toString();
            }
        }
        pending = pending || status == QLatin1String("queued") || status == QLatin1String("interrupted") || status == QLatin1String("running");
    }
    return pending;
}

QString sanitizeFileSlug(const QString &raw)
{
    QString slug = raw.toLower();
    slug.replace(QRegularExpression(QStringLiteral("[^a-z0-9_-]+")), QStringLiteral("_"));
    slug = slug.left(64);
    return slug.isEmpty() ? QStringLiteral("custom_stack") : slug;
}

QString buildEffectGroupXml(const QString &stackName, const QJsonArray &mltFilters)
{
    if (mltFilters.isEmpty()) {
        return {};
    }
    QString xml;
    QXmlStreamWriter writer(&xml);
    writer.setAutoFormatting(true);
    writer.writeStartDocument();
    writer.writeStartElement(QStringLiteral("effectgroup"));
    writer.writeAttribute(QStringLiteral("id"), stackName);
    writer.writeAttribute(QStringLiteral("parentIn"), QStringLiteral("0"));
    for (const QJsonValue &fv : mltFilters) {
        const QJsonObject fObj = fv.toObject();
        const QString service = fObj.value(QStringLiteral("service")).toString();
        if (service.isEmpty()) {
            continue;
        }
        writer.writeStartElement(QStringLiteral("effect"));
        writer.writeAttribute(QStringLiteral("id"), service);
        const QJsonObject props = fObj.value(QStringLiteral("properties")).toObject();
        for (auto it = props.constBegin(); it != props.constEnd(); ++it) {
            writer.writeStartElement(QStringLiteral("property"));
            writer.writeAttribute(QStringLiteral("name"), it.key());
            writer.writeCharacters(it.value().toVariant().toString());
            writer.writeEndElement();
        }
        writer.writeStartElement(QStringLiteral("property"));
        writer.writeAttribute(QStringLiteral("name"), QStringLiteral("kdenlive:collapsed"));
        writer.writeCharacters(QStringLiteral("0"));
        writer.writeEndElement();
        writer.writeEndElement();
    }
    writer.writeTextElement(QStringLiteral("description"), QStringLiteral("Bang On Pro custom effect stack: %1").arg(stackName));
    writer.writeEndElement();
    writer.writeEndDocument();
    return xml;
}
} // namespace

BangOnProIntegration::BangOnProIntegration(QMainWindow *mainWindow, EffectListWidget *effectList, QObject *parent)
    : QObject(parent)
    , m_mainWindow(mainWindow)
    , m_effectList(effectList)
{
    setupEngine();
    setupAiDock();
    setupAgentControlCentre();
    setupTranscriptDock();
    setupPresetBrowser();
    setupRotoStudioDock();
    setupWorkspaceBar();
    // Menu actions (incl. the nine workspace actions) must exist before the shortcut preset is applied,
    // otherwise Alt+Shift+1…9 workspace shortcuts are never bound (V1.2.1 fix).
    setupMenu();
    setupShortcuts();
    loadBuiltinStarterPresetsIfNeeded();
    // V1.2.2: custom profiles, queue and history, unattended queue recovery, open timing session, first launch.
    loadCustomProfiles();
    refreshHistory();
    if (queueHasPendingWork(readJsonFile(queueRoot() + QStringLiteral("/queue.json"), nullptr))) {
        // Work left in the queue (including a job interrupted by a crash or quit) resumes from its checkpoint.
        QTimer::singleShot(2500, this, [this]() { ensureQueueWorker(); });
    }
    {
        QSettings settings(kSettingsOrg, kSettingsApp);
        m_timingSession = settings.value(QStringLiteral("measurement/openSession")).toString();
        if (!m_timingSession.isEmpty()) {
            m_agentCentre->setTimingActive(true, settings.value(QStringLiteral("measurement/paused"), false).toBool());
        }
        if (!settings.value(QStringLiteral("setup/firstLaunchDone"), false).toBool()) {
            QTimer::singleShot(1800, m_mainWindow, [this]() { openSystemCheck(true); });
        }
    }
    qApp->installEventFilter(this);
    // Restored window geometry can come from a larger display; keep the editor on the current screen.
    QTimer::singleShot(0, m_mainWindow, [this]() { fitWindowToScreen(m_mainWindow); });
    QTimer::singleShot(1500, m_mainWindow, [this]() { fitWindowToScreen(m_mainWindow); });
}

BangOnProIntegration::~BangOnProIntegration()
{
    qApp->removeEventFilter(this);
    if (m_aiProcess) {
        m_aiProcess->kill();
        m_aiProcess->waitForFinished(2000);
    }
    // A production stopped by quitting is saved as interrupted and resumes from its checkpoint next time.
    // SIGTERM first so Python records that truthfully; a hard kill would leave a "running" job behind.
    if (m_deptProcess) {
        m_deptProcess->disconnect(this);
        stopProductionProcess(m_deptProcess);
    }
    if (m_queueWorker) {
        m_queueWorker->disconnect(this);
        stopProductionProcess(m_queueWorker);
    }
}

bool BangOnProIntegration::eventFilter(QObject *watched, QEvent *event)
{
    if (!m_timingSession.isEmpty() && m_agentCentre &&
        (event->type() == QEvent::KeyPress || event->type() == QEvent::MouseButtonPress || event->type() == QEvent::Wheel)) {
        auto *widget = qobject_cast<QWidget *>(watched);
        // Input inside the Control Centre is categorised there (setup, review, correction, approval).
        const bool inCentre = widget && m_agentDock && (widget == m_agentDock || m_agentDock->isAncestorOf(widget));
        if (!inCentre) {
            m_agentCentre->noteActivity(QStringLiteral("editing"));
        }
    }
    return QObject::eventFilter(watched, event);
}

QString BangOnProIntegration::locatePythonPackageRoot()
{
    QStringList candidates;
    const QString envPath = qEnvironmentVariable("BANGONPRO_PYTHONPATH");
    if (!envPath.isEmpty()) {
        candidates << envPath;
    }
    const QString appDir = QCoreApplication::applicationDirPath();
    candidates << appDir + QStringLiteral("/../Resources")                  // macOS .app bundle
               << appDir + QStringLiteral("/../Resources/bangonpro-python") // alternate macOS layout
               << appDir + QStringLiteral("/../share/bangonpro/python")     // installed prefix
               << QString::fromUtf8(BANGONPRO_SOURCE_DIR);                  // developer build
    for (const QString &c : qAsConst(candidates)) {
        if (!c.isEmpty() && QFileInfo::exists(c + QStringLiteral("/bangonpro/__init__.py"))) {
            return QDir(c).absolutePath();
        }
    }
    return {};
}

QString BangOnProIntegration::pythonExecutable()
{
    const QString envPy = qEnvironmentVariable("BANGONPRO_PYTHON");
    if (!envPy.isEmpty() && QFileInfo::exists(envPy)) {
        return envPy;
    }
    const QString bundledPy = QCoreApplication::applicationDirPath() + QStringLiteral("/../Resources/python-env/bin/python3");
    if (QFileInfo::exists(bundledPy)) {
        return QFileInfo(bundledPy).canonicalFilePath();
    }
    const QString found = QStandardPaths::findExecutable(QStringLiteral("python3"));
    return found.isEmpty() ? QStringLiteral("python3") : found;
}

QString BangOnProIntegration::dataDir()
{
    const QString dir = QStandardPaths::writableLocation(QStandardPaths::AppDataLocation) + QStringLiteral("/bangonpro");
    QDir().mkpath(dir);
    return dir;
}

QString BangOnProIntegration::ipcSocketPath() const
{
    return m_server ? m_server->socketPath() : QString();
}

WorkspaceDocks BangOnProIntegration::collectWorkspaceDocks() const
{
    WorkspaceDocks docks;
    docks.aiAssistant = m_aiDock;
    docks.agentControlCentre = m_agentDock;
    docks.premiereBrowser = m_presetDock;
    docks.transcriptDock = m_transcriptDock;
    docks.rotoStudio = m_rotoDock;
    return docks;
}

QStringList BangOnProIntegration::collectProjectBinMediaPaths() const
{
    QStringList paths;
    if (!Core::self() || !Core::self()->projectItemModel()) {
        return paths;
    }
    const auto binModel = Core::self()->projectItemModel();
    for (const QString &binId : binModel->getAllClipIds()) {
        if (auto clip = binModel->getClipByBinID(binId)) {
            const QString url = clip->clipUrl();
            if (!url.isEmpty() && QFileInfo::exists(url) && !paths.contains(url)) {
                paths.append(url);
            }
        }
    }
    return paths;
}

void BangOnProIntegration::setupEngine()
{
    // Single authorised writer: every AI edit goes through this engine into the live Kdenlive timeline.
    m_engine = std::make_unique<TimelineCommandEngine>(std::make_shared<KdenliveTimelineBridge>());
    m_server = std::make_unique<TimelineCommandServer>(m_engine.get());
    QString err;
    if (!m_server->start(QString(), &err)) {
        qWarning("Bang On Pro: Timeline Command IPC server disabled: %s", qPrintable(err));
    }
}

void BangOnProIntegration::setupAiDock()
{
    m_aiWidget = new AiAssistantDock(m_mainWindow);
    m_aiDock = createAiAssistantDock(m_mainWindow, m_aiWidget, true);
    m_aiWidget->setTimelineRevision(m_engine->currentRevision());
    m_aiWidget->setCanUndo(m_engine->canUndo());
    if (m_server && !m_server->socketPath().isEmpty()) {
        m_aiWidget->appendActivityLog(QStringLiteral("integration"), QStringLiteral("Timeline Command API v1 listening on %1").arg(m_server->socketPath()));
    }
    if (locatePythonPackageRoot().isEmpty()) {
        m_aiWidget->appendActivityLog(QStringLiteral("integration"),
                                      QStringLiteral("bangonpro Python package not found; set BANGONPRO_PYTHONPATH to enable AI proposals"),
                                      QStringLiteral("WARN"));
    }

    connect(m_aiWidget, &AiAssistantDock::proposalRequested, this, &BangOnProIntegration::startProposal);
    connect(m_aiWidget, &AiAssistantDock::refineRequested, this,
            [this](const QString &, const QString &refinement, const QString &profileId) { startProposal(refinement, profileId, QString()); });
    connect(m_aiWidget, &AiAssistantDock::batchApproveSelectionRequested, this,
            [this](const QString &requestId, const QStringList &opIds) { approveBatchSelection(requestId, opIds, false); });
    connect(m_aiWidget, &AiAssistantDock::batchRebaseApproveRequested, this,
            [this](const QString &requestId, const QStringList &opIds) { approveBatchSelection(requestId, opIds, true); });
    connect(m_aiWidget, &AiAssistantDock::regenerateProposalRequested, this, &BangOnProIntegration::regenerateProposal);
    connect(m_aiWidget, &AiAssistantDock::batchRejectRequested, this, &BangOnProIntegration::rejectBatch);
    connect(m_aiWidget, &AiAssistantDock::undoRequested, this, &BangOnProIntegration::undoLast);
    connect(m_aiWidget, &AiAssistantDock::taskCancelRequested, this, &BangOnProIntegration::cancelTask);
    connect(m_aiWidget, &AiAssistantDock::compareModeToggled, this, &BangOnProIntegration::toggleCompareMode);
    connect(m_aiWidget, &AiAssistantDock::editProfileRequested, this, &BangOnProIntegration::openShowProfileEditor);
    connect(m_aiWidget, &AiAssistantDock::providerSettingsRequested, this, &BangOnProIntegration::openExternalProviderSettings);
    connect(m_aiWidget, &AiAssistantDock::modelManagerRequested, this, &BangOnProIntegration::openModelManager);
    connect(m_aiWidget, &AiAssistantDock::clearCacheRequested, this, &BangOnProIntegration::clearTemporaryCaches);
    connect(m_engine.get(), &TimelineCommandEngine::revisionChanged, m_aiWidget, &AiAssistantDock::setTimelineRevision);
}

QString BangOnProIntegration::storageRoot()
{
    const QString chosen = QSettings(kSettingsOrg, kSettingsApp).value(QStringLiteral("storage/root")).toString();
    return chosen.isEmpty() ? dataDir() : chosen;
}

bool BangOnProIntegration::storageRootChosen()
{
    return !QSettings(kSettingsOrg, kSettingsApp).value(QStringLiteral("storage/root")).toString().isEmpty();
}

QString BangOnProIntegration::departmentJobsRoot()
{
    const QString dir = storageRoot() + QStringLiteral("/jobs");
    QDir().mkpath(dir);
    return dir;
}

QString BangOnProIntegration::queueRoot()
{
    return storageRoot() + QStringLiteral("/queue");
}

QString BangOnProIntegration::measurementRoot()
{
    // Timing sessions stay with the editor's user data even when productions move to an external drive.
    return dataDir() + QStringLiteral("/measurement");
}

void BangOnProIntegration::setupAgentControlCentre()
{
    m_agentCentre = new AgentControlCentre(m_mainWindow);
    m_agentDock = new QDockWidget(QStringLiteral("Agent Control Centre"), m_mainWindow);
    m_agentDock->setObjectName(QString::fromLatin1(kAgentControlCentreDockName));
    // Shared scroll container: dense panels never force the main window wider or taller than the screen.
    m_agentDock->setWidget(makeScrollableDockContent(m_agentCentre, m_agentDock));
    m_mainWindow->addDockWidget(Qt::RightDockWidgetArea, m_agentDock);
    if (m_aiDock) {
        m_mainWindow->tabifyDockWidget(m_aiDock, m_agentDock);
        m_aiDock->raise();
    }
    connect(m_agentCentre, &AgentControlCentre::startJobRequested, this, &BangOnProIntegration::startDepartmentJob);
    connect(m_agentCentre, &AgentControlCentre::cancelJobRequested, this, [this](const QString &jobDir) {
        if (!m_deptProcess && !(m_queueWorker && jobDir == m_queueRunningJobDir) && !jobOwnerAlive(jobDir)) {
            // Nothing is running this production, so nothing would read a CANCEL file: end it directly.
            runQuickDepartmentCommand({QStringLiteral("cancel"), jobDir}, [this, jobDir](const QJsonObject &) {
                m_agentCentre->recoverJob(jobDir, false);
                refreshHistory();
            });
            return;
        }
        // Cooperative cancellation: agents poll the CANCEL file; checkpoints make the job resumable.
        QFile cancelFile(QDir(jobDir).filePath(QStringLiteral("CANCEL")));
        if (cancelFile.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
            cancelFile.write("Cancelled from Agent Control Centre");
        }
        m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Cancellation requested for %1").arg(jobDir), QStringLiteral("WARN"));
    });
    connect(m_agentCentre, &AgentControlCentre::resumeJobRequested, this, [this](const QString &jobDir) {
        if ((m_queueWorker && jobDir == m_queueRunningJobDir) || jobOwnerAlive(jobDir)) {
            // Never start a second copy of a production that is still running: follow the live one instead.
            m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("This production is already running; showing its progress"));
            m_agentCentre->recoverJob(jobDir, true);
            return;
        }
        runDepartmentCommand({QStringLiteral("resume"), jobDir}, jobDir, QStringLiteral("Resuming job from checkpoint"));
    });
    connect(m_agentCentre, &AgentControlCentre::rerunAgentRequested, this, [this](const QString &jobDir, const QString &agentId) {
        runDepartmentCommand({QStringLiteral("rerun"), jobDir, agentId}, jobDir, QStringLiteral("Rerunning %1").arg(agentId));
    });
    connect(m_agentCentre, &AgentControlCentre::providerSettingsRequested, this, &BangOnProIntegration::openExternalProviderSettings);
    connect(m_agentCentre, &AgentControlCentre::reviseDraftRequested, this, [this](const QString &jobDir, const QString &instruction, bool selectionOnly) {
        QStringList args{QStringLiteral("revise"), jobDir, instruction};
        auto *mw = pCore ? pCore->window() : nullptr;
        if (selectionOnly && mw && mw->getCurrentTimeline() && mw->getCurrentTimeline()->controller()) {
            const auto *tc = mw->getCurrentTimeline()->controller();
            const double fps = pCore->getCurrentFps() > 0 ? pCore->getCurrentFps() : 25.0;
            if (tc->zoneOut() > tc->zoneIn()) {
                args << QStringLiteral("--range-timeline") << QString::number(tc->zoneIn() / fps, 'f', 3) << QString::number(tc->zoneOut() / fps, 'f', 3);
            }
        }
        runDepartmentCommand(args, jobDir, QStringLiteral("Revising: %1").arg(instruction));
    });
    connect(m_agentCentre, &AgentControlCentre::undoRevisionRequested, this, [this](const QString &jobDir) {
        runDepartmentCommand({QStringLiteral("undo-revision"), jobDir}, jobDir, QStringLiteral("Undoing the last revision"));
    });
    connect(m_agentCentre, &AgentControlCentre::agentDecisionRequested, this, [this](const QString &jobDir, const QString &agentId, bool accept) {
        runDepartmentCommand({QStringLiteral("decide"), jobDir, agentId, accept ? QStringLiteral("accept") : QStringLiteral("reject")}, jobDir,
                             QStringLiteral("%1 %2").arg(accept ? QStringLiteral("Accepting") : QStringLiteral("Rejecting"), agentId));
    });
    connect(m_agentCentre, &AgentControlCentre::reviewDraftInTimelineRequested, this, &BangOnProIntegration::stageDepartmentDraft);
    connect(m_agentCentre, &AgentControlCentre::promoteDraftRequested, this, &BangOnProIntegration::promoteDepartmentDraft);
    connect(m_agentCentre, &AgentControlCentre::openDraftProjectRequested, this, [this](const QString &path) {
        m_lastKdenliveProjectPath = path;
        openLastGeneratedProject();
    });
    // Job recovery: reopen the most recent production job after a restart or crash.
    {
        const QFileInfoList jobs = QDir(departmentJobsRoot()).entryInfoList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Time);
        for (const QFileInfo &fi : jobs) {
            if (QFileInfo::exists(QDir(fi.absoluteFilePath()).filePath(QStringLiteral("job.json")))) {
                // A production still running in a process that outlived a crash is followed live, not shown as interrupted.
                m_agentCentre->recoverJob(fi.absoluteFilePath(), jobOwnerAlive(fi.absoluteFilePath()));
                break;
            }
        }
    }
    connect(m_agentCentre, &AgentControlCentre::jobFinished, this, [this](const QString &jobDir, const QString &status) {
        m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Job %1 finished: %2").arg(QFileInfo(jobDir).fileName(), status));
        refreshHistory();
        if (!m_timingSession.isEmpty()) {
            // The agent-assisted session is measured against the job being reviewed (machine time stays separate).
            runQuickDepartmentCommand({QStringLiteral("measure"), QStringLiteral("link"), QStringLiteral("--root"), measurementRoot(), QStringLiteral("--session"),
                                       m_timingSession, QStringLiteral("--job-dir"), jobDir});
        }
    });
    // V1.2.2: queue, history and timed sessions.
    connect(m_agentCentre, &AgentControlCentre::queueJobRequested, this, &BangOnProIntegration::queueDepartmentJob);
    connect(m_agentCentre, &AgentControlCentre::historyJobActivated, this, [this](const QString &jobDir) {
        if (m_deptProcess) {
            m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Wait for the running department task to finish"), QStringLiteral("WARN"));
            return;
        }
        m_agentCentre->recoverJob(jobDir, (m_queueWorker && jobDir == m_queueRunningJobDir) || jobOwnerAlive(jobDir));
    });
    connect(m_agentCentre, &AgentControlCentre::queueEntryActionRequested, this, [this](const QString &queueId, const QString &action) {
        runQuickDepartmentCommand({QStringLiteral("queue"), action, queueId, QStringLiteral("--queue-root"), queueRoot()}, [this](const QJsonObject &) { refreshHistory(); });
    });
    connect(m_agentCentre, &AgentControlCentre::timingStartRequested, this, &BangOnProIntegration::startTiming);
    connect(m_agentCentre, &AgentControlCentre::measurementActivity, this, [this](const QString &category) {
        if (!m_timingSession.isEmpty()) {
            runQuickDepartmentCommand({QStringLiteral("measure"), QStringLiteral("activity"), QStringLiteral("--root"), measurementRoot(), QStringLiteral("--session"),
                                       m_timingSession, QStringLiteral("--category"), category});
        }
    });
    connect(m_agentCentre, &AgentControlCentre::timingPauseToggled, this, [this](bool paused) {
        if (m_timingSession.isEmpty()) {
            return;
        }
        runQuickDepartmentCommand({QStringLiteral("measure"), paused ? QStringLiteral("pause") : QStringLiteral("resume"), QStringLiteral("--root"), measurementRoot(),
                                   QStringLiteral("--session"), m_timingSession});
        m_agentCentre->setTimingActive(true, paused);
        QSettings(kSettingsOrg, kSettingsApp).setValue(QStringLiteral("measurement/paused"), paused);
    });
    connect(m_agentCentre, &AgentControlCentre::timingFinishRequested, this, [this](int rating) {
        if (m_timingSession.isEmpty()) {
            m_agentCentre->setTimingActive(false);
            return;
        }
        const QString session = m_timingSession;
        runQuickDepartmentCommand({QStringLiteral("measure"), QStringLiteral("finish"), QStringLiteral("--root"), measurementRoot(), QStringLiteral("--session"), session,
                                   QStringLiteral("--quality"), QString::number(rating)},
                                  [this](const QJsonObject &res) {
                                      if (res.value(QStringLiteral("ok")).toBool()) {
                                          m_aiWidget->appendActivityLog(QStringLiteral("measurement"),
                                                                        QStringLiteral("Timing session saved. Bang On Pro > Export diagnostics or the measurement report collects it."));
                                      }
                                  });
        m_timingSession.clear();
        QSettings settings(kSettingsOrg, kSettingsApp);
        settings.remove(QStringLiteral("measurement/openSession"));
        settings.remove(QStringLiteral("measurement/paused"));
        m_agentCentre->setTimingActive(false);
    });
}

bool BangOnProIntegration::prepareJobArguments(QStringList &args)
{
    m_deptBaseRevision = m_engine->currentRevision();
    m_deptBaseProjectId = m_engine->activeProjectId().isEmpty() ? QStringLiteral("active_project") : m_engine->activeProjectId();
    m_deptBaseSequenceId = m_engine->activeSequenceId().isEmpty() ? QStringLiteral("seq_main") : m_engine->activeSequenceId();
    if (!args.contains(QStringLiteral("--inputs"))) {
        QStringList inputs = collectProjectBinMediaPaths();
        if (inputs.isEmpty()) {
            inputs = QFileDialog::getOpenFileNames(m_mainWindow, QStringLiteral("Select camera / microphone media for this production"), QString(),
                                                   QStringLiteral("Media (*.mp4 *.mov *.mxf *.mkv *.m4v *.wav *.mp3);;All files (*)"));
        }
        if (inputs.isEmpty()) {
            m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Job not started: no source media selected"));
            return false;
        }
        args << QStringLiteral("--inputs") << inputs;
    }
    args << QStringLiteral("--job-root") << departmentJobsRoot() << QStringLiteral("--project-id") << m_deptBaseProjectId << QStringLiteral("--sequence-id")
         << m_deptBaseSequenceId;
    return true;
}

void BangOnProIntegration::runQuickDepartmentCommand(const QStringList &args, const std::function<void(const QJsonObject &)> &onDone)
{
    QStringList full{QStringLiteral("-m"), QStringLiteral("bangonpro.ai_engine.department")};
    full << args;
    QProcess *proc = startPython(full);
    if (!proc) {
        return;
    }
    const QString what = args.isEmpty() ? QString() : args.first();
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, proc, onDone, what](int code, QProcess::ExitStatus status) {
        proc->deleteLater();
        const QJsonObject res = QJsonDocument::fromJson(proc->readAllStandardOutput()).object();
        if (status != QProcess::NormalExit || code != 0) {
            // The department prints {"error", "help": {"title", "fix"}}; show the plain fix.
            const QJsonObject help = res.value(QStringLiteral("help")).toObject();
            const QString text = help.isEmpty() ? QString::fromUtf8(proc->readAllStandardError().right(400))
                                                : QStringLiteral("%1. %2").arg(help.value(QStringLiteral("title")).toString(), help.value(QStringLiteral("fix")).toString());
            m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("%1: %2").arg(what, text), QStringLiteral("WARN"));
        }
        if (onDone) {
            onDone(res);
        }
    });
}

void BangOnProIntegration::refreshHistory()
{
    runQuickDepartmentCommand({QStringLiteral("history"), QStringLiteral("--job-root"), departmentJobsRoot(), QStringLiteral("--queue-root"), queueRoot(),
                               QStringLiteral("--limit"), QStringLiteral("30")},
                              [this](const QJsonObject &res) {
                                  if (res.contains(QStringLiteral("jobs"))) {
                                      m_agentCentre->loadHistory(res);
                                  }
                              });
}

void BangOnProIntegration::queueDepartmentJob(const QStringList &runArguments)
{
    QStringList args = runArguments;
    if (args.isEmpty() || args.first() != QLatin1String("run")) {
        return;
    }
    args.removeFirst();
    args.prepend(QStringLiteral("add"));
    args.prepend(QStringLiteral("queue"));
    if (!prepareJobArguments(args)) {
        return;
    }
    args << QStringLiteral("--queue-root") << queueRoot();
    runQuickDepartmentCommand(args, [this](const QJsonObject &res) {
        const QJsonObject entry = res.value(QStringLiteral("queued")).toObject();
        if (entry.isEmpty()) {
            return;
        }
        m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Added to the production queue: %1").arg(entry.value(QStringLiteral("label")).toString()));
        refreshHistory();
        ensureQueueWorker();
    });
}

void BangOnProIntegration::ensureQueueWorker()
{
    if (m_queueWorker) {
        return;
    }
    QProcess *proc = startPython({QStringLiteral("-m"), QStringLiteral("bangonpro.ai_engine.department"), QStringLiteral("queue"), QStringLiteral("run"),
                                  QStringLiteral("--queue-root"), queueRoot()});
    if (!proc) {
        return;
    }
    m_queueWorker = proc;
    if (!m_queuePollTimer) {
        m_queuePollTimer = new QTimer(this);
        m_queuePollTimer->setInterval(2000);
        connect(m_queuePollTimer, &QTimer::timeout, this, &BangOnProIntegration::pollQueue);
    }
    m_queuePollTimer->start();
    m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Production queue running (it keeps going while Bang On Pro is open)"));
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, proc](int code, QProcess::ExitStatus) {
        proc->deleteLater();
        m_queueWorker.clear();
        m_queuePollTimer->stop();
        pollQueue();
        refreshHistory();
        const QJsonObject res = QJsonDocument::fromJson(proc->readAllStandardOutput()).object();
        if (code != 0) {
            const QJsonObject help = res.value(QStringLiteral("help")).toObject();
            m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Production queue stopped: %1").arg(res.value(QStringLiteral("error")).toString()),
                                          QStringLiteral("WARN"));
            Q_UNUSED(help)
            return; // e.g. another worker owns the queue; do not loop
        }
        // Work added while the worker was finishing: start again.
        if (queueHasPendingWork(readJsonFile(queueRoot() + QStringLiteral("/queue.json"), nullptr))) {
            QTimer::singleShot(1000, this, [this]() { ensureQueueWorker(); });
        }
    });
}

void BangOnProIntegration::pollQueue()
{
    QString runningDir;
    QString runningLabel;
    queueHasPendingWork(readJsonFile(queueRoot() + QStringLiteral("/queue.json"), nullptr), &runningDir, &runningLabel);
    if (!m_queueWorker) {
        runningDir.clear();
    }
    if (runningDir == m_queueRunningJobDir) {
        return;
    }
    m_queueRunningJobDir = runningDir;
    refreshHistory();
    if (m_deptProcess) {
        return; // the editor is watching their own task; the queue entry stays in the list
    }
    if (!runningDir.isEmpty()) {
        // Follow the queued production live: progress, Cancel and per-agent state work as for any job.
        m_agentCentre->setActiveJobDir(runningDir);
        m_agentCentre->setBusy(true, QStringLiteral("Queued production running: %1").arg(runningLabel));
    } else {
        m_agentCentre->setBusy(false);
        m_agentCentre->refreshFromDisk();
    }
}

void BangOnProIntegration::startTiming(const QString &kind, const QString &deliverable)
{
    QString name = deliverable;
    if (name.isEmpty()) {
        bool ok = false;
        name = QInputDialog::getText(m_mainWindow, QStringLiteral("Time this edit"), QStringLiteral("Deliverable (for example the episode title)"), QLineEdit::Normal,
                                     QString(), &ok)
                   .trimmed();
        if (!ok || name.isEmpty()) {
            return;
        }
    }
    QStringList args{QStringLiteral("measure"), QStringLiteral("start"),  QStringLiteral("--root"),   measurementRoot(), QStringLiteral("--deliverable"),
                     name,                      QStringLiteral("--kind"), kind,                       QStringLiteral("--editor"),
                     qEnvironmentVariable("USER", QStringLiteral("editor"))};
    const QString jobDir = m_agentCentre->activeJobDir();
    if (kind == QLatin1String("agent_assisted") && !jobDir.isEmpty() && QFileInfo::exists(QDir(jobDir).filePath(QStringLiteral("job.json")))) {
        args << QStringLiteral("--job-dir") << jobDir;
    }
    runQuickDepartmentCommand(args, [this, name, kind](const QJsonObject &res) {
        const QString session = res.value(QStringLiteral("session_id")).toString();
        if (session.isEmpty()) {
            return;
        }
        m_timingSession = session;
        QSettings settings(kSettingsOrg, kSettingsApp);
        settings.setValue(QStringLiteral("measurement/openSession"), session);
        settings.setValue(QStringLiteral("measurement/paused"), false);
        m_agentCentre->setTimingActive(true);
        m_aiWidget->appendActivityLog(QStringLiteral("measurement"), QStringLiteral("Timing %1 work on '%2'").arg(kind == QLatin1String("manual_baseline") ? QStringLiteral("manual") : QStringLiteral("agent-assisted"), name));
    });
}

void BangOnProIntegration::loadCustomProfiles()
{
    const QFileInfoList files = QDir(customProfilesDir()).entryInfoList({QStringLiteral("*.json")}, QDir::Files, QDir::Name);
    for (const QFileInfo &fi : files) {
        const QJsonObject p = readJsonFile(fi.absoluteFilePath(), nullptr);
        const QString id = p.value(QStringLiteral("profile_id")).toString();
        if (!id.isEmpty()) {
            m_agentCentre->addCustomProfile(id, p.value(QStringLiteral("display_name")).toString(id));
        }
    }
}

void BangOnProIntegration::openSystemCheck(bool firstLaunch)
{
    SystemCheckDialog dlg(m_mainWindow);
    QPointer<SystemCheckDialog> guard(&dlg);
    if (storageRootChosen()) {
        dlg.setStorageFolder(storageRoot());
    }
    connect(&dlg, &SystemCheckDialog::recheckRequested, this, [this, guard]() {
        if (guard) {
            runReadinessInto(guard);
        }
    });
    connect(&dlg, &SystemCheckDialog::modelManagerRequested, this, [this, guard]() {
        openModelManager();
        if (guard) {
            runReadinessInto(guard);
        }
    });
    connect(&dlg, &SystemCheckDialog::exportDiagnosticsRequested, this, &BangOnProIntegration::exportDiagnostics);
    connect(&dlg, &SystemCheckDialog::storageFolderChosen, this, [this, guard](const QString &folder) {
        if (!QDir().mkpath(folder + QStringLiteral("/jobs")) || !QFileInfo(folder).isWritable()) {
            QMessageBox::warning(guard, QStringLiteral("Folder not usable"),
                                 QStringLiteral("Bang On Pro cannot write to %1. Choose a folder you own, or allow access in System Settings > Privacy & Security.").arg(folder));
            return;
        }
        QSettings(kSettingsOrg, kSettingsApp).setValue(QStringLiteral("storage/root"), folder);
        m_aiWidget->appendActivityLog(QStringLiteral("storage"), QStringLiteral("Productions and the analysis cache now go to %1 (existing jobs stay where they are)").arg(folder));
        refreshHistory();
        if (guard) {
            runReadinessInto(guard);
        }
    });
    connect(&dlg, &SystemCheckDialog::cacheActionRequested, this, [this, guard](const QString &action) {
        if (action == QLatin1String("clear") &&
            QMessageBox::question(guard, QStringLiteral("Clear the analysis cache?"),
                                  QStringLiteral("The next production analyses the footage again. Source media and productions are not touched.")) != QMessageBox::Yes) {
            return;
        }
        QStringList args{QStringLiteral("cache"), action};
        if (action == QLatin1String("prune")) {
            args << QStringLiteral("--max-mb") << QStringLiteral("1024");
        }
        if (storageRootChosen()) {
            args << QStringLiteral("--cache-dir") << storageRoot() + QStringLiteral("/cache");
        }
        runQuickDepartmentCommand(args, [this, guard, action](const QJsonObject &res) {
            m_aiWidget->appendActivityLog(QStringLiteral("storage"), QStringLiteral("Analysis cache %1: %2 entries removed").arg(action).arg(res.value(QStringLiteral("removed")).toInt()));
            if (guard) {
                runReadinessInto(guard);
            }
        });
    });
    runReadinessInto(&dlg);
    dlg.exec();
    if (firstLaunch) {
        QSettings(kSettingsOrg, kSettingsApp).setValue(QStringLiteral("setup/firstLaunchDone"), true);
    }
}

void BangOnProIntegration::runReadinessInto(SystemCheckDialog *dialog)
{
    QPointer<SystemCheckDialog> guard(dialog);
    dialog->setBusy(true);
    QStringList args{QStringLiteral("readiness"), QStringLiteral("--job-root"), departmentJobsRoot()};
    if (storageRootChosen()) {
        args << QStringLiteral("--cache-dir") << storageRoot() + QStringLiteral("/cache");
    }
    runQuickDepartmentCommand(args, [guard](const QJsonObject &report) {
        if (guard && !guard->loadReport(report)) {
            guard->setBusy(false);
        }
    });
}

void BangOnProIntegration::exportDiagnostics()
{
    const QString suggested =
        QDir::homePath() + QStringLiteral("/Desktop/BangOnPro-diagnostics-%1.zip").arg(QDateTime::currentDateTime().toString(QStringLiteral("yyyyMMdd-HHmm")));
    const QString out = QFileDialog::getSaveFileName(m_mainWindow, QStringLiteral("Export diagnostics"), suggested, QStringLiteral("Zip archive (*.zip)"));
    if (out.isEmpty()) {
        return;
    }
    QMessageBox ask(QMessageBox::Question, QStringLiteral("Include media?"),
                    QStringLiteral("Diagnostics never contain passwords, API keys, provider settings or camera originals, and home folder paths are shortened to ~."),
                    QMessageBox::Yes | QMessageBox::No, m_mainWindow);
    ask.setInformativeText(QStringLiteral("Also include transcripts, captions, timelines and renders from recent productions? Only do this if the team asked for them."));
    ask.setDefaultButton(QMessageBox::No);
    const bool includeMedia = ask.exec() == QMessageBox::Yes;
    QStringList args{QStringLiteral("diagnostics"), QStringLiteral("export"), QStringLiteral("--out"), out, QStringLiteral("--job-root"), departmentJobsRoot(),
                     QStringLiteral("--queue-root"), queueRoot()};
    const QString jobDir = m_agentCentre ? m_agentCentre->activeJobDir() : QString();
    if (!jobDir.isEmpty() && QFileInfo::exists(QDir(jobDir).filePath(QStringLiteral("job.json")))) {
        args << QStringLiteral("--job") << jobDir;
    }
    if (storageRootChosen()) {
        args << QStringLiteral("--cache-dir") << storageRoot() + QStringLiteral("/cache");
    }
    if (includeMedia) {
        args << QStringLiteral("--include-media");
    }
    m_aiWidget->appendActivityLog(QStringLiteral("diagnostics"), QStringLiteral("Exporting diagnostics to %1").arg(out));
    runQuickDepartmentCommand(args, [this](const QJsonObject &res) {
        const QString zip = res.value(QStringLiteral("zip_path")).toString();
        if (zip.isEmpty()) {
            return;
        }
        const QString text = QStringLiteral("Saved %1.\n%2 files included; %3 items left out (listed in manifest.json inside the zip).")
                                 .arg(zip)
                                 .arg(res.value(QStringLiteral("included")).toInt())
                                 .arg(res.value(QStringLiteral("excluded")).toInt());
        m_aiWidget->appendActivityLog(QStringLiteral("diagnostics"), text);
        QMessageBox::information(m_mainWindow, QStringLiteral("Diagnostics exported"), text);
    });
}

void BangOnProIntegration::startDepartmentJob(const QStringList &runArguments)
{
    if (m_deptProcess) {
        m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("A production job is already running"), QStringLiteral("WARN"));
        return;
    }
    if (m_queueWorker) {
        // One production at a time: while the queue is busy, a new production waits its turn.
        m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("A queued production is running, so this one was added to the queue"));
        queueDepartmentJob(runArguments);
        return;
    }
    QStringList args = runArguments;
    if (!prepareJobArguments(args)) {
        return;
    }
    const QString jobId = QStringLiteral("job_") + QDateTime::currentDateTimeUtc().toString(QStringLiteral("yyyyMMdd-HHmmss"));
    const QString jobDir = departmentJobsRoot() + QLatin1Char('/') + jobId;
    args << QStringLiteral("--job-id") << jobId;
    runDepartmentCommand(args, jobDir, QStringLiteral("Production job started"));
}

void BangOnProIntegration::runDepartmentCommand(const QStringList &subcommandArgs, const QString &jobDir, const QString &description)
{
    if (m_deptProcess) {
        m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Wait for the running department task to finish"), QStringLiteral("WARN"));
        return;
    }
    QStringList args{QStringLiteral("-m"), QStringLiteral("bangonpro.ai_engine.department")};
    args << subcommandArgs;
    QProcess *proc = startPython(args);
    if (!proc) {
        m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Cannot start AI department: bangonpro package not found"),
                                      QStringLiteral("ERROR"));
        return;
    }
    m_deptProcess = proc;
    m_agentCentre->setActiveJobDir(jobDir);
    m_agentCentre->setBusy(true, description);
    m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("%1 (%2)").arg(description, jobDir));
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, proc, jobDir](int code, QProcess::ExitStatus status) {
        m_deptProcess.clear();
        proc->deleteLater();
        m_agentCentre->setBusy(false);
        QString detail;
        if (status != QProcess::NormalExit || code != 0) {
            // V1.2.2: the department explains failures as {"error", "help": {"title", "fix"}}.
            const QJsonObject help = QJsonDocument::fromJson(proc->readAllStandardOutput()).object().value(QStringLiteral("help")).toObject();
            detail = help.isEmpty() ? QString::fromUtf8(proc->readAllStandardError().right(800)).trimmed()
                                    : QStringLiteral("%1. %2").arg(help.value(QStringLiteral("title")).toString(), help.value(QStringLiteral("fix")).toString());
            m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Department task failed (exit=%1): %2").arg(code).arg(detail), QStringLiteral("ERROR"));
        }
        // The process has ended, so the job cannot still be running here. If job.json says it is (the process was
        // killed or crashed before it could record anything), show it as interrupted with Resume, never as a frozen run.
        const bool stillRecordedRunning = recordedJobStatus(jobDir) == QLatin1String("running");
        if (stillRecordedRunning) {
            const bool ownerAlive = jobOwnerAlive(jobDir); // e.g. a resume refused because another process runs it
            m_agentCentre->recoverJob(jobDir, ownerAlive);
            if (!ownerAlive) {
                m_agentCentre->showProblem(detail.isEmpty() ? tr("The production stopped unexpectedly. Resume continues from the last finished step.")
                                                            : tr("The production stopped: %1").arg(detail));
            }
        } else {
            m_agentCentre->refreshFromDisk();
            if (!detail.isEmpty() && recordedJobStatus(jobDir).isEmpty()) {
                m_agentCentre->showProblem(detail); // the job could not even be created
            }
        }
        refreshHistory();
    });
}

void BangOnProIntegration::stageDepartmentDraft(const QString &jobDir)
{
    QString err;
    const QJsonObject job = readJsonFile(QDir(jobDir).filePath(QStringLiteral("job.json")), &err);
    const QString cmdsPath = job.value(QStringLiteral("artifacts")).toObject().value(QStringLiteral("timeline_commands_json")).toString();
    const QJsonObject cmds = readJsonFile(cmdsPath, &err);
    if (cmds.isEmpty()) {
        m_aiWidget->appendActivityLog(QStringLiteral("department"), err, QStringLiteral("ERROR"));
        return;
    }
    // The isolated draft becomes ONE reviewable proposal at the revision captured when the job started;
    // the engine's stale-proposal detection protects any manual edits made since.
    TimelineCommandBatch merged;
    merged.requestId = QStringLiteral("dept-") + QUuid::createUuid().toString(QUuid::WithoutBraces);
    merged.projectId = m_deptBaseProjectId.isEmpty() ? cmds.value(QStringLiteral("project_id")).toString() : m_deptBaseProjectId;
    merged.sequenceId = m_deptBaseSequenceId.isEmpty() ? cmds.value(QStringLiteral("sequence_id")).toString() : m_deptBaseSequenceId;
    merged.agentId = QStringLiteral("executive");
    merged.agentRole = QStringLiteral("ai_production_department");
    merged.requiresApproval = true;
    merged.expectedRevision = m_deptBaseRevision;
    merged.transactionLabel = QStringLiteral("AI Production Draft (%1)").arg(job.value(QStringLiteral("job_id")).toString());
    const QJsonObject batchAgents = cmds.value(QStringLiteral("batch_agents")).toObject();
    for (const QJsonValue &bv : cmds.value(QStringLiteral("batches")).toArray()) {
        TimelineCommandBatch b;
        QString berr;
        if (!TimelineCommandEngine::batchFromJson(bv.toObject(), &b, &berr)) {
            m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Skipped invalid batch: %1").arg(berr), QStringLiteral("WARN"));
            continue;
        }
        const QString agent = batchAgents.value(b.requestId).toString(b.agentId);
        for (TimelineOperation op : std::as_const(b.operations)) {
            op.rationale = QStringLiteral("[%1] %2").arg(agent, op.rationale);
            merged.operations.append(op);
        }
    }
    const TimelineCommandResponse stageResp = m_engine->executeBatch(merged);
    if (stageResp.status == CommandStatus::RevisionConflict) {
        m_aiWidget->displayProposal(merged);
        m_aiWidget->showStaleProposalWarning(stageResp.errorMessage, false);
        return;
    }
    if (stageResp.status != CommandStatus::PendingApproval) {
        m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Failed to stage draft: %1").arg(stageResp.errorMessage),
                                      QStringLiteral("ERROR"));
        return;
    }
    m_activeProposalRequestId = merged.requestId;
    m_aiWidget->displayProposal(merged);
    if (m_aiDock) {
        m_aiDock->raise();
    }
    m_aiWidget->appendActivityLog(
        QStringLiteral("department"),
        QStringLiteral("Draft staged for review: %1 operations (accept/reject per operation; undoable)").arg(merged.operations.size()));
}

void BangOnProIntegration::promoteDepartmentDraft(const QString &jobDir)
{
    const QString target =
        QFileDialog::getSaveFileName(m_mainWindow, QStringLiteral("Promote draft to project file"), QString(), QStringLiteral("Kdenlive project (*.kdenlive)"));
    if (target.isEmpty()) {
        return;
    }
    QStringList args{QStringLiteral("promote"), jobDir, target};
    if (QFileInfo::exists(target)) {
        const auto answer = QMessageBox::question(m_mainWindow, QStringLiteral("Replace existing edit?"),
                                                  QStringLiteral("%1 already exists. Replace it with the AI draft? A timestamped backup is kept.").arg(target));
        if (answer != QMessageBox::Yes) {
            return;
        }
        args << QStringLiteral("--approve");
    }
    runDepartmentCommand(args, jobDir, QStringLiteral("Promoting draft to %1").arg(target));
}

void BangOnProIntegration::setupTranscriptDock()
{
    m_transcriptWidget = new TranscriptEditorWidget(m_mainWindow);
    m_transcriptDock = new QDockWidget(QStringLiteral("Transcript"), m_mainWindow);
    m_transcriptDock->setObjectName(QString::fromLatin1(kTranscriptDockName));
    m_transcriptDock->setWidget(makeScrollableDockContent(m_transcriptWidget, m_transcriptDock));
    m_mainWindow->addDockWidget(Qt::RightDockWidgetArea, m_transcriptDock);
    if (m_aiDock) {
        m_mainWindow->tabifyDockWidget(m_aiDock, m_transcriptDock);
        m_aiDock->raise();
    }
    connect(m_transcriptWidget, &TranscriptEditorWidget::seekToFrameRequested, this, &BangOnProIntegration::seekTimelineToFrame);
    connect(m_transcriptWidget, &TranscriptEditorWidget::applyTranscriptEditsRequested, this, &BangOnProIntegration::applyTranscriptEdits);
}

void BangOnProIntegration::setupPresetBrowser()
{
    m_presetWidget = new PremierePresetBrowser(m_mainWindow);
    m_presetDock = new QDockWidget(QStringLiteral("Premiere Presets"), m_mainWindow);
    m_presetDock->setObjectName(QString::fromLatin1(kPremiereBrowserDockName));
    m_presetDock->setWidget(makeScrollableDockContent(m_presetWidget, m_presetDock));
    m_mainWindow->addDockWidget(Qt::LeftDockWidgetArea, m_presetDock);
    connect(m_presetWidget, &PremierePresetBrowser::importArchiveRequested, this, &BangOnProIntegration::importPresetArchive);
    connect(m_presetWidget, &PremierePresetBrowser::applyPresetRequested, this, &BangOnProIntegration::applyPreset);
    connect(m_presetWidget, &PremierePresetBrowser::customStackApplied, this, &BangOnProIntegration::applyCustomStack);
    loadPresetReportIfPresent();
}

void BangOnProIntegration::setupRotoStudioDock()
{
    m_rotoWidget = new RotoStudioDock(m_mainWindow);
    m_rotoDock = new QDockWidget(QStringLiteral("Roto and 2D"), m_mainWindow);
    m_rotoDock->setObjectName(QString::fromLatin1(kRotoStudioDockName));
    m_rotoDock->setWidget(makeScrollableDockContent(m_rotoWidget, m_rotoDock));
    m_mainWindow->addDockWidget(Qt::RightDockWidgetArea, m_rotoDock);
    if (m_aiDock) {
        m_mainWindow->tabifyDockWidget(m_aiDock, m_rotoDock);
        m_aiDock->raise();
    }
    connect(m_rotoWidget, &RotoStudioDock::commitMaskToTimelineRequested, this, &BangOnProIntegration::commitRotoMaskToTimeline);
    // Masks are drawn on the project monitor's current frame, and AI Segment segments that frame.
    connect(m_rotoDock, &QDockWidget::visibilityChanged, this, [this](bool visible) {
        if (visible) {
            refreshRotoFrame();
        }
    });
    connect(m_rotoWidget, &RotoStudioDock::aiSegmentRequested, this, [this](const QJsonObject &) { runRotoSegmentation(); });
    // Mask tracking (optical-flow propagation) is not connected to the panel in this release: say so instead of a silent button.
    for (const QString &name : {QStringLiteral("rotoTrackBackBtn"), QStringLiteral("rotoTrackFwdBtn")}) {
        if (auto *btn = m_rotoWidget->findChild<QPushButton *>(name)) {
            btn->setEnabled(false);
            btn->setToolTip(
                QStringLiteral("Mask tracking from the panel is not available in this release. Draw or AI-segment the mask on the frame you need."));
        }
    }
    connect(m_rotoWidget, &RotoStudioDock::send2DCompositeToMediaBinRequested, this, &BangOnProIntegration::renderAndImport2DComposite);
    connect(m_rotoWidget, &RotoStudioDock::sendStoryboardToTimelineRequested, this, &BangOnProIntegration::exportStoryboardToTimeline);
}

void BangOnProIntegration::setupWorkspaceBar()
{
    if (!m_mainWindow) {
        return;
    }
    m_workspaceBar = new WorkspaceSwitcherBar(m_mainWindow);
    auto *barToolBar = new QToolBar(QStringLiteral("Bang On Pro Workspaces"), m_mainWindow);
    barToolBar->setObjectName(QStringLiteral("bangonpro_workspace_toolbar"));
    barToolBar->setMovable(false);
    barToolBar->setFloatable(false);
    barToolBar->addWidget(m_workspaceBar);
    m_mainWindow->addToolBar(Qt::TopToolBarArea, barToolBar);

    connect(m_workspaceBar, &WorkspaceSwitcherBar::workspaceSelected, this, &BangOnProIntegration::applyStudioWorkspace);
    connect(m_workspaceBar, &WorkspaceSwitcherBar::aiPanelToggled, this, [this](bool visible) {
        if (m_aiDock) {
            m_aiDock->setVisible(visible);
            if (visible) {
                m_aiDock->raise();
            }
        }
    });
    if (m_aiDock) {
        connect(m_aiDock, &QDockWidget::visibilityChanged, m_workspaceBar,
                [this](bool visible) { m_workspaceBar->setAiPanelVisible(visible, false); });
    }
    m_workspaceBar->attachToMainWindow(m_mainWindow, collectWorkspaceDocks());

    // Hide Kdenlive's legacy light-themed menuBar corner layout bar so Bang On Pro's unified 9-workspace bar is authoritative
    if (m_mainWindow->menuBar()) {
        if (QWidget *legacyCorner = m_mainWindow->menuBar()->cornerWidget(Qt::TopRightCorner)) {
            legacyCorner->hide();
        }
    }
    // Re-apply unified dark studio palette after Kdenlive ThemeManager initializes
    applyBangOnProTheme(qApp);
}

void BangOnProIntegration::setupShortcuts()
{
    m_shortcutManager = std::make_unique<ShortcutPresetManager>();
    if (auto *mw = qobject_cast<MainWindow *>(m_mainWindow)) {
        m_shortcutManager->applyPreset(mw->actionCollection(), m_shortcutManager->activePresetId());
    }
}

void BangOnProIntegration::setupMenu()
{
    if (!m_mainWindow || !m_mainWindow->menuBar()) {
        return;
    }
    QMenu *menu = m_mainWindow->menuBar()->addMenu(QStringLiteral("&Bang On Pro"));

    // Studio Workspaces submenu (all 9 workspaces)
    QMenu *wsMenu = menu->addMenu(QStringLiteral("Workspaces"));
    auto *kdenliveWin = qobject_cast<MainWindow *>(m_mainWindow);
    for (const WorkspaceMode mode : allWorkspaceModes()) {
        QAction *wsAct = wsMenu->addAction(workspaceModeDisplayName(mode));
        const QString actName = QStringLiteral("bangonpro_workspace_") + workspaceModeToId(mode);
        wsAct->setObjectName(actName);
        if (kdenliveWin && kdenliveWin->actionCollection()) {
            kdenliveWin->actionCollection()->addAction(actName, wsAct);
        }
        connect(wsAct, &QAction::triggered, this, [this, mode]() {
            applyStudioWorkspace(mode);
            if (m_workspaceBar) {
                m_workspaceBar->setActiveWorkspace(mode, false);
            }
        });
    }
    wsMenu->addSeparator();
    QAction *premiereLayout = wsMenu->addAction(QStringLiteral("Apply Premiere-Style Edit Layout"));
    connect(premiereLayout, &QAction::triggered, this, [this]() { applyStudioWorkspace(WorkspaceMode::Edit); });

    menu->addSeparator();

    if (m_aiDock) {
        QAction *toggleAi = m_aiDock->toggleViewAction();
        toggleAi->setText(QStringLiteral("AI Assistant Panel"));
        toggleAi->setObjectName(QStringLiteral("bangonpro_toggle_ai"));
        if (kdenliveWin && kdenliveWin->actionCollection()) {
            kdenliveWin->actionCollection()->addAction(QStringLiteral("bangonpro_toggle_ai"), toggleAi);
        }
        menu->addAction(toggleAi);
    }
    if (m_transcriptDock) {
        QAction *toggleTranscript = m_transcriptDock->toggleViewAction();
        toggleTranscript->setText(QStringLiteral("Transcript"));
        menu->addAction(toggleTranscript);
    }
    if (m_rotoDock) {
        QAction *toggleRoto = m_rotoDock->toggleViewAction();
        toggleRoto->setText(QStringLiteral("Roto and 2D"));
        menu->addAction(toggleRoto);
    }
    if (m_presetDock) {
        QAction *togglePresets = m_presetDock->toggleViewAction();
        togglePresets->setText(QStringLiteral("Premiere Presets Browser"));
        menu->addAction(togglePresets);
    }

    menu->addSeparator();

    QAction *shortcutsAct = menu->addAction(QStringLiteral("Keyboard Shortcut Presets (Premiere / Resolve / Kdenlive)…"));
    shortcutsAct->setObjectName(QStringLiteral("bangonpro_shortcut_presets"));
    if (kdenliveWin && kdenliveWin->actionCollection()) {
        kdenliveWin->actionCollection()->addAction(QStringLiteral("bangonpro_shortcut_presets"), shortcutsAct);
    }
    connect(shortcutsAct, &QAction::triggered, this, &BangOnProIntegration::openShortcutPresetsDialog);

    QAction *profileEditorAct = menu->addAction(QStringLiteral("Custom Show Profile Editor…"));
    connect(profileEditorAct, &QAction::triggered, this, &BangOnProIntegration::openShowProfileEditor);

    QAction *providerConsentAct = menu->addAction(QStringLiteral("AI Providers & Subscriptions…"));
    connect(providerConsentAct, &QAction::triggered, this, &BangOnProIntegration::openExternalProviderSettings);

    QAction *modelManagerAct = menu->addAction(QStringLiteral("Manage Local AI Models (Whisper / SAM)…"));
    connect(modelManagerAct, &QAction::triggered, this, &BangOnProIntegration::openModelManager);

    // V1.2.2: the first-launch check is always one click away, and so is a sanitised diagnostics zip.
    QAction *systemCheckAct = menu->addAction(QStringLiteral("System check…"));
    systemCheckAct->setObjectName(QStringLiteral("bangonpro_system_check"));
    connect(systemCheckAct, &QAction::triggered, this, [this]() { openSystemCheck(false); });
    QAction *diagnosticsAct = menu->addAction(QStringLiteral("Export diagnostics…"));
    diagnosticsAct->setObjectName(QStringLiteral("bangonpro_export_diagnostics"));
    connect(diagnosticsAct, &QAction::triggered, this, &BangOnProIntegration::exportDiagnostics);

    QAction *clearCacheAct = menu->addAction(QStringLiteral("Clear Proxy & Render Caches"));
    connect(clearCacheAct, &QAction::triggered, this, &BangOnProIntegration::clearTemporaryCaches);

    menu->addSeparator();

    QAction *importPresets = menu->addAction(QStringLiteral("Import Premiere Preset Pack (.zip / .prfpset)..."));
    connect(importPresets, &QAction::triggered, this, [this]() {
        const QString file = QFileDialog::getOpenFileName(m_mainWindow, QStringLiteral("Import Premiere Pro Preset Pack"), QString(),
                                                          QStringLiteral("Premiere Presets (*.zip *.prfpset *.xml);;All Files (*)"));
        if (!file.isEmpty()) {
            importPresetArchive(file);
        }
    });

    m_openGeneratedProjectAction = menu->addAction(QStringLiteral("Open Last AI First-Cut Project (.kdenlive)"));
    m_openGeneratedProjectAction->setEnabled(false);
    connect(m_openGeneratedProjectAction, &QAction::triggered, this, &BangOnProIntegration::openLastGeneratedProject);
}

void BangOnProIntegration::applyStudioWorkspace(WorkspaceMode mode)
{
    applyWorkspace(m_mainWindow, mode, collectWorkspaceDocks());
    if (m_aiWidget) {
        m_aiWidget->appendActivityLog(QStringLiteral("workspace"), QStringLiteral("Switched to %1 workspace").arg(workspaceModeDisplayName(mode)));
    }
}

void BangOnProIntegration::openShortcutPresetsDialog()
{
    auto *mw = qobject_cast<MainWindow *>(m_mainWindow);
    QObject *target = mw ? static_cast<QObject *>(mw->actionCollection()) : static_cast<QObject *>(m_mainWindow);
    ShortcutPresetDialog dlg(target, m_mainWindow);
    connect(&dlg, &ShortcutPresetDialog::presetApplied, this, [this](const QString &presetId, const QMap<QString, QKeySequence> &shortcuts) {
        if (m_aiWidget) {
            m_aiWidget->appendActivityLog(QStringLiteral("shortcuts"),
                                          QStringLiteral("Applied '%1' keyboard shortcut preset (%2 actions)").arg(presetId).arg(shortcuts.size()));
        }
    });
    dlg.exec();
}

void BangOnProIntegration::openShowProfileEditor()
{
    ShowProfileEditorDialog dlg(m_mainWindow);
    connect(&dlg, &ShowProfileEditorDialog::profileSaved, this, [this](const QString &filePath, const QJsonObject &profileJson) {
        if (m_aiWidget) {
            m_aiWidget->applyCustomProfileJson(profileJson, filePath);
        }
        // V1.2.2: a saved custom profile is also offered to the production department (by profile id).
        const QString id = profileJson.value(QStringLiteral("profile_id")).toString();
        if (!id.isEmpty() && QDir().mkpath(customProfilesDir())) {
            const QString target = QDir(customProfilesDir()).filePath(sanitizeFileSlug(id) + QStringLiteral(".json"));
            QFile out(target);
            if (out.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
                out.write(QJsonDocument(profileJson).toJson(QJsonDocument::Indented));
                out.close();
                m_agentCentre->addCustomProfile(id, profileJson.value(QStringLiteral("display_name")).toString(id));
            }
        }
    });
    dlg.exec();
}

void BangOnProIntegration::openExternalProviderSettings()
{
    ExternalProviderConsentDialog dlg(m_mainWindow, [this](const QStringList &args) { return startPython(args); });
    connect(&dlg, &ExternalProviderConsentDialog::settingsSaved, this, [this](bool connected) {
        if (m_aiWidget) {
            m_aiWidget->appendActivityLog(QStringLiteral("privacy"),
                                          connected ? tr("Connected Subscriptions enabled") : tr("Local Only enabled"));
        }
    });
    dlg.exec();
}

void BangOnProIntegration::openModelManager()
{
    ModelManagerDialog dlg(pythonExecutable(), locatePythonPackageRoot(), m_mainWindow);
    connect(&dlg, &ModelManagerDialog::modelStatusUpdated, this, [this](const QJsonObject &report) {
        const int installed = report.value(QStringLiteral("installed_count")).toInt(0);
        const int ready = report.value(QStringLiteral("ready_count")).toInt(0);
        const double mb = report.value(QStringLiteral("total_mb")).toDouble(0.0);
        if (m_aiWidget) {
            m_aiWidget->setStorageSummaryText(
                QStringLiteral("Local Models: %1 installed (%2 neural-ready) • %3 MB").arg(installed).arg(ready).arg(mb, 0, 'f', 1));
        }
        // Update Roto Studio honest backend badge from actual SAM model status.
        if (m_rotoWidget) {
            bool samNeuralReady = false;
            QString samModelName;
            for (const QJsonValue &v : report.value(QStringLiteral("models")).toArray()) {
                const QJsonObject m = v.toObject();
                if (m.value(QStringLiteral("family")).toString() == QStringLiteral("segmentation_sam") &&
                    (m.value(QStringLiteral("status")).toString() == QStringLiteral("ready") ||
                     m.value(QStringLiteral("status")).toString() == QStringLiteral("loaded"))) {
                    samNeuralReady = true;
                    samModelName = m.value(QStringLiteral("display_name")).toString();
                    break;
                }
            }
            if (samNeuralReady) {
                m_rotoWidget->setSegmentationBackendStatus(samModelName, true, QStringLiteral("local neural weights verified"));
            } else {
                m_rotoWidget->setSegmentationBackendStatus(QStringLiteral("OpenCV GrabCut / Spline"), false,
                                                           QStringLiteral("install MobileSAM ONNX or SAM 2.1 in Model Manager for neural segmentation"));
            }
        }
    });
    connect(&dlg, &ModelManagerDialog::modelOperationCompleted, this, [this](const QString &action, const QString &modelId, bool ok, const QString &msg) {
        if (m_aiWidget) {
            m_aiWidget->appendActivityLog(QStringLiteral("models"), QStringLiteral("[%1 %2] %3").arg(action, modelId, msg),
                                          ok ? QStringLiteral("INFO") : QStringLiteral("ERROR"));
        }
    });
    dlg.refreshStatus();
    dlg.exec();
}

void BangOnProIntegration::clearTemporaryCaches()
{
    const QString cacheDir = dataDir() + QStringLiteral("/cache");
    QDir dir(cacheDir);
    if (dir.exists()) {
        dir.removeRecursively();
        QDir().mkpath(cacheDir);
    }
    if (m_aiWidget) {
        m_aiWidget->setStorageSummaryText(QStringLiteral("Local AI • Cache cleared"));
        m_aiWidget->appendActivityLog(QStringLiteral("storage"), QStringLiteral("Cleared Bang On Pro proxy & temporary caches (source media untouched)"));
    }
}

void BangOnProIntegration::loadPresetReportIfPresent()
{
    const QString report = dataDir() + QStringLiteral("/presets/compatibility_report.json");
    if (QFileInfo::exists(report)) {
        QString err;
        if (!m_presetWidget->loadReportFile(report, &err)) {
            qWarning("Bang On Pro: failed to load preset report: %s", qPrintable(err));
        }
    }
}

void BangOnProIntegration::loadBuiltinStarterPresetsIfNeeded()
{
    const QString report = dataDir() + QStringLiteral("/presets/compatibility_report.json");
    if (QFileInfo::exists(report)) {
        return;
    }
    const QString outDir = dataDir() + QStringLiteral("/presets");
    QDir().mkpath(outDir);
    QProcess *proc = startPython({QStringLiteral("-m"), QStringLiteral("bangonpro.premiere_compat.cli"), QStringLiteral("builtin-library"),
                                  QStringLiteral("--output"), report});
    if (!proc) {
        return;
    }
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, proc](int code, QProcess::ExitStatus status) {
        proc->deleteLater();
        if (status == QProcess::NormalExit && code == 0) {
            loadPresetReportIfPresent();
        }
    });
}

QProcess *BangOnProIntegration::startPython(const QStringList &args)
{
    const QString root = locatePythonPackageRoot();
    if (root.isEmpty()) {
        return nullptr;
    }
    auto *proc = new QProcess(this);
    // Strict allowlist environment: avoid leaking unrelated host secrets into Python child processes.
    const QProcessEnvironment host = QProcessEnvironment::systemEnvironment();
    QProcessEnvironment clean;
    const QStringList allowedVars = {
        QStringLiteral("PATH"),
        QStringLiteral("HOME"),
        QStringLiteral("USER"),
        QStringLiteral("TMPDIR"),
        QStringLiteral("TEMP"),
        QStringLiteral("TMP"),
        QStringLiteral("LANG"),
        QStringLiteral("LC_ALL"),
        QStringLiteral("LC_CTYPE"),
        QStringLiteral("DISPLAY"),
        QStringLiteral("WAYLAND_DISPLAY"),
        QStringLiteral("XDG_RUNTIME_DIR"),
        QStringLiteral("XDG_CACHE_HOME"),
        QStringLiteral("XDG_DATA_HOME"),
        QStringLiteral("XDG_CONFIG_HOME"),
        QStringLiteral("SDL_AUDIODRIVER"),
        QStringLiteral("QT_QPA_PLATFORM"),
        QStringLiteral("SSL_CERT_FILE"),
        QStringLiteral("SSL_CERT_DIR"),
        QStringLiteral("REQUESTS_CA_BUNDLE"),
        QStringLiteral("BANGONPRO_MODEL_DIR"),
        QStringLiteral("BANGONPRO_PYTHON"),
        QStringLiteral("BANGONPRO_PYTHONPATH"),
        QStringLiteral("HF_HOME"),
        QStringLiteral("DYLD_LIBRARY_PATH"),
        QStringLiteral("LD_LIBRARY_PATH"),
        QStringLiteral("MLT_REPOSITORY"),
        QStringLiteral("MLT_DATA"),
        QStringLiteral("MLT_PROFILES_PATH"),
        QStringLiteral("FREI0R_PATH"),
        // V1.2.2 storage locations (test and power-user overrides; never secrets)
        QStringLiteral("BANGONPRO_CACHE_DIR"),
        QStringLiteral("BANGONPRO_PREFS_DIR"),
        QStringLiteral("BANGONPRO_PROFILES_DIR"),
        QStringLiteral("BANGONPRO_QUEUE_DIR"),
        // Shared reasoning router configuration and department intelligence (paths only, never secrets)
        QStringLiteral("BANGONPRO_CONFIG_DIR"),
        QStringLiteral("BANGONPRO_INTEL_DIR"),
    };
    for (const QString &key : allowedVars) {
        if (host.contains(key)) {
            clean.insert(key, host.value(key));
        }
    }
    // The folder chosen in System check also holds the analysis cache (e.g. an external SSD).
    if (!clean.contains(QStringLiteral("BANGONPRO_CACHE_DIR")) && storageRootChosen()) {
        clean.insert(QStringLiteral("BANGONPRO_CACHE_DIR"), storageRoot() + QStringLiteral("/cache"));
    }
    // Agents started by the app talk to this instance's native Timeline Command server, wherever Qt put its socket.
    if (m_server && !m_server->socketPath().isEmpty()) {
        clean.insert(QStringLiteral("BANGONPRO_TIMELINE_SOCKET"), m_server->socketPath());
    }
    const QString existing = host.value(QStringLiteral("PYTHONPATH"));
    clean.insert(QStringLiteral("PYTHONPATH"), existing.isEmpty() ? root : root + QDir::listSeparator() + existing);
    proc->setProcessEnvironment(clean);
    proc->setWorkingDirectory(root);
    // Argument vector only — never a shell, so user-supplied paths cannot inject commands.
    proc->start(pythonExecutable(), args);
    return proc;
}

void BangOnProIntegration::startProposal(const QString &instruction, const QString &profileId, const QString &customProfilePath)
{
    if (m_aiProcess) {
        m_aiWidget->appendActivityLog(QStringLiteral("integration"), QStringLiteral("An AI task is already running"), QStringLiteral("WARN"));
        return;
    }

    // Capture the exact active project, sequence, and timeline revision BEFORE generating the proposal
    // so stale proposals, manual edits, or project switches during async generation are always detected.
    m_proposalBaseRevision = m_engine->currentRevision();
    m_proposalBaseProjectId = m_engine->activeProjectId().isEmpty() ? QStringLiteral("active_project") : m_engine->activeProjectId();
    m_proposalBaseSequenceId = m_engine->activeSequenceId().isEmpty() ? QStringLiteral("seq_main") : m_engine->activeSequenceId();
    m_lastInstruction = instruction;
    m_lastCustomProfilePath = customProfilePath;

    QString activeKdenliveProjectPath;
    if (Core::self() && Core::self()->currentDoc()) {
        const QString docPath = Core::self()->currentDoc()->url().toLocalFile();
        if (!docPath.isEmpty() && QFileInfo::exists(docPath)) {
            activeKdenliveProjectPath = docPath;
        }
    }

    const QJsonObject liveState = m_engine->stateSnapshotJson();
    const bool hasActiveTimelineContext =
        !activeKdenliveProjectPath.isEmpty() || !liveState.value(QStringLiteral("state")).toObject().value(QStringLiteral("clips")).toArray().isEmpty();

    QStringList inputs;
    if (m_aiWidget && m_aiWidget->useProjectBinMedia()) {
        inputs = collectProjectBinMediaPaths();
    }
    // Only prompt the user to select raw media files if there is neither Project Bin media
    // nor an active project/timeline context to edit in place.
    if (inputs.isEmpty() && !hasActiveTimelineContext) {
        inputs = QFileDialog::getOpenFileNames(m_mainWindow, QStringLiteral("Select camera / source media for the AI first cut"), QString(),
                                               QStringLiteral("Media (*.mp4 *.mov *.mxf *.mkv *.m4v *.wav *.mp3);;All files (*)"));
    }
    if (inputs.isEmpty() && !hasActiveTimelineContext) {
        m_aiWidget->appendActivityLog(QStringLiteral("integration"), QStringLiteral("Proposal cancelled: no media or active timeline available"));
        return;
    }

    m_lastInputs = inputs;
    m_lastProfile = customProfilePath.isEmpty() ? profileId : customProfilePath;
    m_aiOutputDir = dataDir() + QStringLiteral("/runs/") + QDateTime::currentDateTimeUtc().toString(QStringLiteral("yyyyMMdd-HHmmss"));
    QDir().mkpath(m_aiOutputDir);

    // Export read-only live timeline state snapshot so the AI planner sees the active project without re-import.
    const QString stateJsonPath = m_aiOutputDir + QStringLiteral("/timeline_state.json");
    {
        QFile sf(stateJsonPath);
        if (sf.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
            sf.write(QJsonDocument(liveState).toJson(QJsonDocument::Indented));
            sf.close();
        }
    }

    QStringList args{QStringLiteral("-m"), QStringLiteral("bangonpro.ai_engine.cli"), instruction};
    if (!inputs.isEmpty()) {
        args << QStringLiteral("--inputs") << inputs;
    }
    if (QFileInfo::exists(stateJsonPath)) {
        args << QStringLiteral("--project-state") << stateJsonPath;
    }
    if (!activeKdenliveProjectPath.isEmpty()) {
        args << QStringLiteral("--kdenlive-project") << activeKdenliveProjectPath;
    }
    args << QStringLiteral("--project-id") << m_proposalBaseProjectId << QStringLiteral("--sequence-id") << m_proposalBaseSequenceId
         << QStringLiteral("--output-dir") << m_aiOutputDir << QStringLiteral("--json");
    if (!m_lastProfile.isEmpty()) {
        args << QStringLiteral("--profile") << m_lastProfile;
    }
    QProcess *proc = startPython(args);
    if (!proc) {
        m_aiWidget->appendActivityLog(QStringLiteral("integration"), QStringLiteral("Cannot start AI engine: bangonpro package not found"),
                                      QStringLiteral("ERROR"));
        return;
    }
    m_aiProcess = proc;
    m_aiWidget->setBusy(true, QStringLiteral("Analysing project context and generating scoped proposal…"), -1);
    m_aiWidget->appendActivityLog(
        QStringLiteral("orchestrator"),
        QStringLiteral("Started planner at rev %1 (%2 input(s)) -> %3").arg(m_proposalBaseRevision).arg(inputs.size()).arg(m_aiOutputDir));
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this,
            [this](int code, QProcess::ExitStatus status) { handleProposalFinished(code, static_cast<int>(status)); });
    connect(proc, &QProcess::errorOccurred, this, [this](QProcess::ProcessError) {
        if (m_aiProcess && m_aiProcess->state() == QProcess::NotRunning) {
            handleProposalFinished(-1, static_cast<int>(QProcess::CrashExit));
        }
    });
}

void BangOnProIntegration::handleProposalFinished(int exitCode, int exitStatus)
{
    if (!m_aiProcess) {
        return;
    }
    QProcess *proc = m_aiProcess;
    m_aiProcess.clear();
    proc->deleteLater();
    m_aiWidget->setBusy(false);

    const QByteArray out = proc->readAllStandardOutput();
    if (exitStatus != static_cast<int>(QProcess::NormalExit) || exitCode != 0) {
        m_aiWidget->appendActivityLog(QStringLiteral("orchestrator"),
                                      QStringLiteral("Pipeline failed (status=%1, exit=%2): %3")
                                          .arg(exitStatus)
                                          .arg(exitCode)
                                          .arg(QString::fromUtf8(proc->readAllStandardError().right(800))),
                                      QStringLiteral("ERROR"));
        return;
    }
    const QJsonObject summary = QJsonDocument::fromJson(out).object();
    if (summary.value(QStringLiteral("needs_clarification")).toBool(false)) {
        const QString question = summary.value(QStringLiteral("clarification_question"))
                                     .toString(QStringLiteral("Please clarify which editorial action you want Bang On Pro to perform."));
        m_aiWidget->showClarificationPrompt(question);
        return;
    }

    const QJsonObject artifacts = summary.value(QStringLiteral("artifacts")).toObject();
    const QString cmdsPath = artifacts.value(QStringLiteral("timeline_commands_json")).toString();
    m_lastKdenliveProjectPath = artifacts.value(QStringLiteral("kdenlive_project")).toString();
    m_lastTranscriptPath = artifacts.value(QStringLiteral("transcript_json")).toString();

    if (m_openGeneratedProjectAction) {
        m_openGeneratedProjectAction->setEnabled(!m_lastKdenliveProjectPath.isEmpty() && QFileInfo::exists(m_lastKdenliveProjectPath));
    }
    if (m_transcriptWidget && !m_lastTranscriptPath.isEmpty() && QFileInfo::exists(m_lastTranscriptPath)) {
        QString terr;
        if (m_transcriptWidget->loadFromFile(m_lastTranscriptPath, &terr)) {
            m_aiWidget->appendActivityLog(QStringLiteral("transcript"),
                                          QStringLiteral("Loaded editable transcript (%1 segments)").arg(m_transcriptWidget->segmentCount()));
        }
    }

    QString err;
    const QJsonObject cmds = readJsonFile(cmdsPath, &err);
    if (cmds.isEmpty()) {
        m_aiWidget->appendActivityLog(QStringLiteral("orchestrator"), err, QStringLiteral("ERROR"));
        return;
    }

    // Merge the agents' versioned batches into one reviewable proposal preserving the original
    // project, sequence, and timeline revision captured before proposal generation started.
    TimelineCommandBatch merged;
    merged.requestId = QStringLiteral("ai-") + QUuid::createUuid().toString(QUuid::WithoutBraces);
    merged.projectId = m_proposalBaseProjectId.isEmpty() ? cmds.value(QStringLiteral("project_id")).toString() : m_proposalBaseProjectId;
    merged.sequenceId = m_proposalBaseSequenceId.isEmpty() ? cmds.value(QStringLiteral("sequence_id")).toString() : m_proposalBaseSequenceId;
    merged.agentId = QStringLiteral("orchestrator");
    merged.agentRole = QStringLiteral("ai_first_cut");
    merged.requiresApproval = true;
    merged.expectedRevision = m_proposalBaseRevision;
    merged.transactionLabel =
        QStringLiteral("AI Edit (%1 / %2)")
            .arg(summary.value(QStringLiteral("profile_id")).toString(), summary.value(QStringLiteral("intent_scope")).toString(QStringLiteral("scoped")));
    const QJsonArray batches = cmds.value(QStringLiteral("batches")).toArray();
    for (const QJsonValue &bv : batches) {
        TimelineCommandBatch b;
        QString berr;
        if (!TimelineCommandEngine::batchFromJson(bv.toObject(), &b, &berr)) {
            m_aiWidget->appendActivityLog(QStringLiteral("orchestrator"), QStringLiteral("Skipped invalid batch: %1").arg(berr), QStringLiteral("WARN"));
            continue;
        }
        merged.operations += b.operations;
    }

    // Stage the proposal inside TimelineCommandEngine (with requiresApproval=true) so the engine
    // captures the structural state fingerprint and enforces stale-proposal / drift detection.
    const TimelineCommandResponse stageResp = m_engine->executeBatch(merged);
    if (stageResp.status == CommandStatus::RevisionConflict) {
        m_aiWidget->displayProposal(merged);
        m_aiWidget->showStaleProposalWarning(stageResp.errorMessage, false);
        m_aiWidget->appendActivityLog(QStringLiteral("orchestrator"), stageResp.errorMessage, QStringLiteral("WARN"));
        return;
    }
    if (stageResp.status != CommandStatus::PendingApproval) {
        m_aiWidget->appendActivityLog(QStringLiteral("orchestrator"), QStringLiteral("Failed to stage proposal: %1").arg(stageResp.errorMessage),
                                      QStringLiteral("ERROR"));
        return;
    }

    m_activeProposalRequestId = merged.requestId;
    for (auto it = artifacts.constBegin(); it != artifacts.constEnd(); ++it) {
        m_aiWidget->appendActivityLog(QStringLiteral("artifact"), QStringLiteral("%1: %2").arg(it.key(), it.value().toString()));
    }
    m_aiWidget->appendActivityLog(QStringLiteral("orchestrator"),
                                  QStringLiteral("Proposal staged at rev %1: %2 ops, %3 shots, ASR=%4 (neural=%5), QC=%6")
                                      .arg(m_proposalBaseRevision)
                                      .arg(merged.operations.size())
                                      .arg(summary.value(QStringLiteral("shot_count")).toInt())
                                      .arg(summary.value(QStringLiteral("asr_backend_used")).toString(QStringLiteral("none")),
                                           summary.value(QStringLiteral("is_neural_inference")).toBool(false) ? QStringLiteral("yes") : QStringLiteral("no"),
                                           summary.value(QStringLiteral("qc_passed")).toBool() ? QStringLiteral("yes") : QStringLiteral("no")));
    m_aiWidget->displayProposal(merged);
}

void BangOnProIntegration::seekTimelineToFrame(int frame)
{
    if (Core::self() && Core::self()->monitorManager()) {
        Core::self()->monitorManager()->activateMonitor(Kdenlive::ProjectMonitor);
        Core::self()->seekMonitor(Kdenlive::ProjectMonitor, qMax(0, frame));
    }
}

void BangOnProIntegration::applyTranscriptEdits(const QJsonObject &editedTranscriptJson)
{
    const QString editedPath = dataDir() + QStringLiteral("/edited_transcript.json");
    QFile outF(editedPath);
    if (!outF.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        return;
    }
    outF.write(QJsonDocument(editedTranscriptJson).toJson(QJsonDocument::Indented));
    outF.close();

    const qint64 capturedRev = m_engine->currentRevision();
    QStringList args{QStringLiteral("-m"), QStringLiteral("bangonpro.ai_engine.cli"), QStringLiteral("--apply-transcript-edits"), editedPath};
    if (!m_lastTranscriptPath.isEmpty() && QFileInfo::exists(m_lastTranscriptPath)) {
        args << QStringLiteral("--original-transcript") << m_lastTranscriptPath;
    }
    QProcess *proc = startPython(args);
    if (!proc) {
        return;
    }
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, proc, capturedRev](int code, QProcess::ExitStatus status) {
        proc->deleteLater();
        if (status != QProcess::NormalExit || code != 0) {
            return;
        }
        const QJsonObject batchObj = QJsonDocument::fromJson(proc->readAllStandardOutput()).object();
        TimelineCommandBatch batch;
        QString berr;
        if (TimelineCommandEngine::batchFromJson(batchObj, &batch, &berr) && !batch.operations.isEmpty()) {
            batch.expectedRevision = capturedRev;
            batch.requiresApproval = true;
            const TimelineCommandResponse stageResp = m_engine->executeBatch(batch);
            if (stageResp.status == CommandStatus::PendingApproval) {
                m_activeProposalRequestId = batch.requestId;
                m_aiWidget->displayProposal(batch);
            } else {
                m_aiWidget->displayProposal(batch);
                m_aiWidget->showStaleProposalWarning(stageResp.errorMessage, false);
            }
            if (m_aiDock) {
                m_aiDock->show();
                m_aiDock->raise();
            }
        }
    });
}

void BangOnProIntegration::commitRotoMaskToTimeline(const QJsonObject &rotoSessionJson)
{
    const QJsonArray masks = rotoSessionJson.value(QStringLiteral("masks")).toArray();
    if (masks.isEmpty()) {
        return;
    }
    const QJsonObject primary = masks.first().toObject();
    TimelineCommandBatch batch;
    batch.requestId = QStringLiteral("roto-") + QUuid::createUuid().toString(QUuid::WithoutBraces);
    batch.projectId = m_engine->activeProjectId().isEmpty() ? QStringLiteral("active_project") : m_engine->activeProjectId();
    batch.sequenceId = m_engine->activeSequenceId().isEmpty() ? QStringLiteral("seq_main") : m_engine->activeSequenceId();
    batch.agentId = QStringLiteral("roto_studio");
    batch.agentRole = QStringLiteral("rotoscoping");
    batch.transactionLabel = QStringLiteral("Apply Rotoscope Mask (%1)").arg(primary.value(QStringLiteral("name")).toString(QStringLiteral("Mask")));
    batch.expectedRevision = m_engine->currentRevision();
    batch.requiresApproval = false;

    TimelineOperation op;
    op.opId = QStringLiteral("op_roto_mask_1");
    op.type = QStringLiteral("apply_mask");
    op.trackId = QStringLiteral("V1");
    op.params.insert(QStringLiteral("mask_id"), primary.value(QStringLiteral("id")).toString(QStringLiteral("mask_1")));
    op.params.insert(QStringLiteral("mask_type"), primary.value(QStringLiteral("tool")).toString(QStringLiteral("polygon")));
    op.params.insert(QStringLiteral("feather"), primary.value(QStringLiteral("feather_px")).toDouble(0.0));
    op.params.insert(QStringLiteral("expand"), primary.value(QStringLiteral("expand_px")).toDouble(0.0));
    op.params.insert(QStringLiteral("invert"), primary.value(QStringLiteral("inverted")).toBool(false));
    op.params.insert(QStringLiteral("operation"), primary.value(QStringLiteral("combine_mode")).toString(QStringLiteral("add")));
    op.params.insert(QStringLiteral("splines_json"), QString::fromUtf8(QJsonDocument(primary.value(QStringLiteral("vertices")).toArray()).toJson(QJsonDocument::Compact)));
    // An AI (SAM) mask has points, not vertices: its segmented matte is applied as a native shape (luma) mask.
    if (primary.value(QStringLiteral("tool")).toString() == QStringLiteral("ai_sam")) {
        if (m_rotoMatteMaskId != primary.value(QStringLiteral("id")).toString() || !QFileInfo::exists(m_rotoMattePath)) {
            if (m_aiWidget) {
                m_aiWidget->appendActivityLog(QStringLiteral("roto-studio"), QStringLiteral("Run AI Segment on this mask before committing it"),
                                              QStringLiteral("WARN"));
            }
            return;
        }
        op.params.insert(QStringLiteral("matte_path"), m_rotoMattePath);
    }
    op.confidence = 0.96;
    op.rationale = QStringLiteral("Commit native rotoscope mask from Bang On Pro Roto Studio");
    batch.operations.append(op);

    const TimelineCommandResponse resp = m_engine->executeBatch(batch);
    if (m_aiWidget) {
        m_aiWidget->setCanUndo(m_engine->canUndo());
        m_aiWidget->appendActivityLog(QStringLiteral("roto-studio"),
                                      QStringLiteral("Commit mask -> %1 (rev %2) %3")
                                          .arg(commandStatusToString(resp.status))
                                          .arg(resp.currentRevision)
                                          .arg(resp.errorMessage));
    }
}

bool BangOnProIntegration::refreshRotoFrame()
{
    if (!m_rotoWidget || !m_rotoWidget->canvasWidget() || !pCore) {
        return false;
    }
    Monitor *monitor = pCore->getMonitor(Kdenlive::ProjectMonitor);
    MonitorProxy *proxy = monitor ? monitor->getControllerProxy() : nullptr;
    const QImage frame = proxy ? proxy->extractFrame() : QImage();
    if (frame.isNull()) {
        return false;
    }
    m_rotoWidget->canvasWidget()->setFrameImage(frame);
    return true;
}

void BangOnProIntegration::runRotoSegmentation()
{
    if (!m_rotoWidget) {
        return;
    }
    if (m_rotoProcess) {
        m_aiWidget->appendActivityLog(QStringLiteral("roto-studio"), QStringLiteral("AI segmentation is already running"), QStringLiteral("WARN"));
        return;
    }
    if (!refreshRotoFrame()) {
        m_aiWidget->appendActivityLog(QStringLiteral("roto-studio"), QStringLiteral("AI Segment needs a clip in the project monitor"), QStringLiteral("WARN"));
        return;
    }
    const QJsonObject session = m_rotoWidget->exportRotoSessionJson(); // after the refresh: sized to the real frame
    QJsonObject aiMask;
    for (const QJsonValue &v : session.value(QStringLiteral("masks")).toArray()) {
        const QJsonObject m = v.toObject();
        if (m.value(QStringLiteral("tool")).toString() == QStringLiteral("ai_sam") &&
            (!m.value(QStringLiteral("positive_points")).toArray().isEmpty() || m.contains(QStringLiteral("bbox")))) {
            aiMask = m;
            break;
        }
    }
    if (aiMask.isEmpty()) {
        m_aiWidget->appendActivityLog(QStringLiteral("roto-studio"), QStringLiteral("Choose the AI (SAM) tool and click the subject before AI Segment"),
                                      QStringLiteral("WARN"));
        return;
    }
    const QString dir = dataDir() + QStringLiteral("/roto/") + QDateTime::currentDateTimeUtc().toString(QStringLiteral("yyyyMMdd-HHmmss-zzz"));
    QDir().mkpath(dir);
    const QString framePath = dir + QStringLiteral("/frame.png");
    const QString sessionPath = dir + QStringLiteral("/session.json");
    QFile sf(sessionPath);
    if (!m_rotoWidget->canvasWidget()->frameImage().save(framePath) || !sf.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        m_aiWidget->appendActivityLog(QStringLiteral("roto-studio"), QStringLiteral("Cannot write the segmentation inputs to %1").arg(dir),
                                      QStringLiteral("ERROR"));
        return;
    }
    sf.write(QJsonDocument(session).toJson(QJsonDocument::Compact));
    sf.close();
    QProcess *proc = startPython({QStringLiteral("-m"), QStringLiteral("bangonpro.roto_studio.cli"), QStringLiteral("segment"), QStringLiteral("--image"),
                                  framePath, QStringLiteral("--session"), sessionPath, QStringLiteral("--output-dir"), dir});
    if (!proc) {
        m_aiWidget->appendActivityLog(QStringLiteral("roto-studio"), QStringLiteral("Cannot start segmentation: bangonpro package not found"),
                                      QStringLiteral("ERROR"));
        return;
    }
    m_rotoProcess = proc;
    const QString maskId = aiMask.value(QStringLiteral("id")).toString();
    m_aiWidget->appendActivityLog(QStringLiteral("roto-studio"), QStringLiteral("AI segmentation started (%1)").arg(maskId));
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, proc, maskId](int code, QProcess::ExitStatus status) {
        m_rotoProcess.clear();
        proc->deleteLater();
        const QJsonObject out = QJsonDocument::fromJson(proc->readAllStandardOutput()).object();
        const QJsonArray frames = out.value(QStringLiteral("matte_frames")).toArray();
        if (status != QProcess::NormalExit || code != 0 || frames.isEmpty()) {
            m_aiWidget->appendActivityLog(
                QStringLiteral("roto-studio"),
                QStringLiteral("AI segmentation failed (exit=%1): %2").arg(code).arg(QString::fromUtf8(proc->readAllStandardError().right(600))),
                QStringLiteral("ERROR"));
            return;
        }
        m_rotoMattePath = frames.first().toString();
        m_rotoMatteMaskId = maskId;
        const QImage matte(m_rotoMattePath);
        if (!matte.isNull() && m_rotoWidget && m_rotoWidget->canvasWidget()) {
            m_rotoWidget->canvasWidget()->setAlphaMaskImage(matte);
        }
        const QJsonObject rep = out.value(QStringLiteral("segmentation_reports")).toArray().first().toObject();
        if (m_rotoWidget && !rep.isEmpty()) {
            const QString reason = rep.value(QStringLiteral("fallback_reason")).toString();
            m_rotoWidget->setSegmentationBackendStatus(rep.value(QStringLiteral("backend_id")).toString(),
                                                       rep.value(QStringLiteral("is_neural_inference")).toBool(),
                                                       reason.isEmpty() ? QStringLiteral("segmented the current frame") : reason);
        }
        m_aiWidget->appendActivityLog(QStringLiteral("roto-studio"),
                                      QStringLiteral("AI segmentation ready: %1 (commit applies it as a native matte mask)").arg(m_rotoMattePath));
    });
}

void BangOnProIntegration::renderAndImport2DComposite(const QJsonObject &canvasJson, bool placeOnTimeline)
{
    const QString outDir = dataDir() + QStringLiteral("/artcraft_plates");
    QDir().mkpath(outDir);
    const QString stamp = QDateTime::currentDateTimeUtc().toString(QStringLiteral("yyyyMMdd-HHmmss"));
    const QString canvasPath = outDir + QStringLiteral("/canvas_") + stamp + QStringLiteral(".json");
    const QString pngPath = outDir + QStringLiteral("/plate_") + stamp + QStringLiteral(".png");

    QFile f(canvasPath);
    if (!f.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        return;
    }
    f.write(QJsonDocument(canvasJson).toJson(QJsonDocument::Indented));
    f.close();

    QProcess *proc = startPython({QStringLiteral("-m"), QStringLiteral("bangonpro.roto_studio.cli"), QStringLiteral("compose-2d"),
                                  QStringLiteral("--canvas-json"), canvasPath, QStringLiteral("--output-png"), pngPath});
    if (!proc) {
        return;
    }
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this,
            [this, proc, pngPath, placeOnTimeline](int code, QProcess::ExitStatus status) {
                proc->deleteLater();
                if (status != QProcess::NormalExit || code != 0 || !QFileInfo::exists(pngPath)) {
                    return;
                }
                TimelineCommandBatch batch;
                batch.requestId = QStringLiteral("plate-") + QUuid::createUuid().toString(QUuid::WithoutBraces);
                batch.projectId = m_engine->activeProjectId().isEmpty() ? QStringLiteral("active_project") : m_engine->activeProjectId();
                batch.sequenceId = m_engine->activeSequenceId().isEmpty() ? QStringLiteral("seq_main") : m_engine->activeSequenceId();
                batch.agentId = QStringLiteral("artcraft_2d");
                batch.agentRole = QStringLiteral("2d_composition");
                batch.transactionLabel = QStringLiteral("Import 2D Composite Plate");
                batch.expectedRevision = m_engine->currentRevision();
                batch.requiresApproval = false;

                TimelineOperation imp;
                imp.opId = QStringLiteral("op_import_plate");
                imp.type = QStringLiteral("import_media");
                imp.mediaRef = QStringLiteral("plate_media");
                imp.params.insert(QStringLiteral("path"), pngPath);
                imp.confidence = 1.0;
                imp.rationale = QStringLiteral("Import ArtCraft-adapted 2D composite plate into Media Bin");
                batch.operations.append(imp);

                if (placeOnTimeline) {
                    TimelineOperation place;
                    place.opId = QStringLiteral("op_place_plate");
                    place.type = QStringLiteral("place_clip");
                    place.mediaRef = QStringLiteral("plate_media");
                    place.trackId = QStringLiteral("V2");
                    place.frameRange.inFrame = 0;
                    place.frameRange.outFrame = 125;
                    place.frameRange.timelineStart = 0;
                    place.confidence = 1.0;
                    place.rationale = QStringLiteral("Place 2D composite plate on overlay track V2");
                    batch.operations.append(place);
                }

                const TimelineCommandResponse resp = m_engine->executeBatch(batch);
                if (m_aiWidget) {
                    m_aiWidget->setCanUndo(m_engine->canUndo());
                    m_aiWidget->appendActivityLog(QStringLiteral("artcraft-2d"),
                                                  QStringLiteral("Rendered plate %1 -> %2").arg(pngPath, commandStatusToString(resp.status)));
                }
            });
}

void BangOnProIntegration::exportStoryboardToTimeline(const QJsonObject &storyboardJson)
{
    const QString outDir = dataDir() + QStringLiteral("/storyboards/") + QDateTime::currentDateTimeUtc().toString(QStringLiteral("yyyyMMdd-HHmmss"));
    QDir().mkpath(outDir);
    const QString sbPath = outDir + QStringLiteral("/storyboard.json");
    QFile f(sbPath);
    if (!f.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        return;
    }
    f.write(QJsonDocument(storyboardJson).toJson(QJsonDocument::Indented));
    f.close();

    const qint64 capturedRev = m_engine->currentRevision();
    QProcess *proc = startPython({QStringLiteral("-m"), QStringLiteral("bangonpro.roto_studio.cli"), QStringLiteral("storyboard-export"),
                                  QStringLiteral("--storyboard-json"), sbPath, QStringLiteral("--output-dir"), outDir});
    if (!proc) {
        return;
    }
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, proc, capturedRev](int code, QProcess::ExitStatus status) {
        proc->deleteLater();
        if (status != QProcess::NormalExit || code != 0) {
            return;
        }
        const QJsonObject result = QJsonDocument::fromJson(proc->readAllStandardOutput()).object();
        const QJsonObject batchObj = result.value(QStringLiteral("command_batch")).toObject();
        TimelineCommandBatch batch;
        QString err;
        if (TimelineCommandEngine::batchFromJson(batchObj, &batch, &err) && !batch.operations.isEmpty()) {
            batch.expectedRevision = capturedRev;
            batch.requiresApproval = true;
            const TimelineCommandResponse stageResp = m_engine->executeBatch(batch);
            if (m_aiWidget) {
                m_aiWidget->displayProposal(batch);
                if (stageResp.status == CommandStatus::PendingApproval) {
                    m_activeProposalRequestId = batch.requestId;
                } else {
                    m_aiWidget->showStaleProposalWarning(stageResp.errorMessage, false);
                }
                m_aiWidget->appendActivityLog(QStringLiteral("storyboard"), QStringLiteral("Storyboard animatic ready (%1 shots, %2 operations)")
                                                                                .arg(result.value(QStringLiteral("shot_count")).toInt())
                                                                                .arg(batch.operations.size()));
            }
            if (m_aiDock) {
                m_aiDock->show();
                m_aiDock->raise();
            }
        }
    });
}

void BangOnProIntegration::approveBatch(const TimelineCommandBatch &batch)
{
    QStringList opIds;
    opIds.reserve(batch.operations.size());
    for (const TimelineOperation &op : batch.operations) {
        opIds.append(op.opId);
    }
    approveBatchSelection(batch.requestId, opIds, false);
}

void BangOnProIntegration::approveBatchSelection(const QString &requestId, const QStringList &selectedOpIds, bool rebaseToCurrentRevision)
{
    m_aiWidget->setCompareModeActive(false);
    m_aiWidget->setIsolatedPreviewSummary(QJsonObject{});

    QSet<QString> opSet;
    for (const QString &id : selectedOpIds) {
        opSet.insert(id);
    }

    // NEVER overwrite expectedRevision silently. Delegate strictly to TimelineCommandEngine so stale
    // revisions, manual edits, project/sequence switches, and broken partial selections are enforced.
    const TimelineCommandResponse resp = m_engine->approveBatchWithSelection(requestId, opSet, QStringLiteral("user"), rebaseToCurrentRevision);

    if (resp.status == CommandStatus::RevisionConflict) {
        const ProposalFreshnessReport freshness = m_engine->validateProposalFreshness(requestId);
        m_aiWidget->showStaleProposalWarning(freshness.summary.isEmpty() ? resp.errorMessage : freshness.summary, freshness.canRebase);
        m_aiWidget->appendActivityLog(QStringLiteral("timeline-writer"), QStringLiteral("Approval blocked (stale/conflict): %1").arg(resp.errorMessage),
                                      QStringLiteral("WARN"));
        return;
    }

    if (resp.status == CommandStatus::ValidationError) {
        m_aiWidget->appendActivityLog(QStringLiteral("timeline-writer"), QStringLiteral("Approval rejected: %1").arg(resp.errorMessage),
                                      QStringLiteral("ERROR"));
        return;
    }

    m_aiWidget->clearStaleProposalWarning();
    m_aiWidget->applyExecutionResponse(resp);
    m_aiWidget->setCanUndo(m_engine->canUndo());
    if (resp.status == CommandStatus::Ok && m_activeProposalRequestId == requestId) {
        m_activeProposalRequestId.clear();
    }
    m_aiWidget->appendActivityLog(
        QStringLiteral("timeline-writer"),
        QStringLiteral("%1 -> revision %2 %3").arg(commandStatusToString(resp.status)).arg(resp.currentRevision).arg(resp.errorMessage),
        resp.status == CommandStatus::Ok ? QStringLiteral("INFO") : QStringLiteral("ERROR"));
    if (resp.status != CommandStatus::Ok && !m_lastKdenliveProjectPath.isEmpty()) {
        m_aiWidget->appendActivityLog(
            QStringLiteral("timeline-writer"),
            QStringLiteral("Live timeline unchanged. Open the complete editable first cut via Bang On Pro -> Open Last AI First-Cut Project (%1)")
                .arg(m_lastKdenliveProjectPath),
            QStringLiteral("WARN"));
    }
}

void BangOnProIntegration::regenerateProposal(const QString &requestId)
{
    if (!requestId.isEmpty()) {
        m_engine->rejectProposal(requestId, QStringLiteral("Regenerating against updated timeline revision"));
    }
    m_activeProposalRequestId.clear();
    m_aiWidget->clearProposal();
    const QString instruction = m_lastInstruction.isEmpty() ? m_aiWidget->instructionText() : m_lastInstruction;
    const QString profileId = m_aiWidget->selectedProfileId();
    startProposal(instruction, profileId, m_lastCustomProfilePath);
}

void BangOnProIntegration::rejectBatch(const QString &requestId)
{
    m_engine->rejectProposal(requestId, QStringLiteral("Rejected by user"));
    if (m_activeProposalRequestId == requestId) {
        m_activeProposalRequestId.clear();
    }
    m_aiWidget->clearProposal();
    m_aiWidget->appendActivityLog(QStringLiteral("integration"), QStringLiteral("Proposal %1 rejected; timeline untouched").arg(requestId));
}

void BangOnProIntegration::toggleCompareMode(bool previewEnabled)
{
    if (!previewEnabled) {
        m_aiWidget->setIsolatedPreviewSummary(QJsonObject{});
        m_aiWidget->appendActivityLog(QStringLiteral("compare"), QStringLiteral("Showing current live timeline (A); isolated preview hidden"));
        return;
    }

    const QString reqId = m_aiWidget->hasPendingProposal() ? m_aiWidget->currentProposal().requestId : m_activeProposalRequestId;
    if (reqId.isEmpty()) {
        m_aiWidget->appendActivityLog(QStringLiteral("compare"), QStringLiteral("No pending proposal to preview; live timeline unmodified"));
        return;
    }

    // Strictly non-destructive isolated clone simulation: NEVER runs undo/redo on the live timeline.
    QSet<QString> selectedSet;
    for (const QString &opId : m_aiWidget->selectedOperationIds()) {
        selectedSet.insert(opId);
    }
    const QJsonObject preview = m_engine->previewPendingProposal(reqId, selectedSet.isEmpty() ? nullptr : &selectedSet);
    m_aiWidget->setIsolatedPreviewSummary(preview);
    m_aiWidget->appendActivityLog(QStringLiteral("compare"),
                                  QStringLiteral("Isolated A/B preview computed for '%1' (live_timeline_mutated=false, can_apply=%2)")
                                      .arg(reqId, preview.value(QStringLiteral("can_apply")).toBool(false) ? QStringLiteral("yes") : QStringLiteral("no")));
}

void BangOnProIntegration::undoLast()
{
    m_aiWidget->setCompareModeActive(false);
    m_aiWidget->setIsolatedPreviewSummary(QJsonObject{});
    const TimelineCommandResponse resp = m_engine->undoLastTransaction(QStringLiteral("user"));
    m_aiWidget->setCanUndo(m_engine->canUndo());
    m_aiWidget->appendActivityLog(
        QStringLiteral("timeline-writer"),
        QStringLiteral("Undo: %1 (revision %2) %3").arg(commandStatusToString(resp.status)).arg(resp.currentRevision).arg(resp.errorMessage));
}

void BangOnProIntegration::cancelTask()
{
    if (m_aiProcess) {
        m_aiProcess->kill();
        m_aiWidget->appendActivityLog(QStringLiteral("integration"), QStringLiteral("AI task cancelled by user"), QStringLiteral("WARN"));
    }
}

void BangOnProIntegration::openLastGeneratedProject()
{
    if (m_lastKdenliveProjectPath.isEmpty() || !QFileInfo::exists(m_lastKdenliveProjectPath)) {
        return;
    }
    if (pCore && pCore->projectManager()) {
        pCore->projectManager()->openFile(QUrl::fromLocalFile(m_lastKdenliveProjectPath));
        if (m_aiWidget) {
            m_aiWidget->appendActivityLog(QStringLiteral("integration"), QStringLiteral("Opened editable project: %1").arg(m_lastKdenliveProjectPath));
        }
    }
}

void BangOnProIntegration::importPresetArchive(const QString &zipPath)
{
    const QString outDir = dataDir() + QStringLiteral("/presets");
    QStringList args{QStringLiteral("-m"),
                     QStringLiteral("bangonpro.premiere_compat.cli"),
                     QStringLiteral("import"),
                     QStringLiteral("--input"),
                     zipPath,
                     QStringLiteral("--output-dir"),
                     outDir};
#ifdef Q_OS_MACOS
    args << QStringLiteral("--platform") << QStringLiteral("macos");
#endif
    QProcess *proc = startPython(args);
    if (!proc) {
        qWarning("Bang On Pro: cannot import presets, bangonpro package not found");
        return;
    }
    if (m_aiWidget) {
        m_aiWidget->appendActivityLog(QStringLiteral("premiere-compat"), QStringLiteral("Importing Premiere preset archive: %1").arg(zipPath));
    }
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, proc](int code, QProcess::ExitStatus status) {
        proc->deleteLater();
        if (status == QProcess::NormalExit && code == 0) {
            loadPresetReportIfPresent();
            if (m_presetDock) {
                m_presetDock->show();
                m_presetDock->raise();
            }
            if (m_aiWidget) {
                m_aiWidget->appendActivityLog(QStringLiteral("premiere-compat"), QStringLiteral("Premiere preset import complete; browser refreshed"));
            }
        } else {
            const QString errText = QString::fromUtf8(proc->readAllStandardError().right(800));
            qWarning("Bang On Pro: preset import failed: %s", qPrintable(errText));
            if (m_aiWidget) {
                m_aiWidget->appendActivityLog(QStringLiteral("premiere-compat"), QStringLiteral("Preset import failed: %1").arg(errText),
                                              QStringLiteral("ERROR"));
            }
        }
    });
}

void BangOnProIntegration::installAndApplyEffectXml(const QString &fileSlug, const QString &effectDisplayName, const QString &xml)
{
    if (xml.isEmpty()) {
        return;
    }
    const QString effDir = QStandardPaths::writableLocation(QStandardPaths::AppDataLocation) + QStringLiteral("/effects/");
    QDir().mkpath(effDir);
    const QString path = effDir + QStringLiteral("bangonpro_") + sanitizeFileSlug(fileSlug) + QStringLiteral(".xml");
    QFile f(path);
    if (!f.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        return;
    }
    f.write(xml.toUtf8());
    f.close();
    if (m_effectList) {
        m_effectList->reloadCustomEffect(path);
    }
    auto *mw = qobject_cast<MainWindow *>(m_mainWindow);
    if (mw && mw->getCurrentTimeline() && mw->getCurrentTimeline()->controller()) {
        mw->getCurrentTimeline()->controller()->addEffectToClip(effectDisplayName);
    }
}

void BangOnProIntegration::applyPreset(const QString &presetId, const QJsonArray &mltFilters)
{
    QString err;
    const QJsonObject report = readJsonFile(dataDir() + QStringLiteral("/presets/compatibility_report.json"), &err);
    const QJsonArray presets = report.value(QStringLiteral("presets")).toArray();
    for (const QJsonValue &pv : presets) {
        const QJsonObject p = pv.toObject();
        if (p.value(QStringLiteral("preset_id")).toString() != presetId) {
            continue;
        }
        const QString xml = p.value(QStringLiteral("kdenlive_effect_xml")).toString();
        if (xml.isEmpty()) {
            qWarning("Bang On Pro: preset %s has no executable mapping (%s)", qPrintable(presetId),
                     qPrintable(p.value(QStringLiteral("compatibility_status")).toString()));
            return;
        }
        installAndApplyEffectXml(presetId, p.value(QStringLiteral("name")).toString(), xml);
        return;
    }
    // Fallback: synthesize Kdenlive XML directly from the provided MLT filter specs
    if (!mltFilters.isEmpty()) {
        const QString xml = buildEffectGroupXml(presetId, mltFilters);
        installAndApplyEffectXml(presetId, presetId, xml);
    }
}

void BangOnProIntegration::applyCustomStack(const QString &stackName, const QJsonArray &combinedMltFilters)
{
    const QString xml = buildEffectGroupXml(stackName, combinedMltFilters);
    installAndApplyEffectXml(QStringLiteral("stack_") + stackName, stackName, xml);
    if (m_aiWidget) {
        m_aiWidget->appendActivityLog(QStringLiteral("effects"),
                                      QStringLiteral("Applied custom effect stack '%1' (%2 filters)").arg(stackName).arg(combinedMltFilters.size()));
    }
}

} // namespace BangOnPro
