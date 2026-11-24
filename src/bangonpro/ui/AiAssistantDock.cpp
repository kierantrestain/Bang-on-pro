/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "AiAssistantDock.h"
#include "BangOnProWorkspace.h"

#include <QBrush>
#include <QColor>
#include <QComboBox>
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QFontDatabase>
#include <QFrame>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QLabel>
#include <QPlainTextEdit>
#include <QProgressBar>
#include <QPushButton>
#include <QSizePolicy>
#include <QStyle>
#include <QTime>
#include <QToolButton>
#include <QTreeWidget>
#include <QTreeWidgetItem>
#include <QVBoxLayout>
#include <cmath>
#include <utility>

namespace BangOnPro {

namespace {

constexpr int kColOpType = 0;
constexpr int kColTarget = 1;
constexpr int kColFrameRange = 2;
constexpr int kColConfidence = 3;
constexpr int kColStatus = 4;
constexpr int kColRationale = 5;

constexpr int kOpIdRole = Qt::UserRole + 1;
constexpr int kLowConfidenceRole = Qt::UserRole + 2;

QString framesToTimecode(int frames, int fpsNum, int fpsDen)
{
    if (fpsNum <= 0 || fpsDen <= 0) {
        return QString::number(frames);
    }
    const double fps = static_cast<double>(fpsNum) / static_cast<double>(fpsDen);
    const int roundedFps = qMax(1, static_cast<int>(std::llround(fps)));
    const int safeFrames = qMax(0, frames);
    const int totalSeconds = safeFrames / roundedFps;
    const int remFrames = safeFrames % roundedFps;
    const int hours = totalSeconds / 3600;
    const int minutes = (totalSeconds % 3600) / 60;
    const int seconds = totalSeconds % 60;
    return QStringLiteral("%1:%2:%3:%4")
        .arg(hours, 2, 10, QLatin1Char('0'))
        .arg(minutes, 2, 10, QLatin1Char('0'))
        .arg(seconds, 2, 10, QLatin1Char('0'))
        .arg(remFrames, 2, 10, QLatin1Char('0'));
}

} // namespace

AiAssistantDock::AiAssistantDock(QWidget *parent)
    : QWidget(parent)
{
    qRegisterMetaType<BangOnPro::TimelineCommandBatch>("BangOnPro::TimelineCommandBatch");
    qRegisterMetaType<BangOnPro::TimelineCommandResponse>("BangOnPro::TimelineCommandResponse");

    setObjectName(QStringLiteral("bangOnProAiAssistant"));
    setMinimumWidth(340);

    buildUi();
    populateDefaultProfiles();
    setStyleSheet(brandStyleSheet());
    updateActionButtonStates();
    setStatusBadge(tr("Ready"), QStringLiteral("idle"));
    appendActivityLog(QStringLiteral("System"), tr("Bang On Pro AI Assistant initialized. Manual timeline editing remains fully active."),
                      QStringLiteral("INFO"));
}

void AiAssistantDock::buildUi()
{
    auto *rootLayout = new QVBoxLayout(this);
    rootLayout->setContentsMargins(8, 8, 8, 8);
    rootLayout->setSpacing(8);

    // 1. Distinct Bang On Pro branding header
    m_headerFrame = new QFrame(this);
    m_headerFrame->setObjectName(QStringLiteral("bangOnProHeader"));
    m_headerFrame->setFrameShape(QFrame::StyledPanel);

    auto *headerVBox = new QVBoxLayout(m_headerFrame);
    headerVBox->setContentsMargins(10, 8, 10, 8);
    headerVBox->setSpacing(4);

    auto *headerTopRow = new QHBoxLayout();
    headerTopRow->setSpacing(6);

    m_brandTitleLabel = new QLabel(QString::fromLatin1(kAppDisplayName), m_headerFrame);
    m_brandTitleLabel->setObjectName(QStringLiteral("bangOnProTitle"));

    m_brandBadgeLabel = new QLabel(tr("AI PRO"), m_headerFrame);
    m_brandBadgeLabel->setObjectName(QStringLiteral("bangOnProBadge"));

    m_statusBadgeLabel = new QLabel(tr("Ready"), m_headerFrame);
    m_statusBadgeLabel->setObjectName(QStringLiteral("bangOnProStatusBadge"));

    headerTopRow->addWidget(m_brandTitleLabel);
    headerTopRow->addWidget(m_brandBadgeLabel);
    headerTopRow->addStretch(1);
    headerTopRow->addWidget(m_statusBadgeLabel);

    auto *headerSubRow = new QHBoxLayout();
    headerSubRow->setSpacing(6);

    auto *manualNoteLabel = new QLabel(tr("Non-destructive proposals • Full manual control preserved"), m_headerFrame);
    manualNoteLabel->setObjectName(QStringLiteral("bangOnProSubtitle"));

    m_revisionLabel = new QLabel(tr("Rev #0"), m_headerFrame);
    m_revisionLabel->setObjectName(QStringLiteral("bangOnProRevision"));

    headerSubRow->addWidget(manualNoteLabel, 1);
    headerSubRow->addWidget(m_revisionLabel);

    headerVBox->addLayout(headerTopRow);
    headerVBox->addLayout(headerSubRow);
    rootLayout->addWidget(m_headerFrame);

    // 2. Show Profile selector
    auto *profileSectionLayout = new QVBoxLayout();
    profileSectionLayout->setSpacing(4);

    auto *profileHeaderRow = new QHBoxLayout();
    auto *profileLabel = new QLabel(tr("Show Profile"), this);
    profileLabel->setObjectName(QStringLiteral("bangOnProSectionLabel"));
    m_profileMetaLabel = new QLabel(tr("Review threshold: 72%"), this);
    m_profileMetaLabel->setObjectName(QStringLiteral("bangOnProMutedLabel"));
    profileHeaderRow->addWidget(profileLabel);
    profileHeaderRow->addStretch(1);
    profileHeaderRow->addWidget(m_profileMetaLabel);

    auto *profileControlRow = new QHBoxLayout();
    profileControlRow->setSpacing(6);

    m_profileCombo = new QComboBox(this);
    m_profileCombo->setObjectName(QStringLiteral("showProfileCombo"));
    m_profileCombo->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);

