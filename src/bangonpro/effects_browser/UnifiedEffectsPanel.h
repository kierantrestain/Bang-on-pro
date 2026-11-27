/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include "PremierePresetBrowser.h"

#include <QHash>
#include <QSet>
#include <QString>
#include <QStringList>
#include <QVector>
#include <QWidget>
#include <functional>

class QButtonGroup;
class QCheckBox;
class QLabel;
class QLineEdit;
class QMimeData;
class QPushButton;
class QTimer;
class QTreeWidgetItem;

namespace BangOnPro {

class UnifiedEffectsTreeWidget;

enum class CatalogAssetKind { VideoEffect, AudioEffect, VideoTransition, AudioTransition, CustomPreset, PremierePreset };

enum class EffectsScopeFilter { All, Video, Audio, Transitions, Presets, Favourites, Recent };

struct EffectCatalogEntry
{
    QString id;
    QString name;
    QString category;
    QString description;
    CatalogAssetKind kind = CatalogAssetKind::VideoEffect;
    bool isLegacyOrUnverified = false;
    bool isExecutable = true;
    QString sourceBadge;
    QString installedEffectId;
};

/**
 * @brief Professional unified Effects, Transitions & Presets library panel.
 *
 * Unifies native MLT video/audio effects, transitions/compositions, saved custom
 * presets, and validated Premiere presets into a single searchable, categorized
 * panel with debounced search, scope filter pills, persistent favourites and
 * recently used items, clean hiding of legacy/unverified entries by default,
 * and native Kdenlive drag-and-drop MIME payloads (`kdenlive/effect` and
 * `kdenlive/composition`).
 */
class UnifiedEffectsPanel : public QWidget
{
    Q_OBJECT

public:
    static constexpr int kMaxRecentItems = 16;

    explicit UnifiedEffectsPanel(QWidget *parent = nullptr, const QString &settingsOrg = QStringLiteral("BangOnPro"),
                                 const QString &settingsApp = QStringLiteral("EffectsLibrary"), bool syncKdenliveSettings = true);
    ~UnifiedEffectsPanel() override = default;

    /** @brief Populate native effects & transitions directly from Kdenlive repositories. */
    void populateFromRepositories();

    /** @brief Deterministic headless population helper for unit tests or custom catalogs. */
    void populateFromEntries(const QVector<EffectCatalogEntry> &entries);

    /** @brief Inject/refresh Premiere presets from PremierePresetBrowser into the unified library. */
    void setPremierePresets(const QVector<PresetDisplayEntry> &presets);

    /**
     * @brief Register an optional callback invoked when dragging a Premiere preset to
     * materialize/register it in EffectsRepository and obtain its native kdenlive/effect ID.
     */
    void setPresetDragIdResolver(std::function<QString(const QString &presetId)> resolver);

    /** @brief Classify a native effect/transition into a Premiere-familiar studio category. */
    static QString classifyCategory(const QString &assetId, const QString &name, const QString &description, CatalogAssetKind kind);

    /** @brief Canonical ordered list of Bang On Pro studio effect categories. */
    static QStringList canonicalCategories();

    /** @brief Returns true if an entry should be hidden unless "Show legacy & unverified" is enabled. */
    static bool isLegacyOrUnverifiedEntry(const QString &assetId, const QString &name, const QString &rawCategory, const QString &description);

    int totalEntryCount() const;
    int visibleEntryCount() const;
    QStringList visibleEntryIds() const;

    EffectsScopeFilter scopeFilter() const;
    void setScopeFilter(EffectsScopeFilter scope);

    bool showLegacyAndUnverified() const;
    void setShowLegacyAndUnverified(bool show);

    QString searchQuery() const;
    void setSearchQuery(const QString &query, bool immediate = true);

    bool isFavorite(const QString &entryId) const;
    void setFavorite(const QString &entryId, bool favorite);
    QStringList favoriteIds() const;

    void recordRecent(const QString &entryId);
    QStringList recentIds() const;

    /** @brief Build drag-and-drop QMimeData for @p entryId (caller takes ownership). */
    QMimeData *createMimeDataForEntry(const QString &entryId) const;

    /** @brief Programmatically trigger apply on the currently selected entry (or @p entryId if non-empty). */
    bool applyEntry(const QString &entryId = QString());

public Q_SLOTS:
    void refreshView();
    void focusSearch();

Q_SIGNALS:
    void applyEffectRequested(const QString &assetId);
    void applyTransitionRequested(const QString &assetId);
    void applyPremierePresetRequested(const QString &presetId);
    void favoriteToggled(const QString &assetId, bool isFavorite);
    void entrySelected(const QString &assetId);

private Q_SLOTS:
    void onSearchTextChanged(const QString &text);
    void onScopeButtonClicked(int id);
    void onShowLegacyToggled(bool checked);
    void onSelectionChanged();
    void onItemActivated(QTreeWidgetItem *item, int column);
    void onApplyClicked();
    void onToggleFavoriteClicked();
    void onContextMenuRequested(const QPoint &pos);

private:
    void buildUi();
    void loadUserState();
    void saveUserState() const;
    void syncKdenliveFavorites(const EffectCatalogEntry &entry, bool favorite) const;
    void rebuildMergedCatalog();
    bool matchesFilters(const EffectCatalogEntry &entry, const QString &query) const;
    const EffectCatalogEntry *findEntry(const QString &entryId) const;
    QString selectedEntryId() const;
    void updateSelectionFooter();

    QString m_settingsOrg;
    QString m_settingsApp;
    bool m_syncKdenliveSettings = true;

    QVector<EffectCatalogEntry> m_baseEntries;
    QVector<EffectCatalogEntry> m_premiereEntries;
    QVector<EffectCatalogEntry> m_allEntries;
    QHash<QString, int> m_indexById;

    QSet<QString> m_favorites;
    QStringList m_recentIds;

    EffectsScopeFilter m_scopeFilter = EffectsScopeFilter::All;
    bool m_showLegacy = false;
    std::function<QString(const QString &)> m_presetDragIdResolver;

    QLineEdit *m_searchEdit = nullptr;
    QTimer *m_searchDebounceTimer = nullptr;
    QButtonGroup *m_scopeGroup = nullptr;
    QCheckBox *m_showLegacyCheck = nullptr;
    QLabel *m_countLabel = nullptr;
    UnifiedEffectsTreeWidget *m_tree = nullptr;
    QPushButton *m_applyBtn = nullptr;
    QPushButton *m_favoriteBtn = nullptr;
    QLabel *m_descriptionLabel = nullptr;
};

} // namespace BangOnPro
