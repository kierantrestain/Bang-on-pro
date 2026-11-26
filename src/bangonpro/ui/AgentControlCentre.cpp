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
#include <QMenu>
#include <QPlainTextEdit>
#include <QProgressBar>
#include <QPushButton>
#include <QSizePolicy>
#include <QSpinBox>
#include <QSplitter>
#include <QStringList>
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
constexpr int kJobDirRole = Qt::UserRole + 2;
constexpr int kQueueIdRole = Qt::UserRole + 3;
constexpr int kQueueStatusRole = Qt::UserRole + 4;

QString minutesSeconds(double seconds)
{
    const int s = qMax(0, qRound(seconds));
    return QStringLiteral("%1:%2").arg(s / 60).arg(s % 60, 2, 10, QLatin1Char('0'));
}

QString plainStatus(const QString &status)
{
    static const QHash<QString, QString> words = {
        {QStringLiteral("queued"), QStringLiteral("Queued")},
        {QStringLiteral("interrupted"), QStringLiteral("Interrupted · resumes next")},
        {QStringLiteral("running"), QStringLiteral("Running")},
        {QStringLiteral("completed"), QStringLiteral("Done")},
        {QStringLiteral("needs_attention"), QStringLiteral("Needs attention")},
        {QStringLiteral("failed"), QStringLiteral("Failed")},
        {QStringLiteral("cancelled"), QStringLiteral("Cancelled")},
        {QStringLiteral("draft_ready"), QStringLiteral("Draft ready")},
        {QStringLiteral("awaiting_review"), QStringLiteral("Needs approval")},
        {QStringLiteral("completed_with_errors"), QStringLiteral("Done with errors")},
        {QStringLiteral("blocked_insufficient_storage"), QStringLiteral("Not enough space")},
        {QStringLiteral("needs_clarification"), QStringLiteral("Needs a clearer brief")},
        {QStringLiteral("planned_manual"), QStringLiteral("Planned")},
    };
    return words.value(status, QString(status).replace(QLatin1Char('_'), QLatin1Char(' ')));
}

