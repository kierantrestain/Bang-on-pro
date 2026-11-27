/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "ModelManagerDialog.h"
#include "BangOnProTheme.h"

#include <QBrush>
#include <QColor>
#include <QDir>
#include <QFileDialog>
#include <QFrame>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QListWidgetItem>
#include <QProcess>
#include <QProcessEnvironment>
#include <QProgressBar>
#include <QPushButton>
#include <QTreeWidget>
#include <QTreeWidgetItem>
#include <QVBoxLayout>

namespace BangOnPro {

namespace {

constexpr int kColModel = 0;
constexpr int kColFamily = 1;
constexpr int kColPackage = 2;
constexpr int kColDisk = 3;
constexpr int kColStatus = 4;
constexpr int kColFit = 5;

constexpr int kModelIdRole = Qt::UserRole + 1;
constexpr int kOnDiskRole = Qt::UserRole + 2;

QProcessEnvironment buildSanitizedPythonEnvironment(const QString &pythonRoot)
{
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
        QStringLiteral("SSL_CERT_FILE"),
        QStringLiteral("SSL_CERT_DIR"),
        QStringLiteral("REQUESTS_CA_BUNDLE"),
        QStringLiteral("BANGONPRO_MODEL_DIR"),
        QStringLiteral("BANGONPRO_PYTHON"),
        QStringLiteral("BANGONPRO_PYTHONPATH"),
        QStringLiteral("HF_HOME"),
        QStringLiteral("XDG_CACHE_HOME"),
        QStringLiteral("XDG_DATA_HOME"),
        QStringLiteral("XDG_CONFIG_HOME"),
        QStringLiteral("LD_LIBRARY_PATH"),
    };
    for (const QString &key : allowedVars) {
        if (host.contains(key)) {
            clean.insert(key, host.value(key));
        }
    }
    clean.insert(QStringLiteral("PYTHONNOUSERSITE"), QStringLiteral("1"));
    if (!pythonRoot.isEmpty()) {
        clean.insert(QStringLiteral("PYTHONPATH"), pythonRoot);
    }
    return clean;
}

QString formatFamilyLabel(const QString &family)
{
    if (family == QStringLiteral("asr_whisper")) {
        return QStringLiteral("ASR (Whisper)");
    }
    if (family == QStringLiteral("segmentation_sam")) {
        return QStringLiteral("Roto (SAM)");
    }
    return family;
}

QString formatHonestStatus(const QString &rawStatus, bool pkgInstalled, bool onDisk)
{
    if (rawStatus == QStringLiteral("loaded")) {
        return QStringLiteral("Loaded (Neural Active)");
    }
    if (rawStatus == QStringLiteral("ready")) {
        return QStringLiteral("Ready (Neural)");
    }
    if (rawStatus == QStringLiteral("backend_missing")) {
        return QStringLiteral("Weights on Disk — Package Missing (Fallback)");
    }
    if (rawStatus == QStringLiteral("weights_missing")) {
        return QStringLiteral("Package Installed — Weights Not Downloaded");
    }
    if (pkgInstalled && !onDisk) {
        return QStringLiteral("Weights Missing");
    }
    return QStringLiteral("Not Installed (Deterministic Fallback)");
}

} // namespace

ModelManagerDialog::ModelManagerDialog(const QString &pythonExecutable, const QString &pythonPackageRoot, QWidget *parent)
    : QDialog(parent)
    , m_pythonExe(pythonExecutable)
    , m_pythonRoot(pythonPackageRoot)
{
    setObjectName(QStringLiteral("BangOnProModelManagerDialog"));
    setWindowTitle(QStringLiteral("Bang On Pro — Local AI Model Manager"));
    resize(920, 560);
    buildUi();
    updateButtonStates();
}

ModelManagerDialog::~ModelManagerDialog()
{
    if (m_activeProcess) {
        m_activeProcess->disconnect(this);
        if (m_activeProcess->state() != QProcess::NotRunning) {
            m_activeProcess->terminate();
            if (!m_activeProcess->waitForFinished(250)) {
                m_activeProcess->kill();
                m_activeProcess->waitForFinished(100);
            }
        }
    }
}

