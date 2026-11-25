/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "AgentControlCentre.h"
#include "BangOnProTheme.h"

#include <QCheckBox>
#include <QColor>
#include <QComboBox>
#include <QDir>
#include <QDoubleSpinBox>
#include <QFile>
#include <QFileInfo>
#include <QFormLayout>
#include <QGridLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QHash>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QListWidgetItem>
#include <QPlainTextEdit>
#include <QProgressBar>
#include <QPushButton>
#include <QSizePolicy>
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
    static const QHash<QString, QString> states = {
        {QStringLiteral("pending"), QStringLiteral("Waiting")},     {QStringLiteral("running"), QStringLiteral("Running")},
        {QStringLiteral("completed"), QStringLiteral("Done")},      {QStringLiteral("skipped"), QStringLiteral("Skipped")},
        {QStringLiteral("failed"), QStringLiteral("Failed")},       {QStringLiteral("blocked"), QStringLiteral("Blocked")},
        {QStringLiteral("cancelled"), QStringLiteral("Cancelled")}, {QStringLiteral("rolled_back"), QStringLiteral("Rolled back")},
    };
    static const QHash<QString, QString> decisions = {
        {QStringLiteral("auto_accepted"), QStringLiteral("applied")},
        {QStringLiteral("accepted"), QStringLiteral("accepted")},
        {QStringLiteral("rejected"), QStringLiteral("rejected")},
        {QStringLiteral("pending"), QStringLiteral("needs approval")},
    };
    QString s = states.value(status, status);
    if (!decision.isEmpty()) {
        s += QStringLiteral(" · ") + decisions.value(decision, decision);
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
    using M = StudioMetrics;
    auto *root = new QVBoxLayout(this);
    root->setContentsMargins(M::kSpace3, M::kSpace3, M::kSpace3, M::kSpace3);
    root->setSpacing(M::kSpace2);

    // Header: what this panel is, and which job it shows (mono meta, signature tally on the status line).
    auto *header = new QHBoxLayout;
    header->setSpacing(M::kSpace2);
    auto *title = new QLabel(tr("Production"), this);
    title->setObjectName(QStringLiteral("acc_title"));
    setTextRole(title, "title");
    m_jobMetaLabel = new QLabel(tr("No job"), this);
    m_jobMetaLabel->setObjectName(QStringLiteral("acc_job_meta"));
    setTextRole(m_jobMetaLabel, "meta");
    m_newJobBtn = new QPushButton(tr("New production"), this);
    m_newJobBtn->setObjectName(QStringLiteral("acc_new_job"));
    m_newJobBtn->setToolTip(tr("Show the production setup"));
    setRole(m_newJobBtn, "quiet");
    m_newJobBtn->setVisible(false);
    header->addWidget(title);
    header->addStretch(1);
    header->addWidget(m_jobMetaLabel);
    header->addWidget(m_newJobBtn);
    root->addLayout(header);

    // 1. Setup — mode, profile, series, brief, options and the one primary action.
    m_setupSection = new QWidget(this);
    m_setupSection->setObjectName(QStringLiteral("acc_setup"));
    auto *setup = new QVBoxLayout(m_setupSection);
    setup->setContentsMargins(0, 0, 0, 0);
    setup->setSpacing(M::kSpace2);
    auto *form = new QFormLayout;
    form->setContentsMargins(0, 0, 0, 0);
    form->setHorizontalSpacing(M::kSpace3);
    form->setVerticalSpacing(M::kSpace2);
    form->setLabelAlignment(Qt::AlignLeft | Qt::AlignVCenter);
    form->setRowWrapPolicy(QFormLayout::WrapLongRows);
    form->setFieldGrowthPolicy(QFormLayout::AllNonFixedFieldsGrow);
    m_modeCombo = new QComboBox(m_setupSection);
    m_modeCombo->setObjectName(QStringLiteral("acc_mode"));
    m_modeCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    m_modeCombo->setMinimumContentsLength(14);
    m_modeCombo->addItem(tr("Manual"), QStringLiteral("manual"));
    m_modeCombo->addItem(tr("Assisted"), QStringLiteral("assisted"));
    m_modeCombo->addItem(tr("Autonomous first cut"), QStringLiteral("autonomous_first_cut"));
    m_modeCombo->addItem(tr("Full production draft"), QStringLiteral("full_production_draft"));
    m_modeCombo->setItemData(0, tr("Agents run only when you ask"), Qt::ToolTipRole);
    m_modeCombo->setItemData(1, tr("Every edit waits for your approval"), Qt::ToolTipRole);
    m_modeCombo->setItemData(2, tr("A reversible first cut in an isolated draft"), Qt::ToolTipRole);
    m_modeCombo->setItemData(3, tr("A complete production draft, isolated until you approve it"), Qt::ToolTipRole);
    m_modeCombo->setCurrentIndex(2);
    m_profileCombo = new QComboBox(m_setupSection);
    m_profileCombo->setObjectName(QStringLiteral("acc_profile"));
    m_profileCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    m_profileCombo->setMinimumContentsLength(14);
    m_profileCombo->addItem(tr("Podcast multicam"), QStringLiteral("podcast_multicam"));
    m_profileCombo->addItem(tr("Gameshow multicam"), QStringLiteral("gameshow_multicam"));
    m_profileCombo->addItem(tr("Sports discussion"), QStringLiteral("sports_discussion"));
    m_profileCombo->addItem(tr("Daily news"), QStringLiteral("daily_news"));
    m_profileCombo->addItem(tr("Interview, 2 cameras"), QStringLiteral("interview_two_camera"));
    m_profileCombo->addItem(tr("Social short 9:16"), QStringLiteral("social_short"));
    m_seriesEdit = new QLineEdit(m_setupSection);
    m_seriesEdit->setObjectName(QStringLiteral("acc_series"));
    m_seriesEdit->setPlaceholderText(tr("Series"));
    m_seriesEdit->setToolTip(tr("Reuses the series' approved production preferences"));
    m_episodeEdit = new QLineEdit(m_setupSection);
    m_episodeEdit->setObjectName(QStringLiteral("acc_episode"));
    m_episodeEdit->setPlaceholderText(tr("Episode title"));
    form->addRow(tr("Mode"), m_modeCombo);
    form->addRow(tr("Profile"), m_profileCombo);
    form->addRow(tr("Series"), m_seriesEdit);
    form->addRow(tr("Episode"), m_episodeEdit);
    setup->addLayout(form);
    auto *briefLabel = new QLabel(tr("Brief"), m_setupSection);
    setTextRole(briefLabel, "secondary");
    m_briefEdit = new QPlainTextEdit(m_setupSection);
    m_briefEdit->setObjectName(QStringLiteral("acc_brief"));
    m_briefEdit->setPlaceholderText(tr("25-minute cut, match cameras, captions, two social clips"));
    m_briefEdit->setFixedHeight(64);
    m_briefEdit->setTabChangesFocus(true);
    setup->addWidget(briefLabel);
    setup->addWidget(m_briefEdit);
    auto *opts = new QHBoxLayout;
    opts->setSpacing(M::kSpace2);
    m_exportCheck = new QCheckBox(tr("Render master"), m_setupSection);
    m_exportCheck->setObjectName(QStringLiteral("acc_export"));
    auto *budgetLabel = new QLabel(tr("External budget"), m_setupSection);
    setTextRole(budgetLabel, "secondary");
    m_budgetSpin = new QDoubleSpinBox(m_setupSection);
    m_budgetSpin->setObjectName(QStringLiteral("acc_budget"));
    m_budgetSpin->setPrefix(QStringLiteral("$"));
    m_budgetSpin->setRange(0.0, 500.0);
    m_budgetSpin->setDecimals(2);
    m_budgetSpin->setToolTip(tr("Spending limit for external models in this job. 0 keeps everything local."));
    budgetLabel->setBuddy(m_budgetSpin);
    opts->addWidget(m_exportCheck);
    opts->addStretch(1);
    opts->addWidget(budgetLabel);
    opts->addWidget(m_budgetSpin);
    setup->addLayout(opts);
    m_startBtn = new QPushButton(tr("Start production"), m_setupSection);
    m_startBtn->setObjectName(QStringLiteral("acc_start"));
    m_startBtn->setProperty("bangOnProPrimary", true);
    m_startBtn->setDefault(true);
    setup->addWidget(m_startBtn);
    root->addWidget(m_setupSection);

    // 2. Run — thin progress line, the current step, cancel / resume.
    m_progress = new QProgressBar(this);
    m_progress->setObjectName(QStringLiteral("acc_progress"));
    m_progress->setRange(0, 100);
    m_progress->setTextVisible(false);
    m_progress->setAccessibleName(tr("Production progress"));
    root->addWidget(m_progress);
    auto *runRow = new QHBoxLayout;
    runRow->setSpacing(M::kSpace2);
    m_statusLabel = new QLabel(tr("No job running"), this);
    m_statusLabel->setObjectName(QStringLiteral("acc_status"));
    m_statusLabel->setWordWrap(true);
    setTextRole(m_statusLabel, "secondary");
    m_cancelBtn = new QPushButton(tr("Cancel"), this);
    m_cancelBtn->setObjectName(QStringLiteral("acc_cancel"));
    m_resumeBtn = new QPushButton(tr("Resume"), this);
    m_resumeBtn->setObjectName(QStringLiteral("acc_resume"));
    m_resumeBtn->setToolTip(tr("Continue from the last checkpoint"));
    runRow->addWidget(m_statusLabel, 1);
    runRow->addWidget(m_cancelBtn);
    runRow->addWidget(m_resumeBtn);
    root->addLayout(runRow);

    // 3. Agents — tally, name (tick to enable for the next job), state, confidence, operations, engine, result.
    auto *split = new QSplitter(Qt::Vertical, this);
    split->setChildrenCollapsible(false);
    m_agentTree = new QTreeWidget(split);
    m_agentTree->setObjectName(QStringLiteral("acc_agents"));
    m_agentTree->setHeaderLabels({tr("Agent"), tr("State"), tr("Conf."), tr("Ops"), tr("Engine"), tr("Result")});
    m_agentTree->setRootIsDecorated(false);
    m_agentTree->setUniformRowHeights(true);
    m_agentTree->setAlternatingRowColors(false);
    m_agentTree->setIconSize(QSize(12, 12));
    m_agentTree->header()->setMinimumSectionSize(36);
    m_agentTree->header()->setStretchLastSection(true);
    m_agentTree->header()->setDefaultAlignment(Qt::AlignLeft | Qt::AlignVCenter);
    m_agentTree->setHorizontalScrollMode(QAbstractItemView::ScrollPerPixel);
    m_agentTree->setTextElideMode(Qt::ElideRight);
    for (const AgentRow &r : kAgents) {
        auto *item = new QTreeWidgetItem(m_agentTree);
        item->setText(ColAgent, tr(r.name));
        item->setData(ColAgent, kIdRole, QString::fromLatin1(r.id));
        item->setIcon(ColAgent, tallyIcon(TallyState::Idle));
        item->setText(ColStatus, tr("Idle"));
        item->setTextAlignment(ColConfidence, Qt::AlignRight | Qt::AlignVCenter);
        item->setTextAlignment(ColOps, Qt::AlignRight | Qt::AlignVCenter);
        const bool locked = qstrcmp(r.id, "executive") == 0;
        // The Executive Editing Agent plans and verifies every job and can never be disabled.
        item->setFlags(locked ? (item->flags() & ~Qt::ItemIsUserCheckable) : (item->flags() | Qt::ItemIsUserCheckable));
        item->setCheckState(ColAgent, Qt::Checked); // executive shows a fixed check so every name aligns
        item->setToolTip(ColAgent, locked ? tr("Plans, delegates and verifies every job") : tr("Untick to leave this agent out of the next job"));
    }
    m_agentTree->setColumnWidth(ColAgent, 188);
    m_agentTree->setColumnWidth(ColStatus, 96);
    m_agentTree->setColumnWidth(ColConfidence, 48);
    m_agentTree->setColumnWidth(ColOps, 40);
    m_agentTree->setColumnWidth(ColEngine, 88);
    m_agentTree->setMinimumHeight(160);
    m_agentTree->setAccessibleName(tr("Production agents"));

    // 4. Review — act on the selected agent, see what needs attention, approve the draft.
    auto *lower = new QWidget(split);
    auto *lowerLay = new QVBoxLayout(lower);
    lowerLay->setContentsMargins(0, M::kSpace2, 0, 0);
    lowerLay->setSpacing(M::kSpace2);
    auto *agentRow = new QHBoxLayout;
    agentRow->setSpacing(M::kSpace2);
    m_acceptBtn = new QPushButton(tr("Accept"), lower);
    m_acceptBtn->setObjectName(QStringLiteral("acc_accept"));
    m_acceptBtn->setToolTip(tr("Keep this agent's edits in the draft"));
    setRole(m_acceptBtn, "accept");
    m_rejectBtn = new QPushButton(tr("Reject"), lower);
    m_rejectBtn->setObjectName(QStringLiteral("acc_reject"));
    m_rejectBtn->setToolTip(tr("Remove this agent's edits; dependent edits are removed or marked stale"));
    setRole(m_rejectBtn, "danger");
    m_rerunBtn = new QPushButton(tr("Rerun"), lower);
    m_rerunBtn->setObjectName(QStringLiteral("acc_rerun"));
    m_rerunBtn->setToolTip(tr("Run this agent again from its checkpoint"));
    agentRow->addWidget(m_acceptBtn);
    agentRow->addWidget(m_rejectBtn);
    agentRow->addWidget(m_rerunBtn);
    agentRow->addStretch(1);
    lowerLay->addLayout(agentRow);
    m_reviewTitle = new QLabel(tr("Review"), lower);
    m_reviewTitle->setObjectName(QStringLiteral("acc_review_title"));
    setTextRole(m_reviewTitle, "section");
    lowerLay->addWidget(m_reviewTitle);
    m_reviewList = new QListWidget(lower);
    m_reviewList->setObjectName(QStringLiteral("acc_review"));
    m_reviewList->setWordWrap(true);
    m_reviewList->setAccessibleName(tr("Items that need your review"));
    lowerLay->addWidget(m_reviewList, 2);
    m_summaryView = new QPlainTextEdit(lower);
    m_summaryView->setObjectName(QStringLiteral("acc_summary"));
    m_summaryView->setReadOnly(true);
    m_summaryView->setPlaceholderText(tr("The production summary appears here when the job finishes"));
    lowerLay->addWidget(m_summaryView, 1);
    auto *draftRow = new QGridLayout;
    draftRow->setHorizontalSpacing(M::kSpace2);
    draftRow->setVerticalSpacing(M::kSpace2);
    m_reviewBtn = new QPushButton(tr("Review in timeline"), lower);
    m_reviewBtn->setObjectName(QStringLiteral("acc_review_draft"));
    m_reviewBtn->setToolTip(tr("Stage the draft as one reviewable, undoable proposal on the current timeline"));
    m_openDraftBtn = new QPushButton(tr("Open draft"), lower);
    m_openDraftBtn->setObjectName(QStringLiteral("acc_open_draft"));
    m_openDraftBtn->setToolTip(tr("Open the draft as its own editable project"));
    m_promoteBtn = new QPushButton(tr("Save approved draft…"), lower);
    m_promoteBtn->setObjectName(QStringLiteral("acc_promote"));
    m_promoteBtn->setToolTip(tr("Copy the draft into a project file. Replacing an existing edit asks for approval and keeps a backup."));
    draftRow->addWidget(m_reviewBtn, 0, 0);
    draftRow->addWidget(m_openDraftBtn, 0, 1);
    draftRow->addWidget(m_promoteBtn, 1, 0, 1, 2);
    lowerLay->addLayout(draftRow);
    split->addWidget(m_agentTree);
    split->addWidget(lower);
    split->setStretchFactor(0, 3);
    split->setStretchFactor(1, 2);
    root->addWidget(split, 1);

    setMinimumWidth(280);
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);
    setTabOrder(m_modeCombo, m_profileCombo);
    setTabOrder(m_profileCombo, m_seriesEdit);
    setTabOrder(m_seriesEdit, m_episodeEdit);
    setTabOrder(m_episodeEdit, m_briefEdit);
    setTabOrder(m_briefEdit, m_exportCheck);
    setTabOrder(m_exportCheck, m_budgetSpin);
    setTabOrder(m_budgetSpin, m_startBtn);

    connect(m_newJobBtn, &QPushButton::clicked, this, [this]() {
        m_setupSection->setVisible(true);
        m_newJobBtn->setVisible(false);
        m_modeCombo->setFocus();
    });
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
        const QString decision = a.value(QStringLiteral("decision")).toString();
        const bool stale = a.value(QStringLiteral("stale")).toBool();
        const bool isActive = a.value(QStringLiteral("active")).toBool();
        const QJsonArray review = a.value(QStringLiteral("review_items")).toArray();
        active += isActive ? 1 : 0;
        done += (isActive && (status == QLatin1String("completed") || status == QLatin1String("failed"))) ? 1 : 0;
        TallyState tally = TallyState::Idle;
        if (status == QLatin1String("running")) {
            tally = TallyState::Running;
        } else if (status == QLatin1String("failed") || status == QLatin1String("blocked") || status == QLatin1String("cancelled")) {
            tally = TallyState::Failed;
        } else if (status == QLatin1String("skipped") || decision == QLatin1String("rejected") || status == QLatin1String("rolled_back")) {
            tally = TallyState::Skipped;
        } else if (status == QLatin1String("completed")) {
            tally = (stale || decision == QLatin1String("pending") || !review.isEmpty()) ? TallyState::Review : TallyState::Done;
        }
        it->setIcon(ColAgent, tallyIcon(tally));
        it->setText(ColStatus, statusLabel(status, decision, stale));
        it->setForeground(ColStatus, tally == TallyState::Idle || tally == TallyState::Skipped
                                         ? QColor(QString::fromLatin1(StudioPaletteTokens::kTextMuted))
                                         : QColor(QString::fromLatin1(StudioPaletteTokens::kTextSecondary)));
        const QJsonValue conf = a.value(QStringLiteral("confidence"));
        it->setText(ColConfidence, conf.isDouble() ? QString::number(conf.toDouble(), 'f', 2) : QString());
        it->setText(ColOps, QString::number(a.value(QStringLiteral("operation_count")).toInt()));
        const QJsonValue neural = a.value(QStringLiteral("neural_inference"));
        it->setText(ColEngine, neural.isBool() ? (neural.toBool() ? tr("Neural") : tr("Fallback")) : tr("Rules"));
        QString summary = a.value(QStringLiteral("summary")).toString();
        if (summary.isEmpty()) {
            summary = a.value(QStringLiteral("activation_reason")).toString();
        }
        const QString err = a.value(QStringLiteral("error")).toString();
        it->setText(ColSummary, err.isEmpty() ? summary : err);
        it->setToolTip(ColSummary, a.value(QStringLiteral("rationale")).toString());
        if (status == QLatin1String("running")) {
            m_statusLabel->setText(tr("%1 · %2").arg(it->text(ColAgent), a.value(QStringLiteral("message")).toString()));
        }
        for (const QJsonValue &r : review) {
            auto *row = new QListWidgetItem(tallyIcon(TallyState::Review), QStringLiteral("%1 · %2").arg(it->text(ColAgent), r.toString()), m_reviewList);
            row->setData(kIdRole, it->data(ColAgent, kIdRole));
        }
        if (!err.isEmpty()) {
            auto *row = new QListWidgetItem(tallyIcon(TallyState::Failed), QStringLiteral("%1 · %2").arg(it->text(ColAgent), err), m_reviewList);
            row->setData(kIdRole, it->data(ColAgent, kIdRole));
        }
    }
    m_reviewTitle->setText(m_reviewList->count() > 0 ? tr("Review · %1").arg(m_reviewList->count()) : tr("Review"));
    m_progress->setValue(qRound(doc.value(QStringLiteral("progress")).toDouble() * 100.0));
    m_draftProject = doc.value(QStringLiteral("artifacts")).toObject().value(QStringLiteral("kdenlive_project")).toString();
    const double cost = doc.value(QStringLiteral("budget")).toObject().value(QStringLiteral("spent_usd")).toDouble();
    m_jobMetaLabel->setText(doc.value(QStringLiteral("job_id")).toString());
    if (m_jobStatus != QLatin1String("running")) {
        m_statusLabel->setText(tr("%1 · %2 of %3 agents · external $%4")
                                   .arg(QString(m_jobStatus).replace(QLatin1Char('_'), QLatin1Char(' ')))
                                   .arg(done)
                                   .arg(active)
                                   .arg(cost, 0, 'f', 2));
    }
    updateActionStates();
}