    m_loadProfileButton = new QPushButton(tr("Load JSON…"), this);
    m_loadProfileButton->setObjectName(QStringLiteral("loadProfileButton"));
    m_loadProfileButton->setToolTip(tr("Load a custom Show Profile v1 JSON file"));

    m_editProfileButton = new QPushButton(tr("Edit Profile…"), this);
    m_editProfileButton->setObjectName(QStringLiteral("editProfileButton"));
    m_editProfileButton->setToolTip(tr("Create or customize a Show Profile (pacing, cameras, audio, graphics)"));

    m_providerSettingsButton = new QPushButton(tr("AI Providers…"), this);
    m_providerSettingsButton->setObjectName(QStringLiteral("providerSettingsButton"));
    m_providerSettingsButton->setToolTip(tr("Configure local AI models or consent-gated external AI providers"));

    profileControlRow->addWidget(m_profileCombo, 1);
    profileControlRow->addWidget(m_loadProfileButton);
    profileControlRow->addWidget(m_editProfileButton);
    profileControlRow->addWidget(m_providerSettingsButton);

    profileSectionLayout->addLayout(profileHeaderRow);
    profileSectionLayout->addLayout(profileControlRow);
    rootLayout->addLayout(profileSectionLayout);

    connect(m_profileCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, &AiAssistantDock::handleProfileSelectionChanged);
    connect(m_loadProfileButton, &QPushButton::clicked, this, &AiAssistantDock::handleLoadCustomProfileClicked);
    connect(m_editProfileButton, &QPushButton::clicked, this, &AiAssistantDock::editProfileRequested);
    connect(m_providerSettingsButton, &QPushButton::clicked, this, &AiAssistantDock::providerSettingsRequested);

    // 3. Instruction box & quick-action chips
    auto *promptHeaderRow = new QHBoxLayout();
    auto *promptLabel = new QLabel(tr("Director Instruction"), this);
    promptLabel->setObjectName(QStringLiteral("bangOnProSectionLabel"));
    m_useBinMediaButton = new QPushButton(tr("Use Bin Media"), this);
    m_useBinMediaButton->setObjectName(QStringLiteral("useBinMediaButton"));
    m_useBinMediaButton->setCheckable(true);
    m_useBinMediaButton->setChecked(true);
    m_useBinMediaButton->setToolTip(tr("Automatically source camera clips from the current Project Bin when available"));
    promptHeaderRow->addWidget(promptLabel);
    promptHeaderRow->addStretch(1);
    promptHeaderRow->addWidget(m_useBinMediaButton);
    rootLayout->addLayout(promptHeaderRow);

    m_instructionInput = new QPlainTextEdit(this);
    m_instructionInput->setObjectName(QStringLiteral("instructionInput"));
    m_instructionInput->setPlaceholderText(
        tr("Describe the edit, e.g. \"Sync all four cameras, remove setup delays, build a first cut and add our standard graphics\""));
    m_instructionInput->setFixedHeight(74);
    m_instructionInput->setTabChangesFocus(true);
    rootLayout->addWidget(m_instructionInput);

    // Quick-action chips grid
    struct QuickChipSpec
    {
        QString label;
        QString prompt;
    };
    const QVector<QuickChipSpec> chips = {
        {tr("4-Cam Sync + First Cut + Graphics"),
         QStringLiteral("Sync all four cameras, remove setup delays, build a first cut and add our standard graphics")},
        {tr("Remove Setup Delays & Dead Air"), QStringLiteral("Remove setup delays and silence pauses while preserving natural pacing")},
        {tr("Active-Speaker Multicam Cut"), QStringLiteral("Build a multicam edit switching on active speaker with reaction shots")},
        {tr("Dialogue Polish + Captions"), QStringLiteral("Normalize dialogue loudness, clean background noise, and add captions and lower thirds")},
    };

    auto *chipsGrid = new QGridLayout();
    chipsGrid->setSpacing(4);
    m_quickPrompts.clear();
    m_quickChipButtons.clear();

    for (int i = 0; i < chips.size(); ++i) {
        const QuickChipSpec &spec = chips.at(i);
        m_quickPrompts.append(spec.prompt);

        auto *chipBtn = new QPushButton(spec.label, this);
        chipBtn->setObjectName(QStringLiteral("quickActionChip_%1").arg(i));
        chipBtn->setProperty("bangOnProChip", true);
        chipBtn->setToolTip(spec.prompt);
        chipBtn->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
        connect(chipBtn, &QPushButton::clicked, this, [this, i]() { triggerQuickAction(i); });

        m_quickChipButtons.append(chipBtn);
        chipsGrid->addWidget(chipBtn, i / 2, i % 2);
    }
    rootLayout->addLayout(chipsGrid);

    // 4. Proposed Changes Diff Tree (with per-change accept/reject checkboxes)
    auto *diffHeaderRow = new QHBoxLayout();
    auto *diffLabel = new QLabel(tr("Proposed Timeline Changes (Diff)"), this);
    diffLabel->setObjectName(QStringLiteral("bangOnProSectionLabel"));
    m_proposalSummaryLabel = new QLabel(tr("No active proposal"), this);
    m_proposalSummaryLabel->setObjectName(QStringLiteral("proposalSummaryLabel"));
    diffHeaderRow->addWidget(diffLabel);
    diffHeaderRow->addStretch(1);
    diffHeaderRow->addWidget(m_proposalSummaryLabel);
    rootLayout->addLayout(diffHeaderRow);

