/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "PasteAttributesDialog.h"

#include <QCheckBox>
#include <QDialogButtonBox>
#include <QHBoxLayout>
#include <QLabel>
#include <QListWidget>
#include <QPushButton>
#include <QSignalBlocker>
#include <QVBoxLayout>
#include <QXmlStreamReader>

namespace BangOnPro {

namespace {

constexpr int kItemIndexRole = Qt::UserRole + 1;

QString extractXmlAttribute(const QString &openTag, const QString &attrName)
{
    QXmlStreamReader reader(openTag + QStringLiteral("</effect>"));
    while (!reader.atEnd()) {
        if (reader.readNext() == QXmlStreamReader::StartElement) {
            return reader.attributes().value(attrName).toString();
        }
    }
    return {};
}

} // namespace

PasteAttributesDialog::PasteAttributesDialog(const QVector<AttributeItem> &items, QWidget *parent)
    : QDialog(parent)
    , m_items(items)
{
    setObjectName(QStringLiteral("BangOnProPasteAttributesDialog"));
    setWindowTitle(QStringLiteral("Paste Attributes"));
    setMinimumWidth(360);
    resize(400, 340);
    buildUi();
    updateActionStates();
}

void PasteAttributesDialog::buildUi()
{
    auto *rootLayout = new QVBoxLayout(this);
    rootLayout->setContentsMargins(12, 12, 12, 12);
    rootLayout->setSpacing(8);

    m_summaryLabel = new QLabel(QStringLiteral("Choose which attributes and effects to paste onto the target clip(s):"), this);
    m_summaryLabel->setWordWrap(true);
    rootLayout->addWidget(m_summaryLabel);

    m_listWidget = new QListWidget(this);
    m_listWidget->setObjectName(QStringLiteral("pasteAttributesList"));
    m_listWidget->setAlternatingRowColors(true);

    for (int i = 0; i < m_items.size(); ++i) {
        const AttributeItem &attr = m_items.at(i);
        QString label = attr.displayName.isEmpty() ? attr.effectId : attr.displayName;
        if (attr.isIntrinsicTransform) {
            label = QStringLiteral("Motion / Transform (%1)").arg(label);
        }
        auto *listItem = new QListWidgetItem(label, m_listWidget);
        listItem->setFlags(listItem->flags() | Qt::ItemIsUserCheckable);
        listItem->setCheckState(attr.selected ? Qt::Checked : Qt::Unchecked);
        listItem->setData(kItemIndexRole, i);
        if (!attr.effectId.isEmpty()) {
            listItem->setToolTip(QStringLiteral("Effect ID: %1").arg(attr.effectId));
        }
        if (attr.isIntrinsicTransform) {
            QFont f = listItem->font();
            f.setBold(true);
            listItem->setFont(f);
        }
    }
    rootLayout->addWidget(m_listWidget, 1);

    auto *selectionRow = new QHBoxLayout();
    selectionRow->setContentsMargins(0, 0, 0, 0);
    selectionRow->setSpacing(6);

    m_selectAllBtn = new QPushButton(QStringLiteral("Select All"), this);
    m_selectAllBtn->setObjectName(QStringLiteral("pasteAttributesSelectAllBtn"));
    m_selectNoneBtn = new QPushButton(QStringLiteral("Select None"), this);
    m_selectNoneBtn->setObjectName(QStringLiteral("pasteAttributesSelectNoneBtn"));

    selectionRow->addWidget(m_selectAllBtn);
    selectionRow->addWidget(m_selectNoneBtn);
    selectionRow->addStretch(1);
    rootLayout->addLayout(selectionRow);

    m_clearExistingCheck = new QCheckBox(QStringLiteral("Clear existing effects before paste"), this);
    m_clearExistingCheck->setObjectName(QStringLiteral("pasteAttributesClearExistingCheck"));
    m_clearExistingCheck->setChecked(false);
    rootLayout->addWidget(m_clearExistingCheck);

    m_buttonBox = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, Qt::Horizontal, this);
    if (QPushButton *okBtn = m_buttonBox->button(QDialogButtonBox::Ok)) {
        okBtn->setText(QStringLiteral("Paste Attributes"));
        okBtn->setDefault(true);
    }
    rootLayout->addWidget(m_buttonBox);

    connect(m_listWidget, &QListWidget::itemChanged, this, &PasteAttributesDialog::onListItemChanged);
    connect(m_selectAllBtn, &QPushButton::clicked, this, &PasteAttributesDialog::selectAll);
    connect(m_selectNoneBtn, &QPushButton::clicked, this, &PasteAttributesDialog::selectNone);
    connect(m_buttonBox, &QDialogButtonBox::accepted, this, &QDialog::accept);
    connect(m_buttonBox, &QDialogButtonBox::rejected, this, &QDialog::reject);
}

QVector<AttributeItem> PasteAttributesDialog::items() const
{
    return m_items;
}

QVector<AttributeItem> PasteAttributesDialog::selectedItems() const
{
    QVector<AttributeItem> out;
    out.reserve(m_items.size());
    for (int i = 0; i < m_items.size(); ++i) {
        if (m_items.at(i).selected) {
            out.append(m_items.at(i));
        }
    }
    return out;
}