void ModelManagerDialog::buildUi()
{
    auto *root = new QVBoxLayout(this);
    root->setContentsMargins(12, 12, 12, 12);
    root->setSpacing(10);

    auto *titleLabel = new QLabel(QStringLiteral("<b>Local AI Model Runtime & Storage</b>"), this);
    root->addWidget(titleLabel);

    auto *infoFrame = new QFrame(this);
    infoFrame->setFrameShape(QFrame::StyledPanel);
    auto *infoLayout = new QVBoxLayout(infoFrame);
    infoLayout->setContentsMargins(10, 8, 10, 8);
    infoLayout->setSpacing(4);

    m_hardwareLabel = new QLabel(QStringLiteral("Hardware: Detecting host CPU/GPU/RAM capabilities…"), infoFrame);
    m_hardwareLabel->setWordWrap(true);
    m_storageLabel = new QLabel(QStringLiteral("Storage: Click Refresh Status to inspect BANGONPRO_MODEL_DIR."), infoFrame);
    m_storageLabel->setWordWrap(true);
    infoLayout->addWidget(m_hardwareLabel);
    infoLayout->addWidget(m_storageLabel);
    root->addWidget(infoFrame);

    m_modelTree = new QTreeWidget(this);
    m_modelTree->setObjectName(QStringLiteral("BangOnProModelManagerTree"));
    m_modelTree->setColumnCount(6);
    m_modelTree->setHeaderLabels({
        QStringLiteral("Model"),
        QStringLiteral("Category"),
        QStringLiteral("Runtime Backend"),
        QStringLiteral("Weights on Disk"),
        QStringLiteral("Honest Runtime Status"),
        QStringLiteral("Hardware Fit"),
    });
    m_modelTree->setRootIsDecorated(false);
    m_modelTree->setAlternatingRowColors(true);
    m_modelTree->setSelectionMode(QAbstractItemView::SingleSelection);
    m_modelTree->header()->setSectionResizeMode(kColModel, QHeaderView::Stretch);
    m_modelTree->header()->setSectionResizeMode(kColFamily, QHeaderView::ResizeToContents);
    m_modelTree->header()->setSectionResizeMode(kColPackage, QHeaderView::ResizeToContents);
    m_modelTree->header()->setSectionResizeMode(kColDisk, QHeaderView::ResizeToContents);
    m_modelTree->header()->setSectionResizeMode(kColStatus, QHeaderView::Stretch);
    m_modelTree->header()->setSectionResizeMode(kColFit, QHeaderView::ResizeToContents);
    root->addWidget(m_modelTree, 1);

    m_progressBar = new QProgressBar(this);
    m_progressBar->setRange(0, 100);
    m_progressBar->setValue(0);
    m_progressBar->setVisible(false);
    root->addWidget(m_progressBar);

    m_statusLabel = new QLabel(QStringLiteral("Ready. All local models remain offline until explicitly installed by you."), this);
    m_statusLabel->setWordWrap(true);
    root->addWidget(m_statusLabel);

    auto *btnRow = new QHBoxLayout();
    btnRow->setSpacing(8);

    m_installBtn = new QPushButton(QStringLiteral("Download && Install"), this);
    m_importLocalBtn = new QPushButton(QStringLiteral("Import Local Weights…"), this);
    m_removeBtn = new QPushButton(QStringLiteral("Remove Model"), this);
    m_cancelBtn = new QPushButton(QStringLiteral("Cancel"), this);
    m_refreshBtn = new QPushButton(QStringLiteral("Refresh"), this);
    m_closeBtn = new QPushButton(QStringLiteral("Close"), this);

    m_cancelBtn->setEnabled(false);

    btnRow->addWidget(m_installBtn);
    btnRow->addWidget(m_importLocalBtn);
    btnRow->addWidget(m_removeBtn);
    btnRow->addWidget(m_cancelBtn);
    btnRow->addStretch(1);
    btnRow->addWidget(m_refreshBtn);
    btnRow->addWidget(m_closeBtn);
    root->addLayout(btnRow);

    connect(m_modelTree, &QTreeWidget::itemSelectionChanged, this, &ModelManagerDialog::handleSelectionChanged);
    connect(m_installBtn, &QPushButton::clicked, this, &ModelManagerDialog::handleInstallClicked);
    connect(m_importLocalBtn, &QPushButton::clicked, this, &ModelManagerDialog::handleImportLocalClicked);
    connect(m_removeBtn, &QPushButton::clicked, this, &ModelManagerDialog::handleRemoveClicked);
    connect(m_cancelBtn, &QPushButton::clicked, this, &ModelManagerDialog::handleCancelClicked);
    connect(m_refreshBtn, &QPushButton::clicked, this, &ModelManagerDialog::refreshStatus);
    connect(m_closeBtn, &QPushButton::clicked, this, &QDialog::accept);
}