TallyState historyTally(const QString &status)
{
    if (status == QLatin1String("running")) {
        return TallyState::Running;
    }
    if (status == QLatin1String("failed") || status.startsWith(QLatin1String("blocked")) || status == QLatin1String("completed_with_errors")) {
        return TallyState::Failed;
    }
    if (status == QLatin1String("draft_ready") || status == QLatin1String("completed")) {
        return TallyState::Done;
    }
    if (status == QLatin1String("cancelled")) {
        return TallyState::Skipped;
    }
    if (status == QLatin1String("queued") || status == QLatin1String("planned_manual")) {
        return TallyState::Idle;
    }
    return TallyState::Review;
}

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
    m_queueCheck = new QCheckBox(tr("Queue"), m_setupSection);
    m_queueCheck->setObjectName(QStringLiteral("acc_queue"));
    m_queueCheck->setToolTip(tr("Add to the production queue instead of starting now. Queued productions run one after another."));
    opts->addWidget(m_exportCheck);
    opts->addWidget(m_queueCheck);
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
    buildTimingRow(root);

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
    // V1.3: ask the department for a change in plain words; only the affected work reruns.
    auto *reviseRow = new QHBoxLayout;
    reviseRow->setSpacing(M::kSpace2);
    m_reviseEdit = new QLineEdit(lower);
    m_reviseEdit->setObjectName(QStringLiteral("acc_revise_text"));
    m_reviseEdit->setPlaceholderText(tr("Ask for a change, e.g. tighten the opening"));
    m_reviseEdit->setAccessibleName(tr("Revision instruction"));
    m_reviseSelection = new QCheckBox(tr("Selection only"), lower);
    m_reviseSelection->setObjectName(QStringLiteral("acc_revise_selection"));
    m_reviseSelection->setToolTip(tr("Change only the timeline selection; everything else stays as it is"));
    m_reviseBtn = new QPushButton(tr("Revise"), lower);
    m_reviseBtn->setObjectName(QStringLiteral("acc_revise"));
    m_reviseBtn->setToolTip(tr("Apply the change to the draft; unaffected work is reused"));
    m_undoRevisionBtn = new QPushButton(tr("Undo"), lower);
    m_undoRevisionBtn->setObjectName(QStringLiteral("acc_revise_undo"));
    m_undoRevisionBtn->setToolTip(tr("Restore the draft from before the last revision"));
    reviseRow->addWidget(m_reviseEdit, 1);
    reviseRow->addWidget(m_reviseSelection);
    reviseRow->addWidget(m_reviseBtn);
    reviseRow->addWidget(m_undoRevisionBtn);
    lowerLay->addLayout(reviseRow);
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
    // 5. Queue and history — compact list; double-click opens a job, right-click manages a queued one.
    m_historyTitle = new QLabel(tr("Queue and history"), lower);
    m_historyTitle->setObjectName(QStringLiteral("acc_history_title"));
    setTextRole(m_historyTitle, "section");
    m_historyList = new QListWidget(lower);
    m_historyList->setObjectName(QStringLiteral("acc_history"));
    m_historyList->setAccessibleName(tr("Production queue and recent jobs"));
    m_historyList->setToolTip(tr("Double-click to open a production. Right-click a queued production to cancel or remove it."));
    m_historyList->setContextMenuPolicy(Qt::CustomContextMenu);
    m_historyList->setMaximumHeight(5 * M::kRowHeight + M::kSpace2);
    m_historyList->setTextElideMode(Qt::ElideRight);
    lowerLay->addWidget(m_historyTitle);
    lowerLay->addWidget(m_historyList);
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
    connect(m_startBtn, &QPushButton::clicked, this, [this]() {
        noteActivity(QStringLiteral("setup"));
        if (queueSelected()) {
            Q_EMIT queueJobRequested(buildRunArguments({}));
        } else {
            Q_EMIT startJobRequested(buildRunArguments({}));
        }
    });
    connect(m_queueCheck, &QCheckBox::toggled, this, [this](bool queued) {
        m_startBtn->setText(queued ? tr("Add to queue") : tr("Start production"));
        updateActionStates();
    });
    // Timed sessions: configuring is setup, inspecting is review, rejecting or rerunning is correction,
    // saving the approved draft is approval (editing elsewhere in the editor is reported by the integration).
    const auto setupActivity = [this]() { noteActivity(QStringLiteral("setup")); };
    connect(m_modeCombo, QOverload<int>::of(&QComboBox::activated), this, setupActivity);
    connect(m_profileCombo, QOverload<int>::of(&QComboBox::activated), this, setupActivity);
    connect(m_seriesEdit, &QLineEdit::textEdited, this, setupActivity);
    connect(m_episodeEdit, &QLineEdit::textEdited, this, setupActivity);
    connect(m_briefEdit, &QPlainTextEdit::textChanged, this, [this]() {
        if (m_briefEdit->hasFocus()) {
            noteActivity(QStringLiteral("setup"));
        }
    });
    connect(m_agentTree, &QTreeWidget::itemClicked, this, [this]() { noteActivity(m_jobDir.isEmpty() ? QStringLiteral("setup") : QStringLiteral("review")); });
    connect(m_reviewList, &QListWidget::itemClicked, this, [this](QListWidgetItem *item) {
        noteActivity(QStringLiteral("review"));
        if (item) {
            selectAgent(item->data(kIdRole).toString());
        }
    });
    connect(m_acceptBtn, &QPushButton::clicked, this, [this]() { noteActivity(QStringLiteral("review")); });
    connect(m_rejectBtn, &QPushButton::clicked, this, [this]() { noteActivity(QStringLiteral("correction")); });
    connect(m_reviseBtn, &QPushButton::clicked, this, [this]() {
        const QString text = m_reviseEdit->text().trimmed();
        if (!m_jobDir.isEmpty() && !text.isEmpty()) {
            noteActivity(QStringLiteral("correction"));
            Q_EMIT reviseDraftRequested(m_jobDir, text, m_reviseSelection->isChecked());
        }
    });
    connect(m_reviseEdit, &QLineEdit::returnPressed, m_reviseBtn, &QPushButton::click);
    connect(m_undoRevisionBtn, &QPushButton::clicked, this, [this]() {
        if (!m_jobDir.isEmpty()) {
            noteActivity(QStringLiteral("correction"));
            Q_EMIT undoRevisionRequested(m_jobDir);
        }
    });
    connect(m_rerunBtn, &QPushButton::clicked, this, [this]() { noteActivity(QStringLiteral("correction")); });
    connect(m_reviewBtn, &QPushButton::clicked, this, [this]() { noteActivity(QStringLiteral("review")); });
    connect(m_openDraftBtn, &QPushButton::clicked, this, [this]() { noteActivity(QStringLiteral("review")); });
    connect(m_promoteBtn, &QPushButton::clicked, this, [this]() { noteActivity(QStringLiteral("approval")); });
    connect(m_historyList, &QListWidget::itemActivated, this, [this](QListWidgetItem *item) {
        const QString dir = item ? item->data(kJobDirRole).toString() : QString();
        if (!dir.isEmpty() && QFileInfo::exists(QDir(dir).filePath(QStringLiteral("job.json")))) {
            Q_EMIT historyJobActivated(dir);
        }
    });
    connect(m_historyList, &QListWidget::customContextMenuRequested, this, [this](const QPoint &pos) {
        QListWidgetItem *item = m_historyList->itemAt(pos);
        const QString queueId = item ? item->data(kQueueIdRole).toString() : QString();
        if (queueId.isEmpty()) {
            return;
        }
        const QString status = item->data(kQueueStatusRole).toString();
        QMenu menu(this);
        QAction *cancel = menu.addAction(tr("Cancel"));
        cancel->setEnabled(status == QLatin1String("queued") || status == QLatin1String("interrupted") || status == QLatin1String("running"));
        QAction *remove = menu.addAction(tr("Remove from list"));
        remove->setEnabled(status != QLatin1String("running"));
        QAction *chosen = menu.exec(m_historyList->viewport()->mapToGlobal(pos));
        if (chosen == cancel) {
            Q_EMIT queueEntryActionRequested(queueId, QStringLiteral("cancel"));
        } else if (chosen == remove) {
            Q_EMIT queueEntryActionRequested(queueId, QStringLiteral("remove"));
        }
    });
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

