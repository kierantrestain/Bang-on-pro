/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "UnifiedEffectsPanel.h"

#include <QAction>
#include <QButtonGroup>
#include <QCheckBox>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonDocument>
#include <QJsonObject>
#include <QKeyEvent>
#include <QLabel>
#include <QLineEdit>
#include <QMenu>
#include <QMessageBox>
#include <QMimeData>
#include <QPushButton>
#include <QSettings>
#include <QTimer>
#include <QTreeWidget>
#include <QVBoxLayout>
#include <algorithm>
#include <utility>

#if !defined(BANGONPRO_STANDALONE_TEST) && defined(__has_include)
#if __has_include("effects/effectsrepository.hpp") && __has_include("transitions/transitionsrepository.hpp") && __has_include("kdenlivesettings.h")
#define BANGONPRO_HAS_KDENLIVE_CORE 1
#include "core.h"
#include "effects/effectsrepository.hpp"
#include "kdenlivesettings.h"
#include "transitions/transitionsrepository.hpp"
#endif
#endif

namespace BangOnPro {

namespace {

constexpr int kEntryIdRole = Qt::UserRole + 1;
constexpr int kIsCategoryRole = Qt::UserRole + 2;

bool containsAnyToken(const QString &haystackLower, const QStringList &tokens)
{
    for (const QString &tok : tokens) {
        if (haystackLower.contains(tok)) {
            return true;
        }
    }
    return false;
}

QString kindDisplayLabel(CatalogAssetKind kind)
{
    switch (kind) {
    case CatalogAssetKind::VideoEffect:
        return QStringLiteral("Video Effect");
    case CatalogAssetKind::AudioEffect:
        return QStringLiteral("Audio Effect");
    case CatalogAssetKind::VideoTransition:
        return QStringLiteral("Video Transition");
    case CatalogAssetKind::AudioTransition:
        return QStringLiteral("Audio Transition");
    case CatalogAssetKind::CustomPreset:
        return QStringLiteral("Custom Preset");
    case CatalogAssetKind::PremierePreset:
        return QStringLiteral("Premiere Preset");
    }
    return QStringLiteral("Effect");
}

QString defaultBadgeForKind(CatalogAssetKind kind)
{
    switch (kind) {
    case CatalogAssetKind::VideoEffect:
        return QStringLiteral("VFX");
    case CatalogAssetKind::AudioEffect:
        return QStringLiteral("AUD");
    case CatalogAssetKind::VideoTransition:
        return QStringLiteral("VTR");
    case CatalogAssetKind::AudioTransition:
        return QStringLiteral("ATR");
    case CatalogAssetKind::CustomPreset:
        return QStringLiteral("CST");
    case CatalogAssetKind::PremierePreset:
        return QStringLiteral("PR");
    }
    return QStringLiteral("FX");
}

} // namespace

class UnifiedEffectsTreeWidget : public QTreeWidget
{
public:
    using MimeProvider = std::function<QMimeData *(const QString &)>;
    using EnterHandler = std::function<void()>;

    explicit UnifiedEffectsTreeWidget(MimeProvider provider, EnterHandler onEnter, QWidget *parent = nullptr)
        : QTreeWidget(parent)
        , m_mimeProvider(std::move(provider))
        , m_onEnter(std::move(onEnter))
    {
        setDragEnabled(true);
        setDragDropMode(QAbstractItemView::DragOnly);
        setSelectionMode(QAbstractItemView::SingleSelection);
        setAlternatingRowColors(true);
        setAnimated(false);
        setIndentation(14);
        setUniformRowHeights(true);
        setContextMenuPolicy(Qt::CustomContextMenu);
    }

protected:
    QStringList mimeTypes() const override
    {
        return {
            QStringLiteral("kdenlive/effect"),
            QStringLiteral("kdenlive/composition"),
            QString::fromLatin1(PremierePresetBrowser::kPresetMimeType),
        };
    }

#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
    QMimeData *mimeData(const QList<QTreeWidgetItem *> &items) const override
#else
    // Qt5 QTreeWidget::mimeData takes QList<QTreeWidgetItem *> by value (ABI virtual override).
    // cppcheck-suppress passedByValue
    QMimeData *mimeData(const QList<QTreeWidgetItem *> items) const override
#endif
    {
        if (items.isEmpty() || !m_mimeProvider) {
            return nullptr;
        }
        QTreeWidgetItem *item = items.first();
        if (!item || item->data(0, kIsCategoryRole).toBool()) {
            return nullptr;
        }
        const QString id = item->data(0, kEntryIdRole).toString();
        if (id.isEmpty()) {
            return nullptr;
        }
        return m_mimeProvider(id);
    }