bool ModelManagerDialog::loadStatusJson(const QJsonObject &reportObj)
{
    if (!reportObj.contains(QStringLiteral("models")) || !reportObj.value(QStringLiteral("models")).isArray()) {
        return false;
    }

    m_lastReport = reportObj;
    const QJsonObject hw = reportObj.value(QStringLiteral("hardware")).toObject();
    const QString osName = hw.value(QStringLiteral("os_name")).toString(QStringLiteral("Unknown OS"));
    const QString arch = hw.value(QStringLiteral("arch")).toString(QStringLiteral("unknown"));
    const double ramGb = hw.value(QStringLiteral("total_ram_gb")).toDouble(0.0);
    const QString tier = hw.value(QStringLiteral("ram_tier")).toString(QStringLiteral("unknown"));
    const bool mps = hw.value(QStringLiteral("mps_available")).toBool(false);
    const bool coreml = hw.value(QStringLiteral("coreml_available")).toBool(false);
    const bool cuda = hw.value(QStringLiteral("cuda_available")).toBool(false);
    const bool appleSi = hw.value(QStringLiteral("is_apple_silicon")).toBool(false);

    QStringList accel;
    if (appleSi) {
        accel << QStringLiteral("Apple Silicon");
    }
    if (mps) {
        accel << QStringLiteral("MPS");
    }
    if (coreml) {
        accel << QStringLiteral("CoreML");
    }
    if (cuda) {
        accel << QStringLiteral("CUDA");
    }
    if (accel.isEmpty()) {
        accel << QStringLiteral("CPU");
    }

    m_hardwareLabel->setText(QStringLiteral("Hardware: %1 (%2) • %3 GB RAM [%4] • Acceleration: %5")
                                 .arg(osName, arch)
                                 .arg(ramGb, 0, 'f', 1)
                                 .arg(tier, accel.join(QStringLiteral(", "))));

    const QString modelDir = reportObj.value(QStringLiteral("model_dir")).toString();
    const double totalMb = reportObj.value(QStringLiteral("total_mb")).toDouble(0.0);
    const int installedCount = reportObj.value(QStringLiteral("installed_count")).toInt(0);
    const int readyCount = reportObj.value(QStringLiteral("ready_count")).toInt(0);
    m_storageLabel->setText(
        QStringLiteral("Storage: %1 • %2 installed (%3 neural-ready) • %4 MB used").arg(modelDir).arg(installedCount).arg(readyCount).arg(totalMb, 0, 'f', 1));

    const QString prevSelected = selectedModelId();
    m_modelTree->clear();

    const QColor okColor(122, 224, 152);
    const QColor warnColor(255, 206, 112);
    const QColor mutedColor(168, 178, 193);

    const QJsonArray models = reportObj.value(QStringLiteral("models")).toArray();
    for (const QJsonValue &val : models) {
        const QJsonObject m = val.toObject();
        const QString modelId = m.value(QStringLiteral("model_id")).toString();
        const QString displayName = m.value(QStringLiteral("display_name")).toString(modelId);
        const QString family = formatFamilyLabel(m.value(QStringLiteral("family")).toString());
        const QString backend = m.value(QStringLiteral("runtime_backend")).toString();
        const bool pkgInstalled = m.value(QStringLiteral("backend_package_installed")).toBool(false);
        const bool onDisk = m.value(QStringLiteral("model_available_on_disk")).toBool(false);
        const QString rawStatus = m.value(QStringLiteral("status")).toString();
        const bool hwCompat = m.value(QStringLiteral("hardware_compatible")).toBool(true);
        const bool recommended = m.value(QStringLiteral("recommended")).toBool(false);
        const int estMb = m.value(QStringLiteral("disk_size_mb")).toInt(0);
        const double diskMb = m.value(QStringLiteral("disk_usage_bytes")).toDouble(0.0) / (1024.0 * 1024.0);
        const QString notes = m.value(QStringLiteral("compatibility_notes")).toString();

        auto *item = new QTreeWidgetItem(m_modelTree);
        item->setData(kColModel, kModelIdRole, modelId);
        item->setData(kColModel, kOnDiskRole, onDisk);
        item->setText(kColModel, displayName);
        item->setText(kColFamily, family);
        item->setText(kColPackage, QStringLiteral("%1 (%2)").arg(backend, pkgInstalled ? QStringLiteral("Installed") : QStringLiteral("Missing")));
        item->setText(kColDisk, onDisk ? QStringLiteral("Installed (%1 MB)").arg(diskMb, 0, 'f', 1) : QStringLiteral("Not downloaded (~%1 MB)").arg(estMb));
        item->setText(kColStatus, formatHonestStatus(rawStatus, pkgInstalled, onDisk));
        item->setText(kColFit, recommended ? QStringLiteral("Recommended") : (hwCompat ? QStringLiteral("Compatible") : QStringLiteral("High RAM")));

        if (rawStatus == QStringLiteral("ready") || rawStatus == QStringLiteral("loaded")) {
            item->setForeground(kColStatus, QBrush(okColor));
        } else if (onDisk || pkgInstalled) {
            item->setForeground(kColStatus, QBrush(warnColor));
        } else {
            item->setForeground(kColStatus, QBrush(mutedColor));
        }

        const QString tip = QStringLiteral("ID: %1\nLicense: %2\n%3").arg(modelId, m.value(QStringLiteral("license_spdx")).toString(), notes);
        for (int col = 0; col < 6; ++col) {
            item->setToolTip(col, tip);
        }
    }

    if (!prevSelected.isEmpty()) {
        selectModelById(prevSelected);
    } else if (m_modelTree->topLevelItemCount() > 0) {
        m_modelTree->setCurrentItem(m_modelTree->topLevelItem(0));
    }

    updateButtonStates();
    Q_EMIT modelStatusUpdated(reportObj);
    return true;
}