void AgentControlCentre::buildTimingRow(QVBoxLayout *root)
{
    using M = StudioMetrics;
    // Timed editing session for the 60-85 % measurement: one quiet control until a session is open.
    m_timingRow = new QWidget(this);
    m_timingRow->setObjectName(QStringLiteral("acc_timing"));
    auto *row = new QHBoxLayout(m_timingRow);
    row->setContentsMargins(0, 0, 0, 0);
    row->setSpacing(M::kSpace2);
    m_timingKind = new QComboBox(m_timingRow);
    m_timingKind->setObjectName(QStringLiteral("acc_timing_kind"));
    m_timingKind->addItem(tr("Agent-assisted"), QStringLiteral("agent_assisted"));
    m_timingKind->addItem(tr("Manual baseline"), QStringLiteral("manual_baseline"));
    m_timingKind->setToolTip(tr("Manual baseline: you edit this deliverable without the agents. Agent-assisted: you review and finish the agents' draft."));
    m_timingStartBtn = new QPushButton(tr("Start timing"), m_timingRow);
    m_timingStartBtn->setObjectName(QStringLiteral("acc_timing_start"));
    m_timingStartBtn->setToolTip(tr("Time your work on this deliverable. Machine processing is recorded separately."));
    setRole(m_timingStartBtn, "quiet");
    m_timingLabel = new QLabel(m_timingRow);
    m_timingLabel->setObjectName(QStringLiteral("acc_timing_label"));
    setTextRole(m_timingLabel, "meta");
    m_timingPauseBtn = new QPushButton(tr("Pause"), m_timingRow);
    m_timingPauseBtn->setObjectName(QStringLiteral("acc_timing_pause"));
    setRole(m_timingPauseBtn, "quiet");
    m_timingRating = new QSpinBox(m_timingRow);
    m_timingRating->setObjectName(QStringLiteral("acc_timing_rating"));
    m_timingRating->setRange(1, 5);
    m_timingRating->setValue(4);
    m_timingRating->setPrefix(tr("Quality "));
    m_timingRating->setToolTip(tr("Your quality rating for the finished deliverable, 1 to 5"));
    m_timingFinishBtn = new QPushButton(tr("Finish"), m_timingRow);
    m_timingFinishBtn->setObjectName(QStringLiteral("acc_timing_finish"));
    m_timingFinishBtn->setToolTip(tr("Stop timing and save the session with your quality rating"));
    row->addWidget(m_timingKind);
    row->addWidget(m_timingStartBtn);
    row->addWidget(m_timingLabel, 1);
    row->addWidget(m_timingPauseBtn);
    row->addWidget(m_timingRating);
    row->addWidget(m_timingFinishBtn);
    root->addWidget(m_timingRow);
    m_timingClock = new QTimer(this);
    m_timingClock->setInterval(1000);
    connect(m_timingClock, &QTimer::timeout, this, &AgentControlCentre::updateTimingLabel);
    connect(m_timingStartBtn, &QPushButton::clicked, this, [this]() { Q_EMIT timingStartRequested(m_timingKind->currentData().toString(), timingDeliverable()); });
    connect(m_timingPauseBtn, &QPushButton::clicked, this, [this]() { Q_EMIT timingPauseToggled(!m_timingPaused); });
    connect(m_timingFinishBtn, &QPushButton::clicked, this, [this]() { Q_EMIT timingFinishRequested(m_timingRating->value()); });
    setTimingActive(false);
}