    auto *diffSelectRow = new QHBoxLayout();
    diffSelectRow->setSpacing(4);
    m_selectAllOpsButton = new QPushButton(tr("Select All"), this);
    m_selectAllOpsButton->setObjectName(QStringLiteral("selectAllOpsButton"));
    m_selectAllOpsButton->setEnabled(false);
    m_uncheckLowConfButton = new QPushButton(tr("Uncheck Low-Confidence"), this);
    m_uncheckLowConfButton->setObjectName(QStringLiteral("uncheckLowConfButton"));
    m_uncheckLowConfButton->setEnabled(false);
    diffSelectRow->addWidget(m_selectAllOpsButton);
    diffSelectRow->addWidget(m_uncheckLowConfButton);
    diffSelectRow->addStretch(1);
    rootLayout->addLayout(diffSelectRow);

    connect(m_selectAllOpsButton, &QPushButton::clicked, this, [this]() { setAllOperationsChecked(true); });
    connect(m_uncheckLowConfButton, &QPushButton::clicked, this, [this]() { uncheckLowConfidenceOperations(); });

    m_diffTree = new QTreeWidget(this);
    m_diffTree->setObjectName(QStringLiteral("proposedDiffTree"));
    m_diffTree->setColumnCount(6);
    m_diffTree->setHeaderLabels({tr("Operation"), tr("Track / Target"), tr("Frame Range"), tr("Confidence"), tr("Review"), tr("Rationale")});
    m_diffTree->setAlternatingRowColors(true);
    m_diffTree->setRootIsDecorated(true);
    m_diffTree->setUniformRowHeights(false);
    m_diffTree->setSelectionMode(QAbstractItemView::SingleSelection);
    m_diffTree->setMinimumHeight(140);

    QHeaderView *header = m_diffTree->header();
    header->setStretchLastSection(true);
    header->setSectionResizeMode(kColOpType, QHeaderView::ResizeToContents);
    header->setSectionResizeMode(kColTarget, QHeaderView::ResizeToContents);
    header->setSectionResizeMode(kColFrameRange, QHeaderView::ResizeToContents);
    header->setSectionResizeMode(kColConfidence, QHeaderView::ResizeToContents);
    header->setSectionResizeMode(kColStatus, QHeaderView::ResizeToContents);

    connect(m_diffTree, &QTreeWidget::itemChanged, this, &AiAssistantDock::handleDiffItemChanged);
    rootLayout->addWidget(m_diffTree, 1);

    // 5. Action Bar (Generate, Accept & Apply, Reject, Compare A/B, Refine, Undo)
    auto *actionGrid = new QGridLayout();
    actionGrid->setSpacing(6);

    m_generateButton = new QPushButton(tr("Generate Proposal"), this);
    m_generateButton->setObjectName(QStringLiteral("generateProposalButton"));
    m_generateButton->setProperty("bangOnProPrimary", true);

    m_acceptButton = new QPushButton(tr("Accept && Apply"), this);
    m_acceptButton->setObjectName(QStringLiteral("acceptProposalButton"));
    m_acceptButton->setProperty("bangOnProAccept", true);

    m_rejectButton = new QPushButton(tr("Reject"), this);
    m_rejectButton->setObjectName(QStringLiteral("rejectProposalButton"));
    m_rejectButton->setProperty("bangOnProDanger", true);

    m_compareButton = new QPushButton(tr("Compare (A/B)"), this);
    m_compareButton->setObjectName(QStringLiteral("compareToggleButton"));
    m_compareButton->setCheckable(true);
    m_compareButton->setToolTip(tr("Toggle between Original Timeline (A) and AI Proposal Preview (B)"));

    m_refineButton = new QPushButton(tr("Refine"), this);
    m_refineButton->setObjectName(QStringLiteral("refineProposalButton"));
    m_refineButton->setToolTip(tr("Request a refined proposal using your updated instructions"));

    m_undoButton = new QPushButton(tr("Undo Last AI Batch"), this);
    m_undoButton->setObjectName(QStringLiteral("undoBatchButton"));
    m_undoButton->setToolTip(tr("Atomically revert the last applied AI batch transaction"));

    actionGrid->addWidget(m_generateButton, 0, 0, 1, 2);
    actionGrid->addWidget(m_acceptButton, 0, 2, 1, 1);
    actionGrid->addWidget(m_rejectButton, 1, 0, 1, 1);
    actionGrid->addWidget(m_compareButton, 1, 1, 1, 1);
    actionGrid->addWidget(m_refineButton, 1, 2, 1, 1);
    actionGrid->addWidget(m_undoButton, 2, 0, 1, 3);
    rootLayout->addLayout(actionGrid);

    connect(m_generateButton, &QPushButton::clicked, this, &AiAssistantDock::handleGenerateClicked);
    connect(m_acceptButton, &QPushButton::clicked, this, &AiAssistantDock::handleAcceptClicked);
    connect(m_rejectButton, &QPushButton::clicked, this, &AiAssistantDock::handleRejectClicked);
    connect(m_compareButton, &QPushButton::toggled, this, &AiAssistantDock::handleCompareToggled);
    connect(m_refineButton, &QPushButton::clicked, this, &AiAssistantDock::handleRefineClicked);
    connect(m_undoButton, &QPushButton::clicked, this, &AiAssistantDock::handleUndoClicked);

    // 6. Collapsible Live Agent Activity Log + Progress Bar + Cancel + Storage Strip
    auto *progressRow = new QHBoxLayout();
    progressRow->setSpacing(6);