int ModelManagerDialog::modelCount() const
{
    return m_modelTree ? m_modelTree->topLevelItemCount() : 0;
}

QString ModelManagerDialog::selectedModelId() const
{
    if (!m_modelTree) {
        return {};
    }
    QTreeWidgetItem *cur = m_modelTree->currentItem();
    return cur ? cur->data(kColModel, kModelIdRole).toString() : QString();
}

bool ModelManagerDialog::selectModelById(const QString &modelId)
{
    if (!m_modelTree || modelId.isEmpty()) {
        return false;
    }
    for (int i = 0; i < m_modelTree->topLevelItemCount(); ++i) {
        QTreeWidgetItem *item = m_modelTree->topLevelItem(i);
        if (item->data(kColModel, kModelIdRole).toString() == modelId) {
            m_modelTree->setCurrentItem(item);
            return true;
        }
    }
    return false;
}

QString ModelManagerDialog::hardwareSummaryText() const
{
    return m_hardwareLabel ? m_hardwareLabel->text() : QString();
}

QProcess *ModelManagerDialog::spawnCli(const QStringList &args)
{
    if (m_pythonExe.isEmpty() || m_pythonRoot.isEmpty()) {
        m_statusLabel->setText(QStringLiteral("Python runtime or bangonpro package root is unavailable."));
        return nullptr;
    }
    QStringList fullArgs{QStringLiteral("-s"), QStringLiteral("-m"), QStringLiteral("bangonpro.ai_engine.cli")};
    if (!m_modelDirOverride.isEmpty()) {
        fullArgs << QStringLiteral("--model-dir") << m_modelDirOverride;
    }
    fullArgs << args;

    auto *proc = new QProcess(this);
    proc->setProcessEnvironment(buildSanitizedPythonEnvironment(m_pythonRoot));
    proc->setWorkingDirectory(m_pythonRoot);
    // Argument vector only — never invoked through a shell.
    proc->start(m_pythonExe, fullArgs);
    return proc;
}

void ModelManagerDialog::setBusy(bool busy, const QString &statusText)
{
    m_busy = busy;
    if (m_progressBar) {
        m_progressBar->setVisible(busy);
        if (!busy) {
            m_progressBar->setValue(0);
        }
    }
    if (!statusText.isEmpty() && m_statusLabel) {
        m_statusLabel->setText(statusText);
    }
    updateButtonStates();
}

void ModelManagerDialog::updateButtonStates()
{
    QTreeWidgetItem *cur = m_modelTree ? m_modelTree->currentItem() : nullptr;
    const bool hasSel = (cur != nullptr);
    const bool onDisk = hasSel && cur->data(kColModel, kOnDiskRole).toBool();

    if (m_installBtn) {
        m_installBtn->setEnabled(!m_busy && hasSel);
    }
    if (m_importLocalBtn) {
        m_importLocalBtn->setEnabled(!m_busy && hasSel);
    }
    if (m_removeBtn) {
        m_removeBtn->setEnabled(!m_busy && hasSel && onDisk);
    }
    if (m_cancelBtn) {
        m_cancelBtn->setEnabled(m_busy && m_activeProcess != nullptr);
    }
    if (m_refreshBtn) {
        m_refreshBtn->setEnabled(!m_busy);
    }
}

