/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "PremierePresetBrowser.h"

#include <QCheckBox>
#include <QColor>
#include <QComboBox>
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QFontDatabase>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QInputDialog>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QMimeData>
#include <QPixmap>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QSplitter>
#include <QTabWidget>
#include <QTextBrowser>
#include <QTimer>
#include <QTreeWidget>
#include <QVBoxLayout>
#include <algorithm>
#include <functional>

namespace BangOnPro {

namespace {

constexpr int kPresetIdRole = Qt::UserRole + 1;

QString htmlEscape(const QString &input)
{
    return input.toHtmlEscaped();
}

} // namespace

/**
 * @brief Internal QTreeWidget supporting drag-and-drop of Bang On Pro presets.
 */
class PresetTreeWidget : public QTreeWidget
{
public:
    using PayloadProvider = std::function<QByteArray(const QString &)>;
    using EffectIdProvider = std::function<QString(const QString &)>;

    explicit PresetTreeWidget(PayloadProvider provider, EffectIdProvider effectIdProvider, QWidget *parent = nullptr)
        : QTreeWidget(parent)
        , m_payloadProvider(std::move(provider))
        , m_effectIdProvider(std::move(effectIdProvider))
    {
        setDragEnabled(true);
        setDragDropMode(QAbstractItemView::DragOnly);
        setSelectionMode(QAbstractItemView::ExtendedSelection);
        setAlternatingRowColors(true);
        setRootIsDecorated(false);
    }

protected:
    QStringList mimeTypes() const override { return {QString::fromLatin1(PremierePresetBrowser::kPresetMimeType), QStringLiteral("kdenlive/effect")}; }

#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
    QMimeData *mimeData(const QList<QTreeWidgetItem *> &items) const override
#else
    // Qt5 QTreeWidget::mimeData takes QList<QTreeWidgetItem *> by value (ABI virtual override).
    // cppcheck-suppress passedByValue
    QMimeData *mimeData(const QList<QTreeWidgetItem *> items) const override
#endif
    {
        if (items.isEmpty() || !m_payloadProvider) {
            return nullptr;
        }
        const QString presetId = items.first()->data(0, kPresetIdRole).toString();
        if (presetId.isEmpty()) {
            return nullptr;
        }
        auto *mime = new QMimeData();
        mime->setData(QString::fromLatin1(PremierePresetBrowser::kPresetMimeType), m_payloadProvider(presetId));
        if (m_effectIdProvider) {
            const QString nativeEffectId = m_effectIdProvider(presetId);
            if (!nativeEffectId.isEmpty()) {
                mime->setData(QStringLiteral("kdenlive/effect"), nativeEffectId.toUtf8());
            }
        }
        mime->setText(presetId);
        return mime;
    }

private:
    PayloadProvider m_payloadProvider;
    EffectIdProvider m_effectIdProvider;
};

PremierePresetBrowser::PremierePresetBrowser(QWidget *parent, bool executableOnlyDefault)
    : QWidget(parent)
    , m_executableOnlyDefault(executableOnlyDefault)
{
    setObjectName(QStringLiteral("BangOnProPremierePresetBrowser"));
    setWindowTitle(QStringLiteral("Bang On Pro — Premiere Presets & Effects"));
    buildUi();
    updateSummaryBanner();
    updateInspectorForPreset(nullptr);
}

void PremierePresetBrowser::buildUi()
{
    auto *rootLayout = new QVBoxLayout(this);
    rootLayout->setContentsMargins(6, 6, 6, 6);
    rootLayout->setSpacing(6);

    // Top banner & Import action
    auto *headerLayout = new QHBoxLayout();
    m_summaryLabel = new QLabel(this);
    m_summaryLabel->setWordWrap(true);
    m_summaryLabel->setTextFormat(Qt::RichText);

    m_importBtn = new QPushButton(QStringLiteral("Import Archive..."), this);
    m_importBtn->setToolTip(QStringLiteral("Import Adobe Premiere Pro .prfpset or .zip preset pack"));
    headerLayout->addWidget(m_summaryLabel, 1);
    headerLayout->addWidget(m_importBtn, 0);
    rootLayout->addLayout(headerLayout);

    // Search & filter toolbar (two rows so search never squashes at narrow dock widths)
    m_searchEdit = new QLineEdit(this);
    m_searchEdit->setPlaceholderText(QStringLiteral("Search presets, packs, effects…"));
    m_searchEdit->setClearButtonEnabled(true);
    rootLayout->addWidget(m_searchEdit);

    auto *filterRow = new QHBoxLayout();
    filterRow->setSpacing(4);
    m_packFilterCombo = new QComboBox(this);
    m_packFilterCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    m_packFilterCombo->setMinimumContentsLength(8);
    m_packFilterCombo->addItem(QStringLiteral("All Packs / Folders"), QString());

    m_statusFilterCombo = new QComboBox(this);
    m_statusFilterCombo->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    m_statusFilterCombo->setMinimumContentsLength(8);
    m_statusFilterCombo->addItem(QStringLiteral("All Statuses"), QStringLiteral("all"));
    m_statusFilterCombo->addItem(QStringLiteral("Executable Only"), QStringLiteral("executable"));
    m_statusFilterCombo->addItem(QStringLiteral("Exact Validated"), QStringLiteral("exact_validated"));
    m_statusFilterCombo->addItem(QStringLiteral("Approximated"), QStringLiteral("approximated"));
    m_statusFilterCombo->addItem(QStringLiteral("Missing Dependency"), QStringLiteral("missing_dependency"));
    m_statusFilterCombo->addItem(QStringLiteral("Unsupported"), QStringLiteral("unsupported"));
    if (m_executableOnlyDefault) {
        const int execIdx = m_statusFilterCombo->findData(QStringLiteral("executable"));
        if (execIdx >= 0) {
            m_statusFilterCombo->setCurrentIndex(execIdx);
        }
    }

    m_favoritesOnlyCheck = new QCheckBox(QStringLiteral("Favourites"), this);
    m_favoritesOnlyCheck->setToolTip(QStringLiteral("Show favourite presets only"));

    filterRow->addWidget(m_packFilterCombo, 1);
    filterRow->addWidget(m_statusFilterCombo, 1);
    filterRow->addWidget(m_favoritesOnlyCheck, 0);
    rootLayout->addLayout(filterRow);

    // Main splitter: preset table (top) + inspector & custom stacks (bottom)
    auto *splitter = new QSplitter(Qt::Vertical, this);

    m_presetTree = new PresetTreeWidget([this](const QString &presetId) { return buildMimePayloadForPreset(presetId); },
                                        [this](const QString &presetId) { return resolveDragEffectId(presetId); }, splitter);
    m_presetTree->setColumnCount(5);
    m_presetTree->setHeaderLabels(
        {QStringLiteral("Preset"), QStringLiteral("Pack"), QStringLiteral("Folder"), QStringLiteral("Status"), QStringLiteral("Fav")});
    m_presetTree->header()->setSectionResizeMode(0, QHeaderView::Stretch);
    m_presetTree->header()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
    m_presetTree->header()->setSectionResizeMode(2, QHeaderView::Interactive);
    m_presetTree->header()->setSectionResizeMode(3, QHeaderView::ResizeToContents);
    m_presetTree->header()->setSectionResizeMode(4, QHeaderView::ResizeToContents);

    // Action row between list and inspector
    auto *bottomPane = new QWidget(splitter);
    auto *bottomLayout = new QVBoxLayout(bottomPane);
    bottomLayout->setContentsMargins(0, 0, 0, 0);
    bottomLayout->setSpacing(6);

    auto *actionRow = new QHBoxLayout();
    actionRow->setSpacing(4);
    m_applyBtn = new QPushButton(QStringLiteral("Apply"), bottomPane);
    m_applyBtn->setToolTip(QStringLiteral("Apply selected preset to the active clip"));
    m_applyBtn->setEnabled(false);
    m_favoriteBtn = new QPushButton(QStringLiteral("Favourite"), bottomPane);
    m_favoriteBtn->setToolTip(QStringLiteral("Toggle favourite status for this preset"));
    m_favoriteBtn->setEnabled(false);
    m_saveStackBtn = new QPushButton(QStringLiteral("Save Stack…"), bottomPane);
    m_saveStackBtn->setToolTip(QStringLiteral("Save to Custom Stack…"));
    m_saveStackBtn->setEnabled(false);
    m_showTechnicalInspectorCheck = new QCheckBox(QStringLiteral("Show technical inspector"), bottomPane);
    m_showTechnicalInspectorCheck->setObjectName(QStringLiteral("presetShowTechnicalInspectorCheck"));
    m_showTechnicalInspectorCheck->setChecked(false);
    m_showTechnicalInspectorCheck->setToolTip(QStringLiteral("Show raw MLT JSON parameters, Kdenlive Effect XML, and Raw Premiere XML inspector tabs"));

    actionRow->addWidget(m_applyBtn);
    actionRow->addWidget(m_favoriteBtn);
    actionRow->addWidget(m_saveStackBtn);
    actionRow->addStretch(1);
    actionRow->addWidget(m_showTechnicalInspectorCheck);
    bottomLayout->addLayout(actionRow);

    // Inspector tabs
    m_inspectorTabs = new QTabWidget(bottomPane);

    auto *overviewTab = new QWidget(m_inspectorTabs);
    auto *overviewLayout = new QVBoxLayout(overviewTab);
    overviewLayout->setContentsMargins(2, 2, 2, 2);
    overviewLayout->setSpacing(4);
    m_previewImageLabel = new QLabel(QStringLiteral("Before / After Preview — Select a preset"), overviewTab);
    m_previewImageLabel->setObjectName(QStringLiteral("presetPreviewLabel"));
    m_previewImageLabel->setAlignment(Qt::AlignCenter);
    m_previewImageLabel->setMinimumHeight(88);
    m_previewImageLabel->setMaximumHeight(140);
    m_overviewBrowser = new QTextBrowser(overviewTab);
    m_overviewBrowser->setOpenExternalLinks(false);
    overviewLayout->addWidget(m_previewImageLabel);
    overviewLayout->addWidget(m_overviewBrowser, 1);
    m_inspectorTabs->addTab(overviewTab, QStringLiteral("Overview && Preview"));

    const QFont monoFont = QFontDatabase::systemFont(QFontDatabase::FixedFont);

    m_paramsTabWidget = new QWidget(m_inspectorTabs);
    auto *paramsLayout = new QVBoxLayout(m_paramsTabWidget);
    paramsLayout->setContentsMargins(2, 2, 2, 2);
    paramsLayout->setSpacing(4);
    m_paramEditorEdit = new QPlainTextEdit(m_paramsTabWidget);
    m_paramEditorEdit->setObjectName(QStringLiteral("presetParamEditor"));
    m_paramEditorEdit->setFont(monoFont);
    m_paramEditorEdit->setPlaceholderText(QStringLiteral("Edit MLT filter parameters & keyframe animation JSON before applying..."));
    m_applyEditedParamsBtn = new QPushButton(QStringLiteral("Apply Edited Parameters / Keyframes"), m_paramsTabWidget);
    m_applyEditedParamsBtn->setEnabled(false);
    paramsLayout->addWidget(m_paramEditorEdit, 1);
    paramsLayout->addWidget(m_applyEditedParamsBtn);
    m_inspectorTabs->addTab(m_paramsTabWidget, QStringLiteral("Parameters && Keyframes"));

    m_kdenliveXmlEdit = new QPlainTextEdit(m_inspectorTabs);
    m_kdenliveXmlEdit->setReadOnly(true);
    m_kdenliveXmlEdit->setFont(monoFont);
    m_inspectorTabs->addTab(m_kdenliveXmlEdit, QStringLiteral("Kdenlive Effect XML"));

    m_rawXmlEdit = new QPlainTextEdit(m_inspectorTabs);
    m_rawXmlEdit->setReadOnly(true);
    m_rawXmlEdit->setFont(monoFont);
    m_inspectorTabs->addTab(m_rawXmlEdit, QStringLiteral("Raw Premiere XML Inspector"));

    // Saved custom stacks tab
    auto *stacksTab = new QWidget(m_inspectorTabs);
    auto *stacksLayout = new QVBoxLayout(stacksTab);
    m_customStacksList = new QListWidget(stacksTab);
    auto *stackButtonsRow = new QHBoxLayout();
    m_applyStackBtn = new QPushButton(QStringLiteral("Apply Stack"), stacksTab);
    m_removeStackBtn = new QPushButton(QStringLiteral("Delete Stack"), stacksTab);
    stackButtonsRow->addWidget(m_applyStackBtn);
    stackButtonsRow->addWidget(m_removeStackBtn);
    stackButtonsRow->addStretch(1);
    stacksLayout->addWidget(m_customStacksList, 1);
    stacksLayout->addLayout(stackButtonsRow);
    m_inspectorTabs->addTab(stacksTab, QStringLiteral("Saved Custom Stacks"));

    updateTechnicalInspectorVisibility();

    bottomLayout->addWidget(m_inspectorTabs, 1);

    splitter->addWidget(m_presetTree);
    splitter->addWidget(bottomPane);
    splitter->setStretchFactor(0, 3);
    splitter->setStretchFactor(1, 2);
    rootLayout->addWidget(splitter, 1);

    // Debounced search timer (120ms) so typing never stutters
    m_searchDebounceTimer = new QTimer(this);
    m_searchDebounceTimer->setSingleShot(true);
    m_searchDebounceTimer->setInterval(120);
    connect(m_searchDebounceTimer, &QTimer::timeout, this, &PremierePresetBrowser::refreshView);

    // Signal connections (dual Qt5/Qt6 safe)
    connect(m_searchEdit, &QLineEdit::textChanged, this, [this](const QString &) {
        if (m_searchDebounceTimer) {
            m_searchDebounceTimer->start();
        }
    });
    connect(m_packFilterCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, &PremierePresetBrowser::onSearchOrFilterChanged);
    connect(m_statusFilterCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, &PremierePresetBrowser::onSearchOrFilterChanged);
    connect(m_favoritesOnlyCheck, &QCheckBox::toggled, this, &PremierePresetBrowser::onSearchOrFilterChanged);
    connect(m_showTechnicalInspectorCheck, &QCheckBox::toggled, this, &PremierePresetBrowser::onShowTechnicalInspectorToggled);
    connect(m_presetTree, &QTreeWidget::itemSelectionChanged, this, &PremierePresetBrowser::onPresetSelectionChanged);
    connect(m_presetTree, &QTreeWidget::itemDoubleClicked, this, [this](QTreeWidgetItem *, int) { onApplyClicked(); });
    connect(m_importBtn, &QPushButton::clicked, this, &PremierePresetBrowser::onImportArchiveClicked);
    connect(m_applyBtn, &QPushButton::clicked, this, &PremierePresetBrowser::onApplyClicked);
    connect(m_favoriteBtn, &QPushButton::clicked, this, &PremierePresetBrowser::onToggleFavoriteClicked);
    connect(m_saveStackBtn, &QPushButton::clicked, this, &PremierePresetBrowser::onSaveCustomStackClicked);
    connect(m_applyStackBtn, &QPushButton::clicked, this, &PremierePresetBrowser::onApplyCustomStackClicked);
    connect(m_removeStackBtn, &QPushButton::clicked, this, &PremierePresetBrowser::onRemoveCustomStackClicked);
    connect(m_applyEditedParamsBtn, &QPushButton::clicked, this, &PremierePresetBrowser::onApplyEditedParamsClicked);
}

bool PremierePresetBrowser::loadReportFile(const QString &jsonPath, QString *errorOut)
{
    QFile file(jsonPath);
    if (!file.open(QIODevice::ReadOnly)) {
        if (errorOut) {
            *errorOut = QStringLiteral("Unable to open compatibility report file: %1").arg(jsonPath);
        }
        return false;
    }

    QJsonParseError parseErr{};
    const QJsonDocument doc = QJsonDocument::fromJson(file.readAll(), &parseErr);
    if (parseErr.error != QJsonParseError::NoError || !doc.isObject()) {
        if (errorOut) {
            *errorOut = QStringLiteral("Invalid JSON in compatibility report: %1").arg(parseErr.errorString());
        }
        return false;
    }
    return loadReportJson(doc.object(), errorOut);
}

bool PremierePresetBrowser::loadReportJson(const QJsonObject &reportObj, QString *errorOut)
{
    const QString schemaVer = reportObj.value(QStringLiteral("schema_version")).toString();
    if (schemaVer != QString::fromLatin1(kPremiereReportSchemaVersion)) {
        if (errorOut) {
            *errorOut = QStringLiteral("Unsupported premiere preset report schema_version: %1").arg(schemaVer);
        }
        return false;
    }

    if (!reportObj.value(QStringLiteral("presets")).isArray()) {
        if (errorOut) {
            *errorOut = QStringLiteral("Compatibility report missing 'presets' array.");
        }
        return false;
    }

    const QJsonObject srcObj = reportObj.value(QStringLiteral("source_archive")).toObject();
    m_archiveName = srcObj.value(QStringLiteral("name")).toString();
    m_archiveSha256 = srcObj.value(QStringLiteral("sha256")).toString();
    m_archiveFileCount = srcObj.value(QStringLiteral("file_count")).toInt(0);

    m_entries.clear();
    m_indexById.clear();
    m_rawJsonById.clear();
    m_previewPixmapCache.clear();

    const QJsonArray presetsArr = reportObj.value(QStringLiteral("presets")).toArray();
    m_entries.reserve(presetsArr.size());

    for (const QJsonValue &val : presetsArr) {
        if (!val.isObject()) {
            continue;
        }
        const QJsonObject obj = val.toObject();
        PresetDisplayEntry entry;
        entry.record.presetId = obj.value(QStringLiteral("preset_id")).toString();
        entry.record.name = obj.value(QStringLiteral("name")).toString();
        entry.record.pack = obj.value(QStringLiteral("pack")).toString();
        entry.record.attribution = obj.value(QStringLiteral("attribution")).toString();
        entry.record.sourceFile = obj.value(QStringLiteral("source_file")).toString();
        entry.record.presetType = obj.value(QStringLiteral("preset_type")).toString(QStringLiteral("scale"));
        entry.record.status = compatibilityStatusFromString(obj.value(QStringLiteral("compatibility_status")).toString());
        entry.record.effectChain = obj.value(QStringLiteral("effect_chain")).toArray();
        entry.record.missingDependencies = obj.value(QStringLiteral("missing_dependencies")).toArray();
        entry.record.mltFilters = obj.value(QStringLiteral("mlt_filters")).toArray();
        entry.record.kdenliveEffectXml = obj.value(QStringLiteral("kdenlive_effect_xml")).toString();
        entry.record.originalXmlPreserved = obj.value(QStringLiteral("original_xml_preserved")).toBool(true);
        entry.originalXml = obj.value(QStringLiteral("original_xml")).toString();
        entry.description = obj.value(QStringLiteral("description")).toString();
        entry.previewBeforePath = obj.value(QStringLiteral("preview_before_path")).toString();
        entry.previewAfterPath = obj.value(QStringLiteral("preview_after_path")).toString();
        entry.previewSplitPath = obj.value(QStringLiteral("preview_split_path")).toString();
        const QJsonValue evVal = obj.value(QStringLiteral("visual_equivalence_evidence"));
        if (evVal.isString()) {
            entry.visualEquivalenceEvidence = evVal.toString();
        } else if (evVal.isObject()) {
            const QJsonObject evObj = evVal.toObject();
            entry.visualEquivalenceEvidence = evObj.value(QStringLiteral("classification")).toString(evObj.value(QStringLiteral("status")).toString());
        }

        const QJsonArray folderArr = obj.value(QStringLiteral("folder_path")).toArray();
        for (const QJsonValue &fv : folderArr) {
            entry.record.folderPath.append(fv.toString());
        }

        const QJsonArray notesArr = obj.value(QStringLiteral("notes")).toArray();
        for (const QJsonValue &nv : notesArr) {
            entry.record.notes.append(nv.toString());
        }

        if (!entry.record.presetId.isEmpty()) {
            m_indexById.insert(entry.record.presetId, m_entries.size());
            m_rawJsonById.insert(entry.record.presetId, obj);
            m_entries.append(entry);
        }
    }

    populatePackFilter();
    updateSummaryBanner();
    refreshView();
    return true;
}

int PremierePresetBrowser::presetCount() const
{
    return m_entries.size();
}

int PremierePresetBrowser::visiblePresetCount() const
{
    return m_presetTree ? m_presetTree->topLevelItemCount() : 0;
}

QVector<PresetDisplayEntry> PremierePresetBrowser::presets() const
{
    return m_entries;
}

bool PremierePresetBrowser::findPresetById(const QString &presetId, PresetDisplayEntry *outEntry) const
{
    const auto it = m_indexById.constFind(presetId);
    if (it == m_indexById.constEnd()) {
        return false;
    }
    if (outEntry) {
        *outEntry = m_entries.at(it.value());
    }
    return true;
}

QJsonObject PremierePresetBrowser::presetJsonById(const QString &presetId) const
{
    return m_rawJsonById.value(presetId);
}

void PremierePresetBrowser::setExecutableOnlyFilter(bool enabled)
{
    if (!m_statusFilterCombo) {
        return;
    }
    const QString targetKey = enabled ? QStringLiteral("executable") : QStringLiteral("all");
    const int idx = m_statusFilterCombo->findData(targetKey);
    if (idx >= 0) {
        m_statusFilterCombo->setCurrentIndex(idx);
    }
}

bool PremierePresetBrowser::isExecutableOnlyFilter() const
{
    return m_statusFilterCombo && m_statusFilterCombo->currentData().toString() == QStringLiteral("executable");
}

void PremierePresetBrowser::setShowTechnicalInspector(bool show)
{
    if (m_showTechnicalInspectorCheck) {
        m_showTechnicalInspectorCheck->setChecked(show);
    }
    updateTechnicalInspectorVisibility();
}

bool PremierePresetBrowser::showTechnicalInspector() const
{
    return m_showTechnicalInspectorCheck && m_showTechnicalInspectorCheck->isChecked();
}

void PremierePresetBrowser::setDragEffectIdResolver(std::function<QString(const QString &presetId)> resolver)
{
    m_dragEffectIdResolver = std::move(resolver);
}

void PremierePresetBrowser::setFavorite(const QString &presetId, bool favorite)
{
    if (presetId.isEmpty()) {
        return;
    }
    if (favorite) {
        m_favorites.insert(presetId);
    } else {
        m_favorites.remove(presetId);
    }
    refreshView();
}

bool PremierePresetBrowser::isFavorite(const QString &presetId) const
{
    return m_favorites.contains(presetId);
}

QStringList PremierePresetBrowser::favoritePresetIds() const
{
    QStringList list = m_favorites.values();
    std::sort(list.begin(), list.end());
    return list;
}

bool PremierePresetBrowser::saveCustomStack(const QString &stackName, const QStringList &presetIds)
{
    const QString trimmed = stackName.trimmed();
    if (trimmed.isEmpty() || presetIds.isEmpty()) {
        return false;
    }

    QJsonArray combined;
    QStringList validIds;
    for (const QString &pid : presetIds) {
        PresetDisplayEntry entry;
        if (findPresetById(pid, &entry)) {
            validIds.append(pid);
            for (const QJsonValue &flt : entry.record.mltFilters) {
                combined.append(flt);
            }
        }
    }
    if (validIds.isEmpty()) {
        return false;
    }

    for (CustomEffectStack &existing : m_customStacks) {
        if (existing.name == trimmed) {
            existing.presetIds = validIds;
            existing.combinedMltFilters = combined;
            refreshCustomStacksList();
            return true;
        }
    }

    CustomEffectStack stack;
    stack.name = trimmed;
    stack.presetIds = validIds;
    stack.combinedMltFilters = combined;
    m_customStacks.append(stack);
    refreshCustomStacksList();
    return true;
}

bool PremierePresetBrowser::removeCustomStack(const QString &stackName)
{
    for (int i = 0; i < m_customStacks.size(); ++i) {
        if (m_customStacks.at(i).name == stackName) {
            m_customStacks.removeAt(i);
            refreshCustomStacksList();
            return true;
        }
    }
    return false;
}

QVector<CustomEffectStack> PremierePresetBrowser::customStacks() const
{
    return m_customStacks;
}

QByteArray PremierePresetBrowser::buildMimePayloadForPreset(const QString &presetId) const
{
    PresetDisplayEntry entry;
    if (!findPresetById(presetId, &entry)) {
        return {};
    }
    QJsonObject payload;
    payload.insert(QStringLiteral("preset_id"), entry.record.presetId);
    payload.insert(QStringLiteral("name"), entry.record.name);
    payload.insert(QStringLiteral("pack"), entry.record.pack);
    payload.insert(QStringLiteral("compatibility_status"), compatibilityStatusToString(entry.record.status));
    payload.insert(QStringLiteral("mlt_filters"), entry.record.mltFilters);
    payload.insert(QStringLiteral("kdenlive_effect_xml"), entry.record.kdenliveEffectXml);
    return QJsonDocument(payload).toJson(QJsonDocument::Compact);
}

QString PremierePresetBrowser::resolveDragEffectId(const QString &presetId) const
{
    if (presetId.isEmpty()) {
        return {};
    }
    // Notify any listener preparing drag materialization
    Q_EMIT const_cast<PremierePresetBrowser *>(this)->prepareDragEffectId(presetId);
    if (m_dragEffectIdResolver) {
        const QString resolved = m_dragEffectIdResolver(presetId);
        if (!resolved.isEmpty()) {
            return resolved;
        }
    }
    PresetDisplayEntry entry;
    if (findPresetById(presetId, &entry) && entry.record.mltFilters.size() == 1) {
        return entry.record.mltFilters.first().toObject().value(QStringLiteral("service")).toString();
    }
    return {};
}

void PremierePresetBrowser::clearFilters()
{
    m_searchEdit->clear();
    m_packFilterCombo->setCurrentIndex(0);
    m_statusFilterCombo->setCurrentIndex(0);
    m_favoritesOnlyCheck->setChecked(false);
    refreshView();
}

void PremierePresetBrowser::populatePackFilter()
{
    m_packFilterCombo->blockSignals(true);
    m_packFilterCombo->clear();
    m_packFilterCombo->addItem(QStringLiteral("All Packs / Folders"), QString());

    QSet<QString> packs;
    for (const PresetDisplayEntry &entry : m_entries) {
        if (!entry.record.pack.isEmpty()) {
            packs.insert(entry.record.pack);
        }
        if (!entry.record.folderPath.isEmpty()) {
            packs.insert(QStringLiteral("folder:") + entry.record.folderPath.join(QStringLiteral(" / ")));
        }
    }

    QStringList sorted = packs.values();
    std::sort(sorted.begin(), sorted.end());
    for (const QString &item : sorted) {
        if (item.startsWith(QStringLiteral("folder:"))) {
            const QString label = QStringLiteral("Folder: ") + item.mid(7);
            m_packFilterCombo->addItem(label, item);
        } else {
            const QString label = QStringLiteral("Pack: ") + item;
            const QString data = QStringLiteral("pack:") + item;
            m_packFilterCombo->addItem(label, data);
        }
    }
    m_packFilterCombo->blockSignals(false);
}

void PremierePresetBrowser::updateSummaryBanner()
{
    int exactCount = 0;
    int approxCount = 0;
    int missingCount = 0;
    int unsupportedCount = 0;

    for (const PresetDisplayEntry &e : m_entries) {
        switch (e.record.status) {
        case PresetCompatibilityStatus::ExactValidated:
            ++exactCount;
            break;
        case PresetCompatibilityStatus::Approximated:
            ++approxCount;
            break;
        case PresetCompatibilityStatus::MissingDependency:
            ++missingCount;
            break;
        case PresetCompatibilityStatus::Unsupported:
            ++unsupportedCount;
            break;
        }
    }

    const QString archiveLabel = m_archiveName.isEmpty() ? QStringLiteral("No archive loaded") : htmlEscape(m_archiveName);
    const QString shaShort = m_archiveSha256.isEmpty() ? QStringLiteral("-") : htmlEscape(m_archiveSha256.left(12));

    m_summaryLabel->setText(QStringLiteral("<b>%1</b> (SHA-256: <code>%2</code>) &mdash; "
                                           "Total: <b>%3</b> | "
                                           "<span style='color:#2E7D32'>Exact: %4</span> | "
                                           "<span style='color:#C77700'>Approx: %5</span> | "
                                           "<span style='color:#B45309'>Missing Dep: %6</span> | "
                                           "<span style='color:#B91C1C'>Unsupported: %7</span>")
                                .arg(archiveLabel, shaShort)
                                .arg(m_entries.size())
                                .arg(exactCount)
                                .arg(approxCount)
                                .arg(missingCount)
                                .arg(unsupportedCount));
}

bool PremierePresetBrowser::matchesActiveFilters(const PresetDisplayEntry &entry) const
{
    if (m_favoritesOnlyCheck->isChecked() && !m_favorites.contains(entry.record.presetId)) {
        return false;
    }

    const QString statusFilter = m_statusFilterCombo->currentData().toString();
    if (!statusFilter.isEmpty() && statusFilter != QStringLiteral("all")) {
        if (statusFilter == QStringLiteral("executable")) {
            const bool isExecutable =
                (entry.record.status == PresetCompatibilityStatus::ExactValidated || entry.record.status == PresetCompatibilityStatus::Approximated) &&
                !entry.record.mltFilters.isEmpty();
            if (!isExecutable) {
                return false;
            }
        } else if (compatibilityStatusToString(entry.record.status) != statusFilter) {
            return false;
        }
    }

    const QString packFilter = m_packFilterCombo->currentData().toString();
    if (!packFilter.isEmpty()) {
        if (packFilter.startsWith(QStringLiteral("pack:"))) {
            if (entry.record.pack != packFilter.mid(5)) {
                return false;
            }
        } else if (packFilter.startsWith(QStringLiteral("folder:"))) {
            if (entry.record.folderPath.join(QStringLiteral(" / ")) != packFilter.mid(7)) {
                return false;
            }
        }
    }

    const QString query = m_searchEdit->text().trimmed();
    if (!query.isEmpty()) {
        const auto containsQuery = [&query](const QString &haystack) { return haystack.contains(query, Qt::CaseInsensitive); };
        bool matched = containsQuery(entry.record.name) || containsQuery(entry.record.pack) || containsQuery(entry.record.attribution) ||
                       containsQuery(entry.record.folderPath.join(QStringLiteral(" "))) || containsQuery(entry.record.notes.join(QStringLiteral(" "))) ||
                       containsQuery(entry.description);
        if (!matched) {
            for (const QJsonValue &ev : entry.record.effectChain) {
                const QJsonObject eo = ev.toObject();
                if (containsQuery(eo.value(QStringLiteral("match_name")).toString()) || containsQuery(eo.value(QStringLiteral("display_name")).toString())) {
                    matched = true;
                    break;
                }
            }
        }
        if (!matched) {
            return false;
        }
    }

    return true;
}

void PremierePresetBrowser::refreshView()
{
    const QString previouslySelected = selectedPresetId();
    m_presetTree->blockSignals(true);
    m_presetTree->clear();

    QTreeWidgetItem *toSelect = nullptr;
    for (const PresetDisplayEntry &entry : m_entries) {
        if (!matchesActiveFilters(entry)) {
            continue;
        }
        auto *item = new QTreeWidgetItem(m_presetTree);
        item->setText(0, entry.record.name);
        item->setData(0, kPresetIdRole, entry.record.presetId);
        item->setText(1, entry.record.pack);
        item->setText(2, entry.record.folderPath.isEmpty() ? QStringLiteral("(root)") : entry.record.folderPath.join(QStringLiteral(" / ")));
        item->setText(3, statusDisplayLabel(entry.record.status));
        item->setForeground(3, QBrush(QColor(statusBadgeColorHex(entry.record.status))));
        item->setText(4, m_favorites.contains(entry.record.presetId) ? QStringLiteral("★") : QString());
        if (!entry.record.notes.isEmpty()) {
            item->setToolTip(3, entry.record.notes.join(QStringLiteral("\n")));
        }

        if (entry.record.presetId == previouslySelected) {
            toSelect = item;
        }
    }

    if (toSelect) {
        m_presetTree->setCurrentItem(toSelect);
    }
    m_presetTree->blockSignals(false);
    onPresetSelectionChanged();
}

QString PremierePresetBrowser::selectedPresetId() const
{
    const QList<QTreeWidgetItem *> selected = m_presetTree ? m_presetTree->selectedItems() : QList<QTreeWidgetItem *>();
    if (selected.isEmpty()) {
        return {};
    }
    return selected.first()->data(0, kPresetIdRole).toString();
}

void PremierePresetBrowser::updateInspectorForPreset(const PresetDisplayEntry *entry)
{
    if (!entry) {
        m_overviewBrowser->setHtml(QStringLiteral("<p><i>Select a Premiere preset to inspect compatibility, missing dependencies, and XML.</i></p>"));
        m_kdenliveXmlEdit->setPlainText(QString());
        m_rawXmlEdit->setPlainText(QString());
        m_applyBtn->setEnabled(false);
        m_favoriteBtn->setEnabled(false);
        m_saveStackBtn->setEnabled(false);
        return;
    }

    const QString badgeColor = statusBadgeColorHex(entry->record.status);
    const QString badgeText = statusDisplayLabel(entry->record.status);
    const QString folderStr = entry->record.folderPath.isEmpty() ? QStringLiteral("(root)") : entry->record.folderPath.join(QStringLiteral(" / "));

    QString html;
    html +=
        QStringLiteral("<h3>%1 <span style='color:%2; font-size:90%;'>[%3]</span></h3>").arg(htmlEscape(entry->record.name), badgeColor, htmlEscape(badgeText));
    html += QStringLiteral("<p><b>Pack:</b> %1 &nbsp;|&nbsp; <b>Folder:</b> %2 &nbsp;|&nbsp; <b>Type:</b> %3</p>")
                .arg(htmlEscape(entry->record.pack), htmlEscape(folderStr), htmlEscape(entry->record.presetType));
    if (!entry->record.attribution.isEmpty()) {
        html += QStringLiteral("<p><b>Attribution:</b> %1</p>").arg(htmlEscape(entry->record.attribution));
    }
    if (!entry->description.isEmpty()) {
        html += QStringLiteral("<p><b>Description:</b> %1</p>").arg(htmlEscape(entry->description));
    }
    if (!entry->visualEquivalenceEvidence.isEmpty()) {
        html += QStringLiteral("<p><b>Visual Equivalence Evidence:</b> <code>%1</code></p>").arg(htmlEscape(entry->visualEquivalenceEvidence));
    }

    // Mapped MLT Filters
    html += QStringLiteral("<h4>Mapped MLT Filter Chain (%1)</h4>").arg(entry->record.mltFilters.size());
    if (entry->record.mltFilters.isEmpty()) {
        html += QStringLiteral("<p><i>No executable native MLT filters mapped. Original Premiere XML is preserved in the Raw XML tab.</i></p>");
    } else {
        html += QStringLiteral("<ul>");
        for (const QJsonValue &fv : entry->record.mltFilters) {
            const QJsonObject fo = fv.toObject();
            const QString svc = fo.value(QStringLiteral("service")).toString();
            const QString src = fo.value(QStringLiteral("source_effect")).toString();
            const QString mediaType = fo.value(QStringLiteral("media_type")).toString();
            html += QStringLiteral("<li><code>%1</code> (%2) &larr; <i>%3</i></li>").arg(htmlEscape(svc), htmlEscape(mediaType), htmlEscape(src));
        }
        html += QStringLiteral("</ul>");
    }

    // Missing Dependencies
    if (!entry->record.missingDependencies.isEmpty()) {
        html += QStringLiteral("<h4 style='color:#B45309'>Missing Dependencies / Platform Plugins (%1)</h4>").arg(entry->record.missingDependencies.size());
        html += QStringLiteral("<ul>");
        for (const QJsonValue &dv : entry->record.missingDependencies) {
            const QJsonObject d = dv.toObject();
            const QString kind = d.value(QStringLiteral("kind")).toString();
            const QString name = d.value(QStringLiteral("name")).toString();
            const QString platform = d.value(QStringLiteral("platform")).toString();
            const QString remediation = d.value(QStringLiteral("remediation")).toString();
            html += QStringLiteral("<li><b>[%1 / %2]</b> <code>%3</code>: %4</li>")
                        .arg(htmlEscape(kind), htmlEscape(platform), htmlEscape(name), htmlEscape(remediation));
        }
        html += QStringLiteral("</ul>");
    }

    // Caveats & Notes
    if (!entry->record.notes.isEmpty()) {
        html += QStringLiteral("<h4>Compatibility Notes &amp; Caveats</h4><ul>");
        for (const QString &note : entry->record.notes) {
            html += QStringLiteral("<li>%1</li>").arg(htmlEscape(note));
        }
        html += QStringLiteral("</ul>");
    }

    m_overviewBrowser->setHtml(html);

    // Visual Before / After preview thumbnail (cached scaled QPixmap so typing/selection never stutters)
    if (m_previewImageLabel) {
        const QString previewPath = !entry->previewSplitPath.isEmpty()
                                        ? entry->previewSplitPath
                                        : (!entry->previewAfterPath.isEmpty() ? entry->previewAfterPath : entry->previewBeforePath);
        if (!previewPath.isEmpty() && QFileInfo::exists(previewPath)) {
            QPixmap scaled;
            const auto cacheIt = m_previewPixmapCache.constFind(previewPath);
            if (cacheIt != m_previewPixmapCache.constEnd()) {
                scaled = cacheIt.value();
            } else {
                const QPixmap pix(previewPath);
                if (!pix.isNull()) {
                    scaled = pix.scaled(360, 120, Qt::KeepAspectRatio, Qt::SmoothTransformation);
                    m_previewPixmapCache.insert(previewPath, scaled);
                }
            }
            if (!scaled.isNull()) {
                m_previewImageLabel->setPixmap(scaled);
            } else {
                m_previewImageLabel->setPixmap(QPixmap());
                m_previewImageLabel->setText(QStringLiteral("Preview available at: %1").arg(previewPath));
            }
        } else {
            m_previewImageLabel->setPixmap(QPixmap());
            m_previewImageLabel->setText(QStringLiteral("%1 • %2 (%3 MLT filter(s))")
                                             .arg(entry->record.name, statusDisplayLabel(entry->record.status))
                                             .arg(entry->record.mltFilters.size()));
        }
    }

    if (m_paramEditorEdit) {
        m_paramEditorEdit->setPlainText(
            QString::fromUtf8(QJsonDocument(entry->record.mltFilters).toJson(QJsonDocument::Indented)));
    }
    if (m_applyEditedParamsBtn) {
        m_applyEditedParamsBtn->setEnabled(!entry->record.mltFilters.isEmpty());
    }

    if (entry->record.kdenliveEffectXml.isEmpty()) {
        m_kdenliveXmlEdit->setPlainText(
            QStringLiteral("<!-- No Kdenlive effect XML generated (status: %1). -->").arg(compatibilityStatusToString(entry->record.status)));
    } else {
        m_kdenliveXmlEdit->setPlainText(entry->record.kdenliveEffectXml);
    }

    if (entry->originalXml.isEmpty()) {
        m_rawXmlEdit->setPlainText(QStringLiteral("<!-- Original XML preserved flag: %1 -->")
                                       .arg(entry->record.originalXmlPreserved ? QStringLiteral("true") : QStringLiteral("false")));
    } else {
        m_rawXmlEdit->setPlainText(entry->originalXml);
    }

    m_applyBtn->setEnabled(!entry->record.mltFilters.isEmpty());
    m_favoriteBtn->setEnabled(true);
    m_favoriteBtn->setText(m_favorites.contains(entry->record.presetId) ? QStringLiteral("Remove Favourite") : QStringLiteral("Add Favourite"));
    m_saveStackBtn->setEnabled(!entry->record.mltFilters.isEmpty());
}

void PremierePresetBrowser::refreshCustomStacksList()
{
    m_customStacksList->clear();
    for (const CustomEffectStack &stack : m_customStacks) {
        const QString label =
            QStringLiteral("%1 (%2 presets, %3 MLT filters)").arg(stack.name).arg(stack.presetIds.size()).arg(stack.combinedMltFilters.size());
        auto *item = new QListWidgetItem(label, m_customStacksList);
        item->setData(Qt::UserRole, stack.name);
    }
}

bool PremierePresetBrowser::saveUserState(const QString &jsonPath) const
{
    QJsonObject root;
    QJsonArray favs;
    for (const QString &fid : favoritePresetIds()) {
        favs.append(fid);
    }
    root.insert(QStringLiteral("favorites"), favs);

    QJsonArray stacks;
    for (const CustomEffectStack &st : m_customStacks) {
        QJsonObject sObj;
        sObj.insert(QStringLiteral("name"), st.name);
        QJsonArray pids;
        for (const QString &pid : st.presetIds) {
            pids.append(pid);
        }
        sObj.insert(QStringLiteral("preset_ids"), pids);
        sObj.insert(QStringLiteral("combined_mlt_filters"), st.combinedMltFilters);
        stacks.append(sObj);
    }
    root.insert(QStringLiteral("custom_stacks"), stacks);

    QFile f(jsonPath);
    if (!f.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        return false;
    }
    f.write(QJsonDocument(root).toJson(QJsonDocument::Indented));
    return true;
}

bool PremierePresetBrowser::loadUserState(const QString &jsonPath)
{
    QFile f(jsonPath);
    if (!f.open(QIODevice::ReadOnly)) {
        return false;
    }
    QJsonParseError err{};
    const QJsonDocument doc = QJsonDocument::fromJson(f.readAll(), &err);
    if (err.error != QJsonParseError::NoError || !doc.isObject()) {
        return false;
    }
    const QJsonObject root = doc.object();
    m_favorites.clear();
    for (const QJsonValue &fv : root.value(QStringLiteral("favorites")).toArray()) {
        const QString fid = fv.toString().trimmed();
        if (!fid.isEmpty()) {
            m_favorites.insert(fid);
        }
    }
    m_customStacks.clear();
    for (const QJsonValue &sv : root.value(QStringLiteral("custom_stacks")).toArray()) {
        const QJsonObject so = sv.toObject();
        CustomEffectStack st;
        st.name = so.value(QStringLiteral("name")).toString().trimmed();
        for (const QJsonValue &pv : so.value(QStringLiteral("preset_ids")).toArray()) {
            st.presetIds.append(pv.toString());
        }
        st.combinedMltFilters = so.value(QStringLiteral("combined_mlt_filters")).toArray();
        if (!st.name.isEmpty()) {
            m_customStacks.append(st);
        }
    }
    refreshCustomStacksList();
    refreshView();
    return true;
}

void PremierePresetBrowser::onApplyEditedParamsClicked()
{
    const QString pid = selectedPresetId();
    if (pid.isEmpty() || !m_paramEditorEdit) {
        return;
    }
    QJsonParseError pe{};
    const QJsonDocument doc = QJsonDocument::fromJson(m_paramEditorEdit->toPlainText().toUtf8(), &pe);
    if (pe.error != QJsonParseError::NoError || !doc.isArray()) {
        return;
    }
    const QJsonArray editedFilters = doc.array();
    if (!editedFilters.isEmpty()) {
        Q_EMIT applyPresetRequested(pid, editedFilters);
    }
}

void PremierePresetBrowser::onShowTechnicalInspectorToggled(bool)
{
    updateTechnicalInspectorVisibility();
}

void PremierePresetBrowser::updateTechnicalInspectorVisibility()
{
    if (!m_inspectorTabs) {
        return;
    }
    const bool showTech = m_showTechnicalInspectorCheck && m_showTechnicalInspectorCheck->isChecked();
    const QList<QWidget *> technicalTabs = {m_paramsTabWidget, m_kdenliveXmlEdit, m_rawXmlEdit};
    for (QWidget *tab : technicalTabs) {
        if (!tab) {
            continue;
        }
        const int idx = m_inspectorTabs->indexOf(tab);
        if (idx >= 0) {
#if QT_VERSION >= QT_VERSION_CHECK(5, 15, 0)
            m_inspectorTabs->setTabVisible(idx, showTech);
#else
            m_inspectorTabs->setTabEnabled(idx, showTech);
#endif
        }
    }
    if (!showTech && m_inspectorTabs->currentIndex() > 0 && technicalTabs.contains(m_inspectorTabs->currentWidget())) {
        m_inspectorTabs->setCurrentIndex(0);
    }
}

void PremierePresetBrowser::onSearchOrFilterChanged()
{
    if (m_searchDebounceTimer) {
        m_searchDebounceTimer->stop();
    }
    refreshView();
}

void PremierePresetBrowser::onPresetSelectionChanged()
{
    const QString pid = selectedPresetId();
    if (pid.isEmpty()) {
        updateInspectorForPreset(nullptr);
        return;
    }
    PresetDisplayEntry entry;
    if (findPresetById(pid, &entry)) {
        updateInspectorForPreset(&entry);
        Q_EMIT presetSelected(pid);
    } else {
        updateInspectorForPreset(nullptr);
    }
}

void PremierePresetBrowser::onApplyClicked()
{
    const QString pid = selectedPresetId();
    PresetDisplayEntry entry;
    if (findPresetById(pid, &entry) && !entry.record.mltFilters.isEmpty()) {
        Q_EMIT applyPresetRequested(entry.record.presetId, entry.record.mltFilters);
    }
}

void PremierePresetBrowser::onImportArchiveClicked()
{
    const QString selected = QFileDialog::getOpenFileName(this, QStringLiteral("Import Adobe Premiere Pro Presets"), QString(),
                                                          QStringLiteral("Premiere Preset Archives (*.zip *.prfpset);;All Files (*)"));
    if (!selected.isEmpty()) {
        Q_EMIT importArchiveRequested(selected);
    }
}

void PremierePresetBrowser::onToggleFavoriteClicked()
{
    const QString pid = selectedPresetId();
    if (!pid.isEmpty()) {
        setFavorite(pid, !isFavorite(pid));
    }
}

void PremierePresetBrowser::onSaveCustomStackClicked()
{
    const QList<QTreeWidgetItem *> selected = m_presetTree->selectedItems();
    QStringList ids;
    for (QTreeWidgetItem *item : selected) {
        const QString pid = item->data(0, kPresetIdRole).toString();
        if (!pid.isEmpty()) {
            ids.append(pid);
        }
    }
    if (ids.isEmpty()) {
        return;
    }

    bool ok = false;
    const QString name = QInputDialog::getText(this, QStringLiteral("Save Custom Effect Stack"), QStringLiteral("Stack Name:"), QLineEdit::Normal,
                                               QStringLiteral("My Custom Stack"), &ok);
    if (ok && !name.trimmed().isEmpty()) {
        saveCustomStack(name, ids);
    }
}

void PremierePresetBrowser::onApplyCustomStackClicked()
{
    QListWidgetItem *item = m_customStacksList->currentItem();
    if (!item) {
        return;
    }
    const QString stackName = item->data(Qt::UserRole).toString();
    for (const CustomEffectStack &stack : m_customStacks) {
        if (stack.name == stackName) {
            Q_EMIT customStackApplied(stack.name, stack.combinedMltFilters);
            Q_EMIT applyPresetRequested(QStringLiteral("stack:") + stack.name, stack.combinedMltFilters);
            return;
        }
    }
}

void PremierePresetBrowser::onRemoveCustomStackClicked()
{
    QListWidgetItem *item = m_customStacksList->currentItem();
    if (!item) {
        return;
    }
    removeCustomStack(item->data(Qt::UserRole).toString());
}

QString PremierePresetBrowser::statusDisplayLabel(PresetCompatibilityStatus status)
{
    switch (status) {
    case PresetCompatibilityStatus::ExactValidated:
        return QStringLiteral("Exact Validated");
    case PresetCompatibilityStatus::Approximated:
        return QStringLiteral("Approximated");
    case PresetCompatibilityStatus::MissingDependency:
        return QStringLiteral("Missing Dependency");
    case PresetCompatibilityStatus::Unsupported:
        return QStringLiteral("Unsupported");
    }
    return QStringLiteral("Unsupported");
}

QString PremierePresetBrowser::statusBadgeColorHex(PresetCompatibilityStatus status)
{
    switch (status) {
    case PresetCompatibilityStatus::ExactValidated:
        return QStringLiteral("#2E7D32");
    case PresetCompatibilityStatus::Approximated:
        return QStringLiteral("#C77700");
    case PresetCompatibilityStatus::MissingDependency:
        return QStringLiteral("#B45309");
    case PresetCompatibilityStatus::Unsupported:
        return QStringLiteral("#B91C1C");
    }
    return QStringLiteral("#B91C1C");
}

} // namespace BangOnPro