    m_activityToggleButton = new QToolButton(this);
    m_activityToggleButton->setObjectName(QStringLiteral("activityLogToggleButton"));
    m_activityToggleButton->setText(tr("Agent Activity Log"));
    m_activityToggleButton->setCheckable(true);
    m_activityToggleButton->setChecked(true);
    m_activityToggleButton->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
    m_activityToggleButton->setArrowType(Qt::DownArrow);
    m_activityToggleButton->setAutoRaise(true);

    m_taskStatusLabel = new QLabel(tr("Idle"), this);
    m_taskStatusLabel->setObjectName(QStringLiteral("taskStatusLabel"));

    m_cancelButton = new QPushButton(tr("Cancel"), this);
    m_cancelButton->setObjectName(QStringLiteral("cancelTaskButton"));
    m_cancelButton->setEnabled(false);

    progressRow->addWidget(m_activityToggleButton);
    progressRow->addStretch(1);
    progressRow->addWidget(m_taskStatusLabel);
    progressRow->addWidget(m_cancelButton);
    rootLayout->addLayout(progressRow);

    m_progressBar = new QProgressBar(this);
    m_progressBar->setObjectName(QStringLiteral("agentProgressBar"));
    m_progressBar->setRange(0, 100);
    m_progressBar->setValue(0);
    m_progressBar->setTextVisible(true);
    m_progressBar->setFixedHeight(14);
    rootLayout->addWidget(m_progressBar);

    m_activityContainer = new QWidget(this);
    m_activityContainer->setObjectName(QStringLiteral("activityLogContainer"));
    auto *logLayout = new QVBoxLayout(m_activityContainer);
    logLayout->setContentsMargins(0, 0, 0, 0);
    logLayout->setSpacing(4);

    m_activityLog = new QPlainTextEdit(m_activityContainer);
    m_activityLog->setObjectName(QStringLiteral("agentActivityLog"));
    m_activityLog->setReadOnly(true);
    m_activityLog->setMaximumBlockCount(500);
    m_activityLog->setMinimumHeight(80);
    m_activityLog->setMaximumHeight(150);
    m_activityLog->setFont(QFontDatabase::systemFont(QFontDatabase::FixedFont));
    logLayout->addWidget(m_activityLog);

    auto *storageRow = new QHBoxLayout();
    m_storageSummaryLabel = new QLabel(tr("Local AI • Cache bounded"), m_activityContainer);
    m_storageSummaryLabel->setObjectName(QStringLiteral("bangOnProMutedLabel"));
    m_clearCacheButton = new QPushButton(tr("Clear Cache"), m_activityContainer);
    m_clearCacheButton->setObjectName(QStringLiteral("clearCacheButton"));
    m_clearCacheButton->setToolTip(tr("Clear temporary proxy & render caches without touching source media"));
    storageRow->addWidget(m_storageSummaryLabel, 1);
    storageRow->addWidget(m_clearCacheButton);
    logLayout->addLayout(storageRow);

    rootLayout->addWidget(m_activityContainer);

    connect(m_activityToggleButton, &QToolButton::toggled, this, &AiAssistantDock::handleActivityToggle);
    connect(m_cancelButton, &QPushButton::clicked, this, &AiAssistantDock::handleCancelClicked);
    connect(m_clearCacheButton, &QPushButton::clicked, this, &AiAssistantDock::clearCacheRequested);
}

void AiAssistantDock::populateDefaultProfiles()
{
    m_profileCombo->blockSignals(true);
    m_profileCombo->clear();
    m_profileCombo->addItem(tr("Podcast Multicam"), QStringLiteral("podcast"));
    m_profileCombo->addItem(tr("Gameshow Multicam"), QStringLiteral("gameshow"));
    m_profileCombo->addItem(tr("Sports Discussion"), QStringLiteral("sports_discussion"));
    m_profileCombo->addItem(tr("Daily News"), QStringLiteral("daily_news"));
    m_profileCombo->addItem(tr("Custom JSON Profile…"), QStringLiteral("custom"));
    m_profileCombo->setCurrentIndex(0);
    m_profileCombo->blockSignals(false);
}

QString AiAssistantDock::selectedProfileId() const
{
    const QString dataId = m_profileCombo->currentData().toString();
    if (dataId == QStringLiteral("custom") && !m_customProfileId.isEmpty()) {
        return m_customProfileId;
    }
    return dataId;
}

QString AiAssistantDock::customProfilePath() const
{
    return m_customProfilePath;
}

bool AiAssistantDock::setSelectedProfileId(const QString &profileId)
{
    for (int i = 0; i < m_profileCombo->count(); ++i) {
        if (m_profileCombo->itemData(i).toString() == profileId) {
            m_profileCombo->setCurrentIndex(i);
            return true;
        }
    }
    return false;
}

bool AiAssistantDock::loadCustomProfileFromFile(const QString &filePath, QString *errorMessage)
{
    QFile file(filePath);
    if (!file.open(QIODevice::ReadOnly)) {
        const QString err = tr("Unable to open show profile file: %1").arg(filePath);
        if (errorMessage) {
            *errorMessage = err;
        }
        appendActivityLog(QStringLiteral("Profile"), err, QStringLiteral("ERROR"));
        return false;
    }

    QJsonParseError parseError{};
    const QJsonDocument doc = QJsonDocument::fromJson(file.readAll(), &parseError);
    if (parseError.error != QJsonParseError::NoError || !doc.isObject()) {
        const QString err = tr("Invalid JSON in show profile: %1").arg(parseError.errorString());
        if (errorMessage) {
            *errorMessage = err;
        }
        appendActivityLog(QStringLiteral("Profile"), err, QStringLiteral("ERROR"));
        return false;
    }

    return applyCustomProfileJson(doc.object(), filePath, errorMessage);
}