void ModelManagerDialog::refreshStatus()
{
    if (m_busy) {
        return;
    }
    QProcess *proc = spawnCli({QStringLiteral("--model-status"), QStringLiteral("--json")});
    if (!proc) {
        return;
    }
    m_activeProcess = proc;
    setBusy(true, QStringLiteral("Inspecting local hardware and AI model storage…"));
    m_progressBar->setRange(0, 0);

    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, proc](int exitCode, QProcess::ExitStatus exitStatus) {
        if (m_activeProcess == proc) {
            m_activeProcess.clear();
        }
        proc->deleteLater();
        m_progressBar->setRange(0, 100);
        setBusy(false);

        if (exitStatus != QProcess::NormalExit || exitCode != 0) {
            const QString err = QString::fromUtf8(proc->readAllStandardError().right(500));
            m_statusLabel->setText(QStringLiteral("Failed to query model status: %1").arg(err));
            return;
        }
        const QJsonObject report = QJsonDocument::fromJson(proc->readAllStandardOutput()).object();
        if (loadStatusJson(report)) {
            m_statusLabel->setText(QStringLiteral("Model runtime status updated."));
        } else {
            m_statusLabel->setText(QStringLiteral("Received invalid model status JSON."));
        }
    });
}

void ModelManagerDialog::handleSelectionChanged()
{
    updateButtonStates();
}

void ModelManagerDialog::handleInstallClicked()
{
    const QString modelId = selectedModelId();
    if (modelId.isEmpty() || m_busy) {
        return;
    }
    runInstallModel(modelId, QString());
}

void ModelManagerDialog::handleImportLocalClicked()
{
    const QString modelId = selectedModelId();
    if (modelId.isEmpty() || m_busy) {
        return;
    }
    const QString file = QFileDialog::getOpenFileName(this, QStringLiteral("Select Local Model Weights File"), QString(),
                                                      QStringLiteral("Model Weights (*.onnx *.bin *.pt *.pth *.safetensors *.gguf);;All Files (*)"));
    if (file.isEmpty()) {
        return;
    }
    runInstallModel(modelId, file);
}

void ModelManagerDialog::runInstallModel(const QString &modelId, const QString &localSourcePath)
{
    QStringList args{QStringLiteral("--install-model"), modelId, QStringLiteral("--json")};
    if (!localSourcePath.isEmpty()) {
        args << QStringLiteral("--model-source-path") << localSourcePath;
    }

    QProcess *proc = spawnCli(args);
    if (!proc) {
        return;
    }
    m_activeProcess = proc;
    setBusy(true, QStringLiteral("Installing '%1'…").arg(modelId));
    m_progressBar->setRange(0, 100);
    m_progressBar->setValue(0);

    connect(proc, &QProcess::readyReadStandardError, this, [this, proc]() {
        const QByteArray chunk = proc->readAllStandardError();
        const QList<QByteArray> lines = chunk.split('\n');
        for (const QByteArray &line : lines) {
            const QByteArray trimmed = line.trimmed();
            if (!trimmed.startsWith('{')) {
                continue;
            }
            const QJsonObject evt = QJsonDocument::fromJson(trimmed).object();
            if (evt.value(QStringLiteral("event")).toString() == QStringLiteral("progress")) {
                const int pct = qBound(0, static_cast<int>(evt.value(QStringLiteral("fraction")).toDouble() * 100.0), 100);
                m_progressBar->setValue(pct);
                const QString msg = evt.value(QStringLiteral("message")).toString();
                if (!msg.isEmpty()) {
                    m_statusLabel->setText(msg);
                }
            }
        }
    });

    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, proc, modelId](int exitCode, QProcess::ExitStatus exitStatus) {
        if (m_activeProcess == proc) {
            m_activeProcess.clear();
        }
        proc->deleteLater();
        setBusy(false);

        const QJsonObject result = QJsonDocument::fromJson(proc->readAllStandardOutput()).object();
        if (exitStatus == QProcess::NormalExit && exitCode == 0 && result.value(QStringLiteral("installed")).toBool()) {
            const QString msg = QStringLiteral("Installed '%1' (SHA-256: %2)").arg(modelId, result.value(QStringLiteral("sha256")).toString().left(12));
            m_statusLabel->setText(msg);
            Q_EMIT modelOperationCompleted(QStringLiteral("install"), modelId, true, msg);
            refreshStatus();
        } else {
            const QString err = result.value(QStringLiteral("error")).toString().isEmpty() ? QString::fromUtf8(proc->readAllStandardError().right(400))
                                                                                           : result.value(QStringLiteral("error")).toString();
            const QString msg = QStringLiteral("Install failed for '%1': %2").arg(modelId, err);
            m_statusLabel->setText(msg);
            Q_EMIT modelOperationCompleted(QStringLiteral("install"), modelId, false, msg);
        }
    });
}

