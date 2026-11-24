/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include <QJsonObject>
#include <QMap>
#include <QString>
#include <QStringList>
#include <QVector>
#include <QWidget>

class QComboBox;
class QLabel;
class QLineEdit;
class QPushButton;
class QTreeWidget;
class QTreeWidgetItem;

namespace BangOnPro {

/**
 * @brief Editable transcript segment model synchronized to timeline frames and speakers.
 */
struct TranscriptSegmentItem
{
    QString segmentId;
    double startSec = 0.0;
    double endSec = 0.0;
    int startFrame = 0;
    int endFrame = 0;
    QString speakerId;
    QString cameraId;
    QString text;
    double confidence = 1.0;
    bool isLowConfidence = false;
    bool isOverlap = false;
    bool isCut = false;
    bool isEdited = false;
    QString sectionLabel;
};

/**
 * @brief Interactive text-based editing & diarised transcript panel linked to
 * Kdenlive's playhead and timeline.
 *
 * Supports loading Agent 2's transcript.json, searching/filtering by text and
 * speaker, batch speaker renaming, clicking/double-clicking segments to seek
 * the timeline playhead, inline text editing, non-destructive Cut/Restore
 * strikethrough toggles, overlap speech badges, and emitting an edited
 * transcript payload to apply cuts back to the timeline.
 */
class TranscriptEditorWidget : public QWidget
{
    Q_OBJECT

public:
    explicit TranscriptEditorWidget(QWidget *parent = nullptr);
    ~TranscriptEditorWidget() override = default;

    /** @brief Load transcript.json from disk. */
    bool loadFromFile(const QString &filePath, QString *errorMessage = nullptr);

    /** @brief Load transcript from an already-parsed JSON object. */
    bool loadFromJson(const QJsonObject &json, QString *errorMessage = nullptr);

    /** @brief Serialize the edited transcript (including cuts, text edits, speaker renames). */
    QJsonObject exportEditedTranscriptJson() const;

    /** @brief Reset all loaded transcript state. */
    void clear();

    /** @brief Total loaded transcript segments. */
    int segmentCount() const;

    /** @brief Number of segments currently marked Cut (strikethrough). */
    int cutSegmentCount() const;

    /** @brief Number of segments flagged with overlapping / crosstalk speech. */
    int overlapSegmentCount() const;

    /** @brief Sorted list of unique speaker names present in the transcript. */
    QStringList speakers() const;

    /** @brief Read-only access to the segment list. */
    const QVector<TranscriptSegmentItem> &segments() const;

    /** @brief Rename every segment belonging to @p oldName to @p newName. Returns updated count. */
    int renameSpeaker(const QString &oldName, const QString &newName);

    /** @brief Programmatically update the text of segment @p segmentId. */
    bool setSegmentText(const QString &segmentId, const QString &newText);

    /** @brief Programmatically mark or restore segment @p segmentId as cut. */
    bool setSegmentCut(const QString &segmentId, bool cut);

    /** @brief Toggle the Cut/Restore state of segment @p segmentId. */
    bool toggleSegmentCut(const QString &segmentId);

    /** @brief Filter visible rows by text/speaker/timecode query. */
    void setSearchFilter(const QString &query);

    /** @brief Filter visible rows by speaker name (empty string shows all speakers). */
    void setSpeakerFilter(const QString &speaker);

    /** @brief Set project framerate used for frame/timecode conversion. */
    void setFrameRate(int fpsNum, int fpsDen = 1);
    double frameRate() const;

    /** @brief Confidence threshold below which segments display a review badge. */
    void setReviewConfidenceThreshold(double threshold);
    double reviewConfidenceThreshold() const;

    /** @brief Access underlying QTreeWidget (for inspection/testing). */
    QTreeWidget *treeWidget() const;

Q_SIGNALS:
    /** @brief Emitted on segment click or double-click to seek the live playhead. */
    void seekToFrameRequested(int frame);

    /** @brief Emitted when user clicks Apply Transcript Edits to Timeline. */
    void applyTranscriptEditsRequested(const QJsonObject &editedTranscriptJson);

    /** @brief Emitted whenever segment text, cut flags, or speaker names change. */
    void transcriptModified();

    /** @brief Emitted after a speaker is batch-renamed across segments. */
    void speakerRenamed(const QString &oldName, const QString &newName);

private Q_SLOTS:
    void handleLoadFileClicked();
    void handleRenameSpeakerClicked();
    void handleToggleCutClicked();
    void handleApplyEditsClicked();
    void handleFilterChanged();
    void handleItemClicked(QTreeWidgetItem *item, int column);
    void handleItemDoubleClicked(QTreeWidgetItem *item, int column);
    void handleItemChanged(QTreeWidgetItem *item, int column);
    void handleSelectionChanged();

private:
    void buildUi();
    void rebuildTree();
    void rebuildSpeakerCombo(const QString &preserveSelection = QString());
    void applyRowVisualStyle(QTreeWidgetItem *item, const TranscriptSegmentItem &seg) const;
    void updateSummaryAndButtons();
    void detectTemporalOverlaps();
    int indexForSegmentId(const QString &segmentId) const;
    QString formatTimecode(double sec) const;
    int secToFrame(double sec) const;

    QLabel *m_summaryLabel = nullptr;
    QLineEdit *m_searchEdit = nullptr;
    QComboBox *m_speakerFilterCombo = nullptr;
    QPushButton *m_renameSpeakerButton = nullptr;
    QTreeWidget *m_tree = nullptr;
    QPushButton *m_loadButton = nullptr;
    QPushButton *m_toggleCutButton = nullptr;
    QPushButton *m_applyEditsButton = nullptr;

    QVector<TranscriptSegmentItem> m_segments;
    QMap<QString, QString> m_speakerRenames;
    QJsonObject m_sourceMetadata;
    int m_fpsNum = 25;
    int m_fpsDen = 1;
    double m_reviewThreshold = 0.72;
    bool m_updatingTree = false;
};

} // namespace BangOnPro
