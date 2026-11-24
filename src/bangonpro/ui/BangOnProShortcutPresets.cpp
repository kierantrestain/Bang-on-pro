/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "BangOnProShortcutPresets.h"

#include <QAction>
#include <QBrush>
#include <QColor>
#include <QComboBox>
#include <QCoreApplication>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QKeySequenceEdit>
#include <QLabel>
#include <QLineEdit>
#include <QPushButton>
#include <QSet>
#include <QSettings>
#include <QTreeWidget>
#include <QTreeWidgetItem>
#include <QVBoxLayout>
#include <QWidget>
#include <utility>

namespace BangOnPro {

namespace {

constexpr int kColLabel = 0;
constexpr int kColCategory = 1;
constexpr int kColActionId = 2;
constexpr int kColShortcut = 3;
constexpr int kColStatus = 4;

constexpr int kActionIdRole = Qt::UserRole + 1;

struct PresetTriple
{
    const char *actionId;
    const char *category;
    const char *label;
    const char *premiereKey;
    const char *resolveKey;
    const char *kdenliveKey;
};

// Maps verified Kdenlive MainWindow / MonitorManager action IDs + Bang On Pro custom actions.
const QVector<PresetTriple> &presetDefinitions()
{
    static const QVector<PresetTriple> kDefs = {
        // Editorial Tools
        {"select_tool", "Tools", "Selection Tool", "V", "A", "S"},
        {"razor_tool", "Tools", "Razor / Blade Tool", "C", "B", "X"},
        {"spacer_tool", "Tools", "Track Select Forward / Spacer Tool", "A", "Alt+Y", "M"},
        {"ripple_tool", "Tools", "Ripple Edit Tool", "B", "T", ""},
        {"slip_tool", "Tools", "Slip Tool", "Y", "U", ""},
        {"multicam_tool", "Tools", "Multicam Switch Mode", "Shift+0", "Shift+M", ""},

        // Transport & Playback
        {"monitor_play", "Playback", "Play / Stop Toggle", "Space", "Space", "Space"},
        {"monitor_seek_backward", "Playback", "Shuttle Reverse (J)", "J", "J", "J"},
        {"monitor_rewind", "Playback", "Rewind Shuttle (Alias)", "J", "J", "J"},
        {"monitor_pause", "Playback", "Pause / Stop Shuttle (K)", "K", "K", "K"},
        {"monitor_seek_forward", "Playback", "Shuttle Forward (L)", "L", "L", "L"},
        {"monitor_forward", "Playback", "Forward Shuttle (Alias)", "L", "L", "L"},
        {"monitor_seek_backward-one-frame", "Playback", "Step Back 1 Frame", "Left", "Left", "Left"},
        {"monitor_seek_forward-one-frame", "Playback", "Step Forward 1 Frame", "Right", "Right", "Right"},
        {"monitor_seek_backward-one-second", "Playback", "Jump Back 1 Second", "Shift+Left", "Shift+Left", "Shift+Left"},
        {"monitor_seek_forward-one-second", "Playback", "Jump Forward 1 Second", "Shift+Right", "Shift+Right", "Shift+Right"},
        {"seek_start", "Playback", "Go to Sequence Start", "Home", "Home", "Ctrl+Home"},
        {"seek_end", "Playback", "Go to Sequence End", "End", "End", "Ctrl+End"},
        {"seek_clip_start", "Playback", "Previous Edit / Clip Start", "Up", "Up", "Alt+Left"},
        {"seek_clip_end", "Playback", "Next Edit / Clip End", "Down", "Down", "Alt+Right"},

        // Marking & 3-Point Editing
        {"mark_in", "Marking & 3-Point", "Mark In", "I", "I", "I"},
        {"mark_out", "Marking & 3-Point", "Mark Out", "O", "O", "O"},
        {"insert_to_in_point", "Marking & 3-Point", "Insert Clip to Timeline", ",", "F9", "V"},
        {"overwrite_to_in_point", "Marking & 3-Point", "Overwrite Clip to Timeline", ".", "F10", "B"},
        {"remove_extract", "Marking & 3-Point", "Extract Zone (Ripple Delete)", "'", "Shift+Backspace", "Shift+X"},
        {"remove_lift", "Marking & 3-Point", "Lift Zone (Leave Gap)", ";", "Backspace", "Z"},

        // Cutting & Timeline Editing
        {"cut_timeline_clip", "Editing", "Add Edit / Cut Clip at Playhead", "Ctrl+K", "Ctrl+B", "Shift+R"},
        {"cut_timeline_all_clips", "Editing", "Cut All Tracks at Playhead", "Ctrl+Shift+K", "Ctrl+Shift+B", "Ctrl+Shift+R"},
        {"delete_timeline_clip", "Editing", "Delete Selected Clip", "Del", "Del", "Del"},
        {"delete_space", "Editing", "Ripple Delete Gap", "Shift+Del", "Shift+Del", "Alt+Backspace"},
        {"group_clip", "Editing", "Group / Link Clips", "Ctrl+G", "Ctrl+Alt+L", "Ctrl+G"},
        {"ungroup_clip", "Editing", "Ungroup / Unlink Clips", "Ctrl+Shift+G", "Ctrl+Shift+L", "Ctrl+Shift+G"},
        {"clip_switch", "Editing", "Enable / Disable Clip", "Shift+E", "D", ""},
        {"snap", "Editing", "Toggle Timeline Snapping", "S", "N", ""},
        {"edit_item_speed", "Editing", "Speed / Duration", "Ctrl+R", "Ctrl+R", ""},
        {"edit_undo", "Editing", "Undo", "Ctrl+Z", "Ctrl+Z", "Ctrl+Z"},
        {"edit_redo", "Editing", "Redo", "Ctrl+Shift+Z", "Ctrl+Shift+Z", "Ctrl+Shift+Z"},
        {"edit_copy", "Editing", "Copy", "Ctrl+C", "Ctrl+C", "Ctrl+C"},
        {"edit_paste", "Editing", "Paste", "Ctrl+V", "Ctrl+V", "Ctrl+V"},

        // Markers & Keyframes
        {"add_marker_guide_quickly", "Markers & Keyframes", "Add Marker / Guide", "M", "M", "*"},
        {"add_guide", "Markers & Keyframes", "Add Named Guide", "Shift+M", "Alt+M", "G"},
        {"keyframe_add", "Markers & Keyframes", "Add / Remove Keyframe", "Ctrl+Alt+K", "Alt+K", ""},
        {"keyframe_previous", "Markers & Keyframes", "Previous Keyframe", "Shift+J", "[", ""},
        {"keyframe_next", "Markers & Keyframes", "Next Keyframe", "Shift+K", "]", ""},

        // View, Zoom & Export
        {"view_zoom_in", "View & Export", "Timeline Zoom In", "=", "Ctrl+=", "Ctrl++"},
        {"timeline_zoom_in", "View & Export", "Timeline Zoom In (Alias)", "=", "Ctrl+=", "Ctrl++"},
        {"view_zoom_out", "View & Export", "Timeline Zoom Out", "-", "Ctrl+-", "Ctrl+-"},
        {"timeline_zoom_out", "View & Export", "Timeline Zoom Out (Alias)", "-", "Ctrl+-", "Ctrl+-"},
        {"zoom_fit", "View & Export", "Zoom to Fit Sequence", "\\", "Shift+Z", "Ctrl+F"},
        {"switch_monitor", "View & Export", "Switch Source / Program Monitor", "Shift+2", "Q", "T"},
        {"project_render", "View & Export", "Export / Render Media…", "Ctrl+M", "Ctrl+Shift+E", "Ctrl+Return"},

        // Bang On Pro Workspaces & AI Controls
        {"bangonpro_toggle_ai", "Workspaces & AI", "Toggle AI Assistant Panel", "Alt+A", "Alt+A", "Alt+A"},
        {"bangonpro_workspace_edit", "Workspaces & AI", "Workspace: Edit", "Alt+Shift+1", "Alt+Shift+1", "Alt+Shift+1"},
        {"bangonpro_workspace_multicam", "Workspaces & AI", "Workspace: Multicam", "Alt+Shift+2", "Alt+Shift+2", "Alt+Shift+2"},
        {"bangonpro_workspace_ai_first_cut", "Workspaces & AI", "Workspace: AI First Cut", "Alt+Shift+3", "Alt+Shift+3", "Alt+Shift+3"},
        {"bangonpro_workspace_effects", "Workspaces & AI", "Workspace: Effects", "Alt+Shift+4", "Alt+Shift+4", "Alt+Shift+4"},
        {"bangonpro_workspace_colour", "Workspaces & AI", "Workspace: Colour", "Alt+Shift+5", "Alt+Shift+5", "Alt+Shift+5"},
        {"bangonpro_workspace_audio", "Workspaces & AI", "Workspace: Audio", "Alt+Shift+6", "Alt+Shift+6", "Alt+Shift+6"},
        {"bangonpro_workspace_captions", "Workspaces & AI", "Workspace: Captions", "Alt+Shift+7", "Alt+Shift+7", "Alt+Shift+7"},
        {"bangonpro_workspace_roto", "Workspaces & AI", "Workspace: Roto & 2D", "Alt+Shift+8", "Alt+Shift+8", "Alt+Shift+8"},
        {"bangonpro_workspace_export", "Workspaces & AI", "Workspace: Export", "Alt+Shift+9", "Alt+Shift+9", "Alt+Shift+9"},
        {"bangonpro_shortcut_presets", "Workspaces & AI", "Keyboard Shortcuts & Presets…", "Ctrl+Alt+Shift+K", "Ctrl+Alt+Shift+K", "Ctrl+Alt+Shift+K"},
    };
    return kDefs;
}

QKeySequence sequenceForTriple(const PresetTriple &def, const QString &presetId)
{
    const char *raw = def.premiereKey;
    if (presetId == QLatin1String(kShortcutPresetResolve)) {
        raw = def.resolveKey;
    } else if (presetId == QLatin1String(kShortcutPresetKdenlive)) {
        raw = def.kdenliveKey;
    }
    const QString text = QString::fromLatin1(raw);
    return text.isEmpty() ? QKeySequence() : QKeySequence(text);
}

QList<QAction *> collectActions(QObject *root)
{
    QList<QAction *> result;
    if (root == nullptr) {
        return result;
    }
    QSet<QAction *> seen;

    const QList<QAction *> childActions = root->findChildren<QAction *>();
    for (QAction *act : childActions) {
        if (act != nullptr && !seen.contains(act)) {
            seen.insert(act);
            result.append(act);
        }
    }

    if (auto *widget = qobject_cast<QWidget *>(root)) {
        const QList<QAction *> widgetActions = widget->actions();
        for (QAction *act : widgetActions) {
            if (act != nullptr && !seen.contains(act)) {
                seen.insert(act);
                result.append(act);
            }
        }
    }

    return result;
}

} // namespace

ShortcutPresetManager::ShortcutPresetManager(const QString &settingsOrg, const QString &settingsApp)
    : m_settingsOrg(settingsOrg)
    , m_settingsApp(settingsApp)
{
}

QStringList ShortcutPresetManager::availablePresetIds() const
{
    return {
        QString::fromLatin1(kShortcutPresetPremiere),
        QString::fromLatin1(kShortcutPresetResolve),
        QString::fromLatin1(kShortcutPresetKdenlive),
    };
}

QString ShortcutPresetManager::presetDisplayName(const QString &presetId) const
{
    if (presetId == QLatin1String(kShortcutPresetPremiere)) {
        return QCoreApplication::translate("BangOnProShortcuts", "Premiere Pro");
    }
    if (presetId == QLatin1String(kShortcutPresetResolve)) {
        return QCoreApplication::translate("BangOnProShortcuts", "DaVinci Resolve");
    }
    if (presetId == QLatin1String(kShortcutPresetKdenlive)) {
        return QCoreApplication::translate("BangOnProShortcuts", "Kdenlive Classic");
    }
    return presetId;
}

QString ShortcutPresetManager::presetDescription(const QString &presetId) const
{
    if (presetId == QLatin1String(kShortcutPresetPremiere)) {
        return QCoreApplication::translate("BangOnProShortcuts",
                                           "Industry Premiere Pro layout: V select, C razor, B ripple, Ctrl+K add edit, J/K/L shuttle, M marker, Ctrl+M export.");
    }
    if (presetId == QLatin1String(kShortcutPresetResolve)) {
        return QCoreApplication::translate("BangOnProShortcuts",
                                           "DaVinci Resolve Edit Page layout: A select, B blade, T trim, Ctrl+B split, N snapping, D disable clip, F9/F10 insert/overwrite.");
    }
    if (presetId == QLatin1String(kShortcutPresetKdenlive)) {
        return QCoreApplication::translate("BangOnProShortcuts",
                                           "Classic Kdenlive layout: S select, X razor, M spacer, Shift+R cut clip, V/B insert/overwrite, * quick marker, Ctrl+Return render.");
    }
    return {};
}

bool ShortcutPresetManager::isValidPresetId(const QString &presetId) const
{
    return availablePresetIds().contains(presetId);
}

QVector<ShortcutActionDescriptor> ShortcutPresetManager::actionsForPreset(const QString &presetId) const
{
    const QString resolved = isValidPresetId(presetId) ? presetId : QString::fromLatin1(kShortcutPresetPremiere);
    const QVector<PresetTriple> &defs = presetDefinitions();
    QVector<ShortcutActionDescriptor> list;
    list.reserve(defs.size());

    for (const PresetTriple &def : defs) {
        ShortcutActionDescriptor desc;
        desc.actionId = QString::fromLatin1(def.actionId);
        desc.category = QString::fromLatin1(def.category);
        desc.label = QString::fromLatin1(def.label);
        desc.defaultShortcut = sequenceForTriple(def, resolved);
        list.append(desc);
    }
    return list;
}

QMap<QString, QKeySequence> ShortcutPresetManager::presetShortcuts(const QString &presetId) const
{
    QMap<QString, QKeySequence> map;
    const QVector<ShortcutActionDescriptor> actions = actionsForPreset(presetId);
    for (const ShortcutActionDescriptor &desc : actions) {
        map.insert(desc.actionId, desc.defaultShortcut);
    }
    return map;
}

QMap<QString, QKeySequence> ShortcutPresetManager::effectiveShortcuts(const QString &presetId) const
{
    const QString resolved = presetId.isEmpty() ? activePresetId() : presetId;
    QMap<QString, QKeySequence> map = presetShortcuts(resolved);
    const QMap<QString, QKeySequence> overrides = customOverrides(resolved);
    for (auto it = overrides.constBegin(); it != overrides.constEnd(); ++it) {
        map.insert(it.key(), it.value());
    }
    return map;
}

QString ShortcutPresetManager::activePresetId() const
{
    QSettings settings(m_settingsOrg, m_settingsApp);
    const QString saved = settings.value(QStringLiteral("active_preset"), QString::fromLatin1(kShortcutPresetPremiere)).toString();
    return isValidPresetId(saved) ? saved : QString::fromLatin1(kShortcutPresetPremiere);
}

bool ShortcutPresetManager::setActivePresetId(const QString &presetId)
{
    if (!isValidPresetId(presetId)) {
        return false;
    }
    QSettings settings(m_settingsOrg, m_settingsApp);
    settings.setValue(QStringLiteral("active_preset"), presetId);
    settings.sync();
    return settings.status() == QSettings::NoError;
}

void ShortcutPresetManager::setCustomOverride(const QString &presetId, const QString &actionId, const QKeySequence &sequence)
{
    if (!isValidPresetId(presetId) || actionId.trimmed().isEmpty()) {
        return;
    }
    QSettings settings(m_settingsOrg, m_settingsApp);
    settings.beginGroup(QStringLiteral("overrides/%1").arg(presetId));
    settings.setValue(actionId, sequence.toString(QKeySequence::PortableText));
    settings.endGroup();
    settings.sync();
}

void ShortcutPresetManager::clearCustomOverride(const QString &presetId, const QString &actionId)
{
    if (!isValidPresetId(presetId) || actionId.trimmed().isEmpty()) {
        return;
    }
    QSettings settings(m_settingsOrg, m_settingsApp);
    settings.beginGroup(QStringLiteral("overrides/%1").arg(presetId));
    settings.remove(actionId);
    settings.endGroup();
    settings.sync();
}

QMap<QString, QKeySequence> ShortcutPresetManager::customOverrides(const QString &presetId) const
{
    QMap<QString, QKeySequence> overrides;
    if (!isValidPresetId(presetId)) {
        return overrides;
    }
    QSettings settings(m_settingsOrg, m_settingsApp);
    settings.beginGroup(QStringLiteral("overrides/%1").arg(presetId));
    const QStringList keys = settings.childKeys();
    for (const QString &actionId : keys) {
        const QString portable = settings.value(actionId).toString();
        overrides.insert(actionId, QKeySequence::fromString(portable, QKeySequence::PortableText));
    }
    settings.endGroup();
    return overrides;
}

void ShortcutPresetManager::resetPresetToDefaults(const QString &presetId)
{
    if (!isValidPresetId(presetId)) {
        return;
    }
    QSettings settings(m_settingsOrg, m_settingsApp);
    settings.beginGroup(QStringLiteral("overrides"));
    settings.remove(presetId);
    settings.endGroup();
    settings.sync();
}

QVector<ShortcutPresetManager::ShortcutConflict> ShortcutPresetManager::detectShortcutConflicts(const QString &presetId, bool ignoreKnownAliases) const
{
    static const QSet<QString> kKnownAliasIds = {
        QStringLiteral("monitor_rewind"),
        QStringLiteral("monitor_forward"),
        QStringLiteral("timeline_zoom_in"),
        QStringLiteral("timeline_zoom_out"),
    };

    const QMap<QString, QKeySequence> map = effectiveShortcuts(presetId);
    QMap<QString, QStringList> byPortableKey;
    for (auto it = map.constBegin(); it != map.constEnd(); ++it) {
        if (ignoreKnownAliases && kKnownAliasIds.contains(it.key())) {
            continue;
        }
        const QString portable = it.value().toString(QKeySequence::PortableText).trimmed();
        if (portable.isEmpty()) {
            continue;
        }
        byPortableKey[portable].append(it.key());
    }

    QVector<ShortcutConflict> conflicts;
    for (auto it = byPortableKey.constBegin(); it != byPortableKey.constEnd(); ++it) {
        if (it.value().size() > 1) {
            ShortcutConflict c;
            c.sequence = QKeySequence::fromString(it.key(), QKeySequence::PortableText);
            c.actionIds = it.value();
            c.actionIds.sort();
            conflicts.append(c);
        }
    }
    return conflicts;
}

int ShortcutPresetManager::applyPreset(QObject *actionCollectionOrWindow, const QString &presetId)
{
    const QString targetPreset = presetId.isEmpty() ? activePresetId() : presetId;
    if (!isValidPresetId(targetPreset)) {
        return 0;
    }

    setActivePresetId(targetPreset);

    if (actionCollectionOrWindow == nullptr) {
        return 0;
    }

    const QMap<QString, QKeySequence> bindings = effectiveShortcuts(targetPreset);
    const QList<QAction *> actions = collectActions(actionCollectionOrWindow);
    int updatedCount = 0;

    for (QAction *action : actions) {
        const QString id = action->objectName();
        if (id.isEmpty()) {
            continue;
        }
        auto it = bindings.constFind(id);
        if (it != bindings.constEnd()) {
            action->setShortcut(it.value());
            ++updatedCount;
        }
    }

    return updatedCount;
}

// =============================================================================
// ShortcutPresetDialog
// =============================================================================

ShortcutPresetDialog::ShortcutPresetDialog(QObject *targetWindowOrCollection, QWidget *parent, const QString &settingsOrg, const QString &settingsApp)
    : QDialog(parent)
    , m_manager(settingsOrg, settingsApp)
    , m_targetObject(targetWindowOrCollection)
{
    setObjectName(QStringLiteral("bangOnProShortcutPresetDialog"));
    setWindowTitle(tr("Bang On Pro — Keyboard Shortcuts & NLE Presets"));
    resize(780, 560);
    buildUi();

    selectPreset(m_manager.activePresetId());
}

void ShortcutPresetDialog::buildUi()
{
    auto *root = new QVBoxLayout(this);
    root->setContentsMargins(12, 12, 12, 12);
    root->setSpacing(10);

    // Preset selector row
    auto *topRow = new QHBoxLayout();
    topRow->setSpacing(8);

    auto *presetLabel = new QLabel(tr("Keymap Preset:"), this);
    m_presetCombo = new QComboBox(this);
    m_presetCombo->setObjectName(QStringLiteral("shortcutPresetCombo"));
    const QStringList presetIds = m_manager.availablePresetIds();
    for (const QString &id : presetIds) {
        m_presetCombo->addItem(m_manager.presetDisplayName(id), id);
    }

    m_searchEdit = new QLineEdit(this);
    m_searchEdit->setObjectName(QStringLiteral("shortcutSearchEdit"));
    m_searchEdit->setPlaceholderText(tr("Filter actions, categories, IDs, or keys…"));
    m_searchEdit->setClearButtonEnabled(true);

    topRow->addWidget(presetLabel);
    topRow->addWidget(m_presetCombo);
    topRow->addSpacing(12);
    topRow->addWidget(m_searchEdit, 1);
    root->addLayout(topRow);

    m_presetDescLabel = new QLabel(this);
    m_presetDescLabel->setObjectName(QStringLiteral("bangOnProMutedLabel"));
    m_presetDescLabel->setWordWrap(true);
    root->addWidget(m_presetDescLabel);

    // Action table
    m_tree = new QTreeWidget(this);
    m_tree->setObjectName(QStringLiteral("shortcutPresetTree"));
    m_tree->setColumnCount(5);
    m_tree->setHeaderLabels({tr("Action"), tr("Category"), tr("Action ID"), tr("Shortcut"), tr("Source")});
    m_tree->setRootIsDecorated(false);
    m_tree->setAlternatingRowColors(true);
    m_tree->setSelectionMode(QAbstractItemView::SingleSelection);
    m_tree->setSortingEnabled(false);

    QHeaderView *hdr = m_tree->header();
    hdr->setSectionResizeMode(kColLabel, QHeaderView::Stretch);
    hdr->setSectionResizeMode(kColCategory, QHeaderView::ResizeToContents);
    hdr->setSectionResizeMode(kColActionId, QHeaderView::ResizeToContents);
    hdr->setSectionResizeMode(kColShortcut, QHeaderView::ResizeToContents);
    hdr->setSectionResizeMode(kColStatus, QHeaderView::ResizeToContents);
    root->addWidget(m_tree, 1);

    // Editor controls row
    auto *editRow = new QHBoxLayout();
    editRow->setSpacing(6);

    m_selectedActionLabel = new QLabel(tr("Select an action to customize its shortcut"), this);
    m_selectedActionLabel->setMinimumWidth(180);

    m_keyEdit = new QKeySequenceEdit(this);
    m_keyEdit->setObjectName(QStringLiteral("shortcutKeySequenceEdit"));
    m_keyEdit->setEnabled(false);

    m_applyKeyButton = new QPushButton(tr("Set Shortcut"), this);
    m_applyKeyButton->setObjectName(QStringLiteral("applyShortcutKeyButton"));
    m_applyKeyButton->setEnabled(false);

    m_clearKeyButton = new QPushButton(tr("Clear"), this);
    m_clearKeyButton->setObjectName(QStringLiteral("clearShortcutKeyButton"));
    m_clearKeyButton->setEnabled(false);

    m_resetActionButton = new QPushButton(tr("Reset Action"), this);
    m_resetActionButton->setObjectName(QStringLiteral("resetSelectedShortcutButton"));
    m_resetActionButton->setEnabled(false);

    editRow->addWidget(m_selectedActionLabel, 1);
    editRow->addWidget(m_keyEdit);
    editRow->addWidget(m_applyKeyButton);
    editRow->addWidget(m_clearKeyButton);
    editRow->addWidget(m_resetActionButton);
    root->addLayout(editRow);

    m_conflictLabel = new QLabel(this);
    m_conflictLabel->setObjectName(QStringLiteral("shortcutConflictLabel"));
    m_conflictLabel->setStyleSheet(QStringLiteral("color: #E5A00D; font-size: 11px;"));
    m_conflictLabel->hide();
    root->addWidget(m_conflictLabel);

    // Footer buttons
    auto *footer = new QHBoxLayout();
    m_resetPresetButton = new QPushButton(tr("Reset Preset to Defaults"), this);
    m_resetPresetButton->setObjectName(QStringLiteral("resetPresetDefaultsButton"));

    m_cancelButton = new QPushButton(tr("Cancel"), this);
    m_okButton = new QPushButton(tr("Apply Preset && Close"), this);
    m_okButton->setObjectName(QStringLiteral("applyPresetDialogButton"));
    m_okButton->setProperty("bangOnProPrimary", true);
    m_okButton->setDefault(true);

    footer->addWidget(m_resetPresetButton);
    footer->addStretch(1);
    footer->addWidget(m_cancelButton);
    footer->addWidget(m_okButton);
    root->addLayout(footer);

    connect(m_presetCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, &ShortcutPresetDialog::handlePresetChanged);
    connect(m_searchEdit, &QLineEdit::textChanged, this, &ShortcutPresetDialog::handleSearchChanged);
    connect(m_tree, &QTreeWidget::itemSelectionChanged, this, &ShortcutPresetDialog::handleSelectionChanged);
    connect(m_keyEdit, &QKeySequenceEdit::keySequenceChanged, this,
            [this](const QKeySequence &seq) { updateConflictBanner(selectedActionId(), seq); });
    connect(m_applyKeyButton, &QPushButton::clicked, this, &ShortcutPresetDialog::handleApplyKeyClicked);
    connect(m_clearKeyButton, &QPushButton::clicked, this, &ShortcutPresetDialog::handleClearKeyClicked);
    connect(m_resetActionButton, &QPushButton::clicked, this, &ShortcutPresetDialog::handleResetSelectedClicked);
    connect(m_resetPresetButton, &QPushButton::clicked, this, &ShortcutPresetDialog::handleResetPresetClicked);
    connect(m_cancelButton, &QPushButton::clicked, this, &QDialog::reject);
    connect(m_okButton, &QPushButton::clicked, this, &ShortcutPresetDialog::handleAcceptClicked);
}

QString ShortcutPresetDialog::selectedPresetId() const
{
    return m_presetCombo->currentData().toString();
}

bool ShortcutPresetDialog::selectPreset(const QString &presetId)
{
    const int idx = m_presetCombo->findData(presetId);
    if (idx < 0) {
        return false;
    }
    m_presetCombo->setCurrentIndex(idx);
    populateTree();
    return true;
}

void ShortcutPresetDialog::setSearchFilter(const QString &query)
{
    m_searchEdit->setText(query);
}

QTreeWidget *ShortcutPresetDialog::shortcutTreeWidget() const
{
    return m_tree;
}

ShortcutPresetManager &ShortcutPresetDialog::manager()
{
    return m_manager;
}

void ShortcutPresetDialog::populateTree()
{
    const QString presetId = selectedPresetId();
    m_presetDescLabel->setText(m_manager.presetDescription(presetId));

    const QVector<ShortcutActionDescriptor> actions = m_manager.actionsForPreset(presetId);
    const QMap<QString, QKeySequence> overrides = m_manager.customOverrides(presetId);

    m_tree->clear();
    const QColor customColor(255, 176, 124);

    for (const ShortcutActionDescriptor &desc : actions) {
        auto *item = new QTreeWidgetItem(m_tree);
        const bool isCustom = overrides.contains(desc.actionId);
        const QKeySequence effectiveSeq = isCustom ? overrides.value(desc.actionId) : desc.defaultShortcut;

        item->setText(kColLabel, desc.label);
        item->setText(kColCategory, desc.category);
        item->setText(kColActionId, desc.actionId);
        item->setText(kColShortcut, effectiveSeq.isEmpty() ? tr("—") : effectiveSeq.toString(QKeySequence::NativeText));
        item->setText(kColStatus, isCustom ? tr("Custom") : tr("Default"));
        item->setData(kColLabel, kActionIdRole, desc.actionId);

        if (isCustom) {
            item->setForeground(kColShortcut, QBrush(customColor));
            item->setForeground(kColStatus, QBrush(customColor));
        }
    }

    applyFilterToTree();
    handleSelectionChanged();
}

void ShortcutPresetDialog::applyFilterToTree()
{
    const QString q = m_searchEdit->text().trimmed();
    for (int i = 0; i < m_tree->topLevelItemCount(); ++i) {
        QTreeWidgetItem *item = m_tree->topLevelItem(i);
        if (q.isEmpty()) {
            item->setHidden(false);
            continue;
        }
        bool match = false;
        for (int col = 0; col < m_tree->columnCount(); ++col) {
            if (item->text(col).contains(q, Qt::CaseInsensitive)) {
                match = true;
                break;
            }
        }
        item->setHidden(!match);
    }
}

void ShortcutPresetDialog::updateConflictBanner(const QString &actionId, const QKeySequence &candidate)
{
    if (actionId.isEmpty() || candidate.isEmpty()) {
        m_conflictLabel->hide();
        return;
    }

    const QMap<QString, QKeySequence> effective = m_manager.effectiveShortcuts(selectedPresetId());
    QStringList conflicts;
    for (auto it = effective.constBegin(); it != effective.constEnd(); ++it) {
        if (it.key() != actionId && !it.value().isEmpty() && it.value() == candidate) {
            conflicts.append(it.key());
        }
    }

    if (conflicts.isEmpty()) {
        m_conflictLabel->hide();
    } else {
        m_conflictLabel->setText(tr("Notice: %1 is also bound to %2 in this preset.")
                                     .arg(candidate.toString(QKeySequence::NativeText), conflicts.join(QStringLiteral(", "))));
        m_conflictLabel->show();
    }
}

QString ShortcutPresetDialog::selectedActionId() const
{
    QTreeWidgetItem *item = m_tree->currentItem();
    return item ? item->data(kColLabel, kActionIdRole).toString() : QString();
}

void ShortcutPresetDialog::handlePresetChanged(int)
{
    populateTree();
}

void ShortcutPresetDialog::handleSearchChanged(const QString &)
{
    applyFilterToTree();
}

void ShortcutPresetDialog::handleSelectionChanged()
{
    QTreeWidgetItem *item = m_tree->currentItem();
    const bool hasSelection = item != nullptr;
    m_keyEdit->setEnabled(hasSelection);
    m_applyKeyButton->setEnabled(hasSelection);
    m_clearKeyButton->setEnabled(hasSelection);
    m_resetActionButton->setEnabled(hasSelection);

    if (!hasSelection) {
        m_selectedActionLabel->setText(tr("Select an action to customize its shortcut"));
        m_keyEdit->clear();
        m_conflictLabel->hide();
        return;
    }

    const QString actionId = item->data(kColLabel, kActionIdRole).toString();
    const QMap<QString, QKeySequence> effective = m_manager.effectiveShortcuts(selectedPresetId());
    m_selectedActionLabel->setText(tr("Edit: %1 (%2)").arg(item->text(kColLabel), actionId));
    m_keyEdit->setKeySequence(effective.value(actionId));
    updateConflictBanner(actionId, effective.value(actionId));
}

void ShortcutPresetDialog::handleApplyKeyClicked()
{
    const QString actionId = selectedActionId();
    if (actionId.isEmpty()) {
        return;
    }
    m_manager.setCustomOverride(selectedPresetId(), actionId, m_keyEdit->keySequence());
    populateTree();
}

void ShortcutPresetDialog::handleClearKeyClicked()
{
    const QString actionId = selectedActionId();
    if (actionId.isEmpty()) {
        return;
    }
    m_manager.setCustomOverride(selectedPresetId(), actionId, QKeySequence());
    populateTree();
}

void ShortcutPresetDialog::handleResetSelectedClicked()
{
    const QString actionId = selectedActionId();
    if (actionId.isEmpty()) {
        return;
    }
    m_manager.clearCustomOverride(selectedPresetId(), actionId);
    populateTree();
}

void ShortcutPresetDialog::handleResetPresetClicked()
{
    m_manager.resetPresetToDefaults(selectedPresetId());
    populateTree();
}

void ShortcutPresetDialog::handleAcceptClicked()
{
    const QString presetId = selectedPresetId();
    m_manager.setActivePresetId(presetId);
    if (m_targetObject) {
        m_manager.applyPreset(m_targetObject.data(), presetId);
    }
    Q_EMIT presetApplied(presetId, m_manager.effectiveShortcuts(presetId));
    accept();
}

} // namespace BangOnPro
