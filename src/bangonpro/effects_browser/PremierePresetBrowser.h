/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include "../BangOnProContracts.h"

#include <QHash>
#include <QJsonArray>
#include <QJsonObject>
#include <QPixmap>
#include <QSet>
#include <QString>
#include <QStringList>
#include <QVector>
#include <QWidget>
#include <functional>

class QCheckBox;
class QComboBox;
class QLabel;
class QLineEdit;
class QListWidget;
class QPlainTextEdit;
class QPushButton;
class QTabWidget;
class QTextBrowser;
class QTimer;
class QTreeWidgetItem;

namespace BangOnPro {

class PresetTreeWidget;

struct CustomEffectStack
{
    QString name;
    QStringList presetIds;
    QJsonArray combinedMltFilters;
};

struct PresetDisplayEntry
{
    PremierePresetRecord record;
    QString originalXml;
    QString description;
    QString previewBeforePath;
    QString previewAfterPath;
    QString previewSplitPath;
    QString visualEquivalenceEvidence;
};

/**
 * @brief Dockable Bang On Pro Premiere Preset & Effects Browser widget.
 *
 * Loads schema-validated compatibility_report.json files, surfaces every preset
 * (including unsupported and missing-dependency entries with colour badges,
 * visual before/after previews, inline parameter/keyframe editor, dependency details,
 * and raw XML inspection), supports persistent favourites, saved custom stacks,
 * and emits drag-and-drop payloads (`application/x-bangonpro-preset`).
 */
class PremierePresetBrowser : public QWidget
{
    Q_OBJECT

public:
    static constexpr const char *kPresetMimeType = "application/x-bangonpro-preset";

    explicit PremierePresetBrowser(QWidget *parent = nullptr, bool executableOnlyDefault = false);
    ~PremierePresetBrowser() override = default;

    bool loadReportFile(const QString &jsonPath, QString *errorOut = nullptr);
    bool loadReportJson(const QJsonObject &reportObj, QString *errorOut = nullptr);

    int presetCount() const;
    int visiblePresetCount() const;
    QVector<PresetDisplayEntry> presets() const;
    bool findPresetById(const QString &presetId, PresetDisplayEntry *outEntry) const;
    /** @brief Return cached raw preset JSON object by ID (avoids re-reading compatibility_report.json from disk). */
    QJsonObject presetJsonById(const QString &presetId) const;

    void setExecutableOnlyFilter(bool enabled);
    bool isExecutableOnlyFilter() const;

    void setShowTechnicalInspector(bool show);
    bool showTechnicalInspector() const;

    /** @brief Optional callback to resolve/register a preset as a native kdenlive/effect ID during drag start. */
    void setDragEffectIdResolver(std::function<QString(const QString &presetId)> resolver);

    void setFavorite(const QString &presetId, bool favorite);
    bool isFavorite(const QString &presetId) const;
    QStringList favoritePresetIds() const;

    bool saveCustomStack(const QString &stackName, const QStringList &presetIds);
    bool removeCustomStack(const QString &stackName);
    QVector<CustomEffectStack> customStacks() const;

    /** @brief Persist favourites and saved custom stacks to a JSON state file. */
    bool saveUserState(const QString &jsonPath) const;

    /** @brief Restore favourites and saved custom stacks from a JSON state file. */
    bool loadUserState(const QString &jsonPath);

    QByteArray buildMimePayloadForPreset(const QString &presetId) const;
    QString resolveDragEffectId(const QString &presetId) const;

public Q_SLOTS:
    void clearFilters();
    void refreshView();

Q_SIGNALS:
    void applyPresetRequested(const QString &presetId, const QJsonArray &mltFilters);
    void importArchiveRequested(const QString &zipPath);
    void presetSelected(const QString &presetId);
    void customStackApplied(const QString &stackName, const QJsonArray &combinedMltFilters);
    void prepareDragEffectId(const QString &presetId);

private Q_SLOTS:
    void onSearchOrFilterChanged();
    void onPresetSelectionChanged();
    void onApplyClicked();
    void onImportArchiveClicked();
    void onToggleFavoriteClicked();
    void onSaveCustomStackClicked();
    void onApplyCustomStackClicked();
    void onRemoveCustomStackClicked();
    void onApplyEditedParamsClicked();
    void onShowTechnicalInspectorToggled(bool show);

private:
    void buildUi();
    void populatePackFilter();
    void updateSummaryBanner();
    void updateInspectorForPreset(const PresetDisplayEntry *entry);
    void updateTechnicalInspectorVisibility();
    void refreshCustomStacksList();
    bool matchesActiveFilters(const PresetDisplayEntry &entry) const;
    QString selectedPresetId() const;
    static QString statusDisplayLabel(PresetCompatibilityStatus status);
    static QString statusBadgeColorHex(PresetCompatibilityStatus status);

    QVector<PresetDisplayEntry> m_entries;
    QHash<QString, int> m_indexById;
    QHash<QString, QJsonObject> m_rawJsonById;
    mutable QHash<QString, QPixmap> m_previewPixmapCache;
    QSet<QString> m_favorites;
    QVector<CustomEffectStack> m_customStacks;
    std::function<QString(const QString &)> m_dragEffectIdResolver;
    bool m_executableOnlyDefault = false;

    QString m_archiveName;
    QString m_archiveSha256;
    int m_archiveFileCount = 0;

    // UI Controls
    QLabel *m_summaryLabel = nullptr;
    QLineEdit *m_searchEdit = nullptr;
    QTimer *m_searchDebounceTimer = nullptr;
    QComboBox *m_packFilterCombo = nullptr;
    QComboBox *m_statusFilterCombo = nullptr;
    QCheckBox *m_favoritesOnlyCheck = nullptr;
    QCheckBox *m_showTechnicalInspectorCheck = nullptr;
    QPushButton *m_importBtn = nullptr;
    QPushButton *m_applyBtn = nullptr;
    QPushButton *m_favoriteBtn = nullptr;
    QPushButton *m_saveStackBtn = nullptr;

    PresetTreeWidget *m_presetTree = nullptr;
    QTabWidget *m_inspectorTabs = nullptr;
    QWidget *m_paramsTabWidget = nullptr;
    QLabel *m_previewImageLabel = nullptr;
    QTextBrowser *m_overviewBrowser = nullptr;
    QPlainTextEdit *m_paramEditorEdit = nullptr;
    QPushButton *m_applyEditedParamsBtn = nullptr;
    QPlainTextEdit *m_kdenliveXmlEdit = nullptr;
    QPlainTextEdit *m_rawXmlEdit = nullptr;

    QListWidget *m_customStacksList = nullptr;
    QPushButton *m_applyStackBtn = nullptr;
    QPushButton *m_removeStackBtn = nullptr;
};

} // namespace BangOnPro
