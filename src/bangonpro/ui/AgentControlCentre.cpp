/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "AgentControlCentre.h"
#include "BangOnProTheme.h"

#include <QAbstractButton>
#include <QButtonGroup>
#include <QCheckBox>
#include <QColor>
#include <QComboBox>
#include <QDateTime>
#include <QDir>
#include <QDoubleSpinBox>
#include <QFile>
#include <QFileInfo>
#include <QFormLayout>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QHash>
#include <QHeaderView>
#include <QHideEvent>
#include <QJsonDocument>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QListWidgetItem>
#include <QMenu>
#include <QPlainTextEdit>
#include <QProgressBar>
#include <QPushButton>
#include <QShowEvent>
#include <QSizePolicy>
#include <QSpinBox>
#include <QStringList>
#include <QTimer>
#include <QToolButton>
#include <QTreeWidget>
#include <QVBoxLayout>

#ifdef Q_OS_UNIX
#include <cerrno>
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>
#endif

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
struct StageRow
{
    const char *id;
    const char *label;
};
// Must match bangonpro/ai_engine/department/stages.py EDITOR_STAGES.
constexpr StageRow kStages[] = {
    {"analyse", "Analysing footage"},
    {"understand", "Transcribing and understanding dialogue"},
    {"rough_cut", "Building the rough cut"},
    {"audio", "Improving audio"},
    {"colour", "Colour correction"},
    {"graphics", "Captions and graphics"},
    {"qc", "Quality control"},
    {"prepare", "Preparing edited sequence"},
};
struct EditTypeRow
{
    const char *id;
    const char *label;
    const char *mode;
    const char *tip;
};
constexpr EditTypeRow kEditTypes[] = {
    {"first_cut", "First cut", "autonomous_first_cut", "Assemble a clean, tight cut with balanced audio, ready for you to finish"},
    {"full_edit", "Full edit", "full_production_draft", "A complete YouTube edit: cut, audio, colour, captions, graphics and checks"},
    {"social_clips", "Social clips", "full_production_draft", "The full edit plus short vertical clips of the strongest moments"},
    {"custom", "Custom edit", "autonomous_first_cut", "Only what you describe; choose the mode and specialists under Advanced"},
};
enum Col { ColAgent = 0, ColStatus, ColConfidence, ColOps, ColEngine, ColSummary };
constexpr int kIdRole = Qt::UserRole + 1;
constexpr int kJobDirRole = Qt::UserRole + 2;
constexpr int kQueueIdRole = Qt::UserRole + 3;
constexpr int kQueueStatusRole = Qt::UserRole + 4;
constexpr int kStageStateRole = Qt::UserRole + 5;
// A worker that is gone for this many consecutive polls (500 ms each) is reported as lost.
constexpr int kDeadPollsBeforeLost = 3;

QString minutesSeconds(double seconds)
{
    const int s = qMax(0, qRound(seconds));
    return QStringLiteral("%1:%2").arg(s / 60).arg(s % 60, 2, 10, QLatin1Char('0'));
}

QDateTime parseIsoUtc(const QString &raw)
{
    if (raw.isEmpty()) {
        return {};
    }
    QDateTime dt = QDateTime::fromString(raw, Qt::ISODateWithMs);
    if (!dt.isValid()) {
        dt = QDateTime::fromString(raw, Qt::ISODate);
    }
    return dt.isValid() ? dt.toUTC() : QDateTime();
}