void AgentControlCentre::setTimingActive(bool active, bool paused)
{
    if (active && !m_timingActive) {
        m_timingAccumulatedMs = 0;
        m_lastActivityCategory.clear();
    }
    if (m_timingActive && !m_timingPaused && m_timingElapsed.isValid()) {
        m_timingAccumulatedMs += m_timingElapsed.elapsed();
    }
    m_timingActive = active;
    m_timingPaused = active && paused;
    if (m_timingActive && !m_timingPaused) {
        m_timingElapsed.start();
        m_timingClock->start();
    } else {
        m_timingElapsed.invalidate();
        m_timingClock->stop();
    }
    m_timingKind->setVisible(!active);
    m_timingStartBtn->setVisible(!active);
    m_timingLabel->setVisible(active);
    m_timingPauseBtn->setVisible(active);
    m_timingRating->setVisible(active);
    m_timingFinishBtn->setVisible(active);
    m_timingPauseBtn->setText(m_timingPaused ? tr("Resume") : tr("Pause"));
    updateTimingLabel();
}

void AgentControlCentre::updateTimingLabel()
{
    const qint64 ms = m_timingAccumulatedMs + (m_timingElapsed.isValid() ? m_timingElapsed.elapsed() : 0);
    const QString kind = m_timingKind->currentData().toString() == QLatin1String("manual_baseline") ? tr("manual") : tr("assisted");
    m_timingLabel->setText(m_timingPaused ? tr("Paused · %1 · %2").arg(kind, minutesSeconds(ms / 1000.0)) : tr("Timing · %1 · %2").arg(kind, minutesSeconds(ms / 1000.0)));
}

bool AgentControlCentre::timingActive() const
{
    return m_timingActive;
}

bool AgentControlCentre::timingPaused() const
{
    return m_timingPaused;
}

QString AgentControlCentre::timingDeliverable() const
{
    const QString episode = m_episodeEdit->text().trimmed();
    const QString series = m_seriesEdit->text().trimmed();
    if (!episode.isEmpty()) {
        return series.isEmpty() ? episode : series + QStringLiteral(" · ") + episode;
    }
    if (!series.isEmpty()) {
        return series;
    }
    return m_jobMetaLabel->text() == tr("No job") ? QString() : m_jobMetaLabel->text();
}

void AgentControlCentre::setActivityHeartbeatSec(int seconds)
{
    m_activityHeartbeatSec = qMax(0, seconds);
}