void ModelManagerDialog::handleRemoveClicked()
{
    const QString modelId = selectedModelId();
    if (modelId.isEmpty() || m_busy) {
        return;
    }

    QProcess *proc = spawnCli({QStringLiteral("--remove-model"), modelId, QStringLiteral("--json")});
    if (!proc) {
        return;
    }
    m_activeProcess = proc;
    setBusy(true, QStringLiteral("Removing '%1' from managed model cache…").arg(modelId));
    m_progressBar->setRange(0, 0);

    connect(proc, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, proc, modelId](int exitCode, QProcess::ExitStatus exitStatus) {
        if (m_activeProcess == proc) {
            m_activeProcess.clear();
        }
        proc->deleteLater();
        m_progressBar->setRange(0, 100);
        setBusy(false);

        const QJsonObject result = QJsonDocument::fromJson(proc->readAllStandardOutput()).object();
        const bool ok = (exitStatus == QProcess::NormalExit && exitCode == 0 && result.value(QStringLiteral("removed")).toBool());
        const QString msg = result.value(QStringLiteral("message"))
                                .toString(ok ? QStringLiteral("Removed '%1'").arg(modelId) : QStringLiteral("Failed to remove '%1'").arg(modelId));
        m_statusLabel->setText(msg);
        Q_EMIT modelOperationCompleted(QStringLiteral("remove"), modelId, ok, msg);
        if (ok) {
            refreshStatus();
        }
    });
}

void ModelManagerDialog::handleCancelClicked()
{
    if (m_activeProcess && m_activeProcess->state() != QProcess::NotRunning) {
        m_activeProcess->kill();
        m_statusLabel->setText(QStringLiteral("Cancelled active model operation; partial downloads cleaned up."));
    }
}

// ---------------------------------------------------------------------------
// V1.2.2 System check
// ---------------------------------------------------------------------------
namespace {
constexpr int kCheckIdRole = Qt::UserRole + 11;
constexpr int kFixActionRole = Qt::UserRole + 12;

TallyState checkTally(const QString &status)
{
    if (status == QLatin1String("ok")) {
        return TallyState::Done;
    }
    if (status == QLatin1String("warning")) {
        return TallyState::Review;
    }
    if (status == QLatin1String("error")) {
        return TallyState::Failed;
    }
    return TallyState::Idle;
}
} // namespace