QString plainStatus(const QString &status)
{
    static const QHash<QString, QString> words = {
        {QStringLiteral("queued"), QStringLiteral("Queued")},
        {QStringLiteral("interrupted"), QStringLiteral("Stopped before finishing")},
        {QStringLiteral("paused"), QStringLiteral("Paused")},
        {QStringLiteral("running"), QStringLiteral("Running")},
        {QStringLiteral("completed"), QStringLiteral("Done")},
        {QStringLiteral("needs_attention"), QStringLiteral("Needs attention")},
        {QStringLiteral("failed"), QStringLiteral("Failed")},
        {QStringLiteral("cancelled"), QStringLiteral("Cancelled")},
        {QStringLiteral("draft_ready"), QStringLiteral("Editing complete")},
        {QStringLiteral("awaiting_review"), QStringLiteral("Editing complete · needs approval")},
        {QStringLiteral("completed_with_errors"), QStringLiteral("Finished with problems")},
        {QStringLiteral("blocked_insufficient_storage"), QStringLiteral("Not enough space")},
        {QStringLiteral("blocked_missing_media"), QStringLiteral("Footage not found")},
        {QStringLiteral("needs_clarification"), QStringLiteral("Needs a clearer brief")},
        {QStringLiteral("planned_manual"), QStringLiteral("Planned")},
        {QStringLiteral("planned"), QStringLiteral("Starting")},
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
    if (status == QLatin1String("cancelled") || status == QLatin1String("interrupted")) {
        return TallyState::Skipped;
    }
    if (status == QLatin1String("paused")) {
        return TallyState::Review;
    }
    if (status == QLatin1String("queued") || status == QLatin1String("planned_manual")) {
        return TallyState::Idle;
    }
    return TallyState::Review;
}

TallyState stageTally(const QString &state)
{
    if (state == QLatin1String("running")) {
        return TallyState::Running;
    }
    if (state == QLatin1String("done")) {
        return TallyState::Done;
    }
    if (state == QLatin1String("failed")) {
        return TallyState::Failed;
    }
    if (state == QLatin1String("stopped") || state == QLatin1String("skipped")) {
        return TallyState::Skipped;
    }
    return TallyState::Idle;
}

QString statusLabel(const QString &status, const QString &decision, bool stale)
{
    static const QHash<QString, QString> states = {
        {QStringLiteral("pending"), QStringLiteral("Waiting")},     {QStringLiteral("running"), QStringLiteral("Running")},
        {QStringLiteral("completed"), QStringLiteral("Done")},      {QStringLiteral("skipped"), QStringLiteral("Skipped")},
        {QStringLiteral("failed"), QStringLiteral("Failed")},       {QStringLiteral("blocked"), QStringLiteral("Blocked")},
        {QStringLiteral("cancelled"), QStringLiteral("Cancelled")}, {QStringLiteral("rolled_back"), QStringLiteral("Rolled back")},
        {QStringLiteral("interrupted"), QStringLiteral("Stopped")}, {QStringLiteral("paused"), QStringLiteral("Paused")},
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

bool isFinishedWithDraft(const QString &status)
{
    return status == QLatin1String("draft_ready") || status == QLatin1String("awaiting_review") || status == QLatin1String("completed_with_errors");
}

bool isResumable(const QString &status)
{
    return status == QLatin1String("paused") || status == QLatin1String("cancelled") || status == QLatin1String("interrupted") ||
           status == QLatin1String("failed") || status == QLatin1String("completed_with_errors") || status.startsWith(QLatin1String("blocked"));
}

QLabel *wrapLabel(const QString &text, QWidget *parent, const char *role)
{
    auto *l = new QLabel(text, parent);
    l->setWordWrap(true);
    setTextRole(l, role);
    return l;
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
    m_elapsedClock = new QTimer(this);
    m_elapsedClock->setInterval(1000);
    connect(m_elapsedClock, &QTimer::timeout, this, &AgentControlCentre::updateElapsedLabel);
    applyEditType(QStringLiteral("full_edit"));
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

QStringList AgentControlCentre::editTypes()
{
    QStringList ids;
    for (const EditTypeRow &r : kEditTypes) {
        ids << QString::fromLatin1(r.id);
    }
    return ids;
}

bool AgentControlCentre::jobWorkerAlive(const QString &jobDir)
{
#ifdef Q_OS_UNIX
    const QByteArray path = QFile::encodeName(QDir(jobDir).filePath(QStringLiteral("worker.lock")));
    const int fd = ::open(path.constData(), O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        return false; // no worker has ever owned this job
    }
    bool alive = false;
    if (::flock(fd, LOCK_EX | LOCK_NB) == 0) {
        ::flock(fd, LOCK_UN);
    } else {
        alive = (errno == EWOULDBLOCK);
    }
    ::close(fd);
    return alive;
#else
    Q_UNUSED(jobDir)
    return false;
#endif
}

// ---------------------------------------------------------------------------- layout
void AgentControlCentre::buildUi()
{
    using M = StudioMetrics;
    auto *root = new QVBoxLayout(this);
    root->setContentsMargins(M::kSpace3, M::kSpace3, M::kSpace3, M::kSpace3);
    root->setSpacing(M::kSpace3);

    // Header: panel name and providers; then where the edit goes (project · sequence).
    auto *header = new QHBoxLayout;
    header->setSpacing(M::kSpace2);
    auto *title = new QLabel(tr("Production"), this);
    title->setObjectName(QStringLiteral("acc_title"));
    setTextRole(title, "title");
    auto *providers = new QPushButton(tr("AI Providers…"), this);
    providers->setObjectName(QStringLiteral("acc_providers"));
    providers->setToolTip(tr("Connected subscriptions used when instructions need reasoning"));
    setRole(providers, "quiet");
    connect(providers, &QPushButton::clicked, this, &AgentControlCentre::providerSettingsRequested);
    header->addWidget(title);
    header->addStretch(1);
    header->addWidget(providers);
    root->addLayout(header);

    m_contextLabel = wrapLabel(tr("No project open"), this, "meta");
    m_contextLabel->setObjectName(QStringLiteral("acc_context"));
    root->addWidget(m_contextLabel);
    auto *seqRow = new QHBoxLayout;
    seqRow->setSpacing(M::kSpace2);
    m_sequenceCombo = new QComboBox(this);
    m_sequenceCombo->setObjectName(QStringLiteral("acc_sequence"));
    m_sequenceCombo->setAccessibleName(tr("Sequence to edit"));
    m_sequenceCombo->setToolTip(tr("The sequence whose footage is edited. Your original sequence is never changed."));
    m_sequenceCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    m_sequenceCombo->setMinimumContentsLength(10);
    m_sequenceCombo->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    m_newSequenceBtn = new QPushButton(tr("New sequence…"), this);
    m_newSequenceBtn->setObjectName(QStringLiteral("acc_new_sequence"));
    m_newSequenceBtn->setToolTip(tr("Create a named sequence in this project. With clips selected in the Project Bin, they are placed in it."));
    seqRow->addWidget(m_sequenceCombo, 1);
    seqRow->addWidget(m_newSequenceBtn);
    root->addLayout(seqRow);
    connect(m_sequenceCombo, QOverload<int>::of(&QComboBox::activated), this, &AgentControlCentre::sequenceSelected);
    connect(m_newSequenceBtn, &QPushButton::clicked, this, &AgentControlCentre::newSequenceRequested);

    root->addWidget(buildSetup());
    root->addWidget(buildRun());
    root->addWidget(buildDone());

    // Advanced: every expert control, collapsed by default.
    m_advancedToggle = new QToolButton(this);
    m_advancedToggle->setObjectName(QStringLiteral("acc_advanced_toggle"));
    m_advancedToggle->setText(tr("Advanced"));
    m_advancedToggle->setCheckable(true);
    m_advancedToggle->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
    m_advancedToggle->setArrowType(Qt::RightArrow);
    m_advancedToggle->setAutoRaise(true);
    m_advancedToggle->setToolTip(tr("Modes, the specialist agents, budgets, timing, job history and draft files"));
    root->addWidget(m_advancedToggle);
    root->addWidget(buildAdvanced());
    m_advancedSection->setVisible(false);
    connect(m_advancedToggle, &QToolButton::toggled, this, [this](bool on) { setAdvancedExpanded(on); });
    root->addStretch(1);

    setMinimumWidth(260);
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Preferred);
}

QWidget *AgentControlCentre::buildSetup()
{
    using M = StudioMetrics;
    m_setupSection = new QWidget(this);
    m_setupSection->setObjectName(QStringLiteral("acc_setup"));
    auto *lay = new QVBoxLayout(m_setupSection);
    lay->setContentsMargins(0, 0, 0, 0);
    lay->setSpacing(M::kSpace2);

    lay->addWidget(wrapLabel(tr("What should Bang On Pro make?"), m_setupSection, "section"));
    // Four choices in a 2 x 2 grid: readable at the narrowest dock width without truncation.
    auto *grid = new QGridLayout;
    grid->setHorizontalSpacing(M::kSpace2);
    grid->setVerticalSpacing(M::kSpace2);
    m_editTypeGroup = new QButtonGroup(this);
    m_editTypeGroup->setExclusive(true);
    int i = 0;
    for (const EditTypeRow &r : kEditTypes) {
        auto *b = new QPushButton(tr(r.label), m_setupSection);
        b->setObjectName(QStringLiteral("acc_edit_%1").arg(QString::fromLatin1(r.id)));
        b->setCheckable(true);
        b->setToolTip(tr(r.tip));
        b->setProperty("editType", QString::fromLatin1(r.id));
        b->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
        m_editTypeGroup->addButton(b, i);
        grid->addWidget(b, i / 2, i % 2);
        ++i;
    }
    lay->addLayout(grid);
    connect(m_editTypeGroup, &QButtonGroup::buttonClicked, this, [this](QAbstractButton *b) {
        noteActivity(QStringLiteral("setup"));
        applyEditType(b->property("editType").toString());
    });

    auto *styleLabel = wrapLabel(tr("Editing style"), m_setupSection, "secondary");
    lay->addWidget(styleLabel);
    auto *styleRow = new QHBoxLayout;
    styleRow->setSpacing(M::kSpace2);
    m_profileCombo = new QComboBox(m_setupSection);
    m_profileCombo->setObjectName(QStringLiteral("acc_profile"));
    m_profileCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    m_profileCombo->setMinimumContentsLength(10);
    m_profileCombo->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    m_profileCombo->addItem(tr("Automatic (by footage)"), QString());
    m_profileCombo->setItemData(0, tr("One camera: YouTube talking head. Two: interview. More: multicamera studio."), Qt::ToolTipRole);
    m_profileCombo->addItem(tr("YouTube talking head"), QStringLiteral("youtube_talking_head"));
    m_profileCombo->addItem(tr("Podcast multicam"), QStringLiteral("podcast_multicam"));
    m_profileCombo->addItem(tr("Interview, 2 cameras"), QStringLiteral("interview_two_camera"));
    m_profileCombo->addItem(tr("Gameshow multicam"), QStringLiteral("gameshow_multicam"));
    m_profileCombo->addItem(tr("Sports discussion"), QStringLiteral("sports_discussion"));
    m_profileCombo->addItem(tr("Daily news"), QStringLiteral("daily_news"));
    m_profileCombo->addItem(tr("Social short 9:16"), QStringLiteral("social_short"));
    styleLabel->setBuddy(m_profileCombo);
    m_referencesBtn = new QPushButton(tr("References…"), m_setupSection);
    m_referencesBtn->setObjectName(QStringLiteral("acc_references"));
    m_referencesBtn->setToolTip(tr("Learn an editing style from reference videos you like and reuse it"));
    styleRow->addWidget(m_profileCombo, 1);
    styleRow->addWidget(m_referencesBtn);
    lay->addLayout(styleRow);
    connect(m_referencesBtn, &QPushButton::clicked, this, &AgentControlCentre::referenceVideosRequested);
    connect(m_profileCombo, QOverload<int>::of(&QComboBox::activated), this, [this]() { noteActivity(QStringLiteral("setup")); });

    auto *cutForm = new QFormLayout;
    cutForm->setContentsMargins(0, 0, 0, 0);
    cutForm->setHorizontalSpacing(M::kSpace2);
    cutForm->setVerticalSpacing(M::kSpace1);
    cutForm->setRowWrapPolicy(QFormLayout::WrapLongRows);
    cutForm->setFieldGrowthPolicy(QFormLayout::AllNonFixedFieldsGrow);

    m_mediaSourceCombo = new QComboBox(m_setupSection);
    m_mediaSourceCombo->setObjectName(QStringLiteral("acc_media_source"));
    m_mediaSourceCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    m_mediaSourceCombo->setMinimumContentsLength(8);
    m_mediaSourceCombo->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    m_mediaSourceCombo->addItem(tr("Current sequence"), QStringLiteral("sequence"));
    m_mediaSourceCombo->addItem(tr("Selected Bin clips"), QStringLiteral("selected_bin"));
    m_mediaSourceCombo->addItem(tr("Project Bin"), QStringLiteral("project_bin"));
    m_mediaSourceCombo->addItem(tr("Choose files…"), QStringLiteral("files"));
    m_mediaSourceCombo->setToolTip(tr("Which footage Bang On Pro should assemble into the edited sequence"));

    m_outputFormatCombo = new QComboBox(m_setupSection);
    m_outputFormatCombo->setObjectName(QStringLiteral("acc_output_format"));
    m_outputFormatCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    m_outputFormatCombo->setMinimumContentsLength(8);
    m_outputFormatCombo->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    m_outputFormatCombo->addItem(tr("Match sequence"), QStringLiteral("match"));
    m_outputFormatCombo->addItem(tr("16:9 Landscape"), QStringLiteral("16:9"));
    m_outputFormatCombo->addItem(tr("9:16 Vertical"), QStringLiteral("9:16"));
    m_outputFormatCombo->addItem(tr("1:1 Square"), QStringLiteral("1:1"));
    m_outputFormatCombo->setToolTip(tr("Output aspect ratio for the edited sequence"));

    m_targetDurationSpin = new QSpinBox(m_setupSection);
    m_targetDurationSpin->setObjectName(QStringLiteral("acc_target_duration"));
    m_targetDurationSpin->setRange(0, 240);
    m_targetDurationSpin->setValue(0);
    m_targetDurationSpin->setSpecialValueText(tr("Auto"));
    m_targetDurationSpin->setSuffix(tr(" min"));
    m_targetDurationSpin->setToolTip(tr("Optional target duration in minutes (0 = Auto)"));

    cutForm->addRow(wrapLabel(tr("Source"), m_setupSection, "secondary"), m_mediaSourceCombo);
    cutForm->addRow(wrapLabel(tr("Format"), m_setupSection, "secondary"), m_outputFormatCombo);
    cutForm->addRow(wrapLabel(tr("Target"), m_setupSection, "secondary"), m_targetDurationSpin);
    lay->addLayout(cutForm);
    connect(m_mediaSourceCombo, QOverload<int>::of(&QComboBox::activated), this, [this]() { noteActivity(QStringLiteral("setup")); });
    connect(m_outputFormatCombo, QOverload<int>::of(&QComboBox::activated), this, [this]() { noteActivity(QStringLiteral("setup")); });
    connect(m_targetDurationSpin, QOverload<int>::of(&QSpinBox::valueChanged), this, [this]() { noteActivity(QStringLiteral("setup")); });

    auto *briefLabel = wrapLabel(tr("Describe the edit"), m_setupSection, "secondary");
    lay->addWidget(briefLabel);
    m_briefEdit = new QPlainTextEdit(m_setupSection);
    m_briefEdit->setObjectName(QStringLiteral("acc_brief"));
    m_briefEdit->setPlaceholderText(tr("For example: tighten the pauses, add captions, open with the strongest moment, keep it punchy"));
    m_briefEdit->setMinimumHeight(72);
    m_briefEdit->setMaximumHeight(120);
    m_briefEdit->setTabChangesFocus(true);
    m_briefEdit->setAccessibleName(tr("Editing instructions"));
    lay->addWidget(m_briefEdit);
    connect(m_briefEdit, &QPlainTextEdit::textChanged, this, [this]() {
        if (m_briefEdit->hasFocus()) {
            noteActivity(QStringLiteral("setup"));
        }
    });

    m_startBtn = new QPushButton(tr("Start editing"), m_setupSection);
    m_startBtn->setObjectName(QStringLiteral("acc_start"));
    m_startBtn->setProperty("bangOnProPrimary", true);
    setRole(m_startBtn, "primary");
    m_startBtn->setDefault(true);
    m_startBtn->setMinimumHeight(StudioMetrics::kControlHeight + 4);
    lay->addWidget(m_startBtn);
    m_setupHint = wrapLabel(tr("Uses the footage in the selected sequence (or the Project Bin). Your original sequence and files are never changed."),
                            m_setupSection, "muted");
    m_setupHint->setObjectName(QStringLiteral("acc_setup_hint"));
    lay->addWidget(m_setupHint);
    connect(m_startBtn, &QPushButton::clicked, this, [this]() {
        noteActivity(QStringLiteral("setup"));
        if (queueSelected()) {
            Q_EMIT queueJobRequested(buildRunArguments({}));
        } else {
            Q_EMIT startJobRequested(buildRunArguments({}));
        }
    });
    return m_setupSection;
}

QWidget *AgentControlCentre::buildRun()
{
    using M = StudioMetrics;
    m_runSection = new QWidget(this);
    m_runSection->setObjectName(QStringLiteral("acc_run"));
    auto *lay = new QVBoxLayout(m_runSection);
    lay->setContentsMargins(0, 0, 0, 0);
    lay->setSpacing(M::kSpace2);

    auto *telemetryRow = new QHBoxLayout;
    telemetryRow->setSpacing(M::kSpace2);
    m_engineBadge = new QLabel(m_runSection);
    m_engineBadge->setObjectName(QStringLiteral("acc_engine_badge"));
    setTextRole(m_engineBadge, "meta");
    m_engineBadge->setProperty("bopPill", true);
    m_engineBadge->setVisible(false);
    m_elapsedLabel = new QLabel(m_runSection);
    m_elapsedLabel->setObjectName(QStringLiteral("acc_elapsed_label"));
    setTextRole(m_elapsedLabel, "meta");
    m_elapsedLabel->setVisible(false);
    telemetryRow->addWidget(m_engineBadge);
    telemetryRow->addStretch(1);
    telemetryRow->addWidget(m_elapsedLabel);
    lay->addLayout(telemetryRow);

    m_resourceLabel = wrapLabel(QString(), m_runSection, "meta");
    m_resourceLabel->setObjectName(QStringLiteral("acc_resource_label"));
    m_resourceLabel->setVisible(false);
    lay->addWidget(m_resourceLabel);

    m_stageList = new QListWidget(m_runSection);
    m_stageList->setObjectName(QStringLiteral("acc_stages"));
    m_stageList->setAccessibleName(tr("Production stages"));
    m_stageList->setSelectionMode(QAbstractItemView::NoSelection);
    m_stageList->setFocusPolicy(Qt::NoFocus);
    m_stageList->setWordWrap(true);
    m_stageList->setUniformItemSizes(false);
    m_stageList->setIconSize(QSize(12, 12));
    m_stageList->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    m_stageList->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    for (const StageRow &s : kStages) {
        auto *item = new QListWidgetItem(tallyIcon(TallyState::Idle), tr(s.label), m_stageList);
        item->setData(kIdRole, QString::fromLatin1(s.id));
        item->setData(kStageStateRole, QStringLiteral("pending"));
        item->setForeground(QColor(QString::fromLatin1(StudioPaletteTokens::kTextMuted)));
    }
    m_stageList->setMinimumHeight(8 * (M::kRowHeight - 4) + M::kSpace2);
    lay->addWidget(m_stageList);
    m_progress = new QProgressBar(m_runSection);
    m_progress->setObjectName(QStringLiteral("acc_progress"));
    m_progress->setTextVisible(false);
    m_progress->setMaximumHeight(6);
    m_progress->setAccessibleName(tr("Progress of the current stage"));
    lay->addWidget(m_progress);
    m_statusLabel = wrapLabel(tr("No production running"), m_runSection, "secondary");
    m_statusLabel->setObjectName(QStringLiteral("acc_status"));
    lay->addWidget(m_statusLabel);
    m_reasoningLabel = wrapLabel(QString(), m_runSection, "meta");
    m_reasoningLabel->setObjectName(QStringLiteral("acc_reasoning"));
    m_reasoningLabel->setVisible(false);
    lay->addWidget(m_reasoningLabel);
    m_problemLabel = wrapLabel(QString(), m_runSection, "primary");
    m_problemLabel->setObjectName(QStringLiteral("acc_problem"));
    m_problemLabel->setProperty("bopBanner", QStringLiteral("danger"));
    m_problemLabel->setTextFormat(Qt::RichText);
    m_problemLabel->setVisible(false);
    lay->addWidget(m_problemLabel);

    auto *actions = new QGridLayout;
    actions->setHorizontalSpacing(M::kSpace2);
    actions->setVerticalSpacing(M::kSpace1);
    m_pauseBtn = new QPushButton(tr("Pause"), m_runSection);
    m_pauseBtn->setObjectName(QStringLiteral("acc_pause"));
    m_pauseBtn->setToolTip(tr("Pause cleanly after the current stage finishes. Resume continues without losing work."));
    m_cancelBtn = new QPushButton(tr("Cancel"), m_runSection);
    m_cancelBtn->setObjectName(QStringLiteral("acc_cancel"));
    m_cancelBtn->setToolTip(tr("Stop after the current step. Finished stages are kept, and Resume continues from there."));
    m_resumeBtn = new QPushButton(tr("Resume"), m_runSection);
    m_resumeBtn->setObjectName(QStringLiteral("acc_resume"));
    m_resumeBtn->setToolTip(tr("Continue from the last finished stage. Nothing is redone twice."));
    m_retryBtn = new QPushButton(tr("Retry"), m_runSection);
    m_retryBtn->setObjectName(QStringLiteral("acc_retry"));
    m_retryBtn->setToolTip(tr("Retry the failed or blocked production from the last verified checkpoint."));
    m_discardBtn = new QPushButton(tr("Discard"), m_runSection);
    m_discardBtn->setObjectName(QStringLiteral("acc_discard"));
    setRole(m_discardBtn, "danger");
    m_discardBtn->setToolTip(tr("Remove this stopped production draft. Your original footage is never touched."));
    m_newFromRunBtn = new QPushButton(tr("New production"), m_runSection);
    m_newFromRunBtn->setObjectName(QStringLiteral("acc_new_from_run"));
    setRole(m_newFromRunBtn, "quiet");
    m_newFromRunBtn->setToolTip(tr("Return to setup and start a new production."));

    actions->addWidget(m_pauseBtn, 0, 0);
    actions->addWidget(m_cancelBtn, 0, 1);
    actions->addWidget(m_resumeBtn, 1, 0);
    actions->addWidget(m_retryBtn, 1, 1);
    actions->addWidget(m_discardBtn, 2, 0);
    actions->addWidget(m_newFromRunBtn, 2, 1);
    lay->addLayout(actions);

    connect(m_pauseBtn, &QPushButton::clicked, this, [this]() {
        if (!m_jobDir.isEmpty()) {
            m_statusLabel->setText(tr("Pausing… finishing the current stage"));
            m_pauseBtn->setEnabled(false);
            Q_EMIT pauseJobRequested(m_jobDir);
        }
    });
    connect(m_cancelBtn, &QPushButton::clicked, this, [this]() {
        if (!m_jobDir.isEmpty()) {
            m_statusLabel->setText(tr("Cancelling… the current step is finishing"));
            m_cancelBtn->setEnabled(false);
            Q_EMIT cancelJobRequested(m_jobDir);
        }
    });
    connect(m_resumeBtn, &QPushButton::clicked, this, [this]() {
        if (!m_jobDir.isEmpty()) {
            Q_EMIT resumeJobRequested(m_jobDir);
        }
    });
    connect(m_retryBtn, &QPushButton::clicked, this, [this]() {
        if (!m_jobDir.isEmpty()) {
            Q_EMIT retryJobRequested(m_jobDir);
        }
    });
    connect(m_discardBtn, &QPushButton::clicked, this, [this]() {
        if (!m_jobDir.isEmpty()) {
            Q_EMIT discardJobRequested(m_jobDir);
        }
    });
    connect(m_newFromRunBtn, &QPushButton::clicked, this, [this]() {
        clearActiveJob();
        Q_EMIT newProductionRequested();
        m_briefEdit->setFocus();
    });
    m_runSection->setVisible(false);
    return m_runSection;
}

QWidget *AgentControlCentre::buildDone()
{
    using M = StudioMetrics;
    m_doneSection = new QWidget(this);
    m_doneSection->setObjectName(QStringLiteral("acc_done"));
    auto *lay = new QVBoxLayout(m_doneSection);
    lay->setContentsMargins(0, 0, 0, 0);
    lay->setSpacing(M::kSpace2);
    m_doneTitle = wrapLabel(tr("Editing complete"), m_doneSection, "title");
    m_doneTitle->setObjectName(QStringLiteral("acc_done_title"));
    lay->addWidget(m_doneTitle);
    m_doneDetail = wrapLabel(QString(), m_doneSection, "secondary");
    m_doneDetail->setObjectName(QStringLiteral("acc_done_detail"));
    lay->addWidget(m_doneDetail);
    m_openSequenceBtn = new QPushButton(tr("Open edited sequence"), m_doneSection);
    m_openSequenceBtn->setObjectName(QStringLiteral("acc_open_sequence"));
    m_openSequenceBtn->setToolTip(tr("Adds the edit as a new sequence in this project, next to your original. Undo removes it."));
    m_openSequenceBtn->setProperty("bangOnProPrimary", true);
    setRole(m_openSequenceBtn, "primary");
    m_openSequenceBtn->setMinimumHeight(StudioMetrics::kControlHeight + 4);
    lay->addWidget(m_openSequenceBtn);
    auto *row = new QHBoxLayout;
    row->setSpacing(M::kSpace2);
    m_exportBtn = new QPushButton(tr("Export…"), m_doneSection);
    m_exportBtn->setObjectName(QStringLiteral("acc_export_sequence"));
    m_exportBtn->setToolTip(tr("Render the open sequence"));
    m_newJobBtn = new QPushButton(tr("New production"), m_doneSection);
    m_newJobBtn->setObjectName(QStringLiteral("acc_new_job"));
    setRole(m_newJobBtn, "quiet");
    row->addWidget(m_exportBtn);
    row->addWidget(m_newJobBtn);
    row->addStretch(1);
    lay->addLayout(row);
    m_qcMarkersCheck = new QCheckBox(tr("Show quality-check markers on the timeline"), m_doneSection);
    m_qcMarkersCheck->setObjectName(QStringLiteral("acc_qc_markers"));
    m_qcMarkersCheck->setToolTip(
        tr("Quality Control's notes (jump cuts, level jumps…) as timeline guides. Off keeps the timeline clean; the notes stay in Worth a look."));
    lay->addWidget(m_qcMarkersCheck);
    connect(m_qcMarkersCheck, &QCheckBox::toggled, this, [this](bool on) {
        if (!m_jobDir.isEmpty()) {
            Q_EMIT qcMarkersToggled(m_jobDir, on);
        }
    });
    lay->addWidget(wrapLabel(tr("Ask for a change"), m_doneSection, "secondary"));
    m_reviseEdit = new QLineEdit(m_doneSection);
    m_reviseEdit->setObjectName(QStringLiteral("acc_revise_text"));
    m_reviseEdit->setPlaceholderText(tr("e.g. tighten the opening, more captions, slower ending"));
    m_reviseEdit->setAccessibleName(tr("Revision instruction"));
    lay->addWidget(m_reviseEdit);
    auto *revRow = new QHBoxLayout;
    revRow->setSpacing(M::kSpace2);
    m_reviseSelection = new QCheckBox(tr("Selection only"), m_doneSection);
    m_reviseSelection->setObjectName(QStringLiteral("acc_revise_selection"));
    m_reviseSelection->setToolTip(tr("Change only the timeline zone you selected; everything else stays as it is"));
    m_reviseBtn = new QPushButton(tr("Revise"), m_doneSection);
    m_reviseBtn->setObjectName(QStringLiteral("acc_revise"));
    m_reviseBtn->setToolTip(tr("Make the change; unaffected work is reused. Open edited sequence shows the new version."));
    m_undoRevisionBtn = new QPushButton(tr("Undo change"), m_doneSection);
    m_undoRevisionBtn->setObjectName(QStringLiteral("acc_revise_undo"));
    m_undoRevisionBtn->setToolTip(tr("Restore the edit from before the last change"));
    revRow->addWidget(m_reviseSelection);
    revRow->addStretch(1);
    revRow->addWidget(m_reviseBtn);
    revRow->addWidget(m_undoRevisionBtn);
    lay->addLayout(revRow);
    m_reviewTitle = wrapLabel(tr("Worth a look"), m_doneSection, "section");
    m_reviewTitle->setObjectName(QStringLiteral("acc_review_title"));
    lay->addWidget(m_reviewTitle);
    m_reviewList = new QListWidget(m_doneSection);
    m_reviewList->setObjectName(QStringLiteral("acc_review"));
    m_reviewList->setWordWrap(true);
    m_reviewList->setAccessibleName(tr("Items worth reviewing"));
    m_reviewList->setMaximumHeight(6 * M::kRowHeight);
    lay->addWidget(m_reviewList);

    connect(m_openSequenceBtn, &QPushButton::clicked, this, [this]() {
        noteActivity(QStringLiteral("review"));
        if (!m_jobDir.isEmpty()) {
            Q_EMIT openEditedSequenceRequested(m_jobDir);
        }
    });
    connect(m_exportBtn, &QPushButton::clicked, this, [this]() {
        noteActivity(QStringLiteral("approval"));
        Q_EMIT exportRequested();
    });
    connect(m_newJobBtn, &QPushButton::clicked, this, [this]() {
        clearActiveJob();
        Q_EMIT newProductionRequested();
        m_briefEdit->setFocus();
    });
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
    connect(m_reviewList, &QListWidget::itemClicked, this, [this](QListWidgetItem *item) {
        noteActivity(QStringLiteral("review"));
        if (item) {
            selectAgent(item->data(kIdRole).toString());
        }
    });
    m_doneSection->setVisible(false);
    return m_doneSection;
}

QWidget *AgentControlCentre::buildAdvanced()
{
    using M = StudioMetrics;
    m_advancedSection = new QWidget(this);
    m_advancedSection->setObjectName(QStringLiteral("acc_advanced"));
    auto *lay = new QVBoxLayout(m_advancedSection);
    lay->setContentsMargins(0, 0, 0, 0);
    lay->setSpacing(M::kSpace2);

    auto *form = new QFormLayout;
    form->setContentsMargins(0, 0, 0, 0);
    form->setHorizontalSpacing(M::kSpace3);
    form->setVerticalSpacing(M::kSpace2);
    form->setRowWrapPolicy(QFormLayout::WrapLongRows);
    form->setFieldGrowthPolicy(QFormLayout::AllNonFixedFieldsGrow);
    m_modeCombo = new QComboBox(m_advancedSection);
    m_modeCombo->setObjectName(QStringLiteral("acc_mode"));
    m_modeCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    m_modeCombo->setMinimumContentsLength(12);
    m_modeCombo->addItem(tr("Manual"), QStringLiteral("manual"));
    m_modeCombo->addItem(tr("Assisted"), QStringLiteral("assisted"));
    m_modeCombo->addItem(tr("Autonomous first cut"), QStringLiteral("autonomous_first_cut"));
    m_modeCombo->addItem(tr("Full production draft"), QStringLiteral("full_production_draft"));
    m_modeCombo->setItemData(0, tr("Agents run only when you ask"), Qt::ToolTipRole);
    m_modeCombo->setItemData(1, tr("Every edit waits for your approval"), Qt::ToolTipRole);
    m_modeCombo->setItemData(2, tr("A reversible first cut"), Qt::ToolTipRole);
    m_modeCombo->setItemData(3, tr("A complete production draft"), Qt::ToolTipRole);
    m_seriesEdit = new QLineEdit(m_advancedSection);
    m_seriesEdit->setObjectName(QStringLiteral("acc_series"));
    m_seriesEdit->setPlaceholderText(tr("Series"));
    m_seriesEdit->setToolTip(tr("Reuses the series' approved production preferences"));
    m_episodeEdit = new QLineEdit(m_advancedSection);
    m_episodeEdit->setObjectName(QStringLiteral("acc_episode"));
    m_episodeEdit->setPlaceholderText(tr("Episode title"));
    form->addRow(tr("Mode"), m_modeCombo);
    form->addRow(tr("Series"), m_seriesEdit);
    form->addRow(tr("Episode"), m_episodeEdit);
    lay->addLayout(form);
    connect(m_modeCombo, QOverload<int>::of(&QComboBox::activated), this, [this]() {
        noteActivity(QStringLiteral("setup"));
        // Picking a mode by hand is a custom edit.
        applyEditType(QStringLiteral("custom"));
    });
    connect(m_seriesEdit, &QLineEdit::textEdited, this, [this]() { noteActivity(QStringLiteral("setup")); });
    connect(m_episodeEdit, &QLineEdit::textEdited, this, [this]() { noteActivity(QStringLiteral("setup")); });

    auto *opts = new QGridLayout;
    opts->setHorizontalSpacing(M::kSpace2);
    opts->setVerticalSpacing(M::kSpace2);
    m_exportCheck = new QCheckBox(tr("Render a master file"), m_advancedSection);
    m_exportCheck->setObjectName(QStringLiteral("acc_export"));
    m_queueCheck = new QCheckBox(tr("Add to queue"), m_advancedSection);
    m_queueCheck->setObjectName(QStringLiteral("acc_queue"));
    m_queueCheck->setToolTip(tr("Add to the production queue instead of starting now. Queued productions run one after another."));
    auto *budgetLabel = wrapLabel(tr("Paid API budget"), m_advancedSection, "secondary");
    m_budgetSpin = new QDoubleSpinBox(m_advancedSection);
    m_budgetSpin->setObjectName(QStringLiteral("acc_budget"));
    m_budgetSpin->setPrefix(QStringLiteral("$"));
    m_budgetSpin->setRange(0.0, 500.0);
    m_budgetSpin->setDecimals(2);
    m_budgetSpin->setToolTip(tr("Legacy limit for paid external models. Connected subscriptions never use API billing; 0 keeps this off."));
    budgetLabel->setBuddy(m_budgetSpin);
    opts->addWidget(m_exportCheck, 0, 0, 1, 2);
    opts->addWidget(m_queueCheck, 1, 0, 1, 2);
    opts->addWidget(budgetLabel, 2, 0);
    opts->addWidget(m_budgetSpin, 2, 1);
    lay->addLayout(opts);
    connect(m_queueCheck, &QCheckBox::toggled, this, [this](bool queued) {
        m_startBtn->setText(queued ? tr("Add to queue") : tr("Start editing"));
        updateActionStates();
    });

    lay->addWidget(wrapLabel(tr("Specialist agents"), m_advancedSection, "section"));
    m_jobMetaLabel = wrapLabel(tr("No job"), m_advancedSection, "meta");
    m_jobMetaLabel->setObjectName(QStringLiteral("acc_job_meta"));
    lay->addWidget(m_jobMetaLabel);
    m_agentTree = new QTreeWidget(m_advancedSection);
    m_agentTree->setObjectName(QStringLiteral("acc_agents"));
    m_agentTree->setHeaderLabels({tr("Agent"), tr("State"), tr("Conf."), tr("Ops"), tr("Engine"), tr("Result")});
    m_agentTree->setRootIsDecorated(false);
    m_agentTree->setUniformRowHeights(true);
    m_agentTree->setIconSize(QSize(12, 12));
    m_agentTree->header()->setMinimumSectionSize(36);
    m_agentTree->header()->setStretchLastSection(true);
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
        item->setFlags(locked ? (item->flags() & ~Qt::ItemIsUserCheckable) : (item->flags() | Qt::ItemIsUserCheckable));
        item->setCheckState(ColAgent, Qt::Checked);
        item->setToolTip(ColAgent, locked ? tr("Plans, delegates and verifies every job") : tr("Untick to leave this agent out of the next job"));
    }
    m_agentTree->setColumnWidth(ColAgent, 180);
    m_agentTree->setColumnWidth(ColStatus, 96);
    m_agentTree->setColumnWidth(ColConfidence, 44);
    m_agentTree->setColumnWidth(ColOps, 40);
    m_agentTree->setColumnWidth(ColEngine, 72);
    m_agentTree->setMinimumHeight(14 * 20);
    m_agentTree->setAccessibleName(tr("Production agents"));
    lay->addWidget(m_agentTree);
    connect(m_agentTree, &QTreeWidget::itemSelectionChanged, this, &AgentControlCentre::updateActionStates);
    connect(m_agentTree, &QTreeWidget::itemClicked, this, [this]() { noteActivity(m_jobDir.isEmpty() ? QStringLiteral("setup") : QStringLiteral("review")); });
    auto *agentRow = new QHBoxLayout;
    agentRow->setSpacing(M::kSpace2);
    m_acceptBtn = new QPushButton(tr("Accept"), m_advancedSection);
    m_acceptBtn->setObjectName(QStringLiteral("acc_accept"));
    m_acceptBtn->setToolTip(tr("Keep this agent's edits"));
    setRole(m_acceptBtn, "accept");
    m_rejectBtn = new QPushButton(tr("Reject"), m_advancedSection);
    m_rejectBtn->setObjectName(QStringLiteral("acc_reject"));
    m_rejectBtn->setToolTip(tr("Remove this agent's edits; dependent edits are removed or marked stale"));
    setRole(m_rejectBtn, "danger");
    m_rerunBtn = new QPushButton(tr("Rerun"), m_advancedSection);
    m_rerunBtn->setObjectName(QStringLiteral("acc_rerun"));
    m_rerunBtn->setToolTip(tr("Run this agent again from its checkpoint"));
    agentRow->addWidget(m_acceptBtn);
    agentRow->addWidget(m_rejectBtn);
    agentRow->addWidget(m_rerunBtn);
    agentRow->addStretch(1);
    lay->addLayout(agentRow);
    connect(m_acceptBtn, &QPushButton::clicked, this, [this]() {
        noteActivity(QStringLiteral("review"));
        if (!m_jobDir.isEmpty() && !selectedAgentId().isEmpty()) {
            Q_EMIT agentDecisionRequested(m_jobDir, selectedAgentId(), true);
        }
    });
    connect(m_rejectBtn, &QPushButton::clicked, this, [this]() {
        noteActivity(QStringLiteral("correction"));
        if (!m_jobDir.isEmpty() && !selectedAgentId().isEmpty()) {
            Q_EMIT agentDecisionRequested(m_jobDir, selectedAgentId(), false);
        }
    });
    connect(m_rerunBtn, &QPushButton::clicked, this, [this]() {
        noteActivity(QStringLiteral("correction"));
        if (!m_jobDir.isEmpty() && !selectedAgentId().isEmpty()) {
            Q_EMIT rerunAgentRequested(m_jobDir, selectedAgentId());
        }
    });

    lay->addWidget(wrapLabel(tr("Draft files"), m_advancedSection, "section"));
    auto *draftRow = new QGridLayout;
    draftRow->setHorizontalSpacing(M::kSpace2);
    draftRow->setVerticalSpacing(M::kSpace2);
    m_reviewBtn = new QPushButton(tr("Review edits one by one"), m_advancedSection);
    m_reviewBtn->setObjectName(QStringLiteral("acc_review_draft"));
    m_reviewBtn->setToolTip(tr("Stage the edits as one reviewable proposal in the AI Assistant: accept or reject each operation"));
    m_openDraftBtn = new QPushButton(tr("Open draft project file"), m_advancedSection);
    m_openDraftBtn->setObjectName(QStringLiteral("acc_open_draft"));
    m_openDraftBtn->setToolTip(tr("Opens the job's own .kdenlive file instead of your project (closes the current project)"));
    m_promoteBtn = new QPushButton(tr("Save draft as project file…"), m_advancedSection);
    m_promoteBtn->setObjectName(QStringLiteral("acc_promote"));
    m_promoteBtn->setToolTip(tr("Copy the draft into a .kdenlive file. Replacing an existing file asks first and keeps a backup."));
    draftRow->addWidget(m_reviewBtn, 0, 0, 1, 2);
    draftRow->addWidget(m_openDraftBtn, 1, 0);
    draftRow->addWidget(m_promoteBtn, 1, 1);
    lay->addLayout(draftRow);
    connect(m_reviewBtn, &QPushButton::clicked, this, [this]() {
        noteActivity(QStringLiteral("review"));
        if (!m_jobDir.isEmpty()) {
            Q_EMIT reviewDraftInTimelineRequested(m_jobDir);
        }
    });
    connect(m_openDraftBtn, &QPushButton::clicked, this, [this]() {
        noteActivity(QStringLiteral("review"));
        if (!m_draftProject.isEmpty()) {
            Q_EMIT openDraftProjectRequested(m_draftProject);
        }
    });
    connect(m_promoteBtn, &QPushButton::clicked, this, [this]() {
        noteActivity(QStringLiteral("approval"));
        if (!m_jobDir.isEmpty()) {
            Q_EMIT promoteDraftRequested(m_jobDir);
        }
    });

    m_summaryView = new QPlainTextEdit(m_advancedSection);
    m_summaryView->setObjectName(QStringLiteral("acc_summary"));
    m_summaryView->setReadOnly(true);
    m_summaryView->setPlaceholderText(tr("The production summary appears here when the job finishes"));
    m_summaryView->setMaximumHeight(160);
    lay->addWidget(m_summaryView);

    lay->addWidget(wrapLabel(tr("Editing-time measurement"), m_advancedSection, "section"));
    buildTimingRow(lay, m_advancedSection);

    m_historyTitle = wrapLabel(tr("Queue and history"), m_advancedSection, "section");
    m_historyTitle->setObjectName(QStringLiteral("acc_history_title"));
    lay->addWidget(m_historyTitle);
    m_historyList = new QListWidget(m_advancedSection);
    m_historyList->setObjectName(QStringLiteral("acc_history"));
    m_historyList->setAccessibleName(tr("Production queue and recent jobs"));
    m_historyList->setToolTip(tr("Double-click to open a production. Right-click a queued production to cancel or remove it."));
    m_historyList->setContextMenuPolicy(Qt::CustomContextMenu);
    m_historyList->setMaximumHeight(6 * M::kRowHeight);
    m_historyList->setTextElideMode(Qt::ElideRight);
    lay->addWidget(m_historyList);
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
    return m_advancedSection;
}

void AgentControlCentre::buildTimingRow(QVBoxLayout *root, QWidget *parent)
{
    using M = StudioMetrics;
    m_timingRow = new QWidget(parent);
    m_timingRow->setObjectName(QStringLiteral("acc_timing"));
    auto *row = new QGridLayout(m_timingRow);
    row->setContentsMargins(0, 0, 0, 0);
    row->setHorizontalSpacing(M::kSpace2);
    row->setVerticalSpacing(M::kSpace2);
    m_timingKind = new QComboBox(m_timingRow);
    m_timingKind->setObjectName(QStringLiteral("acc_timing_kind"));
    m_timingKind->addItem(tr("Agent-assisted"), QStringLiteral("agent_assisted"));
    m_timingKind->addItem(tr("Manual baseline"), QStringLiteral("manual_baseline"));
    m_timingKind->setToolTip(tr("Manual baseline: you edit without the agents. Agent-assisted: you review and finish the agents' edit."));
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
    row->addWidget(m_timingKind, 0, 0);
    row->addWidget(m_timingStartBtn, 0, 1);
    row->addWidget(m_timingLabel, 0, 0);
    row->addWidget(m_timingPauseBtn, 0, 1);
    row->addWidget(m_timingRating, 1, 0);
    row->addWidget(m_timingFinishBtn, 1, 1);
    root->addWidget(m_timingRow);
    m_timingClock = new QTimer(this);
    m_timingClock->setInterval(1000);
    connect(m_timingClock, &QTimer::timeout, this, &AgentControlCentre::updateTimingLabel);
    connect(m_timingStartBtn, &QPushButton::clicked, this, [this]() { Q_EMIT timingStartRequested(m_timingKind->currentData().toString(), timingDeliverable()); });
    connect(m_timingPauseBtn, &QPushButton::clicked, this, [this]() { Q_EMIT timingPauseToggled(!m_timingPaused); });
    connect(m_timingFinishBtn, &QPushButton::clicked, this, [this]() { Q_EMIT timingFinishRequested(m_timingRating->value()); });
    setTimingActive(false);
}

// ---------------------------------------------------------------------------- setup state
void AgentControlCentre::applyEditType(const QString &type)
{
    m_editType = editTypes().contains(type) ? type : QStringLiteral("full_edit");
    for (QAbstractButton *b : m_editTypeGroup->buttons()) {
        const bool on = b->property("editType").toString() == m_editType;
        b->setChecked(on);
        setTone(b, on ? "accent" : "");
    }
    if (m_editType != QLatin1String("custom")) {
        for (const EditTypeRow &r : kEditTypes) {
            if (m_editType == QLatin1String(r.id)) {
                setSelectedMode(QString::fromLatin1(r.mode));
            }
        }
    } else if (!m_advancedToggle->isChecked()) {
        // Custom edits are configured under Advanced: open it so the choices are visible.
        setAdvancedExpanded(true);
    }
}

QString AgentControlCentre::editType() const
{
    return m_editType;
}

void AgentControlCentre::setEditType(const QString &type)
{
    applyEditType(type);
}

QString AgentControlCentre::selectedMediaSource() const
{
    return m_mediaSourceCombo->currentData().toString();
}

void AgentControlCentre::setSelectedMediaSource(const QString &source)
{
    const int idx = m_mediaSourceCombo->findData(source);
    if (idx >= 0) {
        m_mediaSourceCombo->setCurrentIndex(idx);
    }
}

QString AgentControlCentre::selectedOutputFormat() const
{
    return m_outputFormatCombo->currentData().toString();
}

void AgentControlCentre::setSelectedOutputFormat(const QString &format)
{
    const int idx = m_outputFormatCombo->findData(format);
    if (idx >= 0) {
        m_outputFormatCombo->setCurrentIndex(idx);
    }
}

int AgentControlCentre::targetDurationMinutes() const
{
    return m_targetDurationSpin->value();
}

void AgentControlCentre::setTargetDurationMinutes(int minutes)
{
    m_targetDurationSpin->setValue(qBound(0, minutes, 240));
}

bool AgentControlCentre::qcMarkersVisible() const
{
    return m_qcMarkersCheck->isChecked();
}

void AgentControlCentre::setQcMarkersVisible(bool visible)
{
    const QSignalBlocker block(m_qcMarkersCheck);
    m_qcMarkersCheck->setChecked(visible);
}

bool AgentControlCentre::advancedExpanded() const
{
    return m_advancedToggle->isChecked();
}

void AgentControlCentre::setAdvancedExpanded(bool expanded)
{
    if (m_advancedToggle->isChecked() != expanded) {
        const QSignalBlocker block(m_advancedToggle);
        m_advancedToggle->setChecked(expanded);
    }
    m_advancedToggle->setArrowType(expanded ? Qt::DownArrow : Qt::RightArrow);
    m_advancedSection->setVisible(expanded);
}

void AgentControlCentre::setProjectContext(const QString &projectName, const QStringList &sequenceNames, int current)
{
    const QSignalBlocker block(m_sequenceCombo);
    m_sequenceCombo->clear();
    m_sequenceCombo->addItems(sequenceNames);
    if (current >= 0 && current < sequenceNames.size()) {
        m_sequenceCombo->setCurrentIndex(current);
    }
    m_sequenceCombo->setEnabled(!sequenceNames.isEmpty());
    const QString seq = (current >= 0 && current < sequenceNames.size()) ? sequenceNames.at(current) : QString();
    m_contextLabel->setText(projectName.isEmpty()
                                ? tr("No project open")
                                : (seq.isEmpty() ? tr("Project: %1").arg(projectName) : tr("Project: %1 · Sequence: %2").arg(projectName, seq)));
}

void AgentControlCentre::clearActiveJob()
{
    m_pollTimer->stop();
    m_elapsedClock->stop();
    m_jobElapsedWall.invalidate();
    m_jobStartedUtc = QDateTime();
    m_jobDir.clear();
    m_lastDiskSignature.clear();
    m_jobStatus.clear();
    m_draftProject.clear();
    m_lastStatusSeen.clear();
    m_agentSessionText.clear();
    m_agentDetailText.clear();
    m_revisionCount = 0;
    m_workerProcessRunning = false;
    m_progressMeasured = false;
    m_deadPolls = 0;
    m_lostReported = false;
    m_hasDraft = false;
    m_viewingFinished = false;

    for (int i = 0; i < m_stageList->count(); ++i) {
        QListWidgetItem *item = m_stageList->item(i);
        item->setData(kStageStateRole, QStringLiteral("pending"));
        item->setIcon(tallyIcon(TallyState::Idle));
        item->setText(tr(kStages[i].label));
        item->setToolTip(QString());
        item->setForeground(QColor(QString::fromLatin1(StudioPaletteTokens::kTextMuted)));
    }
    for (int i = 0; i < m_agentTree->topLevelItemCount(); ++i) {
        QTreeWidgetItem *it = m_agentTree->topLevelItem(i);
        it->setIcon(ColAgent, tallyIcon(TallyState::Idle));
        it->setText(ColStatus, tr("Idle"));
        it->setForeground(ColStatus, QColor(QString::fromLatin1(StudioPaletteTokens::kTextMuted)));
        it->setText(ColConfidence, QString());
        it->setText(ColOps, QString());
        it->setText(ColEngine, QString());
        it->setText(ColSummary, QString());
        it->setToolTip(ColSummary, QString());
    }
    m_reviewList->clear();
    m_reviewTitle->setText(tr("Worth a look"));
    m_reviewTitle->setVisible(false);
    m_reviewList->setVisible(false);
    m_summaryView->clear();
    m_problemLabel->clear();
    m_problemLabel->setVisible(false);
    m_reasoningLabel->clear();
    m_reasoningLabel->setVisible(false);
    m_doneDetail->clear();
    m_statusLabel->setText(tr("No production running"));
    m_statusLabel->setToolTip(QString());
    m_jobMetaLabel->setText(tr("No job"));
    m_jobMetaLabel->setToolTip(QString());
    m_engineBadge->clear();
    m_engineBadge->setVisible(false);
    m_elapsedLabel->clear();
    m_elapsedLabel->setVisible(false);
    m_resourceLabel->clear();
    m_resourceLabel->setVisible(false);
    m_progress->setRange(0, 1000);
    m_progress->setValue(0);
    m_progress->setVisible(false);
    {
        const QSignalBlocker block(m_qcMarkersCheck);
        m_qcMarkersCheck->setChecked(false);
    }
    m_reviseEdit->clear();
    m_reviseSelection->setChecked(false);
    updateActionStates();
}

void AgentControlCentre::resetForProject(const QString &projectUuid, const QString &projectName, const QStringList &sequences, int activeSequenceIndex)
{
    clearActiveJob();
    m_activeProjectUuid = projectUuid;
    if (m_briefEdit) {
        m_briefEdit->clear();
    }
    if (m_seriesEdit) {
        m_seriesEdit->clear();
    }
    if (m_episodeEdit) {
        m_episodeEdit->clear();
    }
    if (m_mediaSourceCombo) {
        const QSignalBlocker block(m_mediaSourceCombo);
        m_mediaSourceCombo->setCurrentIndex(0);
    }
    if (m_outputFormatCombo) {
        const QSignalBlocker block(m_outputFormatCombo);
        m_outputFormatCombo->setCurrentIndex(0);
    }
    if (m_targetDurationSpin) {
        const QSignalBlocker block(m_targetDurationSpin);
        m_targetDurationSpin->setValue(0);
    }
    m_historyList->clear();
    m_historyTitle->setText(tr("Queue and history"));
    setProjectContext(projectName, sequences, activeSequenceIndex);
    updateActionStates();
}

QString AgentControlCentre::activeProjectUuid() const
{
    return m_activeProjectUuid;
}

QString AgentControlCentre::projectContextText() const
{
    return m_contextLabel->text();
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
    return m_jobDir.isEmpty() ? QString() : QFileInfo(m_jobDir).fileName();
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
    if (profileId.isEmpty()) {
        return;
    }
    const int idx = m_profileCombo->findData(profileId);
    if (idx >= 0) {
        m_profileCombo->setItemText(idx, displayName.isEmpty() ? profileId : displayName);
        return;
    }
    m_profileCombo->addItem(displayName.isEmpty() ? profileId : displayName, profileId);
}

void AgentControlCentre::selectProfile(const QString &profileId)
{
    const int idx = m_profileCombo->findData(profileId);
    if (idx >= 0) {
        m_profileCombo->setCurrentIndex(idx);
    }
}

void AgentControlCentre::removeCustomProfile(const QString &profileId)
{
    const int idx = m_profileCombo->findData(profileId);
    if (idx >= 0) {
        m_profileCombo->removeItem(idx);
    }
}

void AgentControlCentre::loadHistory(const QJsonObject &payload)
{
    m_historyList->clear();
    QStringList shownDirs;
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
    QStringList args{QStringLiteral("run"), brief(), QStringLiteral("--mode"), selectedMode(), QStringLiteral("--edit-type"), m_editType};
    const QString profile = selectedProfileId();
    if (!profile.isEmpty()) {
        args << QStringLiteral("--profile") << profile;
    }
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
    const QString aspect = selectedOutputFormat();
    if (!aspect.isEmpty() && aspect != QLatin1String("match")) {
        args << QStringLiteral("--aspect-ratio") << aspect;
    }
    const int targetMin = targetDurationMinutes();
    if (targetMin > 0) {
        args << QStringLiteral("--target-duration-sec") << QString::number(targetMin * 60);
    }
    const QString source = selectedMediaSource();
    if (!source.isEmpty() && source != QLatin1String("sequence")) {
        args << QStringLiteral("--media-source") << source;
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

// ---------------------------------------------------------------------------- job state
bool AgentControlCentre::isRunningStatus() const
{
    return m_jobStatus == QLatin1String("running") || m_jobStatus == QLatin1String("planned");
}

void AgentControlCentre::renderStages(const QJsonArray &stages)
{
    QHash<QString, QJsonObject> byId;
    for (const QJsonValue &v : stages) {
        byId.insert(v.toObject().value(QStringLiteral("id")).toString(), v.toObject());
    }
    m_progressMeasured = false;
    double fraction = -1.0;
    for (int i = 0; i < m_stageList->count(); ++i) {
        QListWidgetItem *item = m_stageList->item(i);
        const QJsonObject s = byId.value(item->data(kIdRole).toString());
        QString state = s.value(QStringLiteral("state")).toString(QStringLiteral("pending"));
        // A dead worker's "running" is displayed as stopped.
        if (state == QLatin1String("running") && !isRunningStatus()) {
            state = QStringLiteral("stopped");
        }
        item->setData(kStageStateRole, state);
        item->setIcon(tallyIcon(stageTally(state)));
        QString text = tr(kStages[i].label);
        const QString msg = s.value(QStringLiteral("message")).toString();
        if (state == QLatin1String("running") && !msg.isEmpty()) {
            text += QStringLiteral("\n") + msg;
        } else if (state == QLatin1String("failed")) {
            const QJsonArray problems = s.value(QStringLiteral("problems")).toArray();
            const QJsonObject help = problems.isEmpty() ? QJsonObject() : problems.first().toObject().value(QStringLiteral("help")).toObject();
            text += QStringLiteral(" · ") + (help.isEmpty() ? tr("failed") : help.value(QStringLiteral("title")).toString());
        } else if (state == QLatin1String("skipped")) {
            text += tr(" · not needed");
            item->setToolTip(msg);
        } else if (state == QLatin1String("done") && !msg.isEmpty()) {
            text += QStringLiteral(" · ") + msg;
        }
        item->setText(text);
        const bool quiet = state == QLatin1String("pending") || state == QLatin1String("skipped");
        item->setForeground(QColor(QString::fromLatin1(quiet ? StudioPaletteTokens::kTextMuted : StudioPaletteTokens::kTextPrimary)));
        if (state == QLatin1String("running") && s.value(QStringLiteral("fraction")).isDouble()) {
            fraction = s.value(QStringLiteral("fraction")).toDouble();
        }
    }
    if (isRunningStatus()) {
        m_progress->setVisible(true);
        if (fraction > 0.0 && fraction < 1.0) {
            // Measured: the running stage reported how much of its work is done.
            m_progressMeasured = true;
            m_progress->setRange(0, 1000);
            m_progress->setValue(qRound(fraction * 1000.0));
        } else {
            m_progress->setRange(0, 0); // busy indicator: no invented percentage
        }
    } else {
        m_progress->setRange(0, 1000);
        m_progress->setValue(0);
        m_progress->setVisible(false);
    }
}

void AgentControlCentre::renderProblem(const QJsonObject &doc)
{
    const QJsonObject help = doc.value(QStringLiteral("error_help")).toObject();
    const bool show = !help.isEmpty() && (isResumable(m_jobStatus) || m_jobStatus == QLatin1String("failed"));
    if (!show) {
        m_problemLabel->setVisible(false);
        m_problemLabel->clear();
        return;
    }
    QString text = QStringLiteral("<b>%1</b><br>%2")
                       .arg(help.value(QStringLiteral("title")).toString().toHtmlEscaped(), help.value(QStringLiteral("fix")).toString().toHtmlEscaped());
    if (doc.value(QStringLiteral("repeated_failure")).toBool()) {
        text += QStringLiteral("<br>") + tr("This happened the same way last time, so fix the cause before resuming.").toHtmlEscaped();
    }
    m_problemLabel->setText(text);
    m_problemLabel->setToolTip(doc.value(QStringLiteral("error")).toString());
    m_problemLabel->setVisible(true);
}

void AgentControlCentre::showTaskProblem(const QString &title, const QString &fix, const QString &detail)
{
    m_problemLabel->setText(QStringLiteral("<b>%1</b><br>%2").arg(title.toHtmlEscaped(), fix.toHtmlEscaped()));
    m_problemLabel->setToolTip(detail);
    m_problemLabel->setVisible(true);
    m_runSection->setVisible(true);
}

void AgentControlCentre::loadJobStatus(const QJsonObject &doc)
{
    m_jobStatus = doc.value(QStringLiteral("status")).toString();
    const bool alive = m_workerProcessRunning || jobWorkerAlive(m_jobDir);
    if (isRunningStatus() && !alive && !m_jobDir.isEmpty()) {
        // job.json claims a live production but nothing holds the job: never show a frozen "running".
        if (++m_deadPolls >= kDeadPollsBeforeLost || m_jobStatus == QLatin1String("running")) {
            m_jobStatus = QStringLiteral("interrupted");
            if (!m_lostReported) {
                m_lostReported = true;
                Q_EMIT workerLost(m_jobDir);
            }
        }
    } else {
        m_deadPolls = 0;
    }
    const QJsonObject agents = doc.value(QStringLiteral("agents")).toObject();
    m_reviewList->clear();
    int done = 0;
    int active = 0;
    QString runningAgentText;
    for (int i = 0; i < m_agentTree->topLevelItemCount(); ++i) {
        QTreeWidgetItem *it = m_agentTree->topLevelItem(i);
        const QJsonObject a = agents.value(it->data(ColAgent, kIdRole).toString()).toObject();
        if (a.isEmpty()) {
            continue;
        }
        QString status = a.value(QStringLiteral("status")).toString();
        if (status == QLatin1String("running") && !isRunningStatus()) {
            status = QStringLiteral("interrupted");
        }
        const QString decision = a.value(QStringLiteral("decision")).toString();
        const bool stale = a.value(QStringLiteral("stale")).toBool();
        const bool isActive = a.value(QStringLiteral("active")).toBool();
        const QJsonArray review = a.value(QStringLiteral("review_items")).toArray();
        active += isActive ? 1 : 0;
        done += (isActive && (status == QLatin1String("completed") || status == QLatin1String("failed"))) ? 1 : 0;
        TallyState tally = TallyState::Idle;
        if (status == QLatin1String("running")) {
            tally = TallyState::Running;
            runningAgentText = a.value(QStringLiteral("message")).toString();
        } else if (status == QLatin1String("failed") || status == QLatin1String("blocked") || status == QLatin1String("cancelled") ||
                   status == QLatin1String("interrupted")) {
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
        for (const QJsonValue &r : review) {
            auto *row = new QListWidgetItem(tallyIcon(TallyState::Review), r.toString(), m_reviewList);
            row->setData(kIdRole, it->data(ColAgent, kIdRole));
            row->setToolTip(it->text(ColAgent));
        }
        if (!err.isEmpty()) {
            const QJsonObject help = a.value(QStringLiteral("help")).toObject();
            const QString text = help.isEmpty() ? err : QStringLiteral("%1. %2").arg(help.value(QStringLiteral("title")).toString(), help.value(QStringLiteral("fix")).toString());
            auto *row = new QListWidgetItem(tallyIcon(TallyState::Failed), QStringLiteral("%1 · %2").arg(it->text(ColAgent), text), m_reviewList);
            row->setData(kIdRole, it->data(ColAgent, kIdRole));
            row->setToolTip(err);
        }
    }
    // Instructions the department could not act on are listed, never dropped silently.
    for (const QJsonValue &u : doc.value(QStringLiteral("brief_coverage")).toObject().value(QStringLiteral("not_handled")).toArray()) {
        auto *row = new QListWidgetItem(tallyIcon(TallyState::Review),
                                        tr("Not done from your instructions: “%1”").arg(u.toObject().value(QStringLiteral("text")).toString()), m_reviewList);
        row->setToolTip(u.toObject().value(QStringLiteral("reason")).toString());
    }
    m_reviewTitle->setText(m_reviewList->count() > 0 ? tr("Worth a look · %1").arg(m_reviewList->count()) : tr("Worth a look"));
    m_reviewTitle->setVisible(m_reviewList->count() > 0);
    m_reviewList->setVisible(m_reviewList->count() > 0);

    renderStages(doc.value(QStringLiteral("stages")).toArray());
    renderProblem(doc);
    m_draftProject = doc.value(QStringLiteral("artifacts")).toObject().value(QStringLiteral("kdenlive_project")).toString();
    m_hasDraft = !m_draftProject.isEmpty();
    m_revisionCount = 0;
    for (const QJsonValue &r : doc.value(QStringLiteral("revisions")).toArray()) {
        m_revisionCount += r.toObject().value(QStringLiteral("undone")).toBool() ? 0 : 1;
    }
    m_jobMetaLabel->setText(doc.value(QStringLiteral("job_id")).toString());

    // Reasoning & compute-tier badge: honest distinction between deterministic, local neural, and subscription AI.
    const QJsonObject reasoning = doc.value(QStringLiteral("reasoning")).toObject();
    const QString reasoningMsg = reasoning.value(QStringLiteral("message")).toString();
    m_reasoningLabel->setText(reasoningMsg);
    m_reasoningLabel->setToolTip(reasoning.value(QStringLiteral("detail")).toString());
    m_reasoningLabel->setVisible(!reasoningMsg.isEmpty());

    bool anyNeural = !doc.value(QStringLiteral("asr_model_id")).toString().isEmpty();
    for (auto it = agents.constBegin(); it != agents.constEnd(); ++it) {
        if (it.value().toObject().value(QStringLiteral("neural_inference")).toBool()) {
            anyNeural = true;
            break;
        }
    }
    const QString rMode = reasoning.value(QStringLiteral("mode")).toString();
    const int rCalls = reasoning.value(QStringLiteral("calls")).toInt();
    if (rCalls > 0 || (!rMode.isEmpty() && rMode != QLatin1String("local"))) {
        m_engineBadge->setText(tr("Subscription AI"));
    } else if (anyNeural) {
        m_engineBadge->setText(tr("Local neural"));
    } else {
        m_engineBadge->setText(tr("Local deterministic"));
    }
    m_engineBadge->setVisible(true);

    // Lightweight resource telemetry (RSS MB & CPU time) when recorded in job.json.
    const QJsonObject resUsage = doc.value(QStringLiteral("resource_usage")).toObject();
    const QJsonValue rssVal = resUsage.value(QStringLiteral("rss_mb"));
    if (rssVal.isDouble()) {
        const QJsonValue cpuVal = resUsage.value(QStringLiteral("cpu_time_sec"));
        if (cpuVal.isDouble()) {
            m_resourceLabel->setText(tr("RAM %1 MB · CPU %2s").arg(qRound(rssVal.toDouble())).arg(cpuVal.toDouble(), 0, 'f', 1));
        } else {
            m_resourceLabel->setText(tr("RAM %1 MB").arg(qRound(rssVal.toDouble())));
        }
        m_resourceLabel->setVisible(true);
    } else {
        m_resourceLabel->clear();
        m_resourceLabel->setVisible(false);
    }

    // Live elapsed clock while running; final duration when stopped/finished.
    const QDateTime parsedStart = parseIsoUtc(doc.value(QStringLiteral("started_at_utc")).toString());
    if (parsedStart.isValid()) {
        m_jobStartedUtc = parsedStart;
    }

    const QJsonValue machine = doc.value(QStringLiteral("processing_elapsed_sec"));
    if (isRunningStatus()) {
        if (!m_jobStartedUtc.isValid() && !m_jobElapsedWall.isValid()) {
            m_jobElapsedWall.start();
        }
        if (!m_elapsedClock->isActive()) {
            m_elapsedClock->start();
        }
        updateElapsedLabel();
        QString current;
        for (int i = 0; i < m_stageList->count(); ++i) {
            if (m_stageList->item(i)->data(kStageStateRole).toString() == QLatin1String("running")) {
                current = tr(kStages[i].label);
            }
        }
        int countable = 0;
        int finished = 0;
        for (int i = 0; i < m_stageList->count(); ++i) {
            const QString st = m_stageList->item(i)->data(kStageStateRole).toString();
            countable += st == QLatin1String("skipped") ? 0 : 1;
            finished += st == QLatin1String("done") ? 1 : 0;
        }
        QString line = current.isEmpty() ? tr("Starting the production…") : tr("%1 · %2 of %3 stages done").arg(current).arg(finished).arg(countable);
        // Stall report: measured from the department's own last progress event.
        const QDateTime last = QDateTime::fromString(doc.value(QStringLiteral("last_progress_utc")).toString(), Qt::ISODateWithMs);
        if (last.isValid()) {
            const qint64 quiet = last.secsTo(QDateTime::currentDateTimeUtc());
            if (quiet >= 120) {
                line += tr(" · no new progress for %1 min (still working; Cancel stops it safely)").arg(quiet / 60);
            }
        }
        m_statusLabel->setText(line);
        m_statusLabel->setToolTip(runningAgentText);
    } else {
        m_elapsedClock->stop();
        if (machine.isDouble()) {
            m_elapsedLabel->setText(minutesSeconds(machine.toDouble()));
            m_elapsedLabel->setVisible(true);
        } else if (m_jobStartedUtc.isValid() || m_jobElapsedWall.isValid()) {
            updateElapsedLabel();
        } else {
            m_elapsedLabel->clear();
            m_elapsedLabel->setVisible(false);
        }
        QString line = plainStatus(m_jobStatus);
        if (machine.isDouble()) {
            line += tr(" · %1 on this Mac").arg(minutesSeconds(machine.toDouble()));
        }
        const QString revMsg = doc.value(QStringLiteral("last_revision")).toObject().value(QStringLiteral("message")).toString();
        m_statusLabel->setText(revMsg.isEmpty() ? line : revMsg);
        m_statusLabel->setToolTip(tr("%1 of %2 agents finished").arg(done).arg(active));
    }

    // Completion view.
    const bool finished = isFinishedWithDraft(m_jobStatus) && m_hasDraft;
    if (finished) {
        const int reviews = m_reviewList->count();
        if (m_jobStatus == QLatin1String("completed_with_errors")) {
            m_doneTitle->setText(tr("Editing finished with problems"));
        } else {
            m_doneTitle->setText(tr("Editing complete"));
        }
        QStringList detail;
        if (machine.isDouble()) {
            detail << tr("Took %1").arg(minutesSeconds(machine.toDouble()));
        }
        if (reviews > 0) {
            detail << tr("%n item(s) worth a look", nullptr, reviews);
        }
        if (m_jobStatus == QLatin1String("awaiting_review")) {
            detail << tr("each edit waits for your approval (Advanced)");
        }
        m_doneDetail->setText(detail.join(QStringLiteral(" · ")));
    }
    updateActionStates();
}

void AgentControlCentre::setActiveJobDir(const QString &jobDir)
{
    if (jobDir != m_jobDir) {
        m_deadPolls = 0;
        m_lostReported = false;
        m_viewingFinished = true;
        m_problemLabel->setVisible(false);
        m_jobStartedUtc = QDateTime();
        if (!jobDir.isEmpty()) {
            m_jobElapsedWall.start();
        } else {
            m_jobElapsedWall.invalidate();
        }
    }
    m_jobDir = jobDir;
    m_lastDiskSignature.clear();
    m_lastStatusSeen.clear();
    if (!jobDir.isEmpty()) {
        m_pollTimer->start();
    } else {
        m_pollTimer->stop();
        m_elapsedClock->stop();
    }
    updateActionStates();
}

QString AgentControlCentre::activeJobDir() const
{
    return m_jobDir;
}

void AgentControlCentre::setWorkerProcessRunning(bool running)
{
    m_workerProcessRunning = running;
    if (running) {
        m_deadPolls = 0;
        m_lostReported = false;
    }
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
    for (const QJsonValue &u : presence.value(QStringLiteral("learning_review")).toArray()) {
        const QJsonObject item = u.toObject();
        QTreeWidgetItem *owner = agentItem(QStringLiteral("learning"));
        auto *row = new QListWidgetItem(tallyIcon(TallyState::Review),
                                        QStringLiteral("%1 · %2").arg(owner != nullptr ? owner->text(ColAgent) : QStringLiteral("Learning"),
                                                                     item.value(QStringLiteral("message")).toString()),
                                        m_reviewList);
        row->setData(kIdRole, QStringLiteral("learning"));
    }
    m_reviewTitle->setText(m_reviewList->count() > 0 ? tr("Worth a look · %1").arg(m_reviewList->count()) : tr("Worth a look"));
    m_reviewTitle->setVisible(m_reviewList->count() > 0);
    m_reviewList->setVisible(m_reviewList->count() > 0);
}

QString AgentControlCentre::agentSessionText() const
{
    return m_agentSessionText;
}

QString AgentControlCentre::agentDetailText() const
{
    return m_agentDetailText;
}

QString AgentControlCentre::computeDiskSignature() const
{
    const QDir dir(m_jobDir);
    auto fileSig = [&dir](const QString &rel) -> QString {
        const QFileInfo fi(dir.filePath(rel));
        if (!fi.exists()) {
            return QStringLiteral("0");
        }
        return QStringLiteral("1:%1:%2").arg(fi.size()).arg(fi.lastModified().toMSecsSinceEpoch());
    };
    return fileSig(QStringLiteral("job.json")) + QLatin1Char('|') + fileSig(QStringLiteral("draft/agent_presence.json")) + QLatin1Char('|') +
           fileSig(QStringLiteral("draft/agent_edits.json")) + QLatin1Char('|') +
           (QFileInfo::exists(dir.filePath(QStringLiteral("CANCEL"))) ? QLatin1Char('1') : QLatin1Char('0')) +
           (QFileInfo::exists(dir.filePath(QStringLiteral("PAUSE"))) ? QLatin1Char('1') : QLatin1Char('0'));
}

bool AgentControlCentre::refreshFromDisk(bool force)
{
    if (m_jobDir.isEmpty()) {
        return false;
    }
    const QString sig = computeDiskSignature();
    const bool jobExists = sig.startsWith(QLatin1String("1:"));
    if (!jobExists) {
        return false;
    }
    if (!force && sig == m_lastDiskSignature) {
        // Still check worker liveness if the job thinks it is running, so dead workers are never missed.
        if (!isRunningStatus() || m_workerProcessRunning || jobWorkerAlive(m_jobDir)) {
            return true;
        }
    }
    QFile f(QDir(m_jobDir).filePath(QStringLiteral("job.json")));
    if (!f.open(QIODevice::ReadOnly)) {
        return false;
    }
    const QJsonDocument doc = QJsonDocument::fromJson(f.readAll());
    if (!doc.isObject()) {
        return false; // partially written file; next poll will pick it up
    }
    m_lastDiskSignature = sig;
    loadJobStatus(doc.object());
    {
        auto readObj = [this](const QString &rel) {
            QFile pf(QDir(m_jobDir).filePath(rel));
            return pf.open(QIODevice::ReadOnly) ? QJsonDocument::fromJson(pf.readAll()).object() : QJsonObject();
        };
        const QJsonObject presence = readObj(QStringLiteral("draft/agent_presence.json"));
        if (!presence.isEmpty()) {
            loadAgentSession(presence, readObj(QStringLiteral("draft/agent_edits.json")));
        }
    }
    const bool terminal = !isRunningStatus();
    if (terminal && m_jobStatus != m_lastStatusSeen) {
        m_lastStatusSeen = m_jobStatus;
        QFile s(QDir(m_jobDir).filePath(QStringLiteral("summary.md")));
        if (s.open(QIODevice::ReadOnly)) {
            m_summaryView->setPlainText(QString::fromUtf8(s.readAll()));
        }
        if (!m_workerProcessRunning) {
            m_pollTimer->stop(); // nothing will change until the editor acts; actions restart polling
        }
        Q_EMIT jobFinished(m_jobDir, m_jobStatus);
    }
    return true;
}

void AgentControlCentre::recoverJob(const QString &jobDir, bool processAlive)
{
    setWorkerProcessRunning(processAlive);
    setActiveJobDir(jobDir);
    refreshFromDisk(true);
}

void AgentControlCentre::setBusy(bool busy, const QString &text)
{
    setWorkerProcessRunning(busy);
    if (busy) {
        m_jobStatus = QStringLiteral("running");
        m_viewingFinished = true;
        m_problemLabel->setVisible(false);
        if (!m_jobElapsedWall.isValid()) {
            m_jobElapsedWall.start();
        }
        if (!m_elapsedClock->isActive()) {
            m_elapsedClock->start();
        }
        updateElapsedLabel();
        if (!m_jobDir.isEmpty()) {
            m_pollTimer->start();
        }
    } else {
        m_elapsedClock->stop();
        if (m_jobStatus == QLatin1String("running") && (m_jobDir.isEmpty() || !QFileInfo::exists(QDir(m_jobDir).filePath(QStringLiteral("job.json"))))) {
            // Worker terminated before writing job.json: never display an active production without a live worker.
            m_jobStatus = QStringLiteral("failed");
            m_pollTimer->stop();
        }
    }
    if (!text.isEmpty()) {
        m_statusLabel->setText(text);
    }
    updateActionStates();
}

void AgentControlCentre::updateElapsedLabel()
{
    double elapsedSec = 0.0;
    if (m_jobStartedUtc.isValid()) {
        elapsedSec = qMax<qint64>(0, m_jobStartedUtc.msecsTo(QDateTime::currentDateTimeUtc())) / 1000.0;
    } else if (m_jobElapsedWall.isValid()) {
        elapsedSec = m_jobElapsedWall.elapsed() / 1000.0;
    } else {
        m_elapsedLabel->clear();
        m_elapsedLabel->setVisible(false);
        return;
    }
    m_elapsedLabel->setText(minutesSeconds(elapsedSec));
    m_elapsedLabel->setVisible(true);
}

void AgentControlCentre::hideEvent(QHideEvent *event)
{
    m_pollTimer->stop();
    m_elapsedClock->stop();
    QWidget::hideEvent(event);
}

void AgentControlCentre::showEvent(QShowEvent *event)
{
    QWidget::showEvent(event);
    if (!m_jobDir.isEmpty() && (isRunningStatus() || m_workerProcessRunning)) {
        if (!m_pollTimer->isActive()) {
            m_pollTimer->start();
        }
        if (!m_elapsedClock->isActive()) {
            m_elapsedClock->start();
        }
        refreshFromDisk(true);
    }
}

void AgentControlCentre::updateActionStates()
{
    const bool hasJob = !m_jobDir.isEmpty();
    const bool running = (hasJob && isRunningStatus()) || m_workerProcessRunning;
    const bool hasAgent = !selectedAgentId().isEmpty() && selectedAgentId() != QLatin1String("executive");
    const bool finished = hasJob && !running && isFinishedWithDraft(m_jobStatus) && m_hasDraft;
    // One view at a time: set up, follow the run, or act on the finished edit.
    const bool showDone = finished && m_viewingFinished;
    const bool showRun = hasJob && m_viewingFinished && !showDone;
    m_setupSection->setVisible(!showRun && !showDone);
    m_runSection->setVisible(showRun || (!m_problemLabel->isHidden() && !showDone));
    m_doneSection->setVisible(showDone);

    const bool cancelExists = hasJob && QFileInfo::exists(QDir(m_jobDir).filePath(QStringLiteral("CANCEL")));
    const bool pauseExists = hasJob && QFileInfo::exists(QDir(m_jobDir).filePath(QStringLiteral("PAUSE")));
    m_pauseBtn->setVisible(running);
    m_pauseBtn->setEnabled(running && !pauseExists && !cancelExists);
    m_cancelBtn->setVisible(running);
    m_cancelBtn->setEnabled(running && !cancelExists);
    const bool resumable = hasJob && !running && isResumable(m_jobStatus);
    m_resumeBtn->setVisible(resumable);
    m_resumeBtn->setEnabled(resumable);
    const bool retryable = hasJob && !running && (m_jobStatus == QLatin1String("failed") || m_jobStatus.startsWith(QLatin1String("blocked")));
    m_retryBtn->setVisible(retryable);
    m_retryBtn->setEnabled(retryable);
    const bool discardable = hasJob && !running && !finished;
    m_discardBtn->setVisible(discardable);
    m_discardBtn->setEnabled(discardable);
    const bool canNewFromRun = !running && !showDone && (hasJob || !m_problemLabel->isHidden());
    m_newFromRunBtn->setVisible(canNewFromRun);
    m_newFromRunBtn->setEnabled(canNewFromRun);

    m_rerunBtn->setEnabled(hasJob && !running && hasAgent);
    const bool canRevise = finished && m_jobStatus != QLatin1String("completed_with_errors");
    m_reviseEdit->setEnabled(canRevise);
    m_reviseSelection->setEnabled(canRevise);
    m_reviseBtn->setEnabled(canRevise);
    m_undoRevisionBtn->setEnabled(canRevise && m_revisionCount > 0);
    m_undoRevisionBtn->setVisible(m_revisionCount > 0);
    m_acceptBtn->setEnabled(hasJob && !running && hasAgent);
    m_rejectBtn->setEnabled(hasJob && !running && hasAgent);
    m_startBtn->setEnabled(!running || m_queueCheck->isChecked());
    const bool draft = hasJob && !running && m_hasDraft;
    m_openSequenceBtn->setEnabled(draft);
    m_reviewBtn->setEnabled(draft);
    m_openDraftBtn->setEnabled(draft && QFileInfo::exists(m_draftProject));
    m_promoteBtn->setEnabled(draft);
    m_newJobBtn->setEnabled(!running);
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

QStringList AgentControlCentre::stageStates() const
{
    QStringList out;
    for (int i = 0; i < m_stageList->count(); ++i) {
        out << QStringLiteral("%1=%2").arg(QString::fromLatin1(kStages[i].label), m_stageList->item(i)->data(kStageStateRole).toString());
    }
    return out;
}

QString AgentControlCentre::statusText() const
{
    return m_statusLabel->text();
}

QString AgentControlCentre::reasoningText() const
{
    return m_reasoningLabel->text();
}

QString AgentControlCentre::problemText() const
{
    return m_problemLabel->isHidden() ? QString() : m_problemLabel->text();
}

QString AgentControlCentre::elapsedText() const
{
    return m_elapsedLabel->isHidden() ? QString() : m_elapsedLabel->text();
}

QString AgentControlCentre::resourceText() const
{
    return m_resourceLabel->isHidden() ? QString() : m_resourceLabel->text();
}

QString AgentControlCentre::engineBadgeText() const
{
    return m_engineBadge->isHidden() ? QString() : m_engineBadge->text();
}

bool AgentControlCentre::progressIsMeasured() const
{
    return m_progressMeasured;
}

bool AgentControlCentre::completionVisible() const
{
    return m_doneSection->isVisibleTo(const_cast<AgentControlCentre *>(this));
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
