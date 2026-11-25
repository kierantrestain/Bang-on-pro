/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "AgentControlCentre.h"

#include <QCheckBox>
#include <QComboBox>
#include <QDir>
#include <QDoubleSpinBox>
#include <QFile>
#include <QFileInfo>
#include <QFormLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QPlainTextEdit>
#include <QProgressBar>
#include <QPushButton>
#include <QSplitter>
#include <QTimer>
#include <QTreeWidget>
#include <QVBoxLayout>

namespace BangOnPro {

namespace {
struct AgentRow
{
    const char *id;
    const char *name;
};
// Must match bangonpro/ai_engine/department/registry.py AGENT_ORDER.
constexpr AgentRow kAgents[] = {
    {"executive", "Executive Editing Agent"},
    {"learning", "Learning Agent"},
    {"media_ingest", "Media Ingest Agent"},
    {"synchronisation", "Synchronisation Agent"},
    {"editorial_intelligence", "Editorial Intelligence Agent"},
    {"multicam_director", "Multicamera Director"},
    {"rough_cut", "Rough Cut Agent"},
    {"audio_engineer", "Audio Engineer"},
    {"colourist", "Colourist"},
    {"captions", "Captions Agent"},
    {"graphics", "Graphics Agent"},
    {"vfx", "VFX Agent"},
    {"quality_control", "Quality Control Agent"},
    {"distribution", "Distribution Agent"},
};
enum Col { ColAgent = 0, ColStatus, ColConfidence, ColOps, ColEngine, ColSummary };
constexpr int kIdRole = Qt::UserRole + 1;

QString statusLabel(const QString &status, const QString &decision, bool stale)
{
    QString s = status;
    if (!decision.isEmpty()) {
        s += QStringLiteral(" · ") + decision;
    }
    if (stale) {
        s += QStringLiteral(" · stale");
    }
    return s;
}
} // namespace

AgentControlCentre::AgentControlCentre(QWidget *parent)
    : QWidget(parent)
{
    setObjectName(QStringLiteral("bangonpro_agent_control_centre"));
    buildUi();
    m_pollTimer = new QTimer(this);
    m_pollTimer->setInterval(500);
    connect(m_pollTimer, &QTimer::timeout, this, [this]() { refreshFromDisk(); });
    updateActionStates();
}

AgentControlCentre::~AgentControlCentre() = default;

QStringList AgentControlCentre::agentIds()
{
    QStringList ids;
    for (const AgentRow &r : kAgents) {
        ids << QString::fromLatin1(r.id);
    }
    return ids;
}

QStringList AgentControlCentre::automationModes()
{
    return {QStringLiteral("manual"), QStringLiteral("assisted"), QStringLiteral("autonomous_first_cut"), QStringLiteral("full_production_draft")};
}

void AgentControlCentre::buildUi()
{
    auto *root = new QVBoxLayout(this);
    root->setContentsMargins(6, 6, 6, 6);

    auto *jobBox = new QGroupBox(tr("Production Job"), this);
    auto *form = new QFormLayout(jobBox);
    m_modeCombo = new QComboBox(jobBox);
    m_modeCombo->setObjectName(QStringLiteral("acc_mode"));
    m_modeCombo->addItem(tr("Manual editing (agents on request)"), QStringLiteral("manual"));
    m_modeCombo->addItem(tr("Assisted (every edit needs approval)"), QStringLiteral("assisted"));
    m_modeCombo->addItem(tr("Autonomous first cut (isolated draft)"), QStringLiteral("autonomous_first_cut"));
    m_modeCombo->addItem(tr("Full production draft (isolated draft)"), QStringLiteral("full_production_draft"));
    m_modeCombo->setCurrentIndex(2);
    m_profileCombo = new QComboBox(jobBox);
    m_profileCombo->setObjectName(QStringLiteral("acc_profile"));
    m_profileCombo->addItem(tr("Podcast Multicam"), QStringLiteral("podcast_multicam"));
    m_profileCombo->addItem(tr("Gameshow Multicam"), QStringLiteral("gameshow_multicam"));
    m_profileCombo->addItem(tr("Sports Discussion"), QStringLiteral("sports_discussion"));
    m_profileCombo->addItem(tr("Daily News"), QStringLiteral("daily_news"));
    m_profileCombo->addItem(tr("Interview (2-Camera)"), QStringLiteral("interview_two_camera"));
    m_profileCombo->addItem(tr("Social Short (9:16)"), QStringLiteral("social_short"));
    m_seriesEdit = new QLineEdit(jobBox);
    m_seriesEdit->setObjectName(QStringLiteral("acc_series"));
    m_seriesEdit->setPlaceholderText(tr("Series id (reuses approved preferences)"));
    m_episodeEdit = new QLineEdit(jobBox);
    m_episodeEdit->setObjectName(QStringLiteral("acc_episode"));
    m_episodeEdit->setPlaceholderText(tr("Episode title for the title card"));
    m_briefEdit = new QPlainTextEdit(jobBox);
    m_briefEdit->setObjectName(QStringLiteral("acc_brief"));
    m_briefEdit->setPlaceholderText(tr("Production instructions, e.g. \"Tight 25-minute cut, match cameras, captions, social clips\""));
    m_briefEdit->setMaximumHeight(70);
    auto *optRow = new QHBoxLayout;
    m_exportCheck = new QCheckBox(tr("Render master"), jobBox);
    m_exportCheck->setObjectName(QStringLiteral("acc_export"));
    m_budgetSpin = new QDoubleSpinBox(jobBox);
    m_budgetSpin->setObjectName(QStringLiteral("acc_budget"));
    m_budgetSpin->setPrefix(QStringLiteral("$"));
    m_budgetSpin->setRange(0.0, 500.0);
    m_budgetSpin->setToolTip(tr("External model budget for this job (0 = local only)"));
    optRow->addWidget(m_exportCheck);
    optRow->addStretch(1);
    optRow->addWidget(new QLabel(tr("External budget"), jobBox));
    optRow->addWidget(m_budgetSpin);
    form->addRow(tr("Mode"), m_modeCombo);
    form->addRow(tr("Profile"), m_profileCombo);
    form->addRow(tr("Series"), m_seriesEdit);
    form->addRow(tr("Episode"), m_episodeEdit);
    form->addRow(tr("Brief"), m_briefEdit);
    form->addRow(optRow);
    root->addWidget(jobBox);

    auto *runRow = new QHBoxLayout;
    m_startBtn = new QPushButton(tr("Start Job"), this);
    m_startBtn->setObjectName(QStringLiteral("acc_start"));
    m_cancelBtn = new QPushButton(tr("Cancel"), this);
    m_cancelBtn->setObjectName(QStringLiteral("acc_cancel"));
    m_resumeBtn = new QPushButton(tr("Resume"), this);
    m_resumeBtn->setObjectName(QStringLiteral("acc_resume"));
    runRow->addWidget(m_startBtn);
    runRow->addWidget(m_cancelBtn);
    runRow->addWidget(m_resumeBtn);
    root->addLayout(runRow);

    m_progress = new QProgressBar(this);
    m_progress->setObjectName(QStringLiteral("acc_progress"));
    m_progress->setRange(0, 100);
    m_statusLabel = new QLabel(tr("No job running"), this);
    m_statusLabel->setObjectName(QStringLiteral("acc_status"));
    m_statusLabel->setWordWrap(true);
    root->addWidget(m_progress);
    root->addWidget(m_statusLabel);

    auto *split = new QSplitter(Qt::Vertical, this);
    m_agentTree = new QTreeWidget(split);
    m_agentTree->setObjectName(QStringLiteral("acc_agents"));
    m_agentTree->setHeaderLabels({tr("Agent"), tr("Status"), tr("Conf."), tr("Ops"), tr("Engine"), tr("Result / reason")});
    m_agentTree->setRootIsDecorated(false);
    m_agentTree->setUniformRowHeights(true);
    m_agentTree->header()->setStretchLastSection(true);
    for (const AgentRow &r : kAgents) {
        auto *item = new QTreeWidgetItem(m_agentTree);
        item->setText(ColAgent, tr(r.name));
        item->setData(ColAgent, kIdRole, QString::fromLatin1(r.id));
        item->setText(ColStatus, tr("idle"));
        const bool locked = qstrcmp(r.id, "executive") == 0;
        // The Executive Editing Agent plans and verifies every job and can never be disabled.
        item->setFlags(locked ? (item->flags() & ~Qt::ItemIsUserCheckable) : (item->flags() | Qt::ItemIsUserCheckable));
        if (!locked) {
            item->setCheckState(ColAgent, Qt::Checked);
            item->setToolTip(ColAgent, tr("Untick to disable this agent for the next job"));
        }
    }
    m_agentTree->resizeColumnToContents(ColAgent);
    m_agentTree->setTextElideMode(Qt::ElideRight);
    m_agentTree->setMinimumHeight(220);
    setMinimumWidth(340);

    auto *lower = new QWidget(split);
    auto *lowerLay = new QVBoxLayout(lower);
    lowerLay->setContentsMargins(0, 0, 0, 0);
    auto *agentRow = new QHBoxLayout;
    m_acceptBtn = new QPushButton(tr("Accept Agent"), lower);
    m_acceptBtn->setObjectName(QStringLiteral("acc_accept"));
    m_rejectBtn = new QPushButton(tr("Reject Agent"), lower);
    m_rejectBtn->setObjectName(QStringLiteral("acc_reject"));
    m_rerunBtn = new QPushButton(tr("Rerun Agent"), lower);
    m_rerunBtn->setObjectName(QStringLiteral("acc_rerun"));
    agentRow->addWidget(m_acceptBtn);
    agentRow->addWidget(m_rejectBtn);
    agentRow->addWidget(m_rerunBtn);
    lowerLay->addLayout(agentRow);
    lowerLay->addWidget(new QLabel(tr("Needs your attention"), lower));
    m_reviewList = new QListWidget(lower);
    m_reviewList->setObjectName(QStringLiteral("acc_review"));
    lowerLay->addWidget(m_reviewList, 1);
    m_summaryView = new QPlainTextEdit(lower);
    m_summaryView->setObjectName(QStringLiteral("acc_summary"));
    m_summaryView->setReadOnly(true);
    m_summaryView->setPlaceholderText(tr("Final production summary appears here"));
    lowerLay->addWidget(m_summaryView, 1);
    auto *draftRow = new QHBoxLayout;
    m_reviewBtn = new QPushButton(tr("Review Draft in Timeline"), lower);
    m_reviewBtn->setObjectName(QStringLiteral("acc_review_draft"));
    m_openDraftBtn = new QPushButton(tr("Open Draft Project"), lower);
    m_openDraftBtn->setObjectName(QStringLiteral("acc_open_draft"));
    m_promoteBtn = new QPushButton(tr("Promote…"), lower);
    m_promoteBtn->setObjectName(QStringLiteral("acc_promote"));
    m_promoteBtn->setToolTip(tr("Copy the draft into a project file. Replacing an existing edit requires approval."));
    draftRow->addWidget(m_reviewBtn);
    draftRow->addWidget(m_openDraftBtn);
    draftRow->addWidget(m_promoteBtn);
    lowerLay->addLayout(draftRow);
    split->addWidget(m_agentTree);
    split->addWidget(lower);
    root->addWidget(split, 1);

    connect(m_startBtn, &QPushButton::clicked, this, [this]() { Q_EMIT startJobRequested(buildRunArguments({})); });
    connect(m_cancelBtn, &QPushButton::clicked, this, [this]() {
        if (!m_jobDir.isEmpty()) {
            Q_EMIT cancelJobRequested(m_jobDir);
        }
    });
    connect(m_resumeBtn, &QPushButton::clicked, this, [this]() {
        if (!m_jobDir.isEmpty()) {
            Q_EMIT resumeJobRequested(m_jobDir);
        }
    });
    connect(m_rerunBtn, &QPushButton::clicked, this, [this]() {
        if (!m_jobDir.isEmpty() && !selectedAgentId().isEmpty()) {
            Q_EMIT rerunAgentRequested(m_jobDir, selectedAgentId());
        }
    });
    connect(m_acceptBtn, &QPushButton::clicked, this, [this]() {
        if (!m_jobDir.isEmpty() && !selectedAgentId().isEmpty()) {
            Q_EMIT agentDecisionRequested(m_jobDir, selectedAgentId(), true);
        }
    });
    connect(m_rejectBtn, &QPushButton::clicked, this, [this]() {
        if (!m_jobDir.isEmpty() && !selectedAgentId().isEmpty()) {
            Q_EMIT agentDecisionRequested(m_jobDir, selectedAgentId(), false);
        }
    });
    connect(m_reviewBtn, &QPushButton::clicked, this, [this]() {
        if (!m_jobDir.isEmpty()) {
            Q_EMIT reviewDraftInTimelineRequested(m_jobDir);
        }
    });
    connect(m_openDraftBtn, &QPushButton::clicked, this, [this]() {
        if (!m_draftProject.isEmpty()) {
            Q_EMIT openDraftProjectRequested(m_draftProject);
        }
    });
    connect(m_promoteBtn, &QPushButton::clicked, this, [this]() {
        if (!m_jobDir.isEmpty()) {
            Q_EMIT promoteDraftRequested(m_jobDir);
        }
    });
    connect(m_agentTree, &QTreeWidget::itemSelectionChanged, this, &AgentControlCentre::updateActionStates);
}

QStringList AgentControlCentre::buildRunArguments(const QStringList &inputs) const
{
    QStringList args{QStringLiteral("run"), brief(), QStringLiteral("--mode"), selectedMode(), QStringLiteral("--profile"), selectedProfileId()};
    const QString series = m_seriesEdit->text().trimmed();
    if (!series.isEmpty()) {
        args << QStringLiteral("--series") << series;
    }
    const QString episode = m_episodeEdit->text().trimmed();
    if (!episode.isEmpty()) {
        args << QStringLiteral("--episode-title") << episode;
    }
    if (m_exportCheck->isChecked()) {
        args << QStringLiteral("--export");
    }
    if (m_budgetSpin->value() > 0.0) {
        args << QStringLiteral("--budget-usd") << QString::number(m_budgetSpin->value(), 'f', 2);
    }
    const QStringList disabled = disabledAgents();
    if (!disabled.isEmpty()) {
        args << QStringLiteral("--disable") << disabled;
    }
    if (!inputs.isEmpty()) {
        args << QStringLiteral("--inputs") << inputs;
    }
    return args;
}

QString AgentControlCentre::selectedMode() const
{
    return m_modeCombo->currentData().toString();
}

void AgentControlCentre::setSelectedMode(const QString &mode)
{
    const int idx = m_modeCombo->findData(mode);
    if (idx >= 0) {
        m_modeCombo->setCurrentIndex(idx);
    }
}

QString AgentControlCentre::selectedProfileId() const
{
    return m_profileCombo->currentData().toString();
}

void AgentControlCentre::setBrief(const QString &text)
{
    m_briefEdit->setPlainText(text);
}

QString AgentControlCentre::brief() const
{
    return m_briefEdit->toPlainText().trimmed();
}

QTreeWidgetItem *AgentControlCentre::agentItem(const QString &agentId) const
{
    for (int i = 0; i < m_agentTree->topLevelItemCount(); ++i) {
        QTreeWidgetItem *it = m_agentTree->topLevelItem(i);
        if (it->data(ColAgent, kIdRole).toString() == agentId) {
            return it;
        }
    }
    return nullptr;
}

QStringList AgentControlCentre::disabledAgents() const
{
    QStringList out;
    for (int i = 0; i < m_agentTree->topLevelItemCount(); ++i) {
        QTreeWidgetItem *it = m_agentTree->topLevelItem(i);
        const QString id = it->data(ColAgent, kIdRole).toString();
        if (id != QLatin1String("executive") && (it->flags() & Qt::ItemIsUserCheckable) && it->checkState(ColAgent) == Qt::Unchecked) {
            out << id;
        }
    }
    return out;
}

void AgentControlCentre::setAgentEnabled(const QString &agentId, bool enabled)
{
    if (QTreeWidgetItem *it = agentItem(agentId)) {
        if (it->flags() & Qt::ItemIsUserCheckable) {
            it->setCheckState(ColAgent, enabled ? Qt::Checked : Qt::Unchecked);
        }
    }
}

QString AgentControlCentre::selectedAgentId() const
{
    const QList<QTreeWidgetItem *> sel = m_agentTree->selectedItems();
    return sel.isEmpty() ? QString() : sel.first()->data(ColAgent, kIdRole).toString();
}

void AgentControlCentre::selectAgent(const QString &agentId)
{
    if (QTreeWidgetItem *it = agentItem(agentId)) {
        m_agentTree->setCurrentItem(it);
        it->setSelected(true);
    }
}

void AgentControlCentre::loadJobStatus(const QJsonObject &doc)
{
    m_jobStatus = doc.value(QStringLiteral("status")).toString();
    const QJsonObject agents = doc.value(QStringLiteral("agents")).toObject();
    m_reviewList->clear();
    int done = 0;
    int active = 0;
    for (int i = 0; i < m_agentTree->topLevelItemCount(); ++i) {
        QTreeWidgetItem *it = m_agentTree->topLevelItem(i);
        const QJsonObject a = agents.value(it->data(ColAgent, kIdRole).toString()).toObject();
        if (a.isEmpty()) {
            continue;
        }
        const QString status = a.value(QStringLiteral("status")).toString();
        const bool isActive = a.value(QStringLiteral("active")).toBool();
        active += isActive ? 1 : 0;
        done += (isActive && (status == QLatin1String("completed") || status == QLatin1String("failed"))) ? 1 : 0;
        it->setText(ColStatus, statusLabel(status, a.value(QStringLiteral("decision")).toString(), a.value(QStringLiteral("stale")).toBool()));
        const QJsonValue conf = a.value(QStringLiteral("confidence"));
        it->setText(ColConfidence, conf.isDouble() ? QString::number(conf.toDouble(), 'f', 2) : QString());
        it->setText(ColOps, QString::number(a.value(QStringLiteral("operation_count")).toInt()));
        const QJsonValue neural = a.value(QStringLiteral("neural_inference"));
        it->setText(ColEngine, neural.isBool() ? (neural.toBool() ? tr("neural") : tr("fallback")) : tr("deterministic"));
        QString summary = a.value(QStringLiteral("summary")).toString();
        if (summary.isEmpty()) {
            summary = a.value(QStringLiteral("activation_reason")).toString();
        }
        const QString err = a.value(QStringLiteral("error")).toString();
        it->setText(ColSummary, err.isEmpty() ? summary : err);
        it->setToolTip(ColSummary, a.value(QStringLiteral("rationale")).toString());
        if (status == QLatin1String("running")) {
            m_statusLabel->setText(tr("%1: %2").arg(it->text(ColAgent), a.value(QStringLiteral("message")).toString()));
        }
        const QJsonArray review = a.value(QStringLiteral("review_items")).toArray();
        for (const QJsonValue &r : review) {
            m_reviewList->addItem(QStringLiteral("[%1] %2").arg(it->text(ColAgent), r.toString()));
        }
        if (!err.isEmpty()) {
            m_reviewList->addItem(QStringLiteral("[%1] %2").arg(it->text(ColAgent), err));
        }
    }
    m_progress->setValue(qRound(doc.value(QStringLiteral("progress")).toDouble() * 100.0));
    m_draftProject = doc.value(QStringLiteral("artifacts")).toObject().value(QStringLiteral("kdenlive_project")).toString();
    const double cost = doc.value(QStringLiteral("budget")).toObject().value(QStringLiteral("spent_usd")).toDouble();
    if (m_jobStatus != QLatin1String("running")) {
        m_statusLabel->setText(tr("Job %1 — %2 (%3/%4 agents, external cost $%5)")
                                   .arg(doc.value(QStringLiteral("job_id")).toString(), m_jobStatus)
                                   .arg(done)
                                   .arg(active)
                                   .arg(cost, 0, 'f', 2));
    }
    updateActionStates();
}

void AgentControlCentre::setActiveJobDir(const QString &jobDir)
{
    m_jobDir = jobDir;
    m_lastStatusSeen.clear();
    if (!jobDir.isEmpty()) {
        m_pollTimer->start();
    } else {
        m_pollTimer->stop();
    }
    updateActionStates();
}

QString AgentControlCentre::activeJobDir() const
{
    return m_jobDir;
}

bool AgentControlCentre::refreshFromDisk()
{
    if (m_jobDir.isEmpty()) {
        return false;
    }
    QFile f(QDir(m_jobDir).filePath(QStringLiteral("job.json")));
    if (!f.open(QIODevice::ReadOnly)) {
        return false;
    }
    const QJsonDocument doc = QJsonDocument::fromJson(f.readAll());
    if (!doc.isObject()) {
        return false; // partially written file; next poll will pick it up
    }
    loadJobStatus(doc.object());
    if (m_interrupted && m_jobStatus == QLatin1String("running")) {
        m_jobStatus = QStringLiteral("interrupted");
        m_statusLabel->setText(tr("Job %1 was interrupted — Resume continues from the last checkpoint").arg(QFileInfo(m_jobDir).fileName()));
        updateActionStates();
    }
    const bool terminal = m_jobStatus != QLatin1String("running") && m_jobStatus != QLatin1String("planned");
    if (terminal && m_jobStatus != m_lastStatusSeen) {
        m_lastStatusSeen = m_jobStatus;
        QFile s(QDir(m_jobDir).filePath(QStringLiteral("summary.md")));
        if (s.open(QIODevice::ReadOnly)) {
            m_summaryView->setPlainText(QString::fromUtf8(s.readAll()));
        }
        Q_EMIT jobFinished(m_jobDir, m_jobStatus);
    }
    return true;
}

void AgentControlCentre::recoverJob(const QString &jobDir, bool processAlive)
{
    m_interrupted = !processAlive;
    setActiveJobDir(jobDir);
    refreshFromDisk();
    if (!processAlive) {
        m_pollTimer->stop();
    }
}

void AgentControlCentre::setBusy(bool busy, const QString &text)
{
    if (busy) {
        m_interrupted = false;
    }
    m_startBtn->setEnabled(!busy);
    if (!text.isEmpty()) {
        m_statusLabel->setText(text);
    }
    if (busy) {
        m_jobStatus = QStringLiteral("running");
    }
    updateActionStates();
}

void AgentControlCentre::updateActionStates()
{
    const bool hasJob = !m_jobDir.isEmpty();
    const bool running = m_jobStatus == QLatin1String("running");
    const bool hasAgent = !selectedAgentId().isEmpty() && selectedAgentId() != QLatin1String("executive");
    m_cancelBtn->setEnabled(hasJob && running);
    m_resumeBtn->setEnabled(hasJob && !running &&
                            (m_jobStatus == QLatin1String("cancelled") || m_jobStatus == QLatin1String("interrupted") ||
                             m_jobStatus == QLatin1String("completed_with_errors") || m_jobStatus.startsWith(QLatin1String("blocked"))));
    m_rerunBtn->setEnabled(hasJob && !running && hasAgent);
    m_acceptBtn->setEnabled(hasJob && !running && hasAgent);
    m_rejectBtn->setEnabled(hasJob && !running && hasAgent);
    const bool draft = hasJob && !running && !m_draftProject.isEmpty();
    m_reviewBtn->setEnabled(draft);
    m_openDraftBtn->setEnabled(draft && QFileInfo::exists(m_draftProject));
    m_promoteBtn->setEnabled(draft);
}

QString AgentControlCentre::jobStatus() const
{
    return m_jobStatus;
}

int AgentControlCentre::reviewItemCount() const
{
    return m_reviewList->count();
}

QString AgentControlCentre::agentStatus(const QString &agentId) const
{
    const QTreeWidgetItem *it = agentItem(agentId);
    return it ? it->text(ColStatus) : QString();
}

QString AgentControlCentre::summaryText() const
{
    return m_summaryView->toPlainText();
}

} // namespace BangOnPro
