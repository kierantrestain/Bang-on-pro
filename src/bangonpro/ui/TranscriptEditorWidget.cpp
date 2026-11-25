/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "TranscriptEditorWidget.h"

#include <QBrush>
#include <QColor>
#include <QComboBox>
#include <QFile>
#include <QFileDialog>
#include <QFont>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QInputDialog>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QLabel>
#include <QLineEdit>
#include <QPushButton>
#include <QSet>
#include <QTreeWidget>
#include <QTreeWidgetItem>
#include <QVBoxLayout>
#include <cmath>
#include <utility>

namespace BangOnPro {

namespace {

constexpr int kColTimecode = 0;
constexpr int kColSpeaker = 1;
constexpr int kColConfidence = 2;
constexpr int kColText = 3;
constexpr int kColStatus = 4;

constexpr int kSegmentIndexRole = Qt::UserRole + 1;
constexpr int kSegmentIdRole = Qt::UserRole + 2;

} // namespace

TranscriptEditorWidget::TranscriptEditorWidget(QWidget *parent)
    : QWidget(parent)
{
    setObjectName(QStringLiteral("bangOnProTranscriptEditor"));
    buildUi();
    updateSummaryAndButtons();
}

void TranscriptEditorWidget::buildUi()
{
    auto *root = new QVBoxLayout(this);
    root->setContentsMargins(6, 6, 6, 6);
    root->setSpacing(6);

    // Header & live summary
    auto *headerRow = new QHBoxLayout();
    auto *titleLabel = new QLabel(tr("Transcript & Text-Based Cut"), this);
    titleLabel->setObjectName(QStringLiteral("bangOnProSectionLabel"));
    m_summaryLabel = new QLabel(tr("No transcript loaded"), this);
    m_summaryLabel->setObjectName(QStringLiteral("bangOnProMutedLabel"));
    headerRow->addWidget(titleLabel);
    headerRow->addStretch(1);
    headerRow->addWidget(m_summaryLabel);
    root->addLayout(headerRow);

    // Filter + Speaker controls
    auto *filterRow = new QHBoxLayout();
    filterRow->setSpacing(6);

    m_searchEdit = new QLineEdit(this);
    m_searchEdit->setObjectName(QStringLiteral("transcriptSearchEdit"));
    m_searchEdit->setPlaceholderText(tr("Search dialogue, speaker, or timecode…"));
    m_searchEdit->setClearButtonEnabled(true);

    m_speakerFilterCombo = new QComboBox(this);
    m_speakerFilterCombo->setObjectName(QStringLiteral("speakerFilterCombo"));
    m_speakerFilterCombo->addItem(tr("All Speakers"), QString());

    m_renameSpeakerButton = new QPushButton(tr("Rename speaker…"), this);
    m_renameSpeakerButton->setObjectName(QStringLiteral("renameSpeakerButton"));
    m_renameSpeakerButton->setToolTip(tr("Rename all segments for the selected speaker"));

    filterRow->addWidget(m_searchEdit, 1);
    filterRow->addWidget(m_speakerFilterCombo);
    filterRow->addWidget(m_renameSpeakerButton);
    root->addLayout(filterRow);

    // Segments table/tree
    m_tree = new QTreeWidget(this);
    m_tree->setObjectName(QStringLiteral("transcriptTreeWidget"));
    m_tree->setColumnCount(5);
    m_tree->setHeaderLabels({tr("Timecode"), tr("Speaker"), tr("Conf"), tr("Transcript Text"), tr("Status")});
    m_tree->setRootIsDecorated(false);
    m_tree->setAlternatingRowColors(true);
    m_tree->setSelectionMode(QAbstractItemView::SingleSelection);
    m_tree->setEditTriggers(QAbstractItemView::EditKeyPressed | QAbstractItemView::SelectedClicked);
    m_tree->setMinimumHeight(140);

    QHeaderView *hdr = m_tree->header();
    hdr->setSectionResizeMode(kColTimecode, QHeaderView::ResizeToContents);
    hdr->setSectionResizeMode(kColSpeaker, QHeaderView::ResizeToContents);
    hdr->setSectionResizeMode(kColConfidence, QHeaderView::ResizeToContents);
    hdr->setSectionResizeMode(kColText, QHeaderView::Stretch);
    hdr->setSectionResizeMode(kColStatus, QHeaderView::ResizeToContents);
    root->addWidget(m_tree, 1);

    // Action row
    auto *actionRow = new QHBoxLayout();
    actionRow->setSpacing(6);

    m_loadButton = new QPushButton(tr("Load JSON…"), this);
    m_loadButton->setObjectName(QStringLiteral("loadTranscriptButton"));

    m_toggleCutButton = new QPushButton(tr("Cut / restore segment"), this);
    m_toggleCutButton->setObjectName(QStringLiteral("toggleCutButton"));
    m_toggleCutButton->setToolTip(tr("Mark or restore the selected transcript segment for text-based rough cutting"));

    m_applyEditsButton = new QPushButton(tr("Apply transcript edits to timeline"), this);
    m_applyEditsButton->setObjectName(QStringLiteral("applyTranscriptButton"));
    m_applyEditsButton->setProperty("bangOnProPrimary", true);

    actionRow->addWidget(m_loadButton);
    actionRow->addWidget(m_toggleCutButton);
    actionRow->addStretch(1);
    actionRow->addWidget(m_applyEditsButton);
    root->addLayout(actionRow);

    connect(m_searchEdit, &QLineEdit::textChanged, this, &TranscriptEditorWidget::handleFilterChanged);
    connect(m_speakerFilterCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, &TranscriptEditorWidget::handleFilterChanged);
    connect(m_renameSpeakerButton, &QPushButton::clicked, this, &TranscriptEditorWidget::handleRenameSpeakerClicked);
    connect(m_tree, &QTreeWidget::itemClicked, this, &TranscriptEditorWidget::handleItemClicked);
    connect(m_tree, &QTreeWidget::itemDoubleClicked, this, &TranscriptEditorWidget::handleItemDoubleClicked);
    connect(m_tree, &QTreeWidget::itemChanged, this, &TranscriptEditorWidget::handleItemChanged);
    connect(m_tree, &QTreeWidget::itemSelectionChanged, this, &TranscriptEditorWidget::handleSelectionChanged);
    connect(m_loadButton, &QPushButton::clicked, this, &TranscriptEditorWidget::handleLoadFileClicked);
    connect(m_toggleCutButton, &QPushButton::clicked, this, &TranscriptEditorWidget::handleToggleCutClicked);
    connect(m_applyEditsButton, &QPushButton::clicked, this, &TranscriptEditorWidget::handleApplyEditsClicked);
}

bool TranscriptEditorWidget::loadFromFile(const QString &filePath, QString *errorMessage)
{
    QFile file(filePath);
    if (!file.open(QIODevice::ReadOnly)) {
        if (errorMessage != nullptr) {
            *errorMessage = tr("Unable to open transcript file: %1").arg(filePath);
        }
        return false;
    }

    QJsonParseError pe{};
    const QJsonDocument doc = QJsonDocument::fromJson(file.readAll(), &pe);
    if (pe.error != QJsonParseError::NoError || !doc.isObject()) {
        if (errorMessage != nullptr) {
            *errorMessage = tr("Invalid transcript JSON: %1").arg(pe.errorString());
        }
        return false;
    }

    return loadFromJson(doc.object(), errorMessage);
}

bool TranscriptEditorWidget::loadFromJson(const QJsonObject &json, QString *errorMessage)
{
    QJsonArray rawSegments;
    if (json.contains(QStringLiteral("segments")) && json.value(QStringLiteral("segments")).isArray()) {
        rawSegments = json.value(QStringLiteral("segments")).toArray();
    } else if (json.contains(QStringLiteral("transcript_segments")) && json.value(QStringLiteral("transcript_segments")).isArray()) {
        rawSegments = json.value(QStringLiteral("transcript_segments")).toArray();
    } else {
        if (errorMessage != nullptr) {
            *errorMessage = tr("Transcript JSON must contain a 'segments' or 'transcript_segments' array.");
        }
        return false;
    }

    // Parse optional frame rate metadata
    if (json.contains(QStringLiteral("fps_num"))) {
        const int num = json.value(QStringLiteral("fps_num")).toInt(25);
        const int den = json.value(QStringLiteral("fps_den")).toInt(1);
        setFrameRate(num, den);
    } else if (json.contains(QStringLiteral("fps"))) {
        const double fps = json.value(QStringLiteral("fps")).toDouble(25.0);
        if (fps > 0.0) {
            setFrameRate(static_cast<int>(std::llround(fps * 1000.0)), 1000);
        }
    }

    m_sourceMetadata = json;
    m_sourceMetadata.remove(QStringLiteral("segments"));
    m_sourceMetadata.remove(QStringLiteral("transcript_segments"));
    m_segments.clear();
    m_speakerRenames.clear();
    m_segments.reserve(rawSegments.size());

    for (int i = 0; i < rawSegments.size(); ++i) {
        const QJsonObject obj = rawSegments.at(i).toObject();
        TranscriptSegmentItem seg;

        seg.segmentId = obj.value(QStringLiteral("segment_id")).toString();
        if (seg.segmentId.isEmpty()) {
            seg.segmentId = obj.value(QStringLiteral("id")).toString();
        }
        if (seg.segmentId.isEmpty()) {
            seg.segmentId = QStringLiteral("seg_%1").arg(i + 1, 4, 10, QLatin1Char('0'));
        }

        seg.startSec = obj.contains(QStringLiteral("start_sec")) ? obj.value(QStringLiteral("start_sec")).toDouble(0.0)
                                                                 : obj.value(QStringLiteral("start")).toDouble(0.0);
        seg.endSec = obj.contains(QStringLiteral("end_sec")) ? obj.value(QStringLiteral("end_sec")).toDouble(seg.startSec)
                                                             : obj.value(QStringLiteral("end")).toDouble(seg.startSec);

        seg.startFrame = obj.contains(QStringLiteral("start_frame")) ? obj.value(QStringLiteral("start_frame")).toInt(secToFrame(seg.startSec))
                                                                     : secToFrame(seg.startSec);
        seg.endFrame =
            obj.contains(QStringLiteral("end_frame")) ? obj.value(QStringLiteral("end_frame")).toInt(secToFrame(seg.endSec)) : secToFrame(seg.endSec);

        seg.speakerId = obj.value(QStringLiteral("speaker_id")).toString();
        if (seg.speakerId.isEmpty()) {
            seg.speakerId = obj.value(QStringLiteral("speaker")).toString(QStringLiteral("Speaker 1"));
        }
        seg.cameraId = obj.value(QStringLiteral("camera_id")).toString();
        seg.text = obj.value(QStringLiteral("text")).toString();
        seg.confidence = qBound(0.0, obj.value(QStringLiteral("confidence")).toDouble(1.0), 1.0);
        seg.isLowConfidence = obj.value(QStringLiteral("is_low_confidence")).toBool(seg.confidence < m_reviewThreshold);
        seg.isOverlap = obj.value(QStringLiteral("is_overlap")).toBool(false) || obj.value(QStringLiteral("overlap")).toBool(false) ||
                        obj.value(QStringLiteral("crosstalk")).toBool(false);
        seg.isCut = obj.value(QStringLiteral("cut")).toBool(false) || obj.value(QStringLiteral("deleted")).toBool(false);
        seg.isEdited = obj.value(QStringLiteral("edited")).toBool(false);
        seg.sectionLabel = obj.value(QStringLiteral("section_label")).toString();

        m_segments.append(seg);
    }

    detectTemporalOverlaps();
    rebuildSpeakerCombo();
    rebuildTree();
    updateSummaryAndButtons();
    return true;
}

QJsonObject TranscriptEditorWidget::exportEditedTranscriptJson() const
{
    QJsonObject out = m_sourceMetadata;
    out.insert(QStringLiteral("schema_version"), QStringLiteral("1.0"));
    out.insert(QStringLiteral("fps_num"), m_fpsNum);
    out.insert(QStringLiteral("fps_den"), m_fpsDen);

    QJsonArray segArray;
    QJsonArray keptArray;
    QJsonArray deletedIds;
    int editedCount = 0;
    int cutCount = 0;

    for (const TranscriptSegmentItem &seg : m_segments) {
        QJsonObject s;
        s.insert(QStringLiteral("segment_id"), seg.segmentId);
        s.insert(QStringLiteral("start_sec"), seg.startSec);
        s.insert(QStringLiteral("end_sec"), seg.endSec);
        s.insert(QStringLiteral("start_frame"), seg.startFrame);
        s.insert(QStringLiteral("end_frame"), seg.endFrame);
        s.insert(QStringLiteral("speaker_id"), seg.speakerId);
        if (!seg.cameraId.isEmpty()) {
            s.insert(QStringLiteral("camera_id"), seg.cameraId);
        }
        s.insert(QStringLiteral("text"), seg.text);
        s.insert(QStringLiteral("confidence"), seg.confidence);
        s.insert(QStringLiteral("is_low_confidence"), seg.isLowConfidence);
        s.insert(QStringLiteral("is_overlap"), seg.isOverlap);
        s.insert(QStringLiteral("cut"), seg.isCut);
        s.insert(QStringLiteral("deleted"), seg.isCut);
        s.insert(QStringLiteral("edited"), seg.isEdited);
        if (!seg.sectionLabel.isEmpty()) {
            s.insert(QStringLiteral("section_label"), seg.sectionLabel);
        }

        segArray.append(s);
        if (seg.isCut) {
            deletedIds.append(seg.segmentId);
            ++cutCount;
        } else {
            keptArray.append(s);
        }
        if (seg.isEdited) {
            ++editedCount;
        }
    }

    QJsonObject renameMap;
    for (auto it = m_speakerRenames.constBegin(); it != m_speakerRenames.constEnd(); ++it) {
        renameMap.insert(it.key(), it.value());
    }

    out.insert(QStringLiteral("segments"), segArray);
    out.insert(QStringLiteral("kept_segments"), keptArray);
    out.insert(QStringLiteral("deleted_segment_ids"), deletedIds);
    out.insert(QStringLiteral("speaker_renames"), renameMap);
    out.insert(QStringLiteral("total_segments"), m_segments.size());
    out.insert(QStringLiteral("cut_count"), cutCount);
    out.insert(QStringLiteral("edited_count"), editedCount);
    return out;
}

void TranscriptEditorWidget::clear()
{
    m_segments.clear();
    m_speakerRenames.clear();
    m_sourceMetadata = QJsonObject{};
    m_tree->clear();
    rebuildSpeakerCombo();
    updateSummaryAndButtons();
}

int TranscriptEditorWidget::segmentCount() const
{
    return m_segments.size();
}

int TranscriptEditorWidget::cutSegmentCount() const
{
    int count = 0;
    for (const TranscriptSegmentItem &seg : m_segments) {
        if (seg.isCut) {
            ++count;
        }
    }
    return count;
}

int TranscriptEditorWidget::overlapSegmentCount() const
{
    int count = 0;
    for (const TranscriptSegmentItem &seg : m_segments) {
        if (seg.isOverlap) {
            ++count;
        }
    }
    return count;
}

QStringList TranscriptEditorWidget::speakers() const
{
    QSet<QString> unique;
    for (const TranscriptSegmentItem &seg : m_segments) {
        if (!seg.speakerId.isEmpty()) {
            unique.insert(seg.speakerId);
        }
    }
    QStringList list(unique.cbegin(), unique.cend());
    list.sort(Qt::CaseInsensitive);
    return list;
}

const QVector<TranscriptSegmentItem> &TranscriptEditorWidget::segments() const
{
    return m_segments;
}

int TranscriptEditorWidget::renameSpeaker(const QString &oldName, const QString &newName)
{
    const QString src = oldName.trimmed();
    const QString dst = newName.trimmed();
    if (src.isEmpty() || dst.isEmpty() || src == dst) {
        return 0;
    }

    int updated = 0;
    for (TranscriptSegmentItem &seg : m_segments) {
        if (seg.speakerId == src) {
            seg.speakerId = dst;
            seg.isEdited = true;
            ++updated;
        }
    }

    if (updated > 0) {
        m_speakerRenames.insert(src, dst);
        const QString curFilter = m_speakerFilterCombo->currentData().toString();
        const QString nextFilter = (curFilter == src) ? dst : curFilter;
        rebuildSpeakerCombo(nextFilter);
        rebuildTree();
        updateSummaryAndButtons();
        Q_EMIT speakerRenamed(src, dst);
        Q_EMIT transcriptModified();
    }
    return updated;
}

bool TranscriptEditorWidget::setSegmentText(const QString &segmentId, const QString &newText)
{
    const int idx = indexForSegmentId(segmentId);
    if (idx < 0) {
        return false;
    }
    if (m_segments[idx].text == newText) {
        return true;
    }
    m_segments[idx].text = newText;
    m_segments[idx].isEdited = true;

    m_updatingTree = true;
    for (int i = 0; i < m_tree->topLevelItemCount(); ++i) {
        QTreeWidgetItem *item = m_tree->topLevelItem(i);
        if (item->data(kColTimecode, kSegmentIndexRole).toInt() == idx) {
            item->setText(kColText, newText);
            applyRowVisualStyle(item, m_segments.at(idx));
            break;
        }
    }
    m_updatingTree = false;

    updateSummaryAndButtons();
    Q_EMIT transcriptModified();
    return true;
}

bool TranscriptEditorWidget::setSegmentCut(const QString &segmentId, bool cut)
{
    const int idx = indexForSegmentId(segmentId);
    if (idx < 0) {
        return false;
    }
    if (m_segments[idx].isCut == cut) {
        return true;
    }
    m_segments[idx].isCut = cut;

    m_updatingTree = true;
    for (int i = 0; i < m_tree->topLevelItemCount(); ++i) {
        QTreeWidgetItem *item = m_tree->topLevelItem(i);
        if (item->data(kColTimecode, kSegmentIndexRole).toInt() == idx) {
            applyRowVisualStyle(item, m_segments.at(idx));
            break;
        }
    }
    m_updatingTree = false;

    updateSummaryAndButtons();
    Q_EMIT transcriptModified();
    return true;
}

bool TranscriptEditorWidget::toggleSegmentCut(const QString &segmentId)
{
    const int idx = indexForSegmentId(segmentId);
    if (idx < 0) {
        return false;
    }
    return setSegmentCut(segmentId, !m_segments.at(idx).isCut);
}

void TranscriptEditorWidget::setSearchFilter(const QString &query)
{
    m_searchEdit->setText(query);
}

void TranscriptEditorWidget::setSpeakerFilter(const QString &speaker)
{
    const int idx = m_speakerFilterCombo->findData(speaker);
    if (idx >= 0) {
        m_speakerFilterCombo->setCurrentIndex(idx);
    } else if (speaker.isEmpty()) {
        m_speakerFilterCombo->setCurrentIndex(0);
    }
}

void TranscriptEditorWidget::setFrameRate(int fpsNum, int fpsDen)
{
    if (fpsNum > 0 && fpsDen > 0) {
        m_fpsNum = fpsNum;
        m_fpsDen = fpsDen;
    }
}

double TranscriptEditorWidget::frameRate() const
{
    return (m_fpsDen > 0) ? (static_cast<double>(m_fpsNum) / static_cast<double>(m_fpsDen)) : 25.0;
}

void TranscriptEditorWidget::setReviewConfidenceThreshold(double threshold)
{
    m_reviewThreshold = qBound(0.0, threshold, 1.0);
    for (TranscriptSegmentItem &seg : m_segments) {
        seg.isLowConfidence = seg.confidence < m_reviewThreshold;
    }
    rebuildTree();
}

double TranscriptEditorWidget::reviewConfidenceThreshold() const
{
    return m_reviewThreshold;
}

QTreeWidget *TranscriptEditorWidget::treeWidget() const
{
    return m_tree;
}

void TranscriptEditorWidget::detectTemporalOverlaps()
{
    constexpr double kMinOverlapSec = 0.05;
    for (int i = 0; i < m_segments.size(); ++i) {
        for (int j = i + 1; j < m_segments.size(); ++j) {
            const double overlapStart = qMax(m_segments.at(i).startSec, m_segments.at(j).startSec);
            const double overlapEnd = qMin(m_segments.at(i).endSec, m_segments.at(j).endSec);
            if ((overlapEnd - overlapStart) >= kMinOverlapSec) {
                m_segments[i].isOverlap = true;
                m_segments[j].isOverlap = true;
            }
        }
    }
}

void TranscriptEditorWidget::rebuildSpeakerCombo(const QString &preserveSelection)
{
    const QString prev = preserveSelection.isEmpty() ? m_speakerFilterCombo->currentData().toString() : preserveSelection;
    m_speakerFilterCombo->blockSignals(true);
    m_speakerFilterCombo->clear();
    m_speakerFilterCombo->addItem(tr("All Speakers"), QString());

    const QStringList spks = speakers();
    for (const QString &spk : spks) {
        m_speakerFilterCombo->addItem(spk, spk);
    }

    const int restoreIdx = m_speakerFilterCombo->findData(prev);
    m_speakerFilterCombo->setCurrentIndex(restoreIdx >= 0 ? restoreIdx : 0);
    m_speakerFilterCombo->blockSignals(false);
}

void TranscriptEditorWidget::rebuildTree()
{
    m_updatingTree = true;
    m_tree->clear();

    const QString query = m_searchEdit->text().trimmed();
    const QString speakerFilter = m_speakerFilterCombo->currentData().toString();

    for (int i = 0; i < m_segments.size(); ++i) {
        const TranscriptSegmentItem &seg = m_segments.at(i);

        if (!speakerFilter.isEmpty() && seg.speakerId != speakerFilter) {
            continue;
        }

        const QString tc = QStringLiteral("%1–%2").arg(formatTimecode(seg.startSec), formatTimecode(seg.endSec));
        if (!query.isEmpty()) {
            const bool matches = seg.text.contains(query, Qt::CaseInsensitive) || seg.speakerId.contains(query, Qt::CaseInsensitive) ||
                                 tc.contains(query, Qt::CaseInsensitive) || seg.segmentId.contains(query, Qt::CaseInsensitive);
            if (!matches) {
                continue;
            }
        }

        auto *item = new QTreeWidgetItem(m_tree);
        item->setFlags(item->flags() | Qt::ItemIsEditable);
        const int pct = static_cast<int>(std::llround(seg.confidence * 100.0));
        item->setText(kColTimecode, tc);
        item->setText(kColSpeaker, seg.speakerId);
        item->setText(kColConfidence, QStringLiteral("%1%").arg(pct));
        item->setText(kColText, seg.text);
        item->setData(kColTimecode, kSegmentIndexRole, i);
        item->setData(kColTimecode, kSegmentIdRole, seg.segmentId);
        item->setToolTip(kColTimecode, tr("Start frame: %1 • End frame: %2").arg(seg.startFrame).arg(seg.endFrame));
        item->setToolTip(kColText, seg.text);

        applyRowVisualStyle(item, seg);
    }

    m_updatingTree = false;
    handleSelectionChanged();
}

void TranscriptEditorWidget::applyRowVisualStyle(QTreeWidgetItem *item, const TranscriptSegmentItem &seg) const
{
    QFont textFont = item->font(kColText);
    textFont.setStrikeOut(seg.isCut);
    item->setFont(kColText, textFont);

    QFont tcFont = item->font(kColTimecode);
    tcFont.setStrikeOut(seg.isCut);
    item->setFont(kColTimecode, tcFont);

    const QColor cutFg(142, 118, 124);
    const QColor cutBg(46, 20, 26);
    const QColor overlapFg(255, 198, 92);
    const QColor overlapBg(66, 44, 10);
    const QColor normalFg(232, 236, 242);
    const QColor okFg(96, 214, 154);

    if (seg.isCut) {
        item->setText(kColStatus, tr("Cut"));
        for (int c = 0; c < m_tree->columnCount(); ++c) {
            item->setForeground(c, QBrush(cutFg));
            item->setBackground(c, QBrush(cutBg));
        }
        return;
    }

    for (int c = 0; c < m_tree->columnCount(); ++c) {
        item->setBackground(c, QBrush());
        item->setForeground(c, QBrush(normalFg));
    }

    if (seg.isOverlap) {
        item->setText(kColStatus, tr("⚡ Overlap"));
        item->setForeground(kColStatus, QBrush(overlapFg));
        item->setBackground(kColStatus, QBrush(overlapBg));
        item->setForeground(kColConfidence, QBrush(overlapFg));
    } else if (seg.isLowConfidence) {
        item->setText(kColStatus, tr("⚠ Review"));
        item->setForeground(kColStatus, QBrush(overlapFg));
        item->setForeground(kColConfidence, QBrush(overlapFg));
    } else if (seg.isEdited) {
        item->setText(kColStatus, tr("Edited"));
        item->setForeground(kColStatus, QBrush(QColor(123, 184, 255)));
        item->setForeground(kColConfidence, QBrush(okFg));
    } else {
        item->setText(kColStatus, tr("Active"));
        item->setForeground(kColStatus, QBrush(okFg));
        item->setForeground(kColConfidence, QBrush(okFg));
    }
}

void TranscriptEditorWidget::updateSummaryAndButtons()
{
    const int total = m_segments.size();
    const int cut = cutSegmentCount();
    const int overlaps = overlapSegmentCount();
    const int spkCount = speakers().size();

    if (total == 0) {
        m_summaryLabel->setText(tr("No transcript loaded"));
    } else {
        QString text = tr("%1 segs • %2 speakers").arg(total).arg(spkCount);
        if (cut > 0) {
            text += tr(" • %1 cut").arg(cut);
        }
        if (overlaps > 0) {
            text += tr(" • ⚡ %1 overlap").arg(overlaps);
        }
        m_summaryLabel->setText(text);
    }

    m_renameSpeakerButton->setEnabled(spkCount > 0);
    m_applyEditsButton->setEnabled(total > 0);
    handleSelectionChanged();
}

int TranscriptEditorWidget::indexForSegmentId(const QString &segmentId) const
{
    for (int i = 0; i < m_segments.size(); ++i) {
        if (m_segments.at(i).segmentId == segmentId) {
            return i;
        }
    }
    return -1;
}

QString TranscriptEditorWidget::formatTimecode(double sec) const
{
    const double fps = frameRate();
    const int roundedFps = qMax(1, static_cast<int>(std::llround(fps)));
    const int totalFrames = secToFrame(sec);
    const int totalSeconds = totalFrames / roundedFps;
    const int remFrames = totalFrames % roundedFps;
    const int hours = totalSeconds / 3600;
    const int minutes = (totalSeconds % 3600) / 60;
    const int seconds = totalSeconds % 60;
    return QStringLiteral("%1:%2:%3:%4")
        .arg(hours, 2, 10, QLatin1Char('0'))
        .arg(minutes, 2, 10, QLatin1Char('0'))
        .arg(seconds, 2, 10, QLatin1Char('0'))
        .arg(remFrames, 2, 10, QLatin1Char('0'));
}

int TranscriptEditorWidget::secToFrame(double sec) const
{
    return qMax(0, static_cast<int>(std::llround(qMax(0.0, sec) * frameRate())));
}

void TranscriptEditorWidget::handleLoadFileClicked()
{
    const QString path = QFileDialog::getOpenFileName(this, tr("Open Transcript JSON"), QString(), tr("Transcript JSON (*.json);;All Files (*)"));
    if (!path.isEmpty()) {
        loadFromFile(path);
    }
}

void TranscriptEditorWidget::handleRenameSpeakerClicked()
{
    QString targetSpeaker = m_speakerFilterCombo->currentData().toString();
    if (targetSpeaker.isEmpty()) {
        if (QTreeWidgetItem *cur = m_tree->currentItem()) {
            targetSpeaker = cur->text(kColSpeaker);
        } else {
            const QStringList allSpks = speakers();
            if (!allSpks.isEmpty()) {
                targetSpeaker = allSpks.first();
            }
        }
    }
    if (targetSpeaker.isEmpty()) {
        return;
    }

    bool ok = false;
    const QString newName =
        QInputDialog::getText(this, tr("Rename Speaker"), tr("Rename all occurrences of '%1' to:").arg(targetSpeaker), QLineEdit::Normal, targetSpeaker, &ok);
    if (ok && !newName.trimmed().isEmpty()) {
        renameSpeaker(targetSpeaker, newName.trimmed());
    }
}

void TranscriptEditorWidget::handleToggleCutClicked()
{
    QTreeWidgetItem *cur = m_tree->currentItem();
    if (cur == nullptr) {
        return;
    }
    const QString segId = cur->data(kColTimecode, kSegmentIdRole).toString();
    toggleSegmentCut(segId);
}

void TranscriptEditorWidget::handleApplyEditsClicked()
{
    if (m_segments.isEmpty()) {
        return;
    }
    Q_EMIT applyTranscriptEditsRequested(exportEditedTranscriptJson());
}

void TranscriptEditorWidget::handleFilterChanged()
{
    rebuildTree();
}

void TranscriptEditorWidget::handleItemClicked(QTreeWidgetItem *item, int)
{
    if (item == nullptr) {
        return;
    }
    const int idx = item->data(kColTimecode, kSegmentIndexRole).toInt();
    if (idx >= 0 && idx < m_segments.size()) {
        Q_EMIT seekToFrameRequested(m_segments.at(idx).startFrame);
    }
}

void TranscriptEditorWidget::handleItemDoubleClicked(QTreeWidgetItem *item, int column)
{
    if (item == nullptr) {
        return;
    }
    const int idx = item->data(kColTimecode, kSegmentIndexRole).toInt();
    if (idx >= 0 && idx < m_segments.size()) {
        Q_EMIT seekToFrameRequested(m_segments.at(idx).startFrame);
    }
    if (column == kColText) {
        m_tree->editItem(item, kColText);
    }
}

void TranscriptEditorWidget::handleItemChanged(QTreeWidgetItem *item, int column)
{
    if (m_updatingTree || item == nullptr) {
        return;
    }
    const int idx = item->data(kColTimecode, kSegmentIndexRole).toInt();
    if (idx < 0 || idx >= m_segments.size()) {
        return;
    }

    // Only Text column edits mutate the underlying transcript text; revert accidental edits to other columns.
    if (column == kColText) {
        const QString newText = item->text(kColText);
        if (m_segments[idx].text != newText) {
            m_segments[idx].text = newText;
            m_segments[idx].isEdited = true;
            m_updatingTree = true;
            applyRowVisualStyle(item, m_segments.at(idx));
            m_updatingTree = false;
            updateSummaryAndButtons();
            Q_EMIT transcriptModified();
        }
    } else {
        m_updatingTree = true;
        const TranscriptSegmentItem &seg = m_segments.at(idx);
        item->setText(kColTimecode, QStringLiteral("%1–%2").arg(formatTimecode(seg.startSec), formatTimecode(seg.endSec)));
        item->setText(kColSpeaker, seg.speakerId);
        item->setText(kColConfidence, QStringLiteral("%1%").arg(static_cast<int>(std::llround(seg.confidence * 100.0))));
        applyRowVisualStyle(item, seg);
        m_updatingTree = false;
    }
}

void TranscriptEditorWidget::handleSelectionChanged()
{
    QTreeWidgetItem *cur = m_tree->currentItem();
    m_toggleCutButton->setEnabled(cur != nullptr);
    if (cur != nullptr) {
        const int idx = cur->data(kColTimecode, kSegmentIndexRole).toInt();
        if (idx >= 0 && idx < m_segments.size()) {
            m_toggleCutButton->setText(m_segments.at(idx).isCut ? tr("Restore Segment") : tr("Cut Segment"));
        }
    } else {
        m_toggleCutButton->setText(tr("Cut / Restore Segment"));
    }
}

} // namespace BangOnPro