    void keyPressEvent(QKeyEvent *event) override
    {
        if (event && (event->key() == Qt::Key_Return || event->key() == Qt::Key_Enter)) {
            if (m_onEnter) {
                m_onEnter();
                event->accept();
                return;
            }
        }
        QTreeWidget::keyPressEvent(event);
    }

private:
    MimeProvider m_mimeProvider;
    EnterHandler m_onEnter;
};

UnifiedEffectsPanel::UnifiedEffectsPanel(QWidget *parent, const QString &settingsOrg, const QString &settingsApp, bool syncKdenliveSettings)
    : QWidget(parent)
    , m_settingsOrg(settingsOrg)
    , m_settingsApp(settingsApp)
    , m_syncKdenliveSettings(syncKdenliveSettings)
{
    setObjectName(QStringLiteral("BangOnProUnifiedEffectsPanel"));
    setWindowTitle(QStringLiteral("Effects"));
    buildUi();
    loadUserState();
    refreshView();
}

void UnifiedEffectsPanel::buildUi()
{
    auto *rootLayout = new QVBoxLayout(this);
    rootLayout->setContentsMargins(6, 6, 6, 6);
    rootLayout->setSpacing(6);

    // Search bar with 120ms debounce
    m_searchEdit = new QLineEdit(this);
    m_searchEdit->setObjectName(QStringLiteral("unifiedEffectsSearchEdit"));
    m_searchEdit->setPlaceholderText(QStringLiteral("Search effects, transitions, presets…"));
    m_searchEdit->setClearButtonEnabled(true);
    rootLayout->addWidget(m_searchEdit);

    m_searchDebounceTimer = new QTimer(this);
    m_searchDebounceTimer->setSingleShot(true);
    m_searchDebounceTimer->setInterval(120);
    connect(m_searchDebounceTimer, &QTimer::timeout, this, &UnifiedEffectsPanel::refreshView);
    connect(m_searchEdit, &QLineEdit::textChanged, this, &UnifiedEffectsPanel::onSearchTextChanged);

    // Scope filter pills (2 compact rows so narrow docks never clip)
    m_scopeGroup = new QButtonGroup(this);
    m_scopeGroup->setExclusive(true);

    auto *pillGrid = new QGridLayout();
    pillGrid->setContentsMargins(0, 0, 0, 0);
    pillGrid->setHorizontalSpacing(4);
    pillGrid->setVerticalSpacing(4);

    struct PillSpec
    {
        EffectsScopeFilter scope;
        const char *label;
        const char *objectName;
    };
    const QVector<PillSpec> pills = {
        {EffectsScopeFilter::All, "All", "scopePillAll"},
        {EffectsScopeFilter::Video, "Video", "scopePillVideo"},
        {EffectsScopeFilter::Audio, "Audio", "scopePillAudio"},
        {EffectsScopeFilter::Transitions, "Transitions", "scopePillTransitions"},
        {EffectsScopeFilter::Presets, "Presets", "scopePillPresets"},
        {EffectsScopeFilter::Favourites, "Favourites", "scopePillFavourites"},
        {EffectsScopeFilter::Recent, "Recent", "scopePillRecent"},
    };

    for (int i = 0; i < pills.size(); ++i) {
        const PillSpec &spec = pills.at(i);
        auto *btn = new QPushButton(QString::fromLatin1(spec.label), this);
        btn->setObjectName(QString::fromLatin1(spec.objectName));
        btn->setCheckable(true);
        btn->setChecked(spec.scope == EffectsScopeFilter::All);
        btn->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
        btn->setMinimumHeight(24);
        m_scopeGroup->addButton(btn, static_cast<int>(spec.scope));
        const int row = i / 4;
        const int col = i % 4;
        pillGrid->addWidget(btn, row, col);
    }
    rootLayout->addLayout(pillGrid);

#if QT_VERSION >= QT_VERSION_CHECK(5, 15, 0)
    connect(m_scopeGroup, &QButtonGroup::idClicked, this, &UnifiedEffectsPanel::onScopeButtonClicked);
#else
    connect(m_scopeGroup, QOverload<int>::of(&QButtonGroup::buttonClicked), this, &UnifiedEffectsPanel::onScopeButtonClicked);
#endif

    // Secondary row: "Show legacy & unverified" toggle (OFF by default) + count label
    auto *metaRow = new QHBoxLayout();
    metaRow->setContentsMargins(0, 0, 0, 0);
    metaRow->setSpacing(6);

    m_showLegacyCheck = new QCheckBox(QStringLiteral("Show legacy && unverified"), this);
    m_showLegacyCheck->setObjectName(QStringLiteral("unifiedEffectsShowLegacyCheck"));
    m_showLegacyCheck->setChecked(false);
    m_showLegacyCheck->setToolTip(QStringLiteral("Include deprecated MLT filters, legacy effects, and unverified/unsupported presets"));
    connect(m_showLegacyCheck, &QCheckBox::toggled, this, &UnifiedEffectsPanel::onShowLegacyToggled);

    m_countLabel = new QLabel(QStringLiteral("0 items"), this);
    m_countLabel->setObjectName(QStringLiteral("unifiedEffectsCountLabel"));
    m_countLabel->setAlignment(Qt::AlignRight | Qt::AlignVCenter);

    metaRow->addWidget(m_showLegacyCheck, 1);
    metaRow->addWidget(m_countLabel, 0);
    rootLayout->addLayout(metaRow);

    // Categorized drag-and-drop tree
    m_tree = new UnifiedEffectsTreeWidget([this](const QString &entryId) { return createMimeDataForEntry(entryId); }, [this]() { applyEntry(); }, this);
    m_tree->setObjectName(QStringLiteral("unifiedEffectsTree"));
    m_tree->setColumnCount(3);
    m_tree->setHeaderLabels({QStringLiteral("Name"), QStringLiteral("Type"), QStringLiteral("★")});
    m_tree->setHeaderHidden(true);
    m_tree->header()->setStretchLastSection(false);
    m_tree->header()->setSectionResizeMode(0, QHeaderView::Stretch);
    m_tree->header()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
    m_tree->header()->setSectionResizeMode(2, QHeaderView::ResizeToContents);
    rootLayout->addWidget(m_tree, 1);

    connect(m_tree, &QTreeWidget::itemSelectionChanged, this, &UnifiedEffectsPanel::onSelectionChanged);
    connect(m_tree, &QTreeWidget::itemDoubleClicked, this, &UnifiedEffectsPanel::onItemActivated);
    connect(m_tree, &QWidget::customContextMenuRequested, this, &UnifiedEffectsPanel::onContextMenuRequested);

    // Action bar + description footer
    auto *actionRow = new QHBoxLayout();
    actionRow->setContentsMargins(0, 0, 0, 0);
    actionRow->setSpacing(4);

    m_applyBtn = new QPushButton(QStringLiteral("Apply to Selected Clip"), this);
    m_applyBtn->setObjectName(QStringLiteral("unifiedEffectsApplyBtn"));
    m_applyBtn->setEnabled(false);
    connect(m_applyBtn, &QPushButton::clicked, this, &UnifiedEffectsPanel::onApplyClicked);

    m_favoriteBtn = new QPushButton(QStringLiteral("☆ Favourite"), this);
    m_favoriteBtn->setObjectName(QStringLiteral("unifiedEffectsFavBtn"));
    m_favoriteBtn->setEnabled(false);
    connect(m_favoriteBtn, &QPushButton::clicked, this, &UnifiedEffectsPanel::onToggleFavoriteClicked);

    actionRow->addWidget(m_applyBtn, 1);
    actionRow->addWidget(m_favoriteBtn, 0);
    rootLayout->addLayout(actionRow);

    m_descriptionLabel = new QLabel(QStringLiteral("Drag an effect onto a clip, or double-click to apply."), this);
    m_descriptionLabel->setObjectName(QStringLiteral("unifiedEffectsDescLabel"));
    m_descriptionLabel->setWordWrap(true);
    m_descriptionLabel->setMaximumHeight(44);
    rootLayout->addWidget(m_descriptionLabel);
}

QStringList UnifiedEffectsPanel::canonicalCategories()
{
    return {
        QStringLiteral("Transform & Motion"),  QStringLiteral("Colour & Tone"),           QStringLiteral("Blur & Sharpen"),
        QStringLiteral("Crop & Distort"),      QStringLiteral("Keying & Masking"),        QStringLiteral("Stylise & Generate"),
        QStringLiteral("Audio Dynamics & EQ"), QStringLiteral("Audio Cleanup & Utility"), QStringLiteral("Video Transitions"),
        QStringLiteral("Audio Transitions"),   QStringLiteral("Custom Presets"),          QStringLiteral("Premiere Presets"),
    };
}

bool UnifiedEffectsPanel::isLegacyOrUnverifiedEntry(const QString &assetId, const QString &name, const QString &rawCategory, const QString &description)
{
    const QString combined = (assetId + QLatin1Char(' ') + name + QLatin1Char(' ') + rawCategory + QLatin1Char(' ') + description).toLower();
    static const QStringList kLegacyTokens = {
        QStringLiteral("deprecated"), QStringLiteral("legacy"),      QStringLiteral("more checks"),        QStringLiteral("to be completed"),
        QStringLiteral("obsolete"),   QStringLiteral("unsupported"), QStringLiteral("missing_dependency"),
    };
    return containsAnyToken(combined, kLegacyTokens);
}

QString UnifiedEffectsPanel::classifyCategory(const QString &assetId, const QString &name, const QString &description, CatalogAssetKind kind)
{
    if (kind == CatalogAssetKind::VideoTransition) {
        return QStringLiteral("Video Transitions");
    }
    if (kind == CatalogAssetKind::AudioTransition) {
        return QStringLiteral("Audio Transitions");
    }
    if (kind == CatalogAssetKind::CustomPreset) {
        return QStringLiteral("Custom Presets");
    }
    if (kind == CatalogAssetKind::PremierePreset) {
        return QStringLiteral("Premiere Presets");
    }

    const QString text = (assetId + QLatin1Char(' ') + name + QLatin1Char(' ') + description).toLower();

    if (kind == CatalogAssetKind::AudioEffect) {
        static const QStringList kAudioDynamicsTokens = {
            QStringLiteral("volume"),    QStringLiteral("gain"),    QStringLiteral("loudnorm"), QStringLiteral("compressor"), QStringLiteral("limiter"),
            QStringLiteral("equalizer"), QStringLiteral("eq"),      QStringLiteral("bass"),     QStringLiteral("treble"),     QStringLiteral("reverb"),
            QStringLiteral("echo"),      QStringLiteral("delay"),   QStringLiteral("flanger"),  QStringLiteral("phaser"),     QStringLiteral("pitch"),
            QStringLiteral("chorus"),    QStringLiteral("pan"),     QStringLiteral("balance"),  QStringLiteral("normalise"),  QStringLiteral("normalize"),
            QStringLiteral("dynamic"),   QStringLiteral("rbpitch"),
        };
        if (containsAnyToken(text, kAudioDynamicsTokens)) {
            return QStringLiteral("Audio Dynamics & EQ");
        }
        return QStringLiteral("Audio Cleanup & Utility");
    }

    // Video effects classification
    static const QStringList kTransformTokens = {
        QStringLiteral("qtblend"), QStringLiteral("transform"), QStringLiteral("affine"),  QStringLiteral("pan_zoom"), QStringLiteral("rotate"),
        QStringLiteral("mirror"),  QStringLiteral("flip"),      QStringLiteral("vidstab"), QStringLiteral("stabiliz"), QStringLiteral("position"),
        QStringLiteral("zoom"),    QStringLiteral("corner"),    QStringLiteral("speed"),   QStringLiteral("freeze"),   QStringLiteral("pillar_echo"),
    };
    if (containsAnyToken(text, kTransformTokens)) {
        return QStringLiteral("Transform & Motion");
    }

    static const QStringList kKeyingTokens = {
        QStringLiteral("chroma"), QStringLiteral("alpha"),        QStringLiteral("mask"),    QStringLiteral("roto"),
        QStringLiteral("key"),    QStringLiteral("bluescreen"),   QStringLiteral("lumakey"), QStringLiteral("matte"),
        QStringLiteral("shape"),  QStringLiteral("spot_remover"), QStringLiteral("despill"), QStringLiteral("obscure"),
    };
    if (containsAnyToken(text, kKeyingTokens)) {
        return QStringLiteral("Keying & Masking");
    }

    static const QStringList kBlurTokens = {
        QStringLiteral("blur"),    QStringLiteral("sharpen"),   QStringLiteral("unsharp"),  QStringLiteral("gblur"),
        QStringLiteral("boxblur"), QStringLiteral("median"),    QStringLiteral("denoise"),  QStringLiteral("hqdn3d"),
        QStringLiteral("deblock"), QStringLiteral("deflicker"), QStringLiteral("softglow"),
    };
    if (containsAnyToken(text, kBlurTokens)) {
        return QStringLiteral("Blur & Sharpen");
    }

    static const QStringList kCropTokens = {
        QStringLiteral("crop"),       QStringLiteral("distort"),     QStringLiteral("lens"),  QStringLiteral("defish"),
        QStringLiteral("scale0tilt"), QStringLiteral("edge"),        QStringLiteral("wave"),  QStringLiteral("kaleidoscope"),
        QStringLiteral("letterbox"),  QStringLiteral("perspective"), QStringLiteral("shear"), QStringLiteral("deinterlace"),
    };
    if (containsAnyToken(text, kCropTokens)) {
        return QStringLiteral("Crop & Distort");
    }

    static const QStringList kColourTokens = {
        QStringLiteral("color"),         QStringLiteral("colour"),    QStringLiteral("lift_gamma"),  QStringLiteral("brightness"),
        QStringLiteral("contrast"),      QStringLiteral("saturat"),   QStringLiteral("hue"),         QStringLiteral("lut"),
        QStringLiteral("white_balance"), QStringLiteral("gamma"),     QStringLiteral("curves"),      QStringLiteral("bezier"),
        QStringLiteral("levels"),        QStringLiteral("grade"),     QStringLiteral("tint"),        QStringLiteral("sepia"),
        QStringLiteral("greyscale"),     QStringLiteral("grayscale"), QStringLiteral("exposure"),    QStringLiteral("temperature"),
        QStringLiteral("technicolor"),   QStringLiteral("invert"),    QStringLiteral("avfilter.eq"), QStringLiteral("fade_from_black"),
        QStringLiteral("fade_to_black"), QStringLiteral("balanc"),
    };
    if (containsAnyToken(text, kColourTokens)) {
        return QStringLiteral("Colour & Tone");
    }

    return QStringLiteral("Stylise & Generate");
}

void UnifiedEffectsPanel::populateFromRepositories()
{
#ifdef BANGONPRO_HAS_KDENLIVE_CORE
    if (!pCore) {
        return;
    }

    QVector<EffectCatalogEntry> entries;

    // 1. Native effects & saved custom effects from EffectsRepository
    auto &effectsRepo = EffectsRepository::get();
    if (effectsRepo) {
        const auto allEffects = effectsRepo->getNames();
        entries.reserve(allEffects.size() + 64);
        for (const auto &pair : allEffects) {
            const QString id = pair.first;
            const QString name = pair.second;
            const AssetListType::AssetType assetType = effectsRepo->getType(id);
            if (assetType == AssetListType::AssetType::Hidden) {
                continue;
            }
            const QString desc = effectsRepo->getDescription(id);

            EffectCatalogEntry entry;
            entry.id = id;
            entry.name = name;
            entry.description = desc;

            if (assetType == AssetListType::AssetType::Custom || assetType == AssetListType::AssetType::CustomAudio ||
                assetType == AssetListType::AssetType::Template || assetType == AssetListType::AssetType::TemplateAudio ||
                assetType == AssetListType::AssetType::TemplateCustom || assetType == AssetListType::AssetType::TemplateCustomAudio) {
                entry.kind = CatalogAssetKind::CustomPreset;
            } else if (assetType == AssetListType::AssetType::Audio || effectsRepo->isAudioEffect(id)) {
                entry.kind = CatalogAssetKind::AudioEffect;
            } else {
                entry.kind = CatalogAssetKind::VideoEffect;
            }

            entry.category = classifyCategory(id, name, desc, entry.kind);
            entry.isExecutable = true;
            entry.isLegacyOrUnverified = isLegacyOrUnverifiedEntry(id, name, QString(), desc) || !effectsRepo->isIncludedInList(id);
            entry.sourceBadge = defaultBadgeForKind(entry.kind);
            entries.append(entry);
        }

        if (m_syncKdenliveSettings) {
            const QStringList kdenliveFavEffects = KdenliveSettings::favorite_effects();
            for (const QString &fid : kdenliveFavEffects) {
                if (!fid.isEmpty()) {
                    m_favorites.insert(fid);
                }
            }
        }
    }

    // 2. Native transitions & compositions from TransitionsRepository
    auto &transRepo = TransitionsRepository::get();
    if (transRepo) {
        const auto allTransitions = transRepo->getNames();
        for (const auto &pair : allTransitions) {
            const QString id = pair.first;
            const QString name = pair.second;
            const AssetListType::AssetType assetType = transRepo->getType(id);
            if (assetType == AssetListType::AssetType::Hidden) {
                continue;
            }
            const QString desc = transRepo->getDescription(id);

            EffectCatalogEntry entry;
            entry.id = id;
            entry.name = name;
            entry.description = desc;
            entry.kind = transRepo->isAudio(id) ? CatalogAssetKind::AudioTransition : CatalogAssetKind::VideoTransition;
            entry.category = classifyCategory(id, name, desc, entry.kind);
            entry.isExecutable = true;
            entry.isLegacyOrUnverified = isLegacyOrUnverifiedEntry(id, name, QString(), desc);
            entry.sourceBadge = defaultBadgeForKind(entry.kind);
            entries.append(entry);
        }

        if (m_syncKdenliveSettings) {
            const QStringList kdenliveFavTrans = KdenliveSettings::favorite_transitions();
            for (const QString &fid : kdenliveFavTrans) {
                if (!fid.isEmpty()) {
                    m_favorites.insert(fid);
                }
            }
        }
    }

    m_baseEntries = entries;
    rebuildMergedCatalog();
    refreshView();
#endif
}

void UnifiedEffectsPanel::populateFromEntries(const QVector<EffectCatalogEntry> &entries)
{
    m_baseEntries.clear();
    m_baseEntries.reserve(entries.size());
    for (EffectCatalogEntry entry : entries) {
        if (entry.category.isEmpty()) {
            entry.category = classifyCategory(entry.id, entry.name, entry.description, entry.kind);
        }
        if (entry.sourceBadge.isEmpty()) {
            entry.sourceBadge = defaultBadgeForKind(entry.kind);
        }
        if (!entry.isLegacyOrUnverified) {
            entry.isLegacyOrUnverified = !entry.isExecutable || isLegacyOrUnverifiedEntry(entry.id, entry.name, entry.category, entry.description);
        }
        m_baseEntries.append(entry);
    }
    rebuildMergedCatalog();
    refreshView();
}

void UnifiedEffectsPanel::setPremierePresets(const QVector<PresetDisplayEntry> &presets)
{
    m_premiereEntries.clear();
    m_premiereEntries.reserve(presets.size());

    for (const PresetDisplayEntry &p : presets) {
        if (p.record.presetId.isEmpty()) {
            continue;
        }
        const bool executable = (p.record.status == PresetCompatibilityStatus::ExactValidated || p.record.status == PresetCompatibilityStatus::Approximated) &&
                                !p.record.mltFilters.isEmpty();

        EffectCatalogEntry entry;
        entry.id = p.record.presetId;
        entry.name = p.record.name.isEmpty() ? p.record.presetId : p.record.name;
        entry.category = QStringLiteral("Premiere Presets");
        entry.kind = CatalogAssetKind::PremierePreset;
        entry.isExecutable = executable;
        entry.isLegacyOrUnverified = !executable;
        entry.sourceBadge = QStringLiteral("PR");

        QStringList descParts;
        if (!p.record.pack.isEmpty()) {
            descParts.append(p.record.pack);
        }
        if (!p.description.isEmpty()) {
            descParts.append(p.description);
        } else if (!p.record.notes.isEmpty()) {
            descParts.append(p.record.notes.first());
        }
        entry.description = descParts.join(QStringLiteral(" — "));
        m_premiereEntries.append(entry);
    }

    rebuildMergedCatalog();
    refreshView();
}

void UnifiedEffectsPanel::setPresetDragIdResolver(std::function<QString(const QString &presetId)> resolver)
{
    m_presetDragIdResolver = std::move(resolver);
}

void UnifiedEffectsPanel::rebuildMergedCatalog()
{
    m_allEntries.clear();
    m_indexById.clear();
    m_allEntries.reserve(m_baseEntries.size() + m_premiereEntries.size());

    for (const EffectCatalogEntry &e : m_baseEntries) {
        if (e.id.isEmpty() || m_indexById.contains(e.id)) {
            continue;
        }
        m_indexById.insert(e.id, m_allEntries.size());
        m_allEntries.append(e);
    }
    for (const EffectCatalogEntry &e : m_premiereEntries) {
        if (e.id.isEmpty() || m_indexById.contains(e.id)) {
            continue;
        }
        m_indexById.insert(e.id, m_allEntries.size());
        m_allEntries.append(e);
    }
}

int UnifiedEffectsPanel::totalEntryCount() const
{
    return m_allEntries.size();
}

int UnifiedEffectsPanel::visibleEntryCount() const
{
    return visibleEntryIds().size();
}

QStringList UnifiedEffectsPanel::visibleEntryIds() const
{
    QStringList ids;
    if (!m_tree) {
        return ids;
    }
    for (int i = 0; i < m_tree->topLevelItemCount(); ++i) {
        QTreeWidgetItem *top = m_tree->topLevelItem(i);
        if (!top) {
            continue;
        }
        if (top->data(0, kIsCategoryRole).toBool()) {
            for (int c = 0; c < top->childCount(); ++c) {
                QTreeWidgetItem *child = top->child(c);
                if (child) {
                    const QString id = child->data(0, kEntryIdRole).toString();
                    if (!id.isEmpty()) {
                        ids.append(id);
                    }
                }
            }
        } else {
            const QString id = top->data(0, kEntryIdRole).toString();
            if (!id.isEmpty()) {
                ids.append(id);
            }
        }
    }
    return ids;
}

EffectsScopeFilter UnifiedEffectsPanel::scopeFilter() const
{
    return m_scopeFilter;
}

void UnifiedEffectsPanel::setScopeFilter(EffectsScopeFilter scope)
{
    m_scopeFilter = scope;
    if (m_scopeGroup) {
        if (QAbstractButton *btn = m_scopeGroup->button(static_cast<int>(scope))) {
            btn->setChecked(true);
        }
    }
    refreshView();
}

bool UnifiedEffectsPanel::showLegacyAndUnverified() const
{
    return m_showLegacy;
}

void UnifiedEffectsPanel::setShowLegacyAndUnverified(bool show)
{
    m_showLegacy = show;
    if (m_showLegacyCheck && m_showLegacyCheck->isChecked() != show) {
        m_showLegacyCheck->blockSignals(true);
        m_showLegacyCheck->setChecked(show);
        m_showLegacyCheck->blockSignals(false);
    }
    refreshView();
}

QString UnifiedEffectsPanel::searchQuery() const
{
    return m_searchEdit ? m_searchEdit->text() : QString();
}

void UnifiedEffectsPanel::setSearchQuery(const QString &query, bool immediate)
{
    if (!m_searchEdit) {
        return;
    }
    if (immediate) {
        m_searchEdit->blockSignals(true);
        m_searchEdit->setText(query);
        m_searchEdit->blockSignals(false);
        if (m_searchDebounceTimer) {
            m_searchDebounceTimer->stop();
        }
        refreshView();
    } else {
        m_searchEdit->setText(query);
    }
}

bool UnifiedEffectsPanel::isFavorite(const QString &entryId) const
{
    return m_favorites.contains(entryId);
}

void UnifiedEffectsPanel::setFavorite(const QString &entryId, bool favorite)
{
    if (entryId.isEmpty()) {
        return;
    }
    if (favorite) {
        m_favorites.insert(entryId);
    } else {
        m_favorites.remove(entryId);
    }
    if (const EffectCatalogEntry *entry = findEntry(entryId)) {
        syncKdenliveFavorites(*entry, favorite);
    }
    saveUserState();
    Q_EMIT favoriteToggled(entryId, favorite);
    refreshView();
}

QStringList UnifiedEffectsPanel::favoriteIds() const
{
    QStringList list;
    list.reserve(m_favorites.size());
    for (auto it = m_favorites.constBegin(); it != m_favorites.constEnd(); ++it) {
        list.append(*it);
    }
    std::sort(list.begin(), list.end());
    return list;
}

void UnifiedEffectsPanel::recordRecent(const QString &entryId)
{
    if (entryId.isEmpty()) {
        return;
    }
    m_recentIds.removeAll(entryId);
    m_recentIds.prepend(entryId);
    while (m_recentIds.size() > kMaxRecentItems) {
        m_recentIds.removeLast();
    }
    saveUserState();
    if (m_scopeFilter == EffectsScopeFilter::Recent) {
        refreshView();
    }
}

QStringList UnifiedEffectsPanel::recentIds() const
{
    return m_recentIds;
}

QMimeData *UnifiedEffectsPanel::createMimeDataForEntry(const QString &entryId) const
{
    const EffectCatalogEntry *entry = findEntry(entryId);
    if (!entry) {
        return nullptr;
    }

    auto *mime = new QMimeData();
    mime->setText(entry->id);

    if (entry->kind == CatalogAssetKind::VideoTransition || entry->kind == CatalogAssetKind::AudioTransition) {
        mime->setData(QStringLiteral("kdenlive/composition"), entry->id.toUtf8());
        return mime;
    }

    if (entry->kind == CatalogAssetKind::PremierePreset) {
        QString resolvedEffectId = entry->installedEffectId;
        if (resolvedEffectId.isEmpty() && m_presetDragIdResolver) {
            resolvedEffectId = m_presetDragIdResolver(entry->id);
        }
        if (!resolvedEffectId.isEmpty()) {
            mime->setData(QStringLiteral("kdenlive/effect"), resolvedEffectId.toUtf8());
        } else {
            mime->setData(QStringLiteral("kdenlive/effect"), (QStringLiteral("preset:") + entry->id).toUtf8());
        }
        QJsonObject payload;
        payload.insert(QStringLiteral("preset_id"), entry->id);
        payload.insert(QStringLiteral("name"), entry->name);
        mime->setData(QString::fromLatin1(PremierePresetBrowser::kPresetMimeType), QJsonDocument(payload).toJson(QJsonDocument::Compact));
        return mime;
    }

    // Native VideoEffect, AudioEffect, or CustomPreset
    mime->setData(QStringLiteral("kdenlive/effect"), entry->id.toUtf8());
    return mime;
}

bool UnifiedEffectsPanel::applyEntry(const QString &entryId)
{
    const QString targetId = entryId.isEmpty() ? selectedEntryId() : entryId;
    const EffectCatalogEntry *entry = findEntry(targetId);
    if (!entry) {
        return false;
    }
    if (!entry->isExecutable) {
        return false;
    }

    recordRecent(entry->id);

    switch (entry->kind) {
    case CatalogAssetKind::VideoTransition:
    case CatalogAssetKind::AudioTransition:
        Q_EMIT applyTransitionRequested(entry->id);
        return true;
    case CatalogAssetKind::PremierePreset:
        Q_EMIT applyPremierePresetRequested(entry->id);
        return true;
    case CatalogAssetKind::VideoEffect:
    case CatalogAssetKind::AudioEffect:
    case CatalogAssetKind::CustomPreset:
        Q_EMIT applyEffectRequested(entry->id);
        return true;
    }
    return false;
}

bool UnifiedEffectsPanel::matchesFilters(const EffectCatalogEntry &entry, const QString &query) const
{
    if (!m_showLegacy && (entry.isLegacyOrUnverified || !entry.isExecutable)) {
        return false;
    }

    switch (m_scopeFilter) {
    case EffectsScopeFilter::All:
        break;
    case EffectsScopeFilter::Video:
        if (entry.kind != CatalogAssetKind::VideoEffect) {
            return false;
        }
        break;
    case EffectsScopeFilter::Audio:
        if (entry.kind != CatalogAssetKind::AudioEffect) {
            return false;
        }
        break;
    case EffectsScopeFilter::Transitions:
        if (entry.kind != CatalogAssetKind::VideoTransition && entry.kind != CatalogAssetKind::AudioTransition) {
            return false;
        }
        break;
    case EffectsScopeFilter::Presets:
        if (entry.kind != CatalogAssetKind::CustomPreset && entry.kind != CatalogAssetKind::PremierePreset) {
            return false;
        }
        break;
    case EffectsScopeFilter::Favourites:
        if (!m_favorites.contains(entry.id)) {
            return false;
        }
        break;
    case EffectsScopeFilter::Recent:
        if (!m_recentIds.contains(entry.id)) {
            return false;
        }
        break;
    }

    if (!query.isEmpty()) {
        const bool match = entry.name.contains(query, Qt::CaseInsensitive) || entry.category.contains(query, Qt::CaseInsensitive) ||
                           entry.description.contains(query, Qt::CaseInsensitive) || entry.id.contains(query, Qt::CaseInsensitive);
        if (!match) {
            return false;
        }
    }

    return true;
}

void UnifiedEffectsPanel::refreshView()
{
    if (!m_tree) {
        return;
    }

    const QString prevSelected = selectedEntryId();
    const QString query = m_searchEdit ? m_searchEdit->text().trimmed() : QString();

    m_tree->blockSignals(true);
    m_tree->clear();

    QTreeWidgetItem *itemToSelect = nullptr;
    int shownCount = 0;

    auto makeLeafItem = [this, &prevSelected, &itemToSelect, &shownCount](QTreeWidgetItem *parentItem, const EffectCatalogEntry &entry) {
        QTreeWidgetItem *leaf = parentItem ? new QTreeWidgetItem(parentItem) : new QTreeWidgetItem(m_tree);
        QString label = entry.name;
        if (entry.isLegacyOrUnverified) {
            label += QStringLiteral(" [Legacy]");
        }
        leaf->setText(0, label);
        leaf->setText(1, entry.sourceBadge.isEmpty() ? defaultBadgeForKind(entry.kind) : entry.sourceBadge);
        leaf->setText(2, m_favorites.contains(entry.id) ? QStringLiteral("★") : QString());
        leaf->setData(0, kEntryIdRole, entry.id);
        leaf->setData(0, kIsCategoryRole, false);
        leaf->setToolTip(0, entry.description.isEmpty() ? entry.id : QStringLiteral("%1\n(%2)").arg(entry.description, entry.id));
        leaf->setFlags(leaf->flags() | Qt::ItemIsDragEnabled);
        ++shownCount;
        if (!prevSelected.isEmpty() && entry.id == prevSelected) {
            itemToSelect = leaf;
        }
    };

    if (m_scopeFilter == EffectsScopeFilter::Recent) {
        // Show in strict most-recent-first order without category grouping
        for (const QString &rid : m_recentIds) {
            const EffectCatalogEntry *entry = findEntry(rid);
            if (entry && matchesFilters(*entry, query)) {
                makeLeafItem(nullptr, *entry);
            }
        }
    } else {
        QHash<QString, QVector<const EffectCatalogEntry *>> byCategory;
        QStringList extraCategories;

        for (const EffectCatalogEntry &entry : m_allEntries) {
            if (!matchesFilters(entry, query)) {
                continue;
            }
            const QString cat = entry.category.isEmpty() ? classifyCategory(entry.id, entry.name, entry.description, entry.kind) : entry.category;
            if (!byCategory.contains(cat) && !canonicalCategories().contains(cat)) {
                extraCategories.append(cat);
            }
            byCategory[cat].append(&entry);
        }

        QStringList orderedCats = canonicalCategories();
        std::sort(extraCategories.begin(), extraCategories.end());
        orderedCats.append(extraCategories);

        for (const QString &cat : orderedCats) {
            if (!byCategory.contains(cat)) {
                continue;
            }
            QVector<const EffectCatalogEntry *> catEntries = byCategory.value(cat);
            std::sort(catEntries.begin(), catEntries.end(),
                      [](const EffectCatalogEntry *a, const EffectCatalogEntry *b) { return QString::localeAwareCompare(a->name, b->name) < 0; });

            auto *catItem = new QTreeWidgetItem(m_tree);
            catItem->setText(0, QStringLiteral("%1 (%2)").arg(cat).arg(catEntries.size()));
            catItem->setData(0, kIsCategoryRole, true);
            catItem->setFlags((catItem->flags() & ~Qt::ItemIsDragEnabled) | Qt::ItemIsEnabled);

            QFont boldFont = catItem->font(0);
            boldFont.setBold(true);
            catItem->setFont(0, boldFont);

            for (const EffectCatalogEntry *entry : catEntries) {
                makeLeafItem(catItem, *entry);
            }

            const bool shouldExpand = !query.isEmpty() || m_scopeFilter != EffectsScopeFilter::All || byCategory.size() <= 4;
            catItem->setExpanded(shouldExpand);
        }
    }

    if (itemToSelect) {
        m_tree->setCurrentItem(itemToSelect);
    }

    m_tree->blockSignals(false);

    if (m_countLabel) {
        m_countLabel->setText(shownCount == 1 ? QStringLiteral("1 item") : QStringLiteral("%1 items").arg(shownCount));
    }
    updateSelectionFooter();
}

void UnifiedEffectsPanel::focusSearch()
{
    if (m_searchEdit) {
        m_searchEdit->setFocus();
        m_searchEdit->selectAll();
    }
}

void UnifiedEffectsPanel::onSearchTextChanged(const QString &)
{
    if (m_searchDebounceTimer) {
        m_searchDebounceTimer->start();
    }
}

void UnifiedEffectsPanel::onScopeButtonClicked(int id)
{
    m_scopeFilter = static_cast<EffectsScopeFilter>(id);
    refreshView();
}

void UnifiedEffectsPanel::onShowLegacyToggled(bool checked)
{
    m_showLegacy = checked;
    refreshView();
}

void UnifiedEffectsPanel::onSelectionChanged()
{
    updateSelectionFooter();
    const QString id = selectedEntryId();
    if (!id.isEmpty()) {
        Q_EMIT entrySelected(id);
    }
}

void UnifiedEffectsPanel::onItemActivated(QTreeWidgetItem *item, int)
{
    if (!item || item->data(0, kIsCategoryRole).toBool()) {
        return;
    }
    const QString id = item->data(0, kEntryIdRole).toString();
    if (!id.isEmpty()) {
        applyEntry(id);
    }
}

void UnifiedEffectsPanel::onApplyClicked()
{
    applyEntry();
}

void UnifiedEffectsPanel::onToggleFavoriteClicked()
{
    const QString id = selectedEntryId();
    if (!id.isEmpty()) {
        setFavorite(id, !isFavorite(id));
    }
}

void UnifiedEffectsPanel::onContextMenuRequested(const QPoint &pos)
{
    QTreeWidgetItem *item = m_tree ? m_tree->itemAt(pos) : nullptr;
    if (!item || item->data(0, kIsCategoryRole).toBool()) {
        return;
    }
    const QString id = item->data(0, kEntryIdRole).toString();
    const EffectCatalogEntry *entry = findEntry(id);
    if (!entry) {
        return;
    }

    QMenu menu(this);
    QAction *applyAct = menu.addAction(QStringLiteral("Apply to Selected Clip"));
    applyAct->setEnabled(entry->isExecutable);
    connect(applyAct, &QAction::triggered, this, [this, id]() { applyEntry(id); });

    const bool fav = isFavorite(id);
    QAction *favAct = menu.addAction(fav ? QStringLiteral("Remove from Favourites") : QStringLiteral("Add to Favourites"));
    connect(favAct, &QAction::triggered, this, [this, id, fav]() { setFavorite(id, !fav); });

    menu.addSeparator();
    QAction *descAct = menu.addAction(QStringLiteral("Show Description"));
    connect(descAct, &QAction::triggered, this, [this, id]() {
        const EffectCatalogEntry *e = findEntry(id);
        if (!e) {
            return;
        }
        const QString body = QStringLiteral("Name: %1\nID: %2\nType: %3\nCategory: %4\n\n%5")
                                 .arg(e->name, e->id, kindDisplayLabel(e->kind), e->category,
                                      e->description.isEmpty() ? QStringLiteral("No description available.") : e->description);
        QMessageBox::information(this, e->name, body);
    });

    menu.exec(m_tree->viewport()->mapToGlobal(pos));
}

const EffectCatalogEntry *UnifiedEffectsPanel::findEntry(const QString &entryId) const
{
    const auto it = m_indexById.constFind(entryId);
    if (it == m_indexById.constEnd()) {
        return nullptr;
    }
    return &m_allEntries.at(it.value());
}

QString UnifiedEffectsPanel::selectedEntryId() const
{
    if (!m_tree) {
        return {};
    }
    QTreeWidgetItem *item = m_tree->currentItem();
    if (!item || item->data(0, kIsCategoryRole).toBool()) {
        return {};
    }
    return item->data(0, kEntryIdRole).toString();
}

void UnifiedEffectsPanel::updateSelectionFooter()
{
    const QString id = selectedEntryId();
    const EffectCatalogEntry *entry = findEntry(id);
    if (!entry) {
        if (m_applyBtn) {
            m_applyBtn->setEnabled(false);
        }
        if (m_favoriteBtn) {
            m_favoriteBtn->setEnabled(false);
            m_favoriteBtn->setText(QStringLiteral("☆ Favourite"));
        }
        if (m_descriptionLabel) {
            m_descriptionLabel->setText(QStringLiteral("Drag an effect onto a clip, or double-click to apply."));
        }
        return;
    }

    if (m_applyBtn) {
        m_applyBtn->setEnabled(entry->isExecutable);
    }
    if (m_favoriteBtn) {
        m_favoriteBtn->setEnabled(true);
        m_favoriteBtn->setText(m_favorites.contains(entry->id) ? QStringLiteral("★ Favourited") : QStringLiteral("☆ Favourite"));
    }
    if (m_descriptionLabel) {
        const QString desc = entry->description.isEmpty() ? QStringLiteral("%1 (%2)").arg(kindDisplayLabel(entry->kind), entry->id)
                                                          : QStringLiteral("%1 — %2").arg(entry->name, entry->description);
        m_descriptionLabel->setText(desc);
    }
}

void UnifiedEffectsPanel::loadUserState()
{
    QSettings settings(m_settingsOrg, m_settingsApp);
    const QStringList favs = settings.value(QStringLiteral("favourites")).toStringList();
    m_favorites.clear();
    for (const QString &f : favs) {
        if (!f.trimmed().isEmpty()) {
            m_favorites.insert(f.trimmed());
        }
    }
    const QStringList recents = settings.value(QStringLiteral("recent")).toStringList();
    m_recentIds.clear();
    for (const QString &r : recents) {
        const QString trimmed = r.trimmed();
        if (!trimmed.isEmpty() && !m_recentIds.contains(trimmed) && m_recentIds.size() < kMaxRecentItems) {
            m_recentIds.append(trimmed);
        }
    }
}

void UnifiedEffectsPanel::saveUserState() const
{
    QSettings settings(m_settingsOrg, m_settingsApp);
    settings.setValue(QStringLiteral("favourites"), favoriteIds());
    settings.setValue(QStringLiteral("recent"), m_recentIds);
    settings.sync();
}

void UnifiedEffectsPanel::syncKdenliveFavorites(const EffectCatalogEntry &entry, bool favorite) const
{
#ifdef BANGONPRO_HAS_KDENLIVE_CORE
    if (!m_syncKdenliveSettings) {
        return;
    }
    if (entry.kind == CatalogAssetKind::VideoTransition || entry.kind == CatalogAssetKind::AudioTransition) {
        QStringList favs = KdenliveSettings::favorite_transitions();
        if (favorite) {
            if (!favs.contains(entry.id)) {
                favs.append(entry.id);
            }
        } else {
            favs.removeAll(entry.id);
        }
        KdenliveSettings::setFavorite_transitions(favs);
    } else if (entry.kind == CatalogAssetKind::VideoEffect || entry.kind == CatalogAssetKind::AudioEffect || entry.kind == CatalogAssetKind::CustomPreset) {
        QStringList favs = KdenliveSettings::favorite_effects();
        if (favorite) {
            if (!favs.contains(entry.id)) {
                favs.append(entry.id);
            }
        } else {
            favs.removeAll(entry.id);
        }
        KdenliveSettings::setFavorite_effects(favs);
    }
#else
    Q_UNUSED(entry)
    Q_UNUSED(favorite)
#endif
}

} // namespace BangOnPro