SystemCheckDialog::SystemCheckDialog(QWidget *parent)
    : QDialog(parent)
{
    using M = StudioMetrics;
    setObjectName(QStringLiteral("bangonpro_system_check"));
    setWindowTitle(tr("System check"));
    resize(640, 560);
    auto *root = new QVBoxLayout(this);
    root->setContentsMargins(M::kSpace4, M::kSpace4, M::kSpace4, M::kSpace4);
    root->setSpacing(M::kSpace2);

    auto *title = new QLabel(tr("System check"), this);
    setTextRole(title, "title");
    m_overallLabel = new QLabel(tr("Checking this computer…"), this);
    m_overallLabel->setObjectName(QStringLiteral("sc_overall"));
    m_overallLabel->setWordWrap(true);
    setTextRole(m_overallLabel, "secondary");
    root->addWidget(title);
    root->addWidget(m_overallLabel);

    m_items = new QListWidget(this);
    m_items->setObjectName(QStringLiteral("sc_items"));
    m_items->setWordWrap(true);
    m_items->setIconSize(QSize(12, 12));
    m_items->setAccessibleName(tr("System check results"));
    m_items->setToolTip(tr("Double-click a check to fix it"));
    root->addWidget(m_items, 1);

    auto *folderLabel = new QLabel(tr("Productions and cache"), this);
    setTextRole(folderLabel, "section");
    auto *folderRow = new QHBoxLayout;
    folderRow->setSpacing(M::kSpace2);
    m_folderEdit = new QLineEdit(this);
    m_folderEdit->setObjectName(QStringLiteral("sc_folder"));
    m_folderEdit->setReadOnly(true);
    m_folderEdit->setPlaceholderText(tr("Default folder in your user library"));
    m_folderBtn = new QPushButton(tr("Choose…"), this);
    m_folderBtn->setObjectName(QStringLiteral("sc_choose_folder"));
    m_folderBtn->setToolTip(tr("Choose where productions, drafts and the analysis cache are kept. An external SSD works."));
    folderLabel->setBuddy(m_folderBtn);
    folderRow->addWidget(m_folderEdit, 1);
    folderRow->addWidget(m_folderBtn);
    root->addWidget(folderLabel);
    root->addLayout(folderRow);

    auto *cacheRow = new QHBoxLayout;
    cacheRow->setSpacing(M::kSpace2);
    m_cacheLabel = new QLabel(tr("Analysis cache"), this);
    m_cacheLabel->setObjectName(QStringLiteral("sc_cache"));
    setTextRole(m_cacheLabel, "secondary");
    m_pruneBtn = new QPushButton(tr("Prune"), this);
    m_pruneBtn->setObjectName(QStringLiteral("sc_cache_prune"));
    m_pruneBtn->setToolTip(tr("Remove the least recently used analysis until the cache is half its size limit. Source media are never touched."));
    setRole(m_pruneBtn, "quiet");
    m_clearBtn = new QPushButton(tr("Clear"), this);
    m_clearBtn->setObjectName(QStringLiteral("sc_cache_clear"));
    m_clearBtn->setToolTip(tr("Delete all cached analysis. The next production analyses the footage again."));
    setRole(m_clearBtn, "quiet");
    cacheRow->addWidget(m_cacheLabel, 1);
    cacheRow->addWidget(m_pruneBtn);
    cacheRow->addWidget(m_clearBtn);
    root->addLayout(cacheRow);

    auto *buttons = new QHBoxLayout;
    buttons->setSpacing(M::kSpace2);
    m_modelsBtn = new QPushButton(tr("Model Manager…"), this);
    m_modelsBtn->setObjectName(QStringLiteral("sc_models"));
    m_diagnosticsBtn = new QPushButton(tr("Export diagnostics…"), this);
    m_diagnosticsBtn->setObjectName(QStringLiteral("sc_diagnostics"));
    m_diagnosticsBtn->setToolTip(tr("Save a zip for the team. Passwords, keys and footage are never included."));
    setRole(m_diagnosticsBtn, "quiet");
    m_recheckBtn = new QPushButton(tr("Run check again"), this);
    m_recheckBtn->setObjectName(QStringLiteral("sc_recheck"));
    m_doneBtn = new QPushButton(tr("Done"), this);
    m_doneBtn->setObjectName(QStringLiteral("sc_done"));
    buttons->addWidget(m_modelsBtn);
    buttons->addWidget(m_diagnosticsBtn);
    buttons->addStretch(1);
    buttons->addWidget(m_recheckBtn);
    buttons->addWidget(m_doneBtn);
    root->addLayout(buttons);

    connect(m_folderBtn, &QPushButton::clicked, this, &SystemCheckDialog::chooseFolder);
    connect(m_pruneBtn, &QPushButton::clicked, this, [this]() { Q_EMIT cacheActionRequested(QStringLiteral("prune")); });
    connect(m_clearBtn, &QPushButton::clicked, this, [this]() { Q_EMIT cacheActionRequested(QStringLiteral("clear")); });
    connect(m_modelsBtn, &QPushButton::clicked, this, &SystemCheckDialog::modelManagerRequested);
    connect(m_diagnosticsBtn, &QPushButton::clicked, this, &SystemCheckDialog::exportDiagnosticsRequested);
    connect(m_recheckBtn, &QPushButton::clicked, this, &SystemCheckDialog::recheckRequested);
    connect(m_doneBtn, &QPushButton::clicked, this, &QDialog::accept);
    connect(m_items, &QListWidget::itemActivated, this, [this](QListWidgetItem *item) {
        if (item) {
            triggerFixAction(item->data(kFixActionRole).toString());
        }
    });
    updatePrimary();
}

