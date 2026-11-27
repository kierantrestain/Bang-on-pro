/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "BangOnProIntegration.h"
#include "BangOnProAutotest.h"

#include "effects_browser/PremierePresetBrowser.h"
#include "effects_browser/UnifiedEffectsPanel.h"
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
#include "ui/ReferenceStyleDialog.h"
#include "ui/ShowProfileEditorDialog.h"
#include "ui/TranscriptEditorWidget.h"

#include "bin/bin.h"
#include "bin/model/markerlistmodel.hpp"
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
#include "timeline2/model/timelineitemmodel.hpp"
#include "timeline2/view/timelinecontroller.h"
#include "timeline2/view/timelinewidget.h"

#include <KActionCollection>
#include <QAction>
#include <QApplication>
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QDockWidget>
#include <QDomDocument>
#include <QElapsedTimer>
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
#include <QSaveFile>
#include <QScrollArea>
#include <QSettings>
#include <QStandardPaths>
#include <QTimer>
#include <QToolBar>
#include <QUrl>
#include <QUuid>
#include <QXmlStreamWriter>

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

bool writeFileAtomic(const QString &path, const QByteArray &data)
{
    const QFileInfo fi(path);
    if (!fi.dir().exists()) {
        QDir().mkpath(fi.absolutePath());
    }
    QSaveFile sf(path);
    if (!sf.open(QIODevice::WriteOnly)) {
        return false;
    }
    if (sf.write(data) != data.size()) {
        sf.cancelWriting();
        return false;
    }
    return sf.commit();
}

bool isPathInsideDirectory(const QString &candidatePath, const QString &rootDir)
{
    if (candidatePath.isEmpty() || rootDir.isEmpty()) {
        return false;
    }
    const QFileInfo rootInfo(rootDir);
    const QString canonicalRoot = rootInfo.canonicalFilePath();
    if (canonicalRoot.isEmpty()) {
        return false;
    }
    const QFileInfo candInfo(candidatePath);
    if (candInfo.isSymLink()) {
        return false;
    }
    const QString canonicalCand = candInfo.canonicalFilePath();
    if (canonicalCand.isEmpty()) {
        return false;
    }
    const QString prefix = canonicalRoot.endsWith(QLatin1Char('/')) ? canonicalRoot : canonicalRoot + QLatin1Char('/');
    return canonicalCand == canonicalRoot || canonicalCand.startsWith(prefix);
}

QString sanitizeProjectUuid(const QString &raw)
{
    QString clean = raw;
    clean.replace(QRegularExpression(QStringLiteral("[^A-Za-z0-9_.-]+")), QStringLiteral("_"));
    clean = clean.left(96);
    return clean.isEmpty() ? QStringLiteral("default_project") : clean;
}

QString resolveInstalledEffectIdFromXml(const QString &xml, const QString &fallbackDisplayName)
{
    // Kdenlive EffectsRepository::parseCustomAssetFile registers a single-filter <effectgroup>
    // under the inner <effect id="...">, whereas a multi-filter <effectgroup> uses <effectgroup id="...">.
    QDomDocument doc;
    if (!doc.setContent(xml)) {
        return fallbackDisplayName;
    }
    const QDomElement root = doc.documentElement();
    if (root.tagName() == QLatin1String("effectgroup")) {
        const QDomNodeList effects = root.elementsByTagName(QStringLiteral("effect"));
        if (effects.count() == 1) {
            const QString innerId = effects.at(0).toElement().attribute(QStringLiteral("id")).trimmed();
            if (!innerId.isEmpty()) {
                return innerId;
            }
        }
        const QString groupId = root.attribute(QStringLiteral("id")).trimmed();
        if (!groupId.isEmpty()) {
            return groupId;
        }
    } else if (root.tagName() == QLatin1String("effect")) {
        const QString effId = root.attribute(QStringLiteral("id")).trimmed();
        if (!effId.isEmpty()) {
            return effId;
        }
    }
    return fallbackDisplayName;
}

// V1.2.2: Bang On Pro's own settings (storage folder, first launch, per-project timing sessions).
const QString kSettingsOrg = QStringLiteral("BangOnPro");
const QString kSettingsApp = QStringLiteral("BangOnPro");

QString customProfilesDir()
{
    // Must match bangonpro/show_profiles/loader.py default_custom_profiles_dir().
    const QString env = qEnvironmentVariable("BANGONPRO_PROFILES_DIR");
    return env.isEmpty() ? QDir::homePath() + QStringLiteral("/.local/share/bangonpro/profiles") : env;
}

bool queueHasPendingWork(const QJsonObject &queueDoc, QString *runningJobDir = nullptr, QString *runningLabel = nullptr, QString *runningProjectUuid = nullptr)
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
            if (runningProjectUuid) {
                QString pUuid = e.value(QStringLiteral("project_uuid")).toString();
                if (pUuid.isEmpty()) {
                    pUuid = e.value(QStringLiteral("spec")).toObject().value(QStringLiteral("project_uuid")).toString();
                }
                if (pUuid.isEmpty()) {
                    pUuid = e.value(QStringLiteral("spec")).toObject().value(QStringLiteral("project_id")).toString();
                }
                *runningProjectUuid = pUuid;
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
    setupUnifiedEffectsPanel();
    setupRotoStudioDock();
    setupWorkspaceBar();
    // Menu actions (incl. the nine workspace actions) must exist before the shortcut preset is applied,
    // otherwise Alt+Shift+1…9 workspace shortcuts are never bound (V1.2.1 fix).
    setupMenu();
    setupShortcuts();
    connectProjectLifecycle();

    // V1.7 performance: zero idle Python subprocess spawns at startup!
    // Presets, custom profiles, and project-scoped history load lazily when their docks are shown
    // or when a project opens. Only auto-resume the background queue if actual pending work exists.
    const QString qFile = queueRoot() + QStringLiteral("/queue.json");
    if (QFileInfo::exists(qFile) && queueHasPendingWork(readJsonFile(qFile, nullptr))) {
        QTimer::singleShot(2500, this, [this]() { ensureQueueWorker(); });
    }
    {
        QSettings settings(kSettingsOrg, kSettingsApp);
        if (!settings.value(QStringLiteral("setup/firstLaunchDone"), false).toBool() && qEnvironmentVariableIsEmpty("BANGONPRO_AUTOTEST")) {
            QTimer::singleShot(1800, m_mainWindow, [this]() { openSystemCheck(true); });
        }
    }
    qApp->installEventFilter(this);
    // Single consolidated post-restore workspace pass: apply clean Edit studio layout on first run
    // or during acceptance tests, while preserving user-saved layouts on subsequent launches.
    QTimer::singleShot(600, m_mainWindow, [this]() {
        QSettings settings(kSettingsOrg, kSettingsApp);
        const bool forceEdit =
            !settings.value(QStringLiteral("workspace/v17Initialized"), false).toBool() || !qEnvironmentVariableIsEmpty("BANGONPRO_AUTOTEST");
        if (forceEdit) {
            applyStudioWorkspace(WorkspaceMode::Edit);
            settings.setValue(QStringLiteral("workspace/v17Initialized"), true);
        }
        fitWindowToScreen(m_mainWindow);
    });
    // Editor acceptance tests drive this very window when BANGONPRO_AUTOTEST names a script (inactive otherwise).
    BangOnProAutotest::startIfRequested(m_mainWindow, this);
}