void AgentControlCentre::noteActivity(const QString &category)
{
    if (!m_timingActive || m_timingPaused) {
        return;
    }
    const bool due = !m_lastActivityAt.isValid() || m_lastActivityAt.elapsed() >= qint64(m_activityHeartbeatSec) * 1000;
    if (category == m_lastActivityCategory && !due) {
        return;
    }
    m_lastActivityCategory = category;
    m_lastActivityAt.start();
    Q_EMIT measurementActivity(category);
}

bool AgentControlCentre::queueSelected() const
{
    return m_queueCheck->isChecked();
}

void AgentControlCentre::setQueueSelected(bool queued)
{
    m_queueCheck->setChecked(queued);
}

void AgentControlCentre::addCustomProfile(const QString &profileId, const QString &displayName)
{
    if (profileId.isEmpty() || m_profileCombo->findData(profileId) >= 0) {
        return;
    }
    m_profileCombo->addItem(displayName.isEmpty() ? profileId : displayName, profileId);
}

void AgentControlCentre::loadHistory(const QJsonObject &payload)
{
    m_historyList->clear();
    QStringList shownDirs;
    // Active queue entries first (waiting, interrupted or running), then the most recent jobs.
    for (const QJsonValue &v : payload.value(QStringLiteral("queue")).toArray()) {
        const QJsonObject e = v.toObject();
        const QString status = e.value(QStringLiteral("status")).toString();
        if (status != QLatin1String("queued") && status != QLatin1String("interrupted") && status != QLatin1String("running")) {
            continue;
        }
        auto *row = new QListWidgetItem(tallyIcon(historyTally(status)),
                                        QStringLiteral("%1 · %2").arg(e.value(QStringLiteral("label")).toString(), plainStatus(status)), m_historyList);
        row->setData(kQueueIdRole, e.value(QStringLiteral("queue_id")).toString());
        row->setData(kQueueStatusRole, status);
        row->setData(kJobDirRole, e.value(QStringLiteral("job_dir")).toString());
        row->setToolTip(QStringLiteral("%1 · priority %2").arg(e.value(QStringLiteral("job_id")).toString()).arg(e.value(QStringLiteral("priority")).toInt()));
        shownDirs << e.value(QStringLiteral("job_dir")).toString();
    }
    for (const QJsonValue &v : payload.value(QStringLiteral("jobs")).toArray()) {
        const QJsonObject j = v.toObject();
        const QString dir = j.value(QStringLiteral("job_dir")).toString();
        if (shownDirs.contains(dir)) {
            continue;
        }
        const QString status = j.value(QStringLiteral("status")).toString();
        QString label = j.value(QStringLiteral("episode_title")).toString();
        if (label.isEmpty()) {
            label = j.value(QStringLiteral("job_id")).toString();
        }
        auto *row = new QListWidgetItem(tallyIcon(historyTally(status)), QStringLiteral("%1 · %2").arg(label, plainStatus(status)), m_historyList);
        row->setData(kJobDirRole, dir);
        const QJsonValue machine = j.value(QStringLiteral("processing_elapsed_sec"));
        row->setToolTip(QStringLiteral("%1 · %2 · machine %3")
                            .arg(j.value(QStringLiteral("job_id")).toString(), j.value(QStringLiteral("profile_id")).toString(),
                                 machine.isDouble() ? minutesSeconds(machine.toDouble()) : tr("not recorded")));
    }
    m_historyTitle->setText(m_historyList->count() > 0 ? tr("Queue and history · %1").arg(m_historyList->count()) : tr("Queue and history"));
}