void AgentControlCentre::setActiveJobDir(const QString &jobDir)
{
    if (jobDir != m_jobDir) {
        m_setupAutoCollapsed = false;
    }
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
    m_cancelBtn->setVisible(running || !hasJob);
    m_resumeBtn->setVisible(m_resumeBtn->isEnabled());
    m_rerunBtn->setEnabled(hasJob && !running && hasAgent);
    m_acceptBtn->setEnabled(hasJob && !running && hasAgent);
    m_rejectBtn->setEnabled(hasJob && !running && hasAgent);
    m_startBtn->setEnabled(!running);
    const bool draft = hasJob && !running && !m_draftProject.isEmpty();
    m_reviewBtn->setEnabled(draft);
    m_openDraftBtn->setEnabled(draft && QFileInfo::exists(m_draftProject));
    m_promoteBtn->setEnabled(draft);
    // While reviewing a finished draft the setup collapses so review and approval stay in view.
    if (draft && !m_setupAutoCollapsed) {
        m_setupAutoCollapsed = true;
        m_setupSection->setVisible(false);
        m_newJobBtn->setVisible(true);
    }
    // One primary action at a time: Start before a run, Review once a draft is ready.
    const bool reviewIsPrimary = draft;
    if (m_reviewBtn->property("bangOnProPrimary").toBool() != reviewIsPrimary) {
        m_reviewBtn->setProperty("bangOnProPrimary", reviewIsPrimary);
        m_startBtn->setProperty("bangOnProPrimary", !reviewIsPrimary);
        setRole(m_reviewBtn, reviewIsPrimary ? "primary" : "secondary");
        setRole(m_startBtn, reviewIsPrimary ? "secondary" : "primary");
    }
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