QStringList PasteAttributesDialog::selectedEffectXmls() const
{
    QStringList xmls;
    for (int i = 0; i < m_items.size(); ++i) {
        if (m_items.at(i).selected && !m_items.at(i).effectXml.trimmed().isEmpty()) {
            xmls.append(m_items.at(i).effectXml);
        }
    }
    return xmls;
}

QString PasteAttributesDialog::buildCombinedEffectsXml() const
{
    const QStringList xmls = selectedEffectXmls();
    if (xmls.isEmpty()) {
        return {};
    }
    QString out = QStringLiteral("<effects>");
    for (int i = 0; i < xmls.size(); ++i) {
        out.append(xmls.at(i));
    }
    out.append(QStringLiteral("</effects>"));
    return out;
}

bool PasteAttributesDialog::clearExistingBeforePaste() const
{
    return m_clearExistingCheck && m_clearExistingCheck->isChecked();
}

void PasteAttributesDialog::setClearExistingBeforePaste(bool clear)
{
    if (m_clearExistingCheck) {
        m_clearExistingCheck->setChecked(clear);
    }
}

void PasteAttributesDialog::setItemSelected(int index, bool selected)
{
    if (index < 0 || index >= m_items.size()) {
        return;
    }
    m_items[index].selected = selected;
    if (m_listWidget && index < m_listWidget->count()) {
        QSignalBlocker bk(m_listWidget);
        if (QListWidgetItem *item = m_listWidget->item(index)) {
            item->setCheckState(selected ? Qt::Checked : Qt::Unchecked);
        }
    }
    updateActionStates();
}

void PasteAttributesDialog::selectAll()
{
    QSignalBlocker bk(m_listWidget);
    for (int i = 0; i < m_items.size(); ++i) {
        m_items[i].selected = true;
        if (m_listWidget && i < m_listWidget->count()) {
            m_listWidget->item(i)->setCheckState(Qt::Checked);
        }
    }
    updateActionStates();
}

void PasteAttributesDialog::selectNone()
{
    QSignalBlocker bk(m_listWidget);
    for (int i = 0; i < m_items.size(); ++i) {
        m_items[i].selected = false;
        if (m_listWidget && i < m_listWidget->count()) {
            m_listWidget->item(i)->setCheckState(Qt::Unchecked);
        }
    }
    updateActionStates();
}

void PasteAttributesDialog::onListItemChanged(QListWidgetItem *item)
{
    if (!item) {
        return;
    }
    const int idx = item->data(kItemIndexRole).toInt();
    if (idx >= 0 && idx < m_items.size()) {
        m_items[idx].selected = (item->checkState() == Qt::Checked);
    }
    updateActionStates();
}

void PasteAttributesDialog::updateActionStates()
{
    int selectedCount = 0;
    for (int i = 0; i < m_items.size(); ++i) {
        if (m_items.at(i).selected) {
            ++selectedCount;
        }
    }
    if (m_buttonBox) {
        if (QPushButton *okBtn = m_buttonBox->button(QDialogButtonBox::Ok)) {
            okBtn->setEnabled(selectedCount > 0);
        }
    }
}

QVector<AttributeItem> PasteAttributesDialog::parseEffectsXml(const QString &effectsXml)
{
    QVector<AttributeItem> result;
    const QString openMarker = QStringLiteral("<effect");
    const QString closeMarker = QStringLiteral("</effect>");

    int pos = 0;
    while (pos < effectsXml.size()) {
        const int start = effectsXml.indexOf(openMarker, pos);
        if (start < 0) {
            break;
        }
        const int afterName = start + openMarker.size();
        if (afterName < effectsXml.size()) {
            const QChar nextCh = effectsXml.at(afterName);
            if (!nextCh.isSpace() && nextCh != QLatin1Char('>') && nextCh != QLatin1Char('/')) {
                // Skip tags like <effects> or <effectgroup>
                pos = afterName;
                continue;
            }
        }

        const int openEnd = effectsXml.indexOf(QLatin1Char('>'), afterName);
        if (openEnd < 0) {
            break;
        }

        const bool selfClosing = (openEnd > start && effectsXml.at(openEnd - 1) == QLatin1Char('/'));
        int endPos = -1;
        if (selfClosing) {
            endPos = openEnd + 1;
        } else {
            const int closeStart = effectsXml.indexOf(closeMarker, openEnd + 1);
            if (closeStart < 0) {
                break;
            }
            endPos = closeStart + closeMarker.size();
        }

        const QString elementXml = effectsXml.mid(start, endPos - start);
        const QString openTag = selfClosing ? (effectsXml.mid(start, openEnd - start - 1) + QLatin1Char('>')) : effectsXml.mid(start, openEnd - start + 1);
        const QString effectId = extractXmlAttribute(openTag, QStringLiteral("id"));
        const QString attrName = extractXmlAttribute(openTag, QStringLiteral("name"));

        AttributeItem item;
        item.effectId = effectId;
        item.displayName = attrName.isEmpty() ? effectId : attrName;
        item.effectXml = elementXml;
        item.isIntrinsicTransform = (effectId == QLatin1String("qtblend"));
        item.selected = true;
        result.append(item);

        pos = endPos;
    }

    return result;
}

} // namespace BangOnPro