int AgentControlCentre::historyCount() const
{
    return m_historyList->count();
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
            // V1.2.2: say what to do, in plain words; the raw error stays in the tooltip.
            const QJsonObject help = a.value(QStringLiteral("help")).toObject();
            const QString text = help.isEmpty() ? err : QStringLiteral("%1. %2").arg(help.value(QStringLiteral("title")).toString(), help.value(QStringLiteral("fix")).toString());
            auto *row = new QListWidgetItem(tallyIcon(TallyState::Failed), QStringLiteral("%1 · %2").arg(it->text(ColAgent), text), m_reviewList);
            row->setData(kIdRole, it->data(ColAgent, kIdRole));
            row->setToolTip(err);
        }
    }
    const QJsonObject jobHelp = doc.value(QStringLiteral("error_help")).toObject();
    if (!jobHelp.isEmpty()) {
        auto *row = new QListWidgetItem(tallyIcon(TallyState::Failed),
                                        QStringLiteral("%1. %2").arg(jobHelp.value(QStringLiteral("title")).toString(), jobHelp.value(QStringLiteral("fix")).toString()),
                                        m_reviewList);
        row->setToolTip(doc.value(QStringLiteral("error")).toString());
    }
    m_reviewTitle->setText(m_reviewList->count() > 0 ? tr("Review · %1").arg(m_reviewList->count()) : tr("Review"));
    m_progress->setValue(qRound(doc.value(QStringLiteral("progress")).toDouble() * 100.0));
    m_draftProject = doc.value(QStringLiteral("artifacts")).toObject().value(QStringLiteral("kdenlive_project")).toString();
    const double cost = doc.value(QStringLiteral("budget")).toObject().value(QStringLiteral("spent_usd")).toDouble();
    m_revisionCount = 0;
    for (const QJsonValue &r : doc.value(QStringLiteral("revisions")).toArray()) {
        m_revisionCount += r.toObject().value(QStringLiteral("undone")).toBool() ? 0 : 1;
    }
    const QJsonObject lastRev = doc.value(QStringLiteral("last_revision")).toObject();
    m_jobMetaLabel->setText(doc.value(QStringLiteral("job_id")).toString());
    if (m_jobStatus != QLatin1String("running")) {
        // Machine time is shown on its own; it is never counted as editor time.
        const QJsonValue machine = doc.value(QStringLiteral("processing_elapsed_sec"));
        m_statusLabel->setText(tr("%1 · %2 of %3 agents · machine %4 · external $%5")
                                   .arg(QString(m_jobStatus).replace(QLatin1Char('_'), QLatin1Char(' ')))
                                   .arg(done)
                                   .arg(active)
                                   .arg(machine.isDouble() ? minutesSeconds(machine.toDouble()) : tr("–"))
                                   .arg(cost, 0, 'f', 2));
        // A revision the department could not act on is explained, never silently dropped.
        const QString revMsg = lastRev.value(QStringLiteral("message")).toString();
        if (!revMsg.isEmpty()) {
            m_statusLabel->setText(revMsg);
        }
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

void AgentControlCentre::loadAgentSession(const QJsonObject &presence, const QJsonObject &agentEdits)
{
    const QString client = presence.value(QStringLiteral("client")).toString(tr("Reasoning client"));
    const bool connected = presence.value(QStringLiteral("status")).toString() == QLatin1String("connected");
    const QString activity = presence.value(QStringLiteral("activity")).toString();
    const QString agentId = presence.value(QStringLiteral("agent")).toString();
    QString text = connected ? tr("%1 connected").arg(client) : tr("%1 disconnected").arg(client);
    if (connected && !activity.isEmpty() && activity != QLatin1String("idle")) {
        QTreeWidgetItem *it = agentId.isEmpty() ? nullptr : agentItem(agentId);
        text = it != nullptr ? tr("%1 · %2: %3").arg(text, it->text(ColAgent), activity) : tr("%1 · %2").arg(text, activity);
        if (it != nullptr) {
            it->setText(ColSummary, tr("%1 (%2)").arg(activity, client));
        }
    }
    const QJsonArray entries = agentEdits.value(QStringLiteral("entries")).toArray();
    int edits = 0;
    for (const QJsonValue &e : entries) {
        edits += e.toObject().value(QStringLiteral("undone")).toBool() ? 0 : 1;
    }
    if (edits > 0) {
        text = tr("%1 · %n agent edit(s)", nullptr, edits).arg(text);
    }
    // V1.5: app-driven reasoning providers (order, active, last automatic switch) and department learning.
    // Both lines are written by the Python side; an external MCP client's own session is the line above.
    QStringList extra;
    for (const char *key : {"providers", "failover", "learning"}) {
        const QString line = presence.value(QLatin1String(key)).toString();
        if (!line.isEmpty()) {
            extra << (QLatin1String(key) == QLatin1String("failover") ? tr("Switched provider: %1").arg(line) : line);
        }
    }
    m_agentDetailText = extra.join(QStringLiteral("\n"));
    m_agentSessionText = text;
    m_jobMetaLabel->setText(tr("%1 · %2").arg(QFileInfo(m_jobDir).fileName(), text));
    m_jobMetaLabel->setToolTip(m_agentDetailText);
    // Unresolved issues from finished agent tasks go to the same review list, under the owning agent.
    const QJsonObject tasks = agentEdits.value(QStringLiteral("tasks")).toObject();
    for (auto t = tasks.constBegin(); t != tasks.constEnd(); ++t) {
        for (const QJsonValue &u : t.value().toObject().value(QStringLiteral("unresolved")).toArray()) {
            const QJsonObject issue = u.toObject();
            QTreeWidgetItem *owner = agentItem(issue.value(QStringLiteral("agent")).toString());
            auto *row = new QListWidgetItem(tallyIcon(TallyState::Review),
                                            QStringLiteral("%1 · %2").arg(owner != nullptr ? owner->text(ColAgent) : issue.value(QStringLiteral("agent")).toString(),
                                                                         issue.value(QStringLiteral("message")).toString()),
                                            m_reviewList);
            row->setData(kIdRole, issue.value(QStringLiteral("agent")).toString());
        }
    }
    // Learned strategies that wait for the editor's approval are review items of the Learning Agent.
    for (const QJsonValue &u : presence.value(QStringLiteral("learning_review")).toArray()) {
        const QJsonObject item = u.toObject();
        QTreeWidgetItem *owner = agentItem(QStringLiteral("learning"));
        auto *row = new QListWidgetItem(tallyIcon(TallyState::Review),
                                        QStringLiteral("%1 · %2").arg(owner != nullptr ? owner->text(ColAgent) : QStringLiteral("Learning"),
                                                                     item.value(QStringLiteral("message")).toString()),
                                        m_reviewList);
        row->setData(kIdRole, QStringLiteral("learning"));
    }
    m_reviewTitle->setText(m_reviewList->count() > 0 ? tr("Review · %1").arg(m_reviewList->count()) : tr("Review"));
}

QString AgentControlCentre::agentSessionText() const
{
    return m_agentSessionText;
}

QString AgentControlCentre::agentDetailText() const
{
    return m_agentDetailText;
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
    {
        // V1.4: agent session written by the MCP adapter (absent when no reasoning client was connected)
        auto readObj = [this](const QString &rel) {
            QFile pf(QDir(m_jobDir).filePath(rel));
            return pf.open(QIODevice::ReadOnly) ? QJsonDocument::fromJson(pf.readAll()).object() : QJsonObject();
        };
        const QJsonObject presence = readObj(QStringLiteral("draft/agent_presence.json"));
        if (!presence.isEmpty()) {
            loadAgentSession(presence, readObj(QStringLiteral("draft/agent_edits.json")));
        }
    }
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
    const bool hasDraft = hasJob && !running && (m_jobStatus == QLatin1String("draft_ready") || m_jobStatus == QLatin1String("awaiting_review"));
    m_reviseEdit->setEnabled(hasDraft);
    m_reviseSelection->setEnabled(hasDraft);
    m_reviseBtn->setEnabled(hasDraft);
    m_undoRevisionBtn->setEnabled(hasDraft && m_revisionCount > 0);
    m_acceptBtn->setEnabled(hasJob && !running && hasAgent);
    m_rejectBtn->setEnabled(hasJob && !running && hasAgent);
    // Queueing is allowed while a production runs; starting another one is not.
    m_startBtn->setEnabled(!running || m_queueCheck->isChecked());
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

QString AgentControlCentre::revisionInstruction() const
{
    return m_reviseEdit->text();
}

void AgentControlCentre::setRevisionInstruction(const QString &text)
{
    m_reviseEdit->setText(text);
}

bool AgentControlCentre::revisionSelectionOnly() const
{
    return m_reviseSelection->isChecked();
}

void AgentControlCentre::setRevisionSelectionOnly(bool on)
{
    m_reviseSelection->setChecked(on);
}

} // namespace BangOnPro