bool AiAssistantDock::applyCustomProfileJson(const QJsonObject &profileObj, const QString &sourcePath, QString *errorMessage)
{
    const QString profileId = profileObj.value(QStringLiteral("profile_id")).toString();
    const QString displayName = profileObj.value(QStringLiteral("display_name")).toString();
    const QString schemaVersion = profileObj.value(QStringLiteral("schema_version")).toString();

    if (profileId.isEmpty() || displayName.isEmpty()) {
        const QString err = tr("Show profile JSON requires non-empty 'profile_id' and 'display_name'.");
        if (errorMessage) {
            *errorMessage = err;
        }
        appendActivityLog(QStringLiteral("Profile"), err, QStringLiteral("ERROR"));
        return false;
    }
    if (!schemaVersion.isEmpty() && schemaVersion != QStringLiteral("1.0")) {
        const QString err = tr("Unsupported Show Profile schema_version '%1' (expected '1.0').").arg(schemaVersion);
        if (errorMessage) {
            *errorMessage = err;
        }
        appendActivityLog(QStringLiteral("Profile"), err, QStringLiteral("ERROR"));
        return false;
    }

    const QJsonObject editingRules = profileObj.value(QStringLiteral("editing_rules")).toObject();
    if (editingRules.contains(QStringLiteral("review_confidence_threshold"))) {
        const double threshold = editingRules.value(QStringLiteral("review_confidence_threshold")).toDouble(0.72);
        setReviewConfidenceThreshold(threshold);
    }

    m_customProfileId = profileId;
    m_customProfilePath = sourcePath;

    const int customIndex = m_profileCombo->findData(QStringLiteral("custom"));
    m_profileCombo->blockSignals(true);
    if (customIndex >= 0) {
        m_profileCombo->setItemText(customIndex, tr("Custom: %1").arg(displayName));
        m_profileCombo->setCurrentIndex(customIndex);
    }
    m_profileCombo->blockSignals(false);

    appendActivityLog(QStringLiteral("Profile"), tr("Loaded custom Show Profile '%1' (%2)").arg(displayName, profileId), QStringLiteral("INFO"));
    Q_EMIT showProfileChanged(profileId, m_customProfilePath);
    return true;
}

QString AiAssistantDock::instructionText() const
{
    return m_instructionInput->toPlainText().trimmed();
}

void AiAssistantDock::setInstructionText(const QString &instruction)
{
    m_instructionInput->setPlainText(instruction);
}

QStringList AiAssistantDock::quickActionPrompts() const
{
    return m_quickPrompts;
}

void AiAssistantDock::triggerQuickAction(int index)
{
    if (index < 0 || index >= m_quickPrompts.size()) {
        return;
    }
    m_instructionInput->setPlainText(m_quickPrompts.at(index));
    m_instructionInput->setFocus();
    appendActivityLog(QStringLiteral("UI"), tr("Loaded quick-action instruction preset #%1").arg(index + 1), QStringLiteral("INFO"));
}

void AiAssistantDock::displayProposal(const TimelineCommandBatch &batch)
{
    m_currentBatch = batch;
    m_hasPendingProposal = !batch.operations.isEmpty();
    m_lowConfidenceCount = 0;

    m_updatingDiffChecks = true;
    m_diffTree->clear();

    const QColor warningBg(95, 58, 12);
    const QColor warningFg(255, 206, 112);
    const QColor okFg(122, 224, 152);

    for (const TimelineOperation &op : std::as_const(batch.operations)) {
        auto *item = new QTreeWidgetItem(m_diffTree);
        item->setFlags(item->flags() | Qt::ItemIsUserCheckable);
        item->setCheckState(kColOpType, Qt::Checked);

        const bool isLowConf = op.confidence < m_reviewConfidenceThreshold;
        if (isLowConf) {
            ++m_lowConfidenceCount;
        }

        const int pct = static_cast<int>(std::llround(qBound(0.0, op.confidence, 1.0) * 100.0));
        item->setText(kColOpType, op.type);
        item->setText(kColTarget, formatOperationTarget(op));
        item->setText(kColFrameRange, formatFrameRange(op.frameRange));
        item->setText(kColConfidence, QStringLiteral("%1%").arg(pct));
        item->setText(kColStatus, isLowConf ? tr("⚠ Low Confidence") : tr("Ready"));
        item->setText(kColRationale, op.rationale);

        item->setData(kColOpType, kOpIdRole, op.opId);
        item->setData(kColStatus, kLowConfidenceRole, isLowConf);
        item->setToolTip(kColOpType, tr("Operation ID: %1").arg(op.opId));
        item->setToolTip(kColRationale, op.rationale);

        if (isLowConf) {
            for (int col = 0; col < m_diffTree->columnCount(); ++col) {
                item->setBackground(col, QBrush(warningBg));
                item->setForeground(col, QBrush(warningFg));
            }
        } else {
            item->setForeground(kColConfidence, QBrush(okFg));
        }

        // Expandable parameter detail child nodes
        if (!op.params.isEmpty()) {
            const QStringList keys = op.params.keys();
            for (const QString &key : keys) {
                auto *paramItem = new QTreeWidgetItem(item);
                paramItem->setText(kColOpType, QStringLiteral("param.%1").arg(key));
                paramItem->setText(kColTarget, formatJsonValueSummary(op.params.value(key)));
            }
        }
    }
    m_updatingDiffChecks = false;

    if (m_hasPendingProposal) {
        QString summary = tr("%1 ops (%2)").arg(batch.operations.size()).arg(batch.transactionLabel);
        if (m_lowConfidenceCount > 0) {
            summary += tr(" • ⚠ %1 review").arg(m_lowConfidenceCount);
        }
        m_proposalSummaryLabel->setText(summary);
        setStatusBadge(tr("Proposal Ready"), QStringLiteral("proposal"));

        appendActivityLog(
            batch.agentId.isEmpty() ? QStringLiteral("Orchestrator") : batch.agentId,
            tr("Proposed batch '%1' (%2 operations, %3 flagged for review).").arg(batch.requestId).arg(batch.operations.size()).arg(m_lowConfidenceCount),
            m_lowConfidenceCount > 0 ? QStringLiteral("WARN") : QStringLiteral("INFO"));
    } else {
        m_proposalSummaryLabel->setText(tr("Empty proposal"));
        setStatusBadge(tr("Ready"), QStringLiteral("idle"));
    }

    updateActionButtonStates();
}