bool SystemCheckDialog::loadReport(const QJsonObject &report)
{
    const QJsonArray items = report.value(QStringLiteral("items")).toArray();
    if (items.isEmpty()) {
        return false;
    }
    m_items->clear();
    for (const QJsonValue &v : items) {
        const QJsonObject i = v.toObject();
        QString text = QStringLiteral("%1 · %2").arg(i.value(QStringLiteral("title")).toString(), i.value(QStringLiteral("detail")).toString());
        const QString fix = i.value(QStringLiteral("fix")).toString();
        if (!fix.isEmpty()) {
            text += QLatin1Char('\n') + fix;
        }
        auto *row = new QListWidgetItem(tallyIcon(checkTally(i.value(QStringLiteral("status")).toString())), text, m_items);
        row->setData(kCheckIdRole, i.value(QStringLiteral("id")).toString());
        row->setData(kFixActionRole, i.value(QStringLiteral("fix_action")).toString());
        if (i.value(QStringLiteral("id")).toString() == QLatin1String("cache")) {
            m_cacheLabel->setText(tr("Analysis cache · %1").arg(i.value(QStringLiteral("detail")).toString()));
        }
    }
    m_overall = report.value(QStringLiteral("overall")).toString();
    const QJsonObject summary = report.value(QStringLiteral("summary")).toObject();
    if (m_overall == QLatin1String("ready")) {
        m_overallLabel->setText(tr("Ready for your first production."));
        setTone(m_overallLabel, "success");
    } else if (m_overall == QLatin1String("attention")) {
        m_overallLabel->setText(tr("Ready, with %n thing(s) worth a look.", nullptr, summary.value(QStringLiteral("warning")).toInt()));
        setTone(m_overallLabel, "warning");
    } else {
        m_overallLabel->setText(tr("Fix %n problem(s) before your first production.", nullptr, summary.value(QStringLiteral("error")).toInt()));
        setTone(m_overallLabel, "danger");
    }
    const QString folder = report.value(QStringLiteral("paths")).toObject().value(QStringLiteral("job_root")).toString();
    if (m_folderEdit->text().isEmpty() && !folder.isEmpty()) {
        m_folderEdit->setText(folder);
    }
    setBusy(false);
    return true;
}

QString SystemCheckDialog::overallStatus() const
{
    return m_overall;
}

int SystemCheckDialog::itemCount() const
{
    return m_items->count();
}

QString SystemCheckDialog::itemText(const QString &itemId) const
{
    for (int i = 0; i < m_items->count(); ++i) {
        if (m_items->item(i)->data(kCheckIdRole).toString() == itemId) {
            return m_items->item(i)->text();
        }
    }
    return {};
}

void SystemCheckDialog::setStorageFolder(const QString &folder)
{
    m_folderEdit->setText(folder);
}

QString SystemCheckDialog::storageFolder() const
{
    return m_folderEdit->text();
}

void SystemCheckDialog::setBusy(bool busy, const QString &text)
{
    m_recheckBtn->setEnabled(!busy);
    m_pruneBtn->setEnabled(!busy);
    m_clearBtn->setEnabled(!busy);
    m_folderBtn->setEnabled(!busy);
    if (busy) {
        m_overallLabel->setText(text.isEmpty() ? tr("Checking this computer…") : text);
        setTone(m_overallLabel, "");
    }
    updatePrimary();
}

void SystemCheckDialog::triggerFixAction(const QString &fixAction)
{
    if (fixAction == QLatin1String("open_model_manager")) {
        Q_EMIT modelManagerRequested();
    } else if (fixAction == QLatin1String("choose_job_folder")) {
        chooseFolder();
    } else if (fixAction == QLatin1String("export_diagnostics")) {
        Q_EMIT exportDiagnosticsRequested();
    } else if (fixAction == QLatin1String("run_system_check")) {
        Q_EMIT recheckRequested();
    }
}

void SystemCheckDialog::chooseFolder()
{
    const QString dir = QFileDialog::getExistingDirectory(this, tr("Choose a folder for productions and the cache"), m_folderEdit->text());
    if (!dir.isEmpty()) {
        m_folderEdit->setText(dir);
        Q_EMIT storageFolderChosen(dir);
    }
}

void SystemCheckDialog::updatePrimary()
{
    // One primary action at a time: fix-and-recheck while something blocks, otherwise Done.
    const bool blocked = m_overall == QLatin1String("blocked");
    m_recheckBtn->setProperty("bangOnProPrimary", blocked);
    m_doneBtn->setProperty("bangOnProPrimary", !blocked);
    setRole(m_recheckBtn, blocked ? "primary" : "secondary");
    setRole(m_doneBtn, blocked ? "secondary" : "primary");
    (blocked ? m_recheckBtn : m_doneBtn)->setDefault(true);
}

} // namespace BangOnPro
