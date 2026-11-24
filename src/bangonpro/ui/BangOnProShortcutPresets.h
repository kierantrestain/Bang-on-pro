/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include <QDialog>
#include <QKeySequence>
#include <QMap>
#include <QPointer>
#include <QString>
#include <QStringList>
#include <QVector>

class QComboBox;
class QKeySequenceEdit;
class QLabel;
class QLineEdit;
class QObject;
class QPushButton;
class QTreeWidget;
class QTreeWidgetItem;

namespace BangOnPro {

inline constexpr const char *kShortcutPresetPremiere = "premiere";
inline constexpr const char *kShortcutPresetResolve = "resolve";
inline constexpr const char *kShortcutPresetKdenlive = "kdenlive";

/**
 * @brief Metadata and default binding for a single configurable editor action.
 */
struct ShortcutActionDescriptor
{
    QString actionId;
    QString category;
    QString label;
    QKeySequence defaultShortcut;
};

/**
 * @brief Manages configurable NLE keyboard shortcut presets (Premiere Pro,
 * DaVinci Resolve, Kdenlive Classic) and per-preset user custom overrides
 * persisted in QSettings.
 *
 * Operates directly on QMainWindow / KActionCollection QAction children by
 * objectName() without requiring extra KF5 XMLGUI link dependencies.
 */
class ShortcutPresetManager
{
public:
    explicit ShortcutPresetManager(const QString &settingsOrg = QStringLiteral("BangOnPro"),
                                   const QString &settingsApp = QStringLiteral("ShortcutPresets"));

    /** @brief Ordered list of supported preset IDs ("premiere", "resolve", "kdenlive"). */
    QStringList availablePresetIds() const;

    /** @brief Human-readable preset title (e.g. "Premiere Pro"). */
    QString presetDisplayName(const QString &presetId) const;

    /** @brief Short editorial summary of the keymap philosophy. */
    QString presetDescription(const QString &presetId) const;

    /** @brief Returns true if @p presetId is one of the built-in presets. */
    bool isValidPresetId(const QString &presetId) const;

    /** @brief Full ordered list of action descriptors for @p presetId. */
    QVector<ShortcutActionDescriptor> actionsForPreset(const QString &presetId) const;

    /** @brief Built-in default actionId -> QKeySequence map for @p presetId. */
    QMap<QString, QKeySequence> presetShortcuts(const QString &presetId) const;

    /**
     * @brief Effective actionId -> QKeySequence map merging built-in preset
     * defaults with any saved user custom overrides.
     */
    QMap<QString, QKeySequence> effectiveShortcuts(const QString &presetId = QString()) const;

    /** @brief Active persisted preset ID (defaults to "premiere"). */
    QString activePresetId() const;

    /** @brief Persist the active preset ID in QSettings. */
    bool setActivePresetId(const QString &presetId);

    /** @brief Persist a user shortcut override for @p actionId within @p presetId. */
    void setCustomOverride(const QString &presetId, const QString &actionId, const QKeySequence &sequence);

    /** @brief Remove a single user override, restoring the preset default. */
    void clearCustomOverride(const QString &presetId, const QString &actionId);

    /** @brief All saved custom overrides for @p presetId. */
    QMap<QString, QKeySequence> customOverrides(const QString &presetId) const;

    /** @brief Clear all custom overrides for @p presetId, restoring built-in defaults. */
    void resetPresetToDefaults(const QString &presetId);

    /**
     * @brief Applies the effective shortcuts for @p presetId (or activePresetId() if empty)
     * to every matching QAction found on @p actionCollectionOrWindow (QMainWindow or
     * KActionCollection), matching by QAction::objectName().
     *
     * Also updates activePresetId() to @p presetId when valid.
     *
     * @return Number of QAction instances updated.
     */
    int applyPreset(QObject *actionCollectionOrWindow, const QString &presetId = QString());

private:
    QString m_settingsOrg;
    QString m_settingsApp;
};

/**
 * @brief Interactive dark studio dialog for switching NLE keyboard shortcut presets,
 * searching/filtering actions, capturing custom key bindings, detecting conflicts,
 * and resetting to preset defaults.
 */
class ShortcutPresetDialog : public QDialog
{
    Q_OBJECT

public:
    explicit ShortcutPresetDialog(QObject *targetWindowOrCollection = nullptr, QWidget *parent = nullptr,
                                  const QString &settingsOrg = QStringLiteral("BangOnPro"),
                                  const QString &settingsApp = QStringLiteral("ShortcutPresets"));
    ~ShortcutPresetDialog() override = default;

    /** @brief Currently selected preset ID in the dialog. */
    QString selectedPresetId() const;

    /** @brief Programmatically switch the selected preset in the dialog. */
    bool selectPreset(const QString &presetId);

    /** @brief Filter visible action rows by text query. */
    void setSearchFilter(const QString &query);

    /** @brief Access the underlying action tree widget (for inspection/testing). */
    QTreeWidget *shortcutTreeWidget() const;

    /** @brief Access the manager instance backed by QSettings. */
    ShortcutPresetManager &manager();

Q_SIGNALS:
    void presetApplied(const QString &presetId, const QMap<QString, QKeySequence> &shortcuts);

private Q_SLOTS:
    void handlePresetChanged(int index);
    void handleSearchChanged(const QString &query);
    void handleSelectionChanged();
    void handleApplyKeyClicked();
    void handleClearKeyClicked();
    void handleResetSelectedClicked();
    void handleResetPresetClicked();
    void handleAcceptClicked();

private:
    void buildUi();
    void populateTree();
    void applyFilterToTree();
    void updateConflictBanner(const QString &actionId, const QKeySequence &candidate);
    QString selectedActionId() const;

    ShortcutPresetManager m_manager;
    QPointer<QObject> m_targetObject;

    QComboBox *m_presetCombo = nullptr;
    QLabel *m_presetDescLabel = nullptr;
    QLineEdit *m_searchEdit = nullptr;
    QTreeWidget *m_tree = nullptr;
    QLabel *m_selectedActionLabel = nullptr;
    QKeySequenceEdit *m_keyEdit = nullptr;
    QPushButton *m_applyKeyButton = nullptr;
    QPushButton *m_clearKeyButton = nullptr;
    QPushButton *m_resetActionButton = nullptr;
    QPushButton *m_resetPresetButton = nullptr;
    QLabel *m_conflictLabel = nullptr;
    QPushButton *m_okButton = nullptr;
    QPushButton *m_cancelButton = nullptr;
};

} // namespace BangOnPro