void AiAssistantDock::clearProposal()
{
    m_currentBatch = TimelineCommandBatch{};
    m_hasPendingProposal = false;
    m_lowConfidenceCount = 0;
    m_diffTree->clear();
    m_proposalSummaryLabel->setText(tr("No active proposal"));

    if (m_compareButton->isChecked()) {
        m_compareButton->blockSignals(true);
        m_compareButton->setChecked(false);
        m_compareButton->setText(tr("Compare (A/B)"));
        m_compareButton->blockSignals(false);
    }

    setStatusBadge(tr("Ready"), QStringLiteral("idle"));
    updateActionButtonStates();
}

bool AiAssistantDock::hasPendingProposal() const
{
    return m_hasPendingProposal;
}

const TimelineCommandBatch &AiAssistantDock::currentProposal() const
{
    return m_currentBatch;
}

int AiAssistantDock::lowConfidenceOperationCount() const
{
    return m_lowConfidenceCount;
}

double AiAssistantDock::reviewConfidenceThreshold() const
{
    return m_reviewConfidenceThreshold;
}

void AiAssistantDock::setReviewConfidenceThreshold(double threshold)
{
    m_reviewConfidenceThreshold = qBound(0.0, threshold, 1.0);
    const int pct = static_cast<int>(std::llround(m_reviewConfidenceThreshold * 100.0));
    m_profileMetaLabel->setText(tr("Review threshold: %1%").arg(pct));
    if (m_hasPendingProposal) {
        displayProposal(m_currentBatch);
    }
}

void AiAssistantDock::applyExecutionResponse(const TimelineCommandResponse &response)
{
    setTimelineRevision(response.currentRevision);

    // Map opId -> result
    for (int i = 0; i < m_diffTree->topLevelItemCount(); ++i) {
        QTreeWidgetItem *item = m_diffTree->topLevelItem(i);
        const QString opId = item->data(kColOpType, kOpIdRole).toString();
        for (const TimelineOperationResult &res : std::as_const(response.results)) {
            if (res.opId == opId) {
                item->setText(kColStatus, res.applied ? tr("Applied") : tr("Failed"));
                if (!res.message.isEmpty()) {
                    item->setToolTip(kColStatus, res.message);
                }
                break;
            }
        }
    }

    if (response.status == CommandStatus::Ok) {
        m_hasPendingProposal = false;
        m_hasAppliedProposal = true;
        m_canUndo = true;
        setStatusBadge(tr("Applied"), QStringLiteral("ok"));
        appendActivityLog(QStringLiteral("TimelineEngine"),
                          tr("Applied batch '%1' (rev %2 → %3).").arg(response.requestId).arg(response.previousRevision).arg(response.currentRevision),
                          QStringLiteral("INFO"));
    } else if (response.status == CommandStatus::PendingApproval) {
        setStatusBadge(tr("Pending Approval"), QStringLiteral("proposal"));
        appendActivityLog(QStringLiteral("TimelineEngine"), tr("Batch '%1' is awaiting approval.").arg(response.requestId), QStringLiteral("INFO"));
    } else {
        setStatusBadge(commandStatusToString(response.status), QStringLiteral("error"));
        appendActivityLog(QStringLiteral("TimelineEngine"),
                          tr("Batch '%1' status=%2: %3").arg(response.requestId, commandStatusToString(response.status), response.errorMessage),
                          QStringLiteral("ERROR"));
    }

    updateActionButtonStates();
}

void AiAssistantDock::setTimelineRevision(qint64 revision)
{
    m_timelineRevision = revision;
    m_revisionLabel->setText(tr("Rev #%1").arg(revision));
}

qint64 AiAssistantDock::timelineRevision() const
{
    return m_timelineRevision;
}

void AiAssistantDock::setCanUndo(bool canUndo)
{
    m_canUndo = canUndo;
    updateActionButtonStates();
}

bool AiAssistantDock::canUndo() const
{
    return m_canUndo;
}

void AiAssistantDock::setCompareModeActive(bool active)
{
    if (m_compareButton->isChecked() != active) {
        m_compareButton->setChecked(active);
    }
}

bool AiAssistantDock::isCompareModeActive() const
{
    return m_compareButton->isChecked();
}

void AiAssistantDock::setBusy(bool busy, const QString &statusText, int progressPercent)
{
    m_isBusy = busy;
    m_cancelButton->setEnabled(busy);

    if (busy) {
        m_taskStatusLabel->setText(statusText.isEmpty() ? tr("Running…") : statusText);
        if (progressPercent < 0) {
            m_progressBar->setRange(0, 0);
        } else {
            m_progressBar->setRange(0, 100);
            m_progressBar->setValue(qBound(0, progressPercent, 100));
        }
        setStatusBadge(tr("Working"), QStringLiteral("busy"));
    } else {
        m_taskStatusLabel->setText(statusText.isEmpty() ? tr("Idle") : statusText);
        m_progressBar->setRange(0, 100);
        m_progressBar->setValue(progressPercent >= 0 ? qBound(0, progressPercent, 100) : 0);
        if (m_hasPendingProposal) {
            setStatusBadge(tr("Proposal Ready"), QStringLiteral("proposal"));
        } else {
            setStatusBadge(tr("Ready"), QStringLiteral("idle"));
        }
    }

    updateActionButtonStates();
}