BangOnProIntegration::~BangOnProIntegration()
{
    qApp->removeEventFilter(this);
    QList<QPointer<QProcess>> activeProcs;
    auto collect = [&activeProcs](const QPointer<QProcess> &p) {
        if (p && p->state() != QProcess::NotRunning && !activeProcs.contains(p)) {
            activeProcs.append(p);
        }
    };
    collect(m_deptProcess);
    collect(m_queueWorker);
    collect(m_aiProcess);
    collect(m_rotoProcess);
    collect(m_compositeProcess);
    collect(m_storyboardProcess);
    collect(m_transcriptEditProcess);
    collect(m_presetImportProcess);
    for (const QPointer<QProcess> &p : qAsConst(m_trackedProcesses)) {
        collect(p);
    }
    for (const QPointer<QProcess> &p : qAsConst(activeProcs)) {
        if (p) {
            p->disconnect(this);
        }
    }
    // Parallel termination: SIGTERM all workers together (<= 1.2s total) so quitting never beachballs macOS.
    stopProcessesParallel(activeProcs, 1000);
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

QStringList BangOnProIntegration::collectSelectedBinMediaPaths() const
{
    QStringList paths;
    if (!Core::self() || !Core::self()->bin() || !Core::self()->projectItemModel()) {
        return paths;
    }
    const auto binModel = Core::self()->projectItemModel();
    for (const QString &binId : Core::self()->bin()->selectedClipsIds()) {
        if (auto clip = binModel->getClipByBinID(binId)) {
            if (clip->clipType() == ClipType::Timeline || clip->clipType() == ClipType::Text || clip->clipType() == ClipType::Color) {
                continue;
            }
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

QString BangOnProIntegration::projectJobsRoot(const QString &projectUuid)
{
    const QString safe = sanitizeProjectUuid(projectUuid);
    return storageRoot() + QStringLiteral("/projects/") + safe + QStringLiteral("/jobs");
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

QString BangOnProIntegration::timingSettingsPrefix() const
{
    const QString uuid = m_activeProjectUuid.isEmpty() ? QStringLiteral("default") : sanitizeProjectUuid(m_activeProjectUuid);
    return QStringLiteral("measurement/projects/") + uuid;
}

ProjectIdentity BangOnProIntegration::currentProjectIdentity() const
{
    ProjectIdentity id;
    KdenliveDoc *doc = pCore ? pCore->currentDoc() : nullptr;
    if (!doc) {
        return id;
    }
    QString uuid = doc->getDocumentProperty(QStringLiteral("bangonpro.project_uuid")).trimmed();
    if (uuid.isEmpty()) {
        if (!doc->uuid().isNull()) {
            uuid = doc->uuid().toString(QUuid::WithoutBraces);
        } else {
            uuid = QUuid::createUuid().toString(QUuid::WithoutBraces);
        }
        doc->setDocumentProperty(QStringLiteral("bangonpro.project_uuid"), uuid);
    }
    id.projectUuid = uuid;
    const QUrl url = doc->url();
    if (url.isValid() && !url.isEmpty()) {
        id.projectPath = url.toLocalFile();
        id.projectName = QFileInfo(id.projectPath).completeBaseName();
    } else {
        id.projectName = QStringLiteral("Untitled project");
    }
    const QUuid seqUuid = pCore->currentTimelineId();
    if (!seqUuid.isNull()) {
        id.sequenceUuid = seqUuid.toString(QUuid::WithoutBraces);
        if (pCore->projectItemModel()) {
            const QString binId = pCore->projectItemModel()->getAllSequenceClips().value(seqUuid);
            const auto clip = binId.isEmpty() ? nullptr : pCore->projectItemModel()->getClipByBinID(binId);
            if (clip) {
                id.sequenceName = clip->clipName();
            }
        }
    }
    if (id.sequenceUuid.isEmpty()) {
        id.sequenceUuid = QStringLiteral("seq_main");
    }
    if (id.sequenceName.isEmpty()) {
        id.sequenceName = QStringLiteral("Sequence 1");
    }
    return id;
}

bool BangOnProIntegration::jobBelongsToActiveProject(const QJsonObject &job, QString *jobProjectUuidOut) const
{
    const QJsonObject proj = job.value(QStringLiteral("project")).toObject();
    QString jobUuid = proj.value(QStringLiteral("project_uuid")).toString().trimmed();
    if (jobUuid.isEmpty()) {
        jobUuid = proj.value(QStringLiteral("project_id")).toString().trimmed();
    }
    if (jobProjectUuidOut) {
        *jobProjectUuidOut = jobUuid;
    }
    if (m_activeProjectUuid.isEmpty() || jobUuid.isEmpty()) {
        return false;
    }
    return jobUuid == m_activeProjectUuid;
}

void BangOnProIntegration::hydrateEditedSequencesFromDoc(KdenliveDoc *doc)
{
    m_editedSequences.clear();
    if (!doc || !pCore || !pCore->projectItemModel()) {
        return;
    }
    const QMap<QUuid, QString> seqs = pCore->projectItemModel()->getAllSequenceClips();
    for (auto it = seqs.constBegin(); it != seqs.constEnd(); ++it) {
        const QString key = doc->getDocumentProperty(QStringLiteral("bangonpro.edited.%1").arg(it.key().toString(QUuid::WithoutBraces)));
        if (!key.isEmpty()) {
            m_editedSequences.insert(key, it.key().toString());
        }
    }
}

void BangOnProIntegration::resetAllProjectScopedState(const QString &newProjectUuid)
{
    m_activeProjectUuid = newProjectUuid;
    m_contextSignature.clear();
    m_editedSequences.clear();
    m_lastKdenliveProjectPath.clear();
    m_lastTranscriptPath.clear();
    m_lastInputs.clear();
    m_rotoMattePath.clear();
    m_rotoMatteMaskId.clear();
    m_activeProposalRequestId.clear();
    m_deptBaseProjectId.clear();
    m_deptBaseSequenceId.clear();
    m_deptBaseRevision = 0;
    m_proposalBaseProjectId.clear();
    m_proposalBaseSequenceId.clear();
    m_proposalBaseRevision = 0;
    m_timingSession.clear();
    m_historyLoadedForProject = false;
    m_queueRunningJobDir.clear();
    if (m_openGeneratedProjectAction) {
        m_openGeneratedProjectAction->setEnabled(false);
    }
    if (m_agentCentre) {
        m_agentCentre->resetForProject(newProjectUuid, QString(), {}, -1);
        m_agentCentre->setTimingActive(false);
    }
    if (m_aiWidget) {
        m_aiWidget->resetForProject();
    }
    if (m_transcriptWidget) {
        m_transcriptWidget->resetForProject();
    }
    if (m_rotoWidget) {
        m_rotoWidget->resetForProject();
    }
    if (m_engine) {
        m_engine->resetForProject(newProjectUuid, QStringLiteral("seq_main"));
    }
}

void BangOnProIntegration::connectProjectLifecycle()
{
    if (!pCore || !pCore->projectManager()) {
        return;
    }
    ProjectManager *pm = pCore->projectManager();
    connect(pm, &ProjectManager::docAboutToClose, this, &BangOnProIntegration::onProjectClosing);
    connect(pm, &ProjectManager::docOpened, this, &BangOnProIntegration::onProjectOpened);
    // If a document is already open when BangOnProIntegration initializes, bind immediately.
    if (KdenliveDoc *doc = pCore->currentDoc()) {
        onProjectOpened(doc);
    }
}

void BangOnProIntegration::onProjectClosing(KdenliveDoc *)
{
    resetAllProjectScopedState(QString());
}

void BangOnProIntegration::onProjectOpened(KdenliveDoc *doc)
{
    if (!doc) {
        resetAllProjectScopedState(QString());
        return;
    }
    const ProjectIdentity id = currentProjectIdentity();
    resetAllProjectScopedState(id.projectUuid);
    if (m_engine) {
        m_engine->resetForProject(id.projectUuid, id.sequenceUuid);
    }
    hydrateEditedSequencesFromDoc(doc);
    refreshProjectContext();

    // Restore per-project timing session (never leaked across unrelated projects).
    {
        QSettings settings(kSettingsOrg, kSettingsApp);
        const QString prefix = timingSettingsPrefix();
        m_timingSession = settings.value(prefix + QStringLiteral("/openSession")).toString();
        if (!m_timingSession.isEmpty() && m_agentCentre) {
            m_agentCentre->setTimingActive(true, settings.value(prefix + QStringLiteral("/paused"), false).toBool());
        }
    }

    // Recover ONLY an eligible job belonging strictly to THIS project (never from global jobs/).
    recoverActiveProjectJob(id.projectUuid);

    if (m_agentDock && m_agentDock->isVisible()) {
        refreshHistory();
    }
}

void BangOnProIntegration::recoverActiveProjectJob(const QString &projectUuid)
{
    if (!m_agentCentre || projectUuid.isEmpty()) {
        return;
    }
    // If the user switched away while this project's worker was running and switched back, re-attach live UI.
    if (m_deptProcess && !m_deptJobDir.isEmpty() && m_deptJobProjectUuid == projectUuid) {
        m_agentCentre->setActiveJobDir(m_deptJobDir);
        m_agentCentre->setBusy(true, QStringLiteral("Production running for this project"));
        return;
    }
    const QString projJobsDir = projectJobsRoot(projectUuid);
    if (!QFileInfo::exists(projJobsDir)) {
        return; // Completely new or unrelated project -> clean AI Cut setup interface!
    }
    const QFileInfoList jobs = QDir(projJobsDir).entryInfoList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Time);
    for (const QFileInfo &fi : jobs) {
        const QString dir = fi.absoluteFilePath();
        const QString jobJsonPath = QDir(dir).filePath(QStringLiteral("job.json"));
        if (!QFileInfo::exists(jobJsonPath)) {
            continue;
        }
        const QJsonObject job = readJsonFile(jobJsonPath, nullptr);
        if (!jobBelongsToActiveProject(job)) {
            continue;
        }
        const bool alive = AgentControlCentre::jobWorkerAlive(dir);
        const QString status = job.value(QStringLiteral("status")).toString();
        // Recover active, paused, or crashed/interrupted jobs for THIS project so Resume works cleanly.
        if (alive || status == QLatin1String("running") || status == QLatin1String("paused") || status == QLatin1String("interrupted") ||
            status == QLatin1String("blocked_missing_media")) {
            m_agentCentre->recoverJob(dir, alive);
            if (!alive && status == QLatin1String("running")) {
                reconcileJob(dir);
            }
            break;
        }
        // Completed / cancelled / failed jobs stay available in this project's History without
        // blocking a fresh AI Cut workspace when reopening the project.
        break;
    }
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
    connect(m_agentDock, &QDockWidget::visibilityChanged, this, [this](bool visible) {
        if (visible) {
            if (!m_customProfilesLoaded) {
                loadCustomProfiles();
            }
            refreshProjectContext();
            if (!m_historyLoadedForProject) {
                refreshHistory();
            }
            if (m_contextTimer && !m_contextTimer->isActive()) {
                m_contextTimer->start();
            }
        } else if (m_contextTimer && m_contextTimer->isActive()) {
            m_contextTimer->stop();
        }
    });
    connect(m_agentCentre, &AgentControlCentre::startJobRequested, this, &BangOnProIntegration::startDepartmentJob);
    connect(m_agentCentre, &AgentControlCentre::pauseJobRequested, this, &BangOnProIntegration::pauseDepartmentJob);
    connect(m_agentCentre, &AgentControlCentre::retryJobRequested, this, [this](const QString &jobDir) {
        runDepartmentCommand({QStringLiteral("resume"), jobDir}, jobDir, QStringLiteral("Retrying from the last verified stage"));
    });
    connect(m_agentCentre, &AgentControlCentre::discardJobRequested, this, &BangOnProIntegration::discardDepartmentJob);
    connect(m_agentCentre, &AgentControlCentre::newProductionRequested, this, [this]() {
        if (m_aiWidget) {
            m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Ready for a new production (previous history preserved)"));
        }
    });
    connect(m_agentCentre, &AgentControlCentre::cancelJobRequested, this, [this](const QString &jobDir) {
        // Cooperative first: agents poll the CANCEL file between steps; checkpoints make the job resumable.
        writeFileAtomic(QDir(jobDir).filePath(QStringLiteral("CANCEL")), QByteArrayLiteral("Cancelled from the Production panel"));
        m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Cancellation requested for %1").arg(jobDir), QStringLiteral("WARN"));
        escalateCancel(jobDir);
    });
    connect(m_agentCentre, &AgentControlCentre::resumeJobRequested, this, [this](const QString &jobDir) {
        const QJsonObject job = readJsonFile(QDir(jobDir).filePath(QStringLiteral("job.json")), nullptr);
        if (!job.isEmpty() && !jobBelongsToActiveProject(job)) {
            m_agentCentre->showTaskProblem(QStringLiteral("This production belongs to another project"),
                                           QStringLiteral("Open that project before resuming its production."));
            return;
        }
        runDepartmentCommand({QStringLiteral("resume"), jobDir}, jobDir, QStringLiteral("Resuming from the last finished stage"));
    });
    // A job recorded as running with no worker behind it: persist the truth (department status reconciles it).
    connect(m_agentCentre, &AgentControlCentre::workerLost, this, &BangOnProIntegration::reconcileJob);
    connect(m_agentCentre, &AgentControlCentre::openEditedSequenceRequested, this, &BangOnProIntegration::openEditedSequence);
    connect(m_agentCentre, &AgentControlCentre::qcMarkersToggled, this, &BangOnProIntegration::setQcMarkersVisible);
    connect(m_agentCentre, &AgentControlCentre::newSequenceRequested, this, &BangOnProIntegration::createSequenceFromFootage);
    connect(m_agentCentre, &AgentControlCentre::sequenceSelected, this, [this](int index) {
        if (!pCore || !pCore->projectItemModel() || !pCore->projectManager()) {
            return;
        }
        const QMap<QUuid, QString> seqs = pCore->projectItemModel()->getAllSequenceClips();
        if (index >= 0 && index < seqs.size()) {
            const QUuid uuid = seqs.keys().at(index);
            pCore->projectManager()->openTimeline(seqs.value(uuid), -1, uuid);
        }
        refreshProjectContext();
    });
    connect(m_agentCentre, &AgentControlCentre::exportRequested, this, [this]() {
        if (pCore && pCore->window()) {
            if (QAction *render = pCore->window()->actionCollection()->action(QStringLiteral("project_render"))) {
                render->trigger();
            }
        }
    });
    connect(m_agentCentre, &AgentControlCentre::referenceVideosRequested, this, &BangOnProIntegration::openReferenceStyles);
    // Safety-net context refresh timer: runs ONLY while Agent Control Centre dock is visible.
    m_contextTimer = new QTimer(this);
    m_contextTimer->setInterval(2500);
    connect(m_contextTimer, &QTimer::timeout, this, &BangOnProIntegration::refreshProjectContext);
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
    connect(m_agentCentre, &AgentControlCentre::jobFinished, this, [this](const QString &jobDir, const QString &status) {
        m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Job %1 finished: %2").arg(QFileInfo(jobDir).fileName(), status));
        refreshHistory();
        if (!m_timingSession.isEmpty()) {
            // The agent-assisted session is measured against the job being reviewed (machine time stays separate).
            runQuickDepartmentCommand({QStringLiteral("measure"), QStringLiteral("link"), QStringLiteral("--root"), measurementRoot(), QStringLiteral("--session"),
                                       m_timingSession, QStringLiteral("--job-dir"), jobDir});
        }
    });
    // V1.2.2–V1.7: project-scoped queue, history and timed sessions.
    connect(m_agentCentre, &AgentControlCentre::queueJobRequested, this, &BangOnProIntegration::queueDepartmentJob);
    connect(m_agentCentre, &AgentControlCentre::historyJobActivated, this, [this](const QString &jobDir) {
        if (m_deptProcess && m_deptJobProjectUuid == m_activeProjectUuid) {
            m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Wait for the running department task to finish"), QStringLiteral("WARN"));
            return;
        }
        const QJsonObject job = readJsonFile(QDir(jobDir).filePath(QStringLiteral("job.json")), nullptr);
        if (!job.isEmpty() && !jobBelongsToActiveProject(job)) {
            m_agentCentre->showTaskProblem(QStringLiteral("This production belongs to another project"),
                                           QStringLiteral("Switch to that project to inspect or open its production."));
            return;
        }
        m_agentCentre->recoverJob(jobDir, AgentControlCentre::jobWorkerAlive(jobDir));
    });
    connect(m_agentCentre, &AgentControlCentre::queueEntryActionRequested, this, [this](const QString &queueId, const QString &action) {
        runQuickDepartmentCommand({QStringLiteral("queue"), action, queueId, QStringLiteral("--queue-root"), queueRoot()},
                                  [this](const QJsonObject &) { refreshHistory(); });
    });
    connect(m_agentCentre, &AgentControlCentre::timingStartRequested, this, &BangOnProIntegration::startTiming);
    connect(m_agentCentre, &AgentControlCentre::measurementActivity, this, [this](const QString &category) {
        if (!m_timingSession.isEmpty()) {
            runQuickDepartmentCommand({QStringLiteral("measure"), QStringLiteral("activity"), QStringLiteral("--root"), measurementRoot(),
                                       QStringLiteral("--session"), m_timingSession, QStringLiteral("--category"), category},
                                      {}, QStringLiteral("measure_activity_") + category);
        }
    });
    connect(m_agentCentre, &AgentControlCentre::timingPauseToggled, this, [this](bool paused) {
        if (m_timingSession.isEmpty()) {
            return;
        }
        runQuickDepartmentCommand({QStringLiteral("measure"), paused ? QStringLiteral("pause") : QStringLiteral("resume"), QStringLiteral("--root"), measurementRoot(),
                                   QStringLiteral("--session"), m_timingSession});
        m_agentCentre->setTimingActive(true, paused);
        QSettings(kSettingsOrg, kSettingsApp).setValue(timingSettingsPrefix() + QStringLiteral("/paused"), paused);
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
        const QString prefix = timingSettingsPrefix();
        settings.remove(prefix + QStringLiteral("/openSession"));
        settings.remove(prefix + QStringLiteral("/paused"));
        m_agentCentre->setTimingActive(false);
    });
}

bool BangOnProIntegration::prepareJobArguments(QStringList &args, QString *resolvedJobsRootOut)
{
    const ProjectIdentity id = currentProjectIdentity();
    if (!id.projectUuid.isEmpty() && id.projectUuid != m_activeProjectUuid) {
        m_activeProjectUuid = id.projectUuid;
    }
    m_deptBaseRevision = m_engine->currentRevision();
    m_deptBaseProjectId = id.projectUuid.isEmpty() ? m_engine->activeProjectId() : id.projectUuid;
    if (m_deptBaseProjectId.isEmpty()) {
        m_deptBaseProjectId = QUuid::createUuid().toString(QUuid::WithoutBraces);
    }
    m_deptBaseSequenceId = id.sequenceUuid.isEmpty() ? QStringLiteral("seq_main") : id.sequenceUuid;

    const QString projJobsDir = projectJobsRoot(m_deptBaseProjectId);
    QDir().mkpath(projJobsDir);
    if (resolvedJobsRootOut) {
        *resolvedJobsRootOut = projJobsDir;
    }

    if (!args.contains(QStringLiteral("--inputs"))) {
        const QString source = m_agentCentre ? m_agentCentre->selectedMediaSource() : QStringLiteral("sequence");
        QStringList inputs;
        if (source == QLatin1String("files")) {
            inputs = QFileDialog::getOpenFileNames(m_mainWindow, QStringLiteral("Select camera / microphone media for this production"), QString(),
                                                   QStringLiteral("Media (*.mp4 *.mov *.mxf *.mkv *.m4v *.wav *.mp3);;All files (*)"));
        } else if (source == QLatin1String("selected_bin")) {
            inputs = collectSelectedBinMediaPaths();
            if (inputs.isEmpty()) {
                inputs = collectProjectBinMediaPaths();
            }
        } else if (source == QLatin1String("project_bin")) {
            inputs = collectProjectBinMediaPaths();
        } else {
            inputs = collectSequenceMediaPaths();
            if (inputs.isEmpty()) {
                inputs = collectProjectBinMediaPaths();
            }
        }
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
    args << QStringLiteral("--job-root") << projJobsDir << QStringLiteral("--project-id") << m_deptBaseProjectId << QStringLiteral("--project-uuid")
         << m_deptBaseProjectId << QStringLiteral("--project-name") << (id.projectName.isEmpty() ? QStringLiteral("Untitled project") : id.projectName)
         << QStringLiteral("--sequence-id") << m_deptBaseSequenceId << QStringLiteral("--sequence-uuid") << m_deptBaseSequenceId
         << QStringLiteral("--sequence-name") << (id.sequenceName.isEmpty() ? QStringLiteral("Sequence 1") : id.sequenceName);
    if (!id.projectPath.isEmpty()) {
        args << QStringLiteral("--project-path") << id.projectPath;
    }
    return true;
}

void BangOnProIntegration::runQuickDepartmentCommand(const QStringList &args, const std::function<void(const QJsonObject &)> &onDone, const QString &dedupKey)
{
    if (!dedupKey.isEmpty()) {
        auto it = m_inflightQuickCommands.find(dedupKey);
        if (it != m_inflightQuickCommands.end() && it.value() && it.value()->state() != QProcess::NotRunning) {
            return; // Avoid duplicate concurrent spawns (e.g. overlapping history/status polls).
        }
    }
    QStringList full{QStringLiteral("-m"), QStringLiteral("bangonpro.ai_engine.department")};
    full << args;
    QProcess *proc = startPython(full);
    if (!proc) {
        return;
    }
    if (!dedupKey.isEmpty()) {
        m_inflightQuickCommands.insert(dedupKey, proc);
    }
    const QString what = args.isEmpty() ? QString() : args.first();
    connect(
        proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, proc, onDone, what, dedupKey](int code, QProcess::ExitStatus status) {
            if (!dedupKey.isEmpty() && m_inflightQuickCommands.value(dedupKey) == proc) {
                m_inflightQuickCommands.remove(dedupKey);
            }
            proc->deleteLater();
            const QJsonObject res = QJsonDocument::fromJson(proc->readAllStandardOutput()).object();
            if (status != QProcess::NormalExit || code != 0) {
                const QJsonObject help = res.value(QStringLiteral("help")).toObject();
                const QString text =
                    help.isEmpty() ? QString::fromUtf8(proc->readAllStandardError().right(400))
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
    if (!m_agentCentre) {
        return;
    }
    const QString projectUuid = m_activeProjectUuid;
    if (projectUuid.isEmpty()) {
        m_agentCentre->loadHistory(QJsonObject{{QStringLiteral("jobs"), QJsonArray()}, {QStringLiteral("queue"), QJsonArray()}});
        return;
    }
    const QString projJobsDir = projectJobsRoot(projectUuid);
    m_historyLoadedForProject = true;
    runQuickDepartmentCommand(
        {QStringLiteral("history"), QStringLiteral("--job-root"), projJobsDir, QStringLiteral("--queue-root"), queueRoot(), QStringLiteral("--project-uuid"),
         projectUuid, QStringLiteral("--limit"), QStringLiteral("30")},
        [this, projectUuid](const QJsonObject &res) {
            // Guard against project switch while history subprocess was running.
            if (projectUuid != m_activeProjectUuid) {
                return;
            }
            if (res.contains(QStringLiteral("jobs"))) {
                m_agentCentre->loadHistory(res);
            }
        },
        QStringLiteral("history_") + projectUuid);
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
    m_lastQueueFileSig.clear();
    m_queuePollTimer->start();
    m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Production queue running (it keeps going while Bang On Pro is open)"));
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, proc](int code, QProcess::ExitStatus) {
        proc->deleteLater();
        m_queueWorker.clear();
        m_queuePollTimer->stop();
        m_lastQueueFileSig.clear();
        pollQueue();
        refreshHistory();
        const QJsonObject res = QJsonDocument::fromJson(proc->readAllStandardOutput()).object();
        if (code != 0) {
            m_aiWidget->appendActivityLog(QStringLiteral("department"),
                                          QStringLiteral("Production queue stopped: %1").arg(res.value(QStringLiteral("error")).toString()),
                                          QStringLiteral("WARN"));
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
    const QString queuePath = queueRoot() + QStringLiteral("/queue.json");
    const QFileInfo qfi(queuePath);
    const QString sig = qfi.exists() ? QStringLiteral("%1|%2|%3").arg(qfi.lastModified().toMSecsSinceEpoch()).arg(qfi.size()).arg(m_activeProjectUuid)
                                     : QStringLiteral("missing|") + m_activeProjectUuid;
    if (sig == m_lastQueueFileSig && m_queueWorker) {
        return;
    }
    m_lastQueueFileSig = sig;

    QString runningDir;
    QString runningLabel;
    QString runningProjectUuid;
    queueHasPendingWork(readJsonFile(queuePath, nullptr), &runningDir, &runningLabel, &runningProjectUuid);
    if (!m_queueWorker) {
        runningDir.clear();
    }
    // P0 isolation: NEVER hijack the active project's UI with a queued job from another project.
    if (!runningDir.isEmpty() && !runningProjectUuid.isEmpty() && runningProjectUuid != m_activeProjectUuid) {
        runningDir.clear();
    }
    if (runningDir == m_queueRunningJobDir) {
        return;
    }
    m_queueRunningJobDir = runningDir;
    refreshHistory();
    if (m_deptProcess && m_deptJobProjectUuid == m_activeProjectUuid) {
        return; // the editor is watching their own task; the queue entry stays in the list
    }
    if (!runningDir.isEmpty()) {
        // Follow the queued production live ONLY when it belongs to the active project.
        m_agentCentre->setActiveJobDir(runningDir);
        m_agentCentre->setBusy(true, QStringLiteral("Queued production running: %1").arg(runningLabel));
    } else if (m_agentCentre && !m_agentCentre->activeJobDir().isEmpty()) {
        m_agentCentre->setBusy(false);
        m_agentCentre->refreshFromDisk(true);
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
    const QString projectUuidAtStart = m_activeProjectUuid;
    runQuickDepartmentCommand(args, [this, name, kind, projectUuidAtStart](const QJsonObject &res) {
        const QString session = res.value(QStringLiteral("session_id")).toString();
        if (session.isEmpty() || projectUuidAtStart != m_activeProjectUuid) {
            return;
        }
        m_timingSession = session;
        QSettings settings(kSettingsOrg, kSettingsApp);
        const QString prefix = timingSettingsPrefix();
        settings.setValue(prefix + QStringLiteral("/openSession"), session);
        settings.setValue(prefix + QStringLiteral("/paused"), false);
        m_agentCentre->setTimingActive(true);
        m_aiWidget->appendActivityLog(QStringLiteral("measurement"), QStringLiteral("Timing %1 work on '%2'").arg(kind == QLatin1String("manual_baseline") ? QStringLiteral("manual") : QStringLiteral("agent-assisted"), name));
    });
}

void BangOnProIntegration::loadCustomProfiles()
{
    m_customProfilesLoaded = true;
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
    if (firstLaunch) {
        QSettings(kSettingsOrg, kSettingsApp).setValue(QStringLiteral("setup/firstLaunchDone"), true);
    }
    auto *dlg = new SystemCheckDialog(m_mainWindow);
    dlg->setAttribute(Qt::WA_DeleteOnClose);
    QPointer<SystemCheckDialog> guard(dlg);
    if (storageRootChosen()) {
        dlg->setStorageFolder(storageRoot());
    }
    connect(dlg, &SystemCheckDialog::recheckRequested, this, [this, guard]() {
        if (guard) {
            runReadinessInto(guard);
        }
    });
    connect(dlg, &SystemCheckDialog::modelManagerRequested, this, [this, guard]() {
        openModelManager();
        if (guard) {
            runReadinessInto(guard);
        }
    });
    connect(dlg, &SystemCheckDialog::exportDiagnosticsRequested, this, &BangOnProIntegration::exportDiagnostics);
    connect(dlg, &SystemCheckDialog::storageFolderChosen, this, [this, guard](const QString &folder) {
        if (!QDir().mkpath(folder + QStringLiteral("/projects")) || !QFileInfo(folder).isWritable()) {
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
    connect(dlg, &SystemCheckDialog::cacheActionRequested, this, [this, guard](const QString &action) {
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
    runReadinessInto(dlg);
    if (firstLaunch) {
        // Non-modal on first launch so the editor is never blocked at startup.
        dlg->show();
        dlg->raise();
    } else {
        dlg->exec();
    }
}

void BangOnProIntegration::runReadinessInto(SystemCheckDialog *dialog)
{
    QPointer<SystemCheckDialog> guard(dialog);
    dialog->setBusy(true);
    const QString rootForCheck = m_activeProjectUuid.isEmpty() ? departmentJobsRoot() : projectJobsRoot(m_activeProjectUuid);
    QDir().mkpath(rootForCheck);
    QStringList args{QStringLiteral("readiness"), QStringLiteral("--job-root"), rootForCheck};
    if (storageRootChosen()) {
        args << QStringLiteral("--cache-dir") << storageRoot() + QStringLiteral("/cache");
    }
    runQuickDepartmentCommand(
        args,
        [guard](const QJsonObject &report) {
            if (guard && !guard->loadReport(report)) {
                guard->setBusy(false);
            }
        },
        QStringLiteral("readiness"));
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
    const QString projJobsDir = m_activeProjectUuid.isEmpty() ? departmentJobsRoot() : projectJobsRoot(m_activeProjectUuid);
    QDir().mkpath(projJobsDir);
    QStringList args{QStringLiteral("diagnostics"),  QStringLiteral("export"),
                     QStringLiteral("--out"),        out,
                     QStringLiteral("--job-root"),   projJobsDir,
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
        if (m_deptJobProjectUuid == m_activeProjectUuid) {
            m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("A production job is already running for this project"),
                                          QStringLiteral("WARN"));
            return;
        }
        // Another project's background worker is still running: queue this project's production safely.
        m_aiWidget->appendActivityLog(QStringLiteral("department"),
                                      QStringLiteral("Another project's background production is running; queued this production"));
        queueDepartmentJob(runArguments);
        return;
    }
    if (m_queueWorker) {
        // One production at a time: while the queue is busy, a new production waits its turn.
        m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("A queued production is running, so this one was added to the queue"));
        queueDepartmentJob(runArguments);
        return;
    }
    QStringList args = runArguments;
    QString projJobsDir;
    if (!prepareJobArguments(args, &projJobsDir)) {
        return;
    }
    // Unique collision-free job ID (prevents same-second collisions).
    const QString suffix = QUuid::createUuid().toString(QUuid::WithoutBraces).left(6);
    const QString jobId = QStringLiteral("job_") + QDateTime::currentDateTimeUtc().toString(QStringLiteral("yyyyMMdd-HHmmss")) + QLatin1Char('_') + suffix;
    const QString jobDir = projJobsDir + QLatin1Char('/') + jobId;
    args << QStringLiteral("--job-id") << jobId;
    runDepartmentCommand(args, jobDir, QStringLiteral("Production job started"));
}

void BangOnProIntegration::pauseDepartmentJob(const QString &jobDir)
{
    if (jobDir.isEmpty()) {
        return;
    }
    writeFileAtomic(QDir(jobDir).filePath(QStringLiteral("PAUSE")), QByteArrayLiteral("Paused from the Production panel"));
    runQuickDepartmentCommand({QStringLiteral("pause"), jobDir}, [this, jobDir](const QJsonObject &) {
        if (m_agentCentre && m_agentCentre->activeJobDir() == jobDir) {
            m_agentCentre->refreshFromDisk(true);
        }
        refreshHistory();
    });
    if (m_aiWidget) {
        m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Pause requested for %1").arg(QFileInfo(jobDir).fileName()));
    }
}

void BangOnProIntegration::discardDepartmentJob(const QString &jobDir)
{
    if (jobDir.isEmpty()) {
        return;
    }
    if (AgentControlCentre::jobWorkerAlive(jobDir)) {
        if (m_agentCentre) {
            m_agentCentre->showTaskProblem(QStringLiteral("Stop the running production first"),
                                           QStringLiteral("Press Cancel or Pause before discarding this production."));
        }
        return;
    }
    const QString projectUuidAtCall = m_activeProjectUuid;
    runQuickDepartmentCommand({QStringLiteral("discard"), jobDir}, [this, jobDir, projectUuidAtCall](const QJsonObject &res) {
        if (!res.value(QStringLiteral("error")).toString().isEmpty()) {
            if (m_agentCentre && projectUuidAtCall == m_activeProjectUuid) {
                m_agentCentre->showTaskProblem(QStringLiteral("Could not discard production"), res.value(QStringLiteral("error")).toString());
            }
            return;
        }
        if (m_agentCentre && projectUuidAtCall == m_activeProjectUuid && m_agentCentre->activeJobDir() == jobDir) {
            m_agentCentre->clearActiveJob();
        }
        if (projectUuidAtCall == m_activeProjectUuid) {
            refreshHistory();
        }
    });
}

void BangOnProIntegration::runDepartmentCommand(const QStringList &subcommandArgs, const QString &jobDir, const QString &description)
{
    if (m_deptProcess) {
        m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Wait for the running department task to finish"), QStringLiteral("WARN"));
        return;
    }
    if (!jobDir.isEmpty() && QFileInfo::exists(QDir(jobDir).filePath(QStringLiteral("job.json"))) && AgentControlCentre::jobWorkerAlive(jobDir)) {
        // Another process (a queue worker, or a worker left running by a crashed session) owns this job.
        m_agentCentre->recoverJob(jobDir, true);
        m_agentCentre->showTaskProblem(QStringLiteral("This production is already running"),
                                       QStringLiteral("It is being followed below. Press Cancel to stop it, then Resume."));
        return;
    }
    QStringList args{QStringLiteral("-m"), QStringLiteral("bangonpro.ai_engine.department")};
    args << subcommandArgs;
    QProcess *proc = startPython(args);
    if (!proc) {
        m_agentCentre->showTaskProblem(QStringLiteral("Bang On Pro's production engine is missing"),
                                       QStringLiteral("Reinstall Bang On Pro from the DMG, then run System check."));
        m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Cannot start AI department: bangonpro package not found"),
                                      QStringLiteral("ERROR"));
        return;
    }
    const QString jobProjectUuid = m_activeProjectUuid;
    m_deptProcess = proc;
    m_deptJobDir = jobDir;
    m_deptJobProjectUuid = jobProjectUuid;
    m_agentCentre->setActiveJobDir(jobDir);
    m_agentCentre->setBusy(true, description);
    m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("%1 (%2)").arg(description, jobDir));
    // A process that never starts emits errorOccurred but never finished(): without this the panel waited for ever.
    connect(proc, &QProcess::errorOccurred, this, [this, proc, jobDir, jobProjectUuid](QProcess::ProcessError error) {
        if (error != QProcess::FailedToStart || m_deptProcess != proc) {
            return;
        }
        m_deptProcess.clear();
        m_deptJobDir.clear();
        m_deptJobProjectUuid.clear();
        proc->deleteLater();
        if (jobProjectUuid == m_activeProjectUuid && m_agentCentre) {
            m_agentCentre->setBusy(false);
            m_agentCentre->showTaskProblem(QStringLiteral("The production could not start"),
                                           QStringLiteral("Bang On Pro could not run its Python engine. Run System check, or reinstall from the DMG."),
                                           proc->errorString());
        }
        reconcileJob(jobDir);
    });
    connect(
        proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, proc, jobDir, jobProjectUuid](int code, QProcess::ExitStatus status) {
            m_deptProcess.clear();
            m_deptJobDir.clear();
            m_deptJobProjectUuid.clear();
            proc->deleteLater();
            const bool sameProject = (jobProjectUuid == m_activeProjectUuid);
            if (sameProject && m_agentCentre) {
                m_agentCentre->setBusy(false);
                m_agentCentre->refreshFromDisk(true);
            }
            if (status != QProcess::NormalExit || code != 0) {
                const QJsonObject res = QJsonDocument::fromJson(proc->readAllStandardOutput()).object();
                const QJsonObject help = res.value(QStringLiteral("help")).toObject();
                const QString stderrTail = QString::fromUtf8(proc->readAllStandardError().right(1200));
                const QString detail =
                    help.isEmpty() ? stderrTail
                                   : QStringLiteral("%1. %2").arg(help.value(QStringLiteral("title")).toString(), help.value(QStringLiteral("fix")).toString());
                if (sameProject && m_aiWidget) {
                    m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Department task failed (exit=%1): %2").arg(code).arg(detail),
                                                  QStringLiteral("ERROR"));
                }
                const QJsonObject job = readJsonFile(QDir(jobDir).filePath(QStringLiteral("job.json")), nullptr);
                if (sameProject && m_agentCentre && job.value(QStringLiteral("error_help")).toObject().isEmpty()) {
                    m_agentCentre->showTaskProblem(
                        help.isEmpty() ? QStringLiteral("The task stopped unexpectedly") : help.value(QStringLiteral("title")).toString(),
                        help.isEmpty() ? QStringLiteral("Press Resume to continue from the last finished stage. If it happens again, export diagnostics.")
                                       : help.value(QStringLiteral("fix")).toString(),
                        res.value(QStringLiteral("error")).toString(stderrTail));
                }
                reconcileJob(jobDir);
            } else if (sameProject) {
                refreshHistory();
            }
        });
}

void BangOnProIntegration::reconcileJob(const QString &jobDir)
{
    if (jobDir.isEmpty() || !QFileInfo::exists(QDir(jobDir).filePath(QStringLiteral("job.json")))) {
        return;
    }
    // `status` takes the job lock non-blockingly: a live worker's state is never overwritten.
    runQuickDepartmentCommand(
        {QStringLiteral("status"), jobDir},
        [this, jobDir](const QJsonObject &) {
            if (m_agentCentre && m_agentCentre->activeJobDir() == jobDir) {
                m_agentCentre->refreshFromDisk(true);
            }
            refreshHistory();
        },
        QStringLiteral("reconcile_") + jobDir);
}

void BangOnProIntegration::escalateCancel(const QString &jobDir)
{
    // Most steps stop within seconds. A step stuck inside a long native call (a render, a damaged file) is asked to
    // stop (SIGTERM: the job records "cancelled" itself), and only killed if it still does not respond.
    QPointer<QProcess> proc = (m_deptProcess && m_deptJobDir == jobDir) ? m_deptProcess : QPointer<QProcess>();
    if (!proc) {
        // Not our process (a queued production, or a worker left by a crashed session): the CANCEL file stops it,
        // and if nothing holds the job any more the department records the cancellation now.
        runQuickDepartmentCommand({QStringLiteral("cancel"), jobDir}, [this, jobDir](const QJsonObject &) {
            if (m_agentCentre && m_agentCentre->activeJobDir() == jobDir) {
                m_agentCentre->refreshFromDisk(true);
            }
        });
        return;
    }
    QTimer::singleShot(20000, this, [this, proc]() {
        if (proc && proc->state() != QProcess::NotRunning) {
            m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("The current step did not stop in 20 s; stopping it"),
                                          QStringLiteral("WARN"));
            proc->terminate();
            QTimer::singleShot(8000, this, [proc]() {
                if (proc && proc->state() != QProcess::NotRunning) {
                    proc->kill(); // finished() then reconciles job.json to "cancelled" (the CANCEL file is present)
                }
            });
        }
    });
}

void BangOnProIntegration::stopProcessGracefully(QProcess *proc, int terminateWaitMs)
{
    if (!proc || proc->state() == QProcess::NotRunning) {
        return;
    }
    // SIGTERM lets the department record a truthful "interrupted" state before exiting (V1.5 used SIGKILL only,
    // which left "running" behind).
    proc->terminate();
    if (!proc->waitForFinished(terminateWaitMs)) {
        proc->kill();
        proc->waitForFinished(500);
    }
}

void BangOnProIntegration::stopProcessesParallel(const QList<QPointer<QProcess>> &procs, int terminateWaitMs)
{
    if (procs.isEmpty()) {
        return;
    }
    for (const QPointer<QProcess> &proc : procs) {
        if (proc && proc->state() != QProcess::NotRunning) {
            proc->terminate();
        }
    }
    QElapsedTimer timer;
    timer.start();
    for (const QPointer<QProcess> &proc : procs) {
        if (!proc || proc->state() == QProcess::NotRunning) {
            continue;
        }
        const int remaining = qMax(0, terminateWaitMs - int(timer.elapsed()));
        if (remaining > 0) {
            proc->waitForFinished(remaining);
        }
    }
    for (const QPointer<QProcess> &proc : procs) {
        if (proc && proc->state() != QProcess::NotRunning) {
            proc->kill();
        }
    }
    for (const QPointer<QProcess> &proc : procs) {
        if (proc && proc->state() != QProcess::NotRunning) {
            proc->waitForFinished(250);
        }
    }
}

// ---------------------------------------------------------------------------- V1.6–V1.7 sequences
namespace {
QString sequenceNameFromMap(const QMap<QUuid, QString> &seqs, const QUuid &uuid)
{
    if (!pCore || !pCore->projectItemModel()) {
        return {};
    }
    const QString binId = seqs.value(uuid);
    const auto clip = binId.isEmpty() ? nullptr : pCore->projectItemModel()->getClipByBinID(binId);
    return clip ? clip->clipName() : QString();
}

QString sequenceName(const QUuid &uuid)
{
    if (!pCore || !pCore->projectItemModel()) {
        return {};
    }
    return sequenceNameFromMap(pCore->projectItemModel()->getAllSequenceClips(), uuid);
}

bool isQcMarker(const TimelineOperation &op)
{
    return op.type == QLatin1String("add_marker") && op.params.value(QStringLiteral("category")).toString().startsWith(QLatin1String("qc"));
}
} // namespace

void BangOnProIntegration::refreshProjectContext()
{
    if (!m_agentCentre || !pCore || !pCore->currentDoc() || !pCore->projectItemModel()) {
        if (m_agentCentre && !m_contextSignature.isEmpty()) {
            m_contextSignature.clear();
            m_agentCentre->setProjectContext(QString(), {}, -1);
        }
        return;
    }
    const ProjectIdentity id = currentProjectIdentity();
    if (!id.projectUuid.isEmpty() && id.projectUuid != m_activeProjectUuid) {
        // Safety net in case a document was swapped outside docOpened.
        onProjectOpened(pCore->currentDoc());
        return;
    }
    const QMap<QUuid, QString> seqs = pCore->projectItemModel()->getAllSequenceClips();
    const QUuid current = pCore->currentTimelineId();
    QStringList names;
    names.reserve(seqs.size());
    int currentIndex = -1;
    for (auto it = seqs.constBegin(); it != seqs.constEnd(); ++it) {
        if (it.key() == current) {
            currentIndex = names.size();
        }
        names << sequenceNameFromMap(seqs, it.key());
    }
    const QString signature =
        id.projectUuid + QLatin1Char('|') + id.projectName + QLatin1Char('|') + names.join(QLatin1Char('|')) + QLatin1Char('|') + QString::number(currentIndex);
    if (signature != m_contextSignature) {
        m_contextSignature = signature;
        m_agentCentre->setProjectContext(id.projectName, names, currentIndex);
    }
}

void BangOnProIntegration::createSequenceFromFootage()
{
    if (!pCore || !pCore->currentDoc() || !pCore->bin() || !pCore->window()) {
        return;
    }
    // Footage selected in the Project Bin goes into the new sequence, in Bin order (Premiere's "New Sequence From Clip").
    std::vector<QString> selected;
    for (const QString &id : pCore->bin()->selectedClipsIds()) {
        const auto clip = pCore->projectItemModel()->getClipByBinID(id);
        if (clip && clip->clipType() != ClipType::Timeline && !clip->clipUrl().isEmpty()) {
            selected.push_back(id);
        }
    }
    const QUuid before = pCore->currentTimelineId();
    pCore->bin()->buildSequenceClip(); // Kdenlive's own named-sequence dialog; it opens the new sequence
    const QUuid created = pCore->currentTimelineId();
    if (created == before || created.isNull()) {
        return; // cancelled
    }
    if (!selected.empty()) {
        if (TimelineWidget *tw = pCore->window()->getCurrentTimeline(); tw && tw->controller()) {
            const std::shared_ptr<TimelineItemModel> tl = pCore->projectManager()->getTimeline();
            const QList<int> videoTracks = tl ? tl->getTracksIds(false) : QList<int>();
            int pos = 0;
            for (const QString &id : selected) {
                const int tid = videoTracks.isEmpty() ? tw->controller()->activeTrack() : videoTracks.first();
                const int clipId = tw->controller()->insertClip(tid, pos, id, true, true, true);
                if (clipId > -1 && tl) {
                    pos += tl->getClipPlaytime(clipId);
                }
            }
        }
    }
    refreshProjectContext();
    m_aiWidget->appendActivityLog(QStringLiteral("project"),
                                  QStringLiteral("New sequence: %1 (%2 clip(s) placed)").arg(sequenceName(created)).arg(selected.size()));
}

QStringList BangOnProIntegration::collectSequenceMediaPaths() const
{
    // The footage the editor put in the open sequence, in timeline order (V1.5 always sent the whole Project Bin).
    QStringList paths;
    if (!pCore || !pCore->projectManager() || !pCore->projectItemModel()) {
        return paths;
    }
    const std::shared_ptr<TimelineItemModel> tl = pCore->projectManager()->getTimeline();
    if (!tl) {
        return paths;
    }
    QVector<QPair<int, QString>> ordered;
    for (bool audio : {false, true}) {
        for (int tid : tl->getTracksIds(audio)) {
            for (int item : tl->getItemsInRange(tid, 0, -1, false)) {
                const QString binId = tl->getClipBinId(item);
                const auto clip = binId.isEmpty() ? nullptr : pCore->projectItemModel()->getClipByBinID(binId);
                if (!clip || clip->clipType() == ClipType::Timeline || clip->clipType() == ClipType::Text || clip->clipType() == ClipType::Color) {
                    continue;
                }
                const QString url = clip->clipUrl();
                if (!url.isEmpty() && QFileInfo::exists(url)) {
                    ordered.append({tl->getClipPosition(item), url});
                }
            }
        }
    }
    std::sort(ordered.begin(), ordered.end(), [](const auto &a, const auto &b) { return a.first < b.first; });
    for (const auto &e : std::as_const(ordered)) {
        if (!paths.contains(e.second)) {
            paths << e.second;
        }
    }
    return paths;
}

void BangOnProIntegration::openEditedSequence(const QString &jobDir)
{
    if (!pCore || !pCore->currentDoc() || !pCore->projectItemModel() || !pCore->projectManager()) {
        m_agentCentre->showTaskProblem(QStringLiteral("Open a project first"),
                                       QStringLiteral("The edited sequence is added to the open project, next to your original."));
        return;
    }
    QString err;
    const QJsonObject job = readJsonFile(QDir(jobDir).filePath(QStringLiteral("job.json")), &err);
    if (job.isEmpty()) {
        m_agentCentre->showTaskProblem(QStringLiteral("Production metadata is missing"), QStringLiteral("Could not read job.json for this production."), err);
        return;
    }
    QString jobProjectUuid;
    if (!jobBelongsToActiveProject(job, &jobProjectUuid)) {
        m_agentCentre->showTaskProblem(
            QStringLiteral("Blocked cross-project edit"),
            QStringLiteral("This production belongs to a different project (%1). Open that project before adding its edited sequence.")
                .arg(jobProjectUuid.isEmpty() ? QStringLiteral("unknown") : jobProjectUuid));
        m_aiWidget->appendActivityLog(
            QStringLiteral("department"),
            QStringLiteral("Blocked openEditedSequence: job project '%1' != active project '%2'").arg(jobProjectUuid, m_activeProjectUuid),
            QStringLiteral("WARN"));
        return;
    }
    const QString cmdsPath = job.value(QStringLiteral("artifacts")).toObject().value(QStringLiteral("timeline_commands_json")).toString();
    if (!isPathInsideDirectory(cmdsPath, jobDir)) {
        m_agentCentre->showTaskProblem(QStringLiteral("Blocked unsafe artifact path"),
                                       QStringLiteral("The timeline commands file lies outside the production folder."), cmdsPath);
        return;
    }
    QFile f(cmdsPath);
    if (!f.open(QIODevice::ReadOnly)) {
        m_agentCentre->showTaskProblem(QStringLiteral("The edit is not available"),
                                       QStringLiteral("Its files were moved or deleted. Run the production again."), cmdsPath);
        return;
    }
    const QByteArray raw = f.readAll();
    const QString key = jobDir + QLatin1Char('|') + QString::fromLatin1(QCryptographicHash::hash(raw, QCryptographicHash::Sha1).toHex().left(12));
    const QMap<QUuid, QString> seqs = pCore->projectItemModel()->getAllSequenceClips();
    // Already in this project (including hydrated entries after project reopen): show it instead of duplicating.
    if (m_editedSequences.contains(key)) {
        const QUuid uuid(m_editedSequences.value(key));
        if (seqs.contains(uuid)) {
            pCore->projectManager()->openTimeline(seqs.value(uuid), -1, uuid);
            refreshProjectContext();
            return;
        }
    }
    const QJsonObject cmds = QJsonDocument::fromJson(raw).object();
    // Name it after the sequence the editor started from; later versions of the same production are v2, v3…
    QString base = sequenceName(pCore->currentTimelineId());
    if (base.isEmpty() || base.contains(QStringLiteral(" · Edited"))) {
        base = base.section(QStringLiteral(" · Edited"), 0, 0);
    }
    if (base.isEmpty()) {
        base = job.value(QStringLiteral("options")).toObject().value(QStringLiteral("episode_title")).toString(QStringLiteral("Sequence"));
    }
    QStringList existing;
    for (const QUuid &u : seqs.keys()) {
        existing << sequenceName(u);
    }
    QString name = QStringLiteral("%1 · Edited").arg(base);
    for (int v = 2; existing.contains(name); ++v) {
        name = QStringLiteral("%1 · Edited v%2").arg(base).arg(v);
    }

    TimelineCommandBatch merged;
    merged.requestId = QStringLiteral("dept-seq-") + QUuid::createUuid().toString(QUuid::WithoutBraces);
    merged.projectId = m_engine->activeProjectId().isEmpty() ? cmds.value(QStringLiteral("project_id")).toString(QStringLiteral("active_project"))
                                                             : m_engine->activeProjectId();
    // Not a sequence UUID on purpose: every operation resolves to the sequence the first operation creates
    // (Kdenlive opens it as the current timeline). The editor's original sequence is never addressed.
    merged.sequenceId = QStringLiteral("bop_edited_sequence");
    merged.agentId = QStringLiteral("executive");
    merged.agentRole = QStringLiteral("ai_production_department");
    merged.requiresApproval = false; // the editor's click is the approval; one undoable transaction
    merged.expectedRevision = m_engine->currentRevision();
    merged.transactionLabel = QStringLiteral("Bang On Pro: %1").arg(name);
    TimelineOperation create;
    bool haveCreate = false;
    int videoTracks = 0;
    int audioTracks = 0;
    QVector<TimelineOperation> rest;
    QHash<QString, TimelineOperation> heldCameraOps;
    QJsonArray qcMarkers;
    const QJsonObject batchAgents = cmds.value(QStringLiteral("batch_agents")).toObject();
    for (const QJsonValue &bv : cmds.value(QStringLiteral("batches")).toArray()) {
        TimelineCommandBatch b;
        QString berr;
        if (!TimelineCommandEngine::batchFromJson(bv.toObject(), &b, &berr)) {
            m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Skipped invalid batch: %1").arg(berr), QStringLiteral("WARN"));
            continue;
        }
        const QString agent = batchAgents.value(b.requestId).toString(b.agentId);
        const QJsonArray rawOps = bv.toObject().value(QStringLiteral("operations")).toArray();
        for (int oi = 0; oi < b.operations.size(); ++oi) {
            TimelineOperation op = b.operations.at(oi);
            if (op.type == QLatin1String("export_sequence")) {
                continue; // exporting is the editor's own action
            }
            if (op.type == QLatin1String("sync_clips")) {
                // The agents already applied each camera's sync offset to its placements' in/out points; replaying
                // the grouping step here would address clips that do not exist yet (and needs two cameras).
                continue;
            }
            if (op.type == QLatin1String("create_sequence") && !haveCreate) {
                create = op;
                haveCreate = true;
                continue;
            }
            if (op.type == QLatin1String("create_track")) {
                // Tracks are created with the sequence; V1/V2/A1/A2 then resolve to them by position.
                (op.params.value(QStringLiteral("track_type")).toString() == QLatin1String("audio") ? audioTracks : videoTracks) += 1;
                continue;
            }
            if (isQcMarker(op)) {
                qcMarkers.append(rawOps.at(oi));
                if (!m_agentCentre->qcMarkersVisible()) {
                    continue; // clean timeline by default; the notes stay in the panel's review list
                }
            }
            op.rationale = QStringLiteral("[%1] %2").arg(agent, op.rationale);
            if (op.type == QLatin1String("select_camera") && !op.clipId.trimmed().isEmpty()) {
                // The agents record the camera choice before the shot exists; native Kdenlive needs the clip first.
                heldCameraOps.insert(op.clipId.trimmed(), op);
                continue;
            }
            rest.append(op);
            if (op.type == QLatin1String("place_clip") && op.params.value(QStringLiteral("stream")).toString() != QLatin1String("audio")) {
                // Revised drafts name placements shot_0001_v / shot_0001_a; the camera choice belongs to the picture.
                const QString placed = op.clipId.trimmed();
                const QString shot = placed.endsWith(QLatin1String("_v")) ? placed.chopped(2) : placed;
                if (heldCameraOps.contains(shot)) {
                    TimelineOperation cam = heldCameraOps.take(shot);
                    cam.clipId = placed;
                    rest.append(cam);
                }
            }
        }
    }
    if (!haveCreate) {
        create.opId = QStringLiteral("op_create_edited_sequence");
        create.type = QStringLiteral("create_sequence");
    }
    create.params.insert(QStringLiteral("name"), name);
    create.params.insert(QStringLiteral("video_tracks"), qMax(1, videoTracks));
    create.params.insert(QStringLiteral("audio_tracks"), qMax(1, audioTracks));
    merged.operations = {create};
    merged.operations += rest;
    const TimelineCommandResponse resp = m_engine->executeBatch(merged);
    if (resp.status != CommandStatus::Ok) {
        QStringList failed;
        for (const TimelineOperationResult &r : resp.results) {
            if (!r.applied) {
                failed << QStringLiteral("%1: %2").arg(r.opId, r.message);
            }
        }
        qWarning("Bang On Pro: edited sequence failed: %s | %s", qPrintable(resp.errorMessage), qPrintable(failed.join(QStringLiteral(" | "))));
        m_agentCentre->showTaskProblem(QStringLiteral("The edited sequence could not be created"),
                                       QStringLiteral("Your original sequence was not changed. Export diagnostics and send them to the team."),
                                       resp.errorMessage + QStringLiteral("\n") + failed.join(QLatin1Char('\n')));
        m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Edited sequence failed: %1").arg(resp.errorMessage),
                                      QStringLiteral("ERROR"));
        return;
    }
    const QUuid createdUuid = pCore->currentTimelineId();
    m_editedSequences.insert(key, createdUuid.toString());
    if (KdenliveDoc *doc = pCore->currentDoc()) {
        // Kept with the project so the overlay toggle and "already added" work after reopening it.
        doc->setDocumentProperty(QStringLiteral("bangonpro.edited.%1").arg(createdUuid.toString(QUuid::WithoutBraces)), key);
        doc->setDocumentProperty(QStringLiteral("bangonpro.qc.%1").arg(createdUuid.toString(QUuid::WithoutBraces)),
                                 QString::fromUtf8(QJsonDocument(qcMarkers).toJson(QJsonDocument::Compact)));
    }
    refreshProjectContext();
    m_aiWidget->appendActivityLog(QStringLiteral("department"),
                                  QStringLiteral("Edited sequence '%1' added to the project: %2 operations in one undoable step (%3 QC markers %4)")
                                      .arg(name)
                                      .arg(merged.operations.size())
                                      .arg(qcMarkers.size())
                                      .arg(m_agentCentre->qcMarkersVisible() ? QStringLiteral("shown") : QStringLiteral("kept off the timeline")));
}

void BangOnProIntegration::setQcMarkersVisible(const QString &jobDir, bool visible)
{
    Q_UNUSED(jobDir)
    if (!pCore || !pCore->currentDoc() || !pCore->projectManager()) {
        return;
    }
    const QUuid uuid = pCore->currentTimelineId();
    const QString stored = pCore->currentDoc()->getDocumentProperty(QStringLiteral("bangonpro.qc.%1").arg(uuid.toString(QUuid::WithoutBraces)));
    const std::shared_ptr<TimelineItemModel> tl = pCore->projectManager()->getTimeline();
    if (stored.isEmpty() || !tl) {
        m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Open the edited sequence to show or hide its quality-check markers"));
        return;
    }
    const QJsonArray ops = QJsonDocument::fromJson(stored.toUtf8()).array();
    const std::shared_ptr<MarkerListModel> guides = tl->getGuideModel();
    QMap<GenTime, QString> toAdd;
    int removed = 0;
    for (const QJsonValue &v : ops) {
        const QJsonObject op = v.toObject();
        const QJsonObject fr = op.value(QStringLiteral("frame_range")).toObject();
        const double fps = qMax(1.0, fr.value(QStringLiteral("fps_num")).toDouble(25.0) / qMax(1.0, fr.value(QStringLiteral("fps_den")).toDouble(1.0)));
        const GenTime pos(fr.value(QStringLiteral("timeline_start")).toInt(), fps);
        const QString label = op.value(QStringLiteral("params")).toObject().value(QStringLiteral("label")).toString();
        bool exists = false;
        const CommentedTime marker = guides->getMarker(pos, &exists);
        if (visible && !(exists && marker.comment() == label)) {
            toAdd.insert(pos, toAdd.contains(pos) ? toAdd.value(pos) + QStringLiteral(" · ") + label : label);
        } else if (!visible && exists && marker.comment().startsWith(QStringLiteral("QC"))) {
            removed += guides->removeMarker(pos) ? 1 : 0; // each removal is one native undo step
        }
    }
    if (visible && !toAdd.isEmpty()) {
        guides->addMarkers(toAdd); // one native undo step
    }
    m_aiWidget->appendActivityLog(QStringLiteral("department"), visible ? QStringLiteral("Showing %1 quality-check marker(s)").arg(toAdd.size())
                                                                        : QStringLiteral("Hid %1 quality-check marker(s)").arg(removed));
}

void BangOnProIntegration::openReferenceStyles()
{
    auto *dlg = new ReferenceStyleDialog(m_mainWindow);
    dlg->setAttribute(Qt::WA_DeleteOnClose);
    QPointer<ReferenceStyleDialog> guard(dlg);
    const QString profilesDir = customProfilesDir();
    QDir().mkpath(profilesDir);

    auto refreshSaved = [this, guard, profilesDir]() {
        runQuickDepartmentCommand({QStringLiteral("style"), QStringLiteral("list"), QStringLiteral("--out"), profilesDir}, [guard](const QJsonObject &res) {
            if (!guard) {
                return;
            }
            if (!res.value(QStringLiteral("error")).toString().isEmpty()) {
                guard->setStatusMessage(res.value(QStringLiteral("error")).toString(), true);
                return;
            }
            guard->setSavedStyles(res.value(QStringLiteral("styles")).toArray());
        });
    };

    connect(dlg, &ReferenceStyleDialog::analyzeRequested, this, [this, guard, profilesDir](const QStringList &videos, const QString &styleName) {
        if (!guard || videos.isEmpty()) {
            return;
        }
        guard->setBusy(true, QStringLiteral("Analysing %1 reference video(s) locally…").arg(videos.size()));
        QStringList args{QStringLiteral("style"), QStringLiteral("analyze")};
        args += videos;
        if (!styleName.isEmpty()) {
            args << QStringLiteral("--name") << styleName;
        }
        args << QStringLiteral("--out") << profilesDir;
        runQuickDepartmentCommand(args, [this, guard](const QJsonObject &res) {
            if (!guard) {
                return;
            }
            guard->setBusy(false);
            const QString err = res.value(QStringLiteral("error")).toString();
            if (!err.isEmpty()) {
                guard->setStatusMessage(err, true);
                m_aiWidget->appendActivityLog(QStringLiteral("styles"), QStringLiteral("Reference analysis failed: %1").arg(err), QStringLiteral("WARN"));
                return;
            }
            guard->setAnalysisResult(res);
            m_aiWidget->appendActivityLog(QStringLiteral("styles"),
                                          QStringLiteral("Analysed reference style '%1'").arg(res.value(QStringLiteral("display_name")).toString()));
        });
    });

    connect(dlg, &ReferenceStyleDialog::saveProfileRequested, this, [this, guard, profilesDir, refreshSaved](const QJsonObject &profileJson) {
        if (!guard || profileJson.isEmpty()) {
            return;
        }
        const QString tmpPath =
            QDir(QDir::tempPath()).filePath(QStringLiteral("bop_ref_style_%1.json").arg(QUuid::createUuid().toString(QUuid::WithoutBraces)));
        if (!writeFileAtomic(tmpPath, QJsonDocument(profileJson).toJson(QJsonDocument::Indented))) {
            guard->setStatusMessage(QStringLiteral("Could not write temporary style file"), true);
            return;
        }

        guard->setBusy(true, QStringLiteral("Validating and saving editing style…"));
        const QStringList args{QStringLiteral("style"), QStringLiteral("save"), tmpPath, QStringLiteral("--out"), profilesDir};
        runQuickDepartmentCommand(args, [this, guard, tmpPath, refreshSaved](const QJsonObject &res) {
            QFile::remove(tmpPath);
            if (!guard) {
                return;
            }
            guard->setBusy(false);
            const QString err = res.value(QStringLiteral("error")).toString();
            if (!err.isEmpty()) {
                guard->setStatusMessage(err, true);
                return;
            }
            const QString pid = res.value(QStringLiteral("profile_id")).toString();
            const QString name = res.value(QStringLiteral("display_name")).toString(pid);
            if (!pid.isEmpty() && m_agentCentre) {
                m_agentCentre->addCustomProfile(pid, name);
                m_agentCentre->selectProfile(pid);
            }
            refreshSaved();
            guard->setStatusMessage(QStringLiteral("Saved and selected style '%1'.").arg(name));
            m_aiWidget->appendActivityLog(QStringLiteral("styles"), QStringLiteral("Saved reference editing style '%1' (%2)").arg(name, pid));
        });
    });

    connect(dlg, &ReferenceStyleDialog::selectSavedStyleRequested, this, [this](const QString &profileId, const QString &displayName) {
        if (m_agentCentre && !profileId.isEmpty()) {
            m_agentCentre->addCustomProfile(profileId, displayName);
            m_agentCentre->selectProfile(profileId);
            m_aiWidget->appendActivityLog(QStringLiteral("styles"), QStringLiteral("Selected editing style '%1'").arg(displayName));
        }
    });

    connect(dlg, &ReferenceStyleDialog::deleteSavedStyleRequested, this, [this, guard, profilesDir, refreshSaved](const QString &profileId) {
        if (!guard || profileId.isEmpty()) {
            return;
        }
        guard->setBusy(true, QStringLiteral("Removing style…"));
        runQuickDepartmentCommand({QStringLiteral("style"), QStringLiteral("delete"), profileId, QStringLiteral("--out"), profilesDir},
                                  [this, guard, profileId, refreshSaved](const QJsonObject &res) {
                                      if (!guard) {
                                          return;
                                      }
                                      guard->setBusy(false);
                                      if (res.value(QStringLiteral("deleted")).toBool()) {
                                          if (m_agentCentre) {
                                              m_agentCentre->removeCustomProfile(profileId);
                                          }
                                          guard->setStatusMessage(QStringLiteral("Removed style '%1'.").arg(profileId));
                                          refreshSaved();
                                      } else {
                                          guard->setStatusMessage(QStringLiteral("Could not remove style '%1'.").arg(profileId), true);
                                      }
                                  });
    });

    refreshSaved();
    dlg->show();
    dlg->raise();
    dlg->activateWindow();
}

void BangOnProIntegration::stageDepartmentDraft(const QString &jobDir)
{
    QString err;
    const QJsonObject job = readJsonFile(QDir(jobDir).filePath(QStringLiteral("job.json")), &err);
    if (job.isEmpty()) {
        m_aiWidget->appendActivityLog(QStringLiteral("department"), err, QStringLiteral("ERROR"));
        return;
    }
    QString jobProjectUuid;
    if (!jobBelongsToActiveProject(job, &jobProjectUuid)) {
        if (m_agentCentre) {
            m_agentCentre->showTaskProblem(QStringLiteral("Blocked cross-project draft staging"),
                                           QStringLiteral("This production belongs to another project (%1). Open that project first.")
                                               .arg(jobProjectUuid.isEmpty() ? QStringLiteral("unknown") : jobProjectUuid));
        }
        return;
    }
    const QString cmdsPath = job.value(QStringLiteral("artifacts")).toObject().value(QStringLiteral("timeline_commands_json")).toString();
    if (!isPathInsideDirectory(cmdsPath, jobDir)) {
        m_aiWidget->appendActivityLog(QStringLiteral("department"), QStringLiteral("Blocked unsafe timeline_commands_json path outside job folder"),
                                      QStringLiteral("ERROR"));
        return;
    }
    const QJsonObject cmds = readJsonFile(cmdsPath, &err);
    if (cmds.isEmpty()) {
        m_aiWidget->appendActivityLog(QStringLiteral("department"), err, QStringLiteral("ERROR"));
        return;
    }
    // The isolated draft becomes ONE reviewable proposal at the revision captured when the job started;
    // the engine's stale-proposal detection protects any manual edits made since.
    const QJsonObject projMeta = job.value(QStringLiteral("project")).toObject();
    TimelineCommandBatch merged;
    merged.requestId = QStringLiteral("dept-") + QUuid::createUuid().toString(QUuid::WithoutBraces);
    merged.projectId = !jobProjectUuid.isEmpty() ? jobProjectUuid : m_activeProjectUuid;
    merged.sequenceId = projMeta.value(QStringLiteral("sequence_uuid")).toString(projMeta.value(QStringLiteral("sequence_id")).toString(m_deptBaseSequenceId));
    merged.agentId = QStringLiteral("executive");
    merged.agentRole = QStringLiteral("ai_production_department");
    merged.requiresApproval = true;
    merged.expectedRevision = static_cast<qint64>(projMeta.value(QStringLiteral("base_revision")).toDouble(double(m_deptBaseRevision)));
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
    m_presetWidget = new PremierePresetBrowser(m_mainWindow, true);
    m_presetDock = new QDockWidget(QStringLiteral("Premiere Presets"), m_mainWindow);
    m_presetDock->setObjectName(QString::fromLatin1(kPremiereBrowserDockName));
    m_presetDock->setWidget(makeScrollableDockContent(m_presetWidget, m_presetDock));
    m_mainWindow->addDockWidget(Qt::LeftDockWidgetArea, m_presetDock);
    connect(m_presetDock, &QDockWidget::visibilityChanged, this, [this](bool visible) {
        if (visible) {
            ensurePresetsLoaded();
        }
    });
    m_presetWidget->setDragEffectIdResolver([this](const QString &presetId) -> QString {
        if (!m_presetWidget) {
            return {};
        }
        const QJsonObject p = m_presetWidget->presetJsonById(presetId);
        const QString xml = p.value(QStringLiteral("kdenlive_effect_xml")).toString();
        if (xml.isEmpty()) {
            return {};
        }
        return installEffectXmlOnly(presetId, p.value(QStringLiteral("name")).toString(presetId), xml);
    });
    connect(m_presetWidget, &PremierePresetBrowser::importArchiveRequested, this, &BangOnProIntegration::importPresetArchive);
    connect(m_presetWidget, &PremierePresetBrowser::applyPresetRequested, this, &BangOnProIntegration::applyPreset);
    connect(m_presetWidget, &PremierePresetBrowser::customStackApplied, this, &BangOnProIntegration::applyCustomStack);
}

void BangOnProIntegration::setupUnifiedEffectsPanel()
{
    auto *mw = qobject_cast<MainWindow *>(m_mainWindow);
    if (!mw || !mw->unifiedEffectsPanel()) {
        return;
    }
    UnifiedEffectsPanel *panel = mw->unifiedEffectsPanel();
    panel->setPresetDragIdResolver([this](const QString &presetId) -> QString {
        if (!m_presetWidget) {
            return {};
        }
        ensurePresetsLoaded();
        const QJsonObject p = m_presetWidget->presetJsonById(presetId);
        const QString xml = p.value(QStringLiteral("kdenlive_effect_xml")).toString();
        if (xml.isEmpty()) {
            return {};
        }
        return installEffectXmlOnly(presetId, p.value(QStringLiteral("name")).toString(presetId), xml);
    });
    connect(panel, &UnifiedEffectsPanel::applyPremierePresetRequested, this, [this](const QString &presetId) {
        ensurePresetsLoaded();
        applyPreset(presetId, QJsonArray());
    });
    if (QDockWidget *effDock = m_mainWindow->findChild<QDockWidget *>(QStringLiteral("effect_list"))) {
        connect(effDock, &QDockWidget::visibilityChanged, this, [this](bool visible) {
            if (visible) {
                ensurePresetsLoaded();
            }
        });
    }
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
    // Theme was already applied once in main.cpp; avoid redundant full-tree QStyle/QSS repolish at startup.
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

    QAction *newSeqAct = menu->addAction(QStringLiteral("New Sequence from Selected Clips…"));
    newSeqAct->setObjectName(QStringLiteral("bangonpro_new_sequence"));
    connect(newSeqAct, &QAction::triggered, this, &BangOnProIntegration::createSequenceFromFootage);

    QAction *refStylesAct = menu->addAction(QStringLiteral("Reference Video Editing Styles…"));
    refStylesAct->setObjectName(QStringLiteral("bangonpro_reference_styles"));
    connect(refStylesAct, &QAction::triggered, this, &BangOnProIntegration::openReferenceStyles);

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
            if (writeFileAtomic(target, QJsonDocument(profileJson).toJson(QJsonDocument::Indented))) {
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
    const QString cacheDir = storageRoot() + QStringLiteral("/cache");
    const QFileInfo fi(cacheDir);
    if (fi.exists() && fi.isDir() && !fi.isSymLink() && isPathInsideDirectory(cacheDir, storageRoot())) {
        QDir(cacheDir).removeRecursively();
        QDir().mkpath(cacheDir);
    }
    if (m_aiWidget) {
        m_aiWidget->setStorageSummaryText(QStringLiteral("Local AI • Cache cleared"));
        m_aiWidget->appendActivityLog(QStringLiteral("storage"), QStringLiteral("Cleared Bang On Pro proxy & temporary caches (source media untouched)"));
    }
}

void BangOnProIntegration::ensurePresetsLoaded()
{
    if (m_presetsLoaded) {
        return;
    }
    const QString report = dataDir() + QStringLiteral("/presets/compatibility_report.json");
    if (QFileInfo::exists(report)) {
        loadPresetReportIfPresent();
    } else if (!m_builtinPresetsSpawned) {
        loadBuiltinStarterPresetsIfNeeded();
    }
}

void BangOnProIntegration::loadPresetReportIfPresent()
{
    const QString report = dataDir() + QStringLiteral("/presets/compatibility_report.json");
    if (QFileInfo::exists(report)) {
        QString err;
        if (m_presetWidget && m_presetWidget->loadReportFile(report, &err)) {
            m_presetsLoaded = true;
            if (auto *mw = qobject_cast<MainWindow *>(m_mainWindow); mw && mw->unifiedEffectsPanel()) {
                mw->unifiedEffectsPanel()->setPremierePresets(m_presetWidget->presets());
            }
        } else if (!err.isEmpty()) {
            qWarning("Bang On Pro: failed to load preset report: %s", qPrintable(err));
        }
    }
}

void BangOnProIntegration::loadBuiltinStarterPresetsIfNeeded()
{
    const QString report = dataDir() + QStringLiteral("/presets/compatibility_report.json");
    if (QFileInfo::exists(report)) {
        loadPresetReportIfPresent();
        return;
    }
    if (m_builtinPresetsSpawned) {
        return;
    }
    m_builtinPresetsSpawned = true;
    const QString outDir = dataDir() + QStringLiteral("/presets");
    QDir().mkpath(outDir);
    QProcess *proc = startPython({QStringLiteral("-m"), QStringLiteral("bangonpro.premiere_compat.cli"), QStringLiteral("builtin-library"),
                                  QStringLiteral("--output"), report});
    if (!proc) {
        m_builtinPresetsSpawned = false;
        return;
    }
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, proc](int code, QProcess::ExitStatus status) {
        proc->deleteLater();
        if (status == QProcess::NormalExit && code == 0) {
            loadPresetReportIfPresent();
        } else {
            m_builtinPresetsSpawned = false;
        }
    });
}

void BangOnProIntegration::trackProcess(QProcess *proc)
{
    if (!proc) {
        return;
    }
    m_trackedProcesses.append(proc);
    connect(proc, &QObject::destroyed, this, [this, proc]() { m_trackedProcesses.removeAll(proc); });
}

QProcess *BangOnProIntegration::startPython(const QStringList &args)
{
    const QString root = locatePythonPackageRoot();
    if (root.isEmpty()) {
        return nullptr;
    }
    auto *proc = new QProcess(this);
    // Strict allowlist environment: avoid leaking host secrets, host PYTHONPATH, or DYLD_LIBRARY_PATH into Python child processes.
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
    // Isolate from user site-packages and unrelated host PYTHONPATH entries.
    clean.insert(QStringLiteral("PYTHONNOUSERSITE"), QStringLiteral("1"));
    clean.insert(QStringLiteral("PYTHONPATH"), root);
    proc->setProcessEnvironment(clean);
    proc->setWorkingDirectory(root);
    QStringList finalArgs;
    finalArgs.reserve(args.size() + 1);
    finalArgs << QStringLiteral("-s");
    finalArgs << args;
    // Argument vector only — never a shell, so user-supplied paths cannot inject commands.
    trackProcess(proc);
    proc->start(pythonExecutable(), finalArgs);
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
    writeFileAtomic(stateJsonPath, QJsonDocument(liveState).toJson(QJsonDocument::Indented));

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
    if (m_transcriptEditProcess) {
        m_aiWidget->appendActivityLog(QStringLiteral("transcript"), QStringLiteral("Transcript edit application is already running"), QStringLiteral("WARN"));
        return;
    }
    const QString editedPath = dataDir() + QStringLiteral("/edited_transcript.json");
    if (!writeFileAtomic(editedPath, QJsonDocument(editedTranscriptJson).toJson(QJsonDocument::Indented))) {
        return;
    }

    const qint64 capturedRev = m_engine->currentRevision();
    const QString projectUuidAtStart = m_activeProjectUuid;
    QStringList args{QStringLiteral("-m"), QStringLiteral("bangonpro.ai_engine.cli"), QStringLiteral("--apply-transcript-edits"), editedPath};
    if (!m_lastTranscriptPath.isEmpty() && QFileInfo::exists(m_lastTranscriptPath)) {
        args << QStringLiteral("--original-transcript") << m_lastTranscriptPath;
    }
    QProcess *proc = startPython(args);
    if (!proc) {
        return;
    }
    m_transcriptEditProcess = proc;
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this,
            [this, proc, capturedRev, projectUuidAtStart](int code, QProcess::ExitStatus status) {
                m_transcriptEditProcess.clear();
                proc->deleteLater();
                if (status != QProcess::NormalExit || code != 0 || projectUuidAtStart != m_activeProjectUuid) {
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
    if (!m_rotoWidget->canvasWidget()->frameImage().save(framePath) || !writeFileAtomic(sessionPath, QJsonDocument(session).toJson(QJsonDocument::Compact))) {
        m_aiWidget->appendActivityLog(QStringLiteral("roto-studio"), QStringLiteral("Cannot write the segmentation inputs to %1").arg(dir),
                                      QStringLiteral("ERROR"));
        return;
    }
    QProcess *proc = startPython({QStringLiteral("-m"), QStringLiteral("bangonpro.roto_studio.cli"), QStringLiteral("segment"), QStringLiteral("--image"),
                                  framePath, QStringLiteral("--session"), sessionPath, QStringLiteral("--output-dir"), dir});
    if (!proc) {
        m_aiWidget->appendActivityLog(QStringLiteral("roto-studio"), QStringLiteral("Cannot start segmentation: bangonpro package not found"),
                                      QStringLiteral("ERROR"));
        return;
    }
    m_rotoProcess = proc;
    const QString maskId = aiMask.value(QStringLiteral("id")).toString();
    const QString projectUuidAtStart = m_activeProjectUuid;
    m_aiWidget->appendActivityLog(QStringLiteral("roto-studio"), QStringLiteral("AI segmentation started (%1)").arg(maskId));
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this,
            [this, proc, maskId, dir, projectUuidAtStart](int code, QProcess::ExitStatus status) {
                m_rotoProcess.clear();
                proc->deleteLater();
                if (projectUuidAtStart != m_activeProjectUuid) {
                    return;
                }
                const QJsonObject out = QJsonDocument::fromJson(proc->readAllStandardOutput()).object();
                const QJsonArray frames = out.value(QStringLiteral("matte_frames")).toArray();
                if (status != QProcess::NormalExit || code != 0 || frames.isEmpty()) {
                    m_aiWidget->appendActivityLog(
                        QStringLiteral("roto-studio"),
                        QStringLiteral("AI segmentation failed (exit=%1): %2").arg(code).arg(QString::fromUtf8(proc->readAllStandardError().right(600))),
                        QStringLiteral("ERROR"));
                    return;
                }
                const QString mattePath = frames.first().toString();
                if (!isPathInsideDirectory(mattePath, dir)) {
                    m_aiWidget->appendActivityLog(QStringLiteral("roto-studio"),
                                                  QStringLiteral("Rejected unsafe segmentation matte path outside output directory"), QStringLiteral("ERROR"));
                    return;
                }
                m_rotoMattePath = mattePath;
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
    if (m_compositeProcess) {
        m_aiWidget->appendActivityLog(QStringLiteral("artcraft-2d"), QStringLiteral("2D composite render is already running"), QStringLiteral("WARN"));
        return;
    }
    const QString outDir = dataDir() + QStringLiteral("/artcraft_plates");
    QDir().mkpath(outDir);
    const QString stamp = QDateTime::currentDateTimeUtc().toString(QStringLiteral("yyyyMMdd-HHmmss"));
    const QString canvasPath = outDir + QStringLiteral("/canvas_") + stamp + QStringLiteral(".json");
    const QString pngPath = outDir + QStringLiteral("/plate_") + stamp + QStringLiteral(".png");

    if (!writeFileAtomic(canvasPath, QJsonDocument(canvasJson).toJson(QJsonDocument::Indented))) {
        return;
    }

    QProcess *proc = startPython({QStringLiteral("-m"), QStringLiteral("bangonpro.roto_studio.cli"), QStringLiteral("compose-2d"),
                                  QStringLiteral("--canvas-json"), canvasPath, QStringLiteral("--output-png"), pngPath});
    if (!proc) {
        return;
    }
    m_compositeProcess = proc;
    const QString projectUuidAtStart = m_activeProjectUuid;
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this,
            [this, proc, pngPath, placeOnTimeline, projectUuidAtStart](int code, QProcess::ExitStatus status) {
                m_compositeProcess.clear();
                proc->deleteLater();
                if (status != QProcess::NormalExit || code != 0 || !QFileInfo::exists(pngPath) || projectUuidAtStart != m_activeProjectUuid) {
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
    if (m_storyboardProcess) {
        m_aiWidget->appendActivityLog(QStringLiteral("storyboard"), QStringLiteral("Storyboard export is already running"), QStringLiteral("WARN"));
        return;
    }
    const QString outDir = dataDir() + QStringLiteral("/storyboards/") + QDateTime::currentDateTimeUtc().toString(QStringLiteral("yyyyMMdd-HHmmss"));
    QDir().mkpath(outDir);
    const QString sbPath = outDir + QStringLiteral("/storyboard.json");
    if (!writeFileAtomic(sbPath, QJsonDocument(storyboardJson).toJson(QJsonDocument::Indented))) {
        return;
    }

    const qint64 capturedRev = m_engine->currentRevision();
    const QString projectUuidAtStart = m_activeProjectUuid;
    QProcess *proc = startPython({QStringLiteral("-m"), QStringLiteral("bangonpro.roto_studio.cli"), QStringLiteral("storyboard-export"),
                                  QStringLiteral("--storyboard-json"), sbPath, QStringLiteral("--output-dir"), outDir});
    if (!proc) {
        return;
    }
    m_storyboardProcess = proc;
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this,
            [this, proc, capturedRev, projectUuidAtStart](int code, QProcess::ExitStatus status) {
                m_storyboardProcess.clear();
                proc->deleteLater();
                if (status != QProcess::NormalExit || code != 0 || projectUuidAtStart != m_activeProjectUuid) {
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
    if (m_presetImportProcess) {
        if (m_aiWidget) {
            m_aiWidget->appendActivityLog(QStringLiteral("premiere-compat"), QStringLiteral("A Premiere preset import is already running"),
                                          QStringLiteral("WARN"));
        }
        return;
    }
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
    m_presetImportProcess = proc;
    if (m_aiWidget) {
        m_aiWidget->appendActivityLog(QStringLiteral("premiere-compat"), QStringLiteral("Importing Premiere preset archive: %1").arg(zipPath));
    }
    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, proc](int code, QProcess::ExitStatus status) {
        m_presetImportProcess.clear();
        proc->deleteLater();
        if (status == QProcess::NormalExit && code == 0) {
            m_presetsLoaded = false;
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

QString BangOnProIntegration::installEffectXmlOnly(const QString &fileSlug, const QString &effectDisplayName, const QString &xml)
{
    if (xml.isEmpty()) {
        return {};
    }
    const QString effDir = QStandardPaths::writableLocation(QStandardPaths::AppDataLocation) + QStringLiteral("/effects/");
    QDir().mkpath(effDir);
    const QString path = effDir + QStringLiteral("bangonpro_") + sanitizeFileSlug(fileSlug) + QStringLiteral(".xml");
    if (!writeFileAtomic(path, xml.toUtf8())) {
        return {};
    }
    if (m_effectList) {
        m_effectList->reloadCustomEffect(path);
    }
    return resolveInstalledEffectIdFromXml(xml, effectDisplayName);
}

void BangOnProIntegration::installAndApplyEffectXml(const QString &fileSlug, const QString &effectDisplayName, const QString &xml)
{
    const QString resolvedEffectId = installEffectXmlOnly(fileSlug, effectDisplayName, xml);
    if (resolvedEffectId.isEmpty()) {
        return;
    }
    auto *mw = qobject_cast<MainWindow *>(m_mainWindow);
    if (mw && mw->getCurrentTimeline() && mw->getCurrentTimeline()->controller()) {
        mw->getCurrentTimeline()->controller()->addEffectToClip(resolvedEffectId);
        mw->showEffectControls();
    }
}

void BangOnProIntegration::applyPreset(const QString &presetId, const QJsonArray &mltFilters)
{
    ensurePresetsLoaded();
    QJsonObject presetObj = m_presetWidget ? m_presetWidget->presetJsonById(presetId) : QJsonObject();
    if (presetObj.isEmpty()) {
        QString err;
        const QJsonObject report = readJsonFile(dataDir() + QStringLiteral("/presets/compatibility_report.json"), &err);
        for (const QJsonValue &pv : report.value(QStringLiteral("presets")).toArray()) {
            const QJsonObject p = pv.toObject();
            if (p.value(QStringLiteral("preset_id")).toString() == presetId) {
                presetObj = p;
                break;
            }
        }
    }
    if (!presetObj.isEmpty()) {
        const QString xml = presetObj.value(QStringLiteral("kdenlive_effect_xml")).toString();
        if (xml.isEmpty()) {
            qWarning("Bang On Pro: preset %s has no executable mapping (%s)", qPrintable(presetId),
                     qPrintable(presetObj.value(QStringLiteral("compatibility_status")).toString()));
            return;
        }
        installAndApplyEffectXml(presetId, presetObj.value(QStringLiteral("name")).toString(presetId), xml);
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
