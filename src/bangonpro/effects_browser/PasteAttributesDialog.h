/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include <QDialog>
#include <QString>
#include <QStringList>
#include <QVector>

class QCheckBox;
class QDialogButtonBox;
class QLabel;
class QListWidget;
class QListWidgetItem;
class QPushButton;

namespace BangOnPro {

struct AttributeItem
{
    QString effectId;
    QString displayName;
    QString effectXml;
    bool isIntrinsicTransform = false;
    bool selected = true;
};

/**
 * @brief Compact dialog for selectively pasting Motion/Transform and individual
 * effects from a copied clip's effect stack onto one or more target clips.
 */
class PasteAttributesDialog : public QDialog
{
    Q_OBJECT

public:
    explicit PasteAttributesDialog(const QVector<AttributeItem> &items, QWidget *parent = nullptr);
    ~PasteAttributesDialog() override = default;

    /** @brief Return all attribute items with their current checked state. */
    QVector<AttributeItem> items() const;

    /** @brief Return only the attribute items currently checked by the user. */
    QVector<AttributeItem> selectedItems() const;

    /** @brief Return the verbatim effect XML blocks for all checked items. */
    QStringList selectedEffectXmls() const;

    /** @brief Build a combined `<effects>...</effects>` XML document for the checked items. */
    QString buildCombinedEffectsXml() const;

    /** @brief Whether existing effects on the destination clip should be cleared before pasting. */
    bool clearExistingBeforePaste() const;
    void setClearExistingBeforePaste(bool clear);

    /** @brief Programmatically toggle a single item by index (useful for tests/automation). */
    void setItemSelected(int index, bool selected);

    /** @brief Parse a Kdenlive `<effects>...</effects>` XML string into AttributeItem entries. */
    static QVector<AttributeItem> parseEffectsXml(const QString &effectsXml);

public Q_SLOTS:
    void selectAll();
    void selectNone();

private Q_SLOTS:
    void onListItemChanged(QListWidgetItem *item);
    void updateActionStates();

private:
    void buildUi();

    QVector<AttributeItem> m_items;
    QLabel *m_summaryLabel = nullptr;
    QListWidget *m_listWidget = nullptr;
    QPushButton *m_selectAllBtn = nullptr;
    QPushButton *m_selectNoneBtn = nullptr;
    QCheckBox *m_clearExistingCheck = nullptr;
    QDialogButtonBox *m_buttonBox = nullptr;
};

} // namespace BangOnPro