bool AiAssistantDock::isBusy() const
{
    return m_isBusy;
}

void AiAssistantDock::appendActivityLog(const QString &agent, const QString &message, const QString &level)
{
    const QString ts = QTime::currentTime().toString(QStringLiteral("HH:mm:ss"));
    const QString line = QStringLiteral("[%1] [%2] [%3] %4").arg(ts, level.toUpper(), agent, message);
    m_activityLog->appendPlainText(line);
}

void AiAssistantDock::clearActivityLog()
{
    m_activityLog->clear();
}

QString AiAssistantDock::activityLogText() const
{
    return m_activityLog->toPlainText();
}

void AiAssistantDock::setActivityLogExpanded(bool expanded)
{
    m_activityToggleButton->setChecked(expanded);
}

bool AiAssistantDock::isActivityLogExpanded() const
{
    return m_activityToggleButton->isChecked();
}

QTreeWidget *AiAssistantDock::diffTreeWidget() const
{
    return m_diffTree;
}

QStringList AiAssistantDock::selectedOperationIds() const
{
    QStringList ids;
    for (int i = 0; i < m_diffTree->topLevelItemCount(); ++i) {
        QTreeWidgetItem *item = m_diffTree->topLevelItem(i);
        if (item->checkState(kColOpType) == Qt::Checked) {
            const QString opId = item->data(kColOpType, kOpIdRole).toString();
            if (!opId.isEmpty()) {
                ids.append(opId);
            }
        }
    }
    return ids;
}

void AiAssistantDock::setAllOperationsChecked(bool checked)
{
    m_updatingDiffChecks = true;
    const Qt::CheckState st = checked ? Qt::Checked : Qt::Unchecked;
    for (int i = 0; i < m_diffTree->topLevelItemCount(); ++i) {
        m_diffTree->topLevelItem(i)->setCheckState(kColOpType, st);
    }
    m_updatingDiffChecks = false;
    updateActionButtonStates();
    Q_EMIT selectedOperationIdsChanged(selectedOperationIds());
}

void AiAssistantDock::uncheckLowConfidenceOperations()
{
    m_updatingDiffChecks = true;
    for (int i = 0; i < m_diffTree->topLevelItemCount(); ++i) {
        QTreeWidgetItem *item = m_diffTree->topLevelItem(i);
        if (item->data(kColStatus, kLowConfidenceRole).toBool()) {
            item->setCheckState(kColOpType, Qt::Unchecked);
        }
    }
    m_updatingDiffChecks = false;
    updateActionButtonStates();
    Q_EMIT selectedOperationIdsChanged(selectedOperationIds());
}

bool AiAssistantDock::useProjectBinMedia() const
{
    return m_useBinMediaButton != nullptr && m_useBinMediaButton->isChecked();
}

void AiAssistantDock::setUseProjectBinMedia(bool useBin)
{
    if (m_useBinMediaButton != nullptr) {
        m_useBinMediaButton->setChecked(useBin);
    }
}

void AiAssistantDock::setStorageSummaryText(const QString &summary)
{
    if (m_storageSummaryLabel != nullptr) {
        m_storageSummaryLabel->setText(summary);
    }
}

void AiAssistantDock::handleDiffItemChanged(QTreeWidgetItem *item, int column)
{
    if (m_updatingDiffChecks || item == nullptr || item->parent() != nullptr || column != kColOpType) {
        return;
    }
    updateActionButtonStates();
    Q_EMIT selectedOperationIdsChanged(selectedOperationIds());
}

void AiAssistantDock::handleProfileSelectionChanged(int index)
{
    if (index < 0) {
        return;
    }
    const QString dataId = m_profileCombo->itemData(index).toString();
    if (dataId != QStringLiteral("custom")) {
        m_customProfilePath.clear();
        m_customProfileId.clear();
        setReviewConfidenceThreshold(0.72);
    }
    const QString activeId = selectedProfileId();
    appendActivityLog(QStringLiteral("Profile"), tr("Selected Show Profile: %1").arg(activeId), QStringLiteral("INFO"));
    Q_EMIT showProfileChanged(activeId, m_customProfilePath);
}

void AiAssistantDock::handleLoadCustomProfileClicked()
{
    const QString filePath = QFileDialog::getOpenFileName(this, tr("Open Show Profile JSON"), QString(), tr("Show Profile JSON (*.json);;All Files (*)"));
    if (filePath.isEmpty()) {
        return;
    }
    loadCustomProfileFromFile(filePath);
}

void AiAssistantDock::handleGenerateClicked()
{
    const QString prompt = instructionText();
    if (prompt.isEmpty()) {
        appendActivityLog(QStringLiteral("UI"), tr("Enter an instruction or click a quick-action chip first."), QStringLiteral("WARN"));
        m_instructionInput->setFocus();
        return;
    }
    const QString profileId = selectedProfileId();
    appendActivityLog(QStringLiteral("UI"), tr("Requesting proposal for profile '%1'…").arg(profileId), QStringLiteral("INFO"));
    Q_EMIT proposalRequested(prompt, profileId, m_customProfilePath);
}

void AiAssistantDock::handleAcceptClicked()
{
    if (!m_hasPendingProposal) {
        return;
    }
    const QStringList checkedIds = selectedOperationIds();
    TimelineCommandBatch filteredBatch = m_currentBatch;
    if (!checkedIds.isEmpty() && checkedIds.size() < m_currentBatch.operations.size()) {
        QVector<TimelineOperation> kept;
        for (const TimelineOperation &op : std::as_const(m_currentBatch.operations)) {
            if (checkedIds.contains(op.opId)) {
                kept.append(op);
            }
        }
        filteredBatch.operations = kept;
    } else if (checkedIds.isEmpty()) {
        appendActivityLog(QStringLiteral("Approval"), tr("No operations selected; check at least one change to apply."), QStringLiteral("WARN"));
        return;
    }

    appendActivityLog(QStringLiteral("Approval"),
                      tr("User approved batch '%1' (%2/%3 selected operations).")
                          .arg(filteredBatch.requestId)
                          .arg(filteredBatch.operations.size())
                          .arg(m_currentBatch.operations.size()),
                      QStringLiteral("INFO"));
    Q_EMIT batchApproveRequested(filteredBatch);
}

void AiAssistantDock::handleRejectClicked()
{
    if (!m_hasPendingProposal) {
        return;
    }
    const QString reqId = m_currentBatch.requestId;
    appendActivityLog(QStringLiteral("Approval"), tr("User rejected batch '%1'.").arg(reqId), QStringLiteral("INFO"));
    clearProposal();
    Q_EMIT batchRejectRequested(reqId);
}

void AiAssistantDock::handleCompareToggled(bool checked)
{
    m_compareButton->setText(checked ? tr("Compare: Preview (B)") : tr("Compare: Original (A)"));
    appendActivityLog(QStringLiteral("Compare"),
                      checked ? tr("Switched monitor preview to AI Proposal (B).") : tr("Switched monitor preview back to Original (A)."),
                      QStringLiteral("INFO"));
    Q_EMIT compareModeToggled(checked);
}

void AiAssistantDock::handleRefineClicked()
{
    if (!m_hasPendingProposal) {
        return;
    }
    const QString feedback = instructionText();
    const QString profileId = selectedProfileId();
    appendActivityLog(QStringLiteral("Refine"), tr("Requesting refinement for batch '%1'.").arg(m_currentBatch.requestId), QStringLiteral("INFO"));
    Q_EMIT refineRequested(m_currentBatch.requestId, feedback, profileId);
}

void AiAssistantDock::handleUndoClicked()
{
    if (!m_canUndo) {
        return;
    }
    appendActivityLog(QStringLiteral("Undo"), tr("Requested atomic undo of last AI edit batch."), QStringLiteral("INFO"));
    Q_EMIT undoRequested();
}

void AiAssistantDock::handleCancelClicked()
{
    if (!m_isBusy) {
        return;
    }
    appendActivityLog(QStringLiteral("Task"), tr("Cancellation requested by user."), QStringLiteral("WARN"));
    Q_EMIT taskCancelRequested();
}

void AiAssistantDock::handleActivityToggle(bool expanded)
{
    m_activityContainer->setVisible(expanded);
    m_activityToggleButton->setArrowType(expanded ? Qt::DownArrow : Qt::RightArrow);
}

void AiAssistantDock::updateActionButtonStates()
{
    const bool allowActions = !m_isBusy;
    const int checkedCount = m_hasPendingProposal ? selectedOperationIds().size() : 0;
    m_generateButton->setEnabled(allowActions);
    m_acceptButton->setEnabled(allowActions && m_hasPendingProposal && checkedCount > 0);
    m_rejectButton->setEnabled(allowActions && m_hasPendingProposal);
    m_refineButton->setEnabled(allowActions && m_hasPendingProposal);
    m_compareButton->setEnabled(allowActions && (m_hasPendingProposal || m_hasAppliedProposal));
    m_undoButton->setEnabled(allowActions && m_canUndo);
    if (m_selectAllOpsButton) {
        m_selectAllOpsButton->setEnabled(allowActions && m_hasPendingProposal);
    }
    if (m_uncheckLowConfButton) {
        m_uncheckLowConfButton->setEnabled(allowActions && m_hasPendingProposal && m_lowConfidenceCount > 0);
    }
}

void AiAssistantDock::setStatusBadge(const QString &text, const QString &stateKey)
{
    m_statusBadgeLabel->setText(text);
    m_statusBadgeLabel->setProperty("statusState", stateKey);
    m_statusBadgeLabel->style()->unpolish(m_statusBadgeLabel);
    m_statusBadgeLabel->style()->polish(m_statusBadgeLabel);
}

QString AiAssistantDock::formatFrameRange(const FrameRange &range)
{
    if (range.inFrame == 0 && range.outFrame == 0 && range.timelineStart == 0) {
        return QStringLiteral("—");
    }
    const QString inTc = framesToTimecode(range.inFrame, range.fpsNum, range.fpsDen);
    const QString outTc = framesToTimecode(range.outFrame, range.fpsNum, range.fpsDen);
    return QStringLiteral("%1–%2 (@%3f)").arg(inTc, outTc).arg(range.timelineStart);
}

QString AiAssistantDock::formatOperationTarget(const TimelineOperation &op)
{
    QStringList parts;
    if (!op.trackId.isEmpty()) {
        parts.append(op.trackId);
    }
    if (!op.clipId.isEmpty()) {
        parts.append(op.clipId);
    } else if (!op.mediaRef.isEmpty()) {
        parts.append(QFileInfo(op.mediaRef).fileName());
    }
    return parts.isEmpty() ? QStringLiteral("sequence") : parts.join(QStringLiteral(" / "));
}

QString AiAssistantDock::formatJsonValueSummary(const QJsonValue &value)
{
    if (value.isString()) {
        return value.toString();
    }
    if (value.isBool()) {
        return value.toBool() ? QStringLiteral("true") : QStringLiteral("false");
    }
    if (value.isDouble()) {
        return QString::number(value.toDouble());
    }
    if (value.isArray()) {
        return QStringLiteral("[%1 items]").arg(value.toArray().size());
    }
    if (value.isObject()) {
        return QStringLiteral("{%1 keys}").arg(value.toObject().size());
    }
    return QStringLiteral("null");
}

} // namespace BangOnPro
