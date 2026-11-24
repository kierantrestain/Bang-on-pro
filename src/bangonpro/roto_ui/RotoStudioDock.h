/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include "RotoMaskCanvasWidget.h"

#include <QJsonArray>
#include <QJsonObject>
#include <QString>
#include <QVector>
#include <QWidget>

class QCheckBox;
class QComboBox;
class QDoubleSpinBox;
class QLabel;
class QLineEdit;
class QListWidget;
class QPushButton;
class QSlider;
class QSpinBox;
class QTabWidget;

namespace BangOnPro {

struct StudioMaskLayerEntry
{
    QString id;
    QString name;
    QString combineMode = QStringLiteral("add");
    bool inverted = false;
    double featherPx = 0.0;
    double expandPx = 0.0;
    double edgeRefine = 0.25;
    double opacity = 1.0;
    QJsonObject geometryJson;
    QMap<int, QJsonArray> keyframedPaths;
};

struct StudioCanvas2DLayer
{
    QString id;
    QString name;
    QString kind = QStringLiteral("solid");
    QString blendMode = QStringLiteral("normal");
    double opacity = 1.0;
    bool visible = true;
    QString imagePath;
    QString text;
};

struct StudioStoryboardCard
{
    QString id;
    int shotNumber = 1;
    QString title;
    QString dialogue;
    QString action;
    QString prompt;
    double durationSec = 3.0;
    QString imagePath;
};

/**
 * @brief Native Qt5/Qt6 Stage Two Rotoscoping, Masking & Selective ArtCraft 2D Studio Dock.
 *
 * Three integrated tabs inside Bang On Pro (zero separate app or second editor engine):
 *  1. Rotoscope & Mask — AI +/- selection, polygon/Bézier/freehand/brush, multi-mask boolean ops,
 *     feather/expand/hair-edge refinement, forward/backward tracking, keyframe overrides, 4 preview modes.
 *  2. 2D Composite (ArtCraft-adapted) — multi-layer RGBA canvas with undo/redo, blend modes, collage pack,
 *     and one-click send to Kdenlive Media Bin & Timeline.
 *  3. Storyboard & AI Plates (ArtCraft-adapted) — visual shot cards with local plate rendering,
 *     consent-gated generative AI, and one-click Storyboard Animatic export to Timeline.
 */
class RotoStudioDock : public QWidget
{
    Q_OBJECT

public:
    explicit RotoStudioDock(QWidget *parent = nullptr);
    ~RotoStudioDock() override = default;

    RotoMaskCanvasWidget *canvasWidget() const { return m_canvas; }
    QTabWidget *tabWidget() const { return m_tabs; }

    // Mask management API
    int maskCount() const { return m_masks.size(); }
    QString addMaskLayer(const QString &name = QString());
    bool duplicateSelectedMask();
    bool removeSelectedMask();
    void setMaskCombineMode(int index, const QString &mode);
    void setMaskInverted(int index, bool inverted);
    void addKeyframeAtCurrentFrame();
    int currentFrame() const;
    void setCurrentFrame(int frame);
    QJsonObject exportRotoSessionJson() const;

    /** @brief Display honest runtime backend status (neural SAM / MobileSAM ONNX vs OpenCV fallback). */
    void setSegmentationBackendStatus(const QString &backendName, bool isNeuralInference, const QString &details = QString());
    QString segmentationBackendStatusText() const;

    // ArtCraft-adapted 2D Layer Composition API
    int canvas2DLayerCount() const { return m_canvasLayers.size(); }
    QString addCanvas2DLayer(const QString &name, const QString &kind = QStringLiteral("solid"),
                             const QString &imagePath = QString(), const QString &text = QString());
    bool moveSelectedCanvas2DLayer(int delta);
    bool undoCanvas2D();
    bool redoCanvas2D();
    bool canUndoCanvas2D() const { return !m_canvasUndoStack.isEmpty(); }
    bool canRedoCanvas2D() const { return !m_canvasRedoStack.isEmpty(); }
    QJsonObject exportCanvas2DJson() const;

    // ArtCraft-adapted Storyboard API
    int storyboardShotCount() const { return m_storyboardShots.size(); }
    QString addStoryboardShot(const QString &title = QString(), double durationSec = 3.0,
                              const QString &dialogue = QString(), const QString &action = QString());
    bool deleteSelectedStoryboardShot();
    bool moveSelectedStoryboardShot(int delta);
    QJsonObject exportStoryboardJson() const;

Q_SIGNALS:
    void commitMaskToTimelineRequested(const QJsonObject &rotoSessionJson);
    void trackMaskRequested(const QString &direction, const QJsonObject &rotoSessionJson);
    void aiSegmentRequested(const QJsonObject &rotoSessionJson);
    void send2DCompositeToMediaBinRequested(const QJsonObject &canvasJson, bool placeOnTimeline);
    void sendStoryboardToTimelineRequested(const QJsonObject &storyboardJson);

private Q_SLOTS:
    void handleMaskSelectionChanged(int row);
    void handleMaskParamsChanged();
    void handleToolComboChanged(int index);
    void handlePreviewComboChanged(int index);
    void handleCanvasGeometryChanged();
    void handleAddKeyframeClicked();
    void handleCommitMaskClicked();

    void handleCanvasLayerSelectionChanged(int row);
    void handleCanvasLayerPropsChanged();
    void handleStoryboardSelectionChanged(int row);
    void handleStoryboardPropsChanged();

private:
    void buildUi();
    QWidget *buildRotoTab();
    QWidget *buildCanvas2DTab();
    QWidget *buildStoryboardTab();

    void refreshMaskList();
    void syncUiFromSelectedMask();
    void pushCanvas2DUndoSnapshot();
    void refreshCanvas2DList();
    void refreshStoryboardList();

    QTabWidget *m_tabs = nullptr;

    // Tab 1: Rotoscope & Mask
    RotoMaskCanvasWidget *m_canvas = nullptr;
    QComboBox *m_toolCombo = nullptr;
    QComboBox *m_previewCombo = nullptr;
    QComboBox *m_backgroundCombo = nullptr;
    QListWidget *m_maskList = nullptr;
    QComboBox *m_combineCombo = nullptr;
    QCheckBox *m_invertCheck = nullptr;
    QSlider *m_featherSlider = nullptr;
    QSlider *m_expandSlider = nullptr;
    QSlider *m_edgeRefineSlider = nullptr;
    QSlider *m_opacitySlider = nullptr;
    QSpinBox *m_frameSpin = nullptr;
    QLabel *m_backendStatusLabel = nullptr;
    QLabel *m_rotoStatusLabel = nullptr;

    QVector<StudioMaskLayerEntry> m_masks;
    int m_selectedMaskIdx = -1;
    bool m_syncingMaskUi = false;

    // Tab 2: 2D Composite (ArtCraft)
    QListWidget *m_layerList = nullptr;
    QComboBox *m_layerBlendCombo = nullptr;
    QSlider *m_layerOpacitySlider = nullptr;
    QCheckBox *m_layerVisibleCheck = nullptr;
    QPushButton *m_layerUndoBtn = nullptr;
    QPushButton *m_layerRedoBtn = nullptr;
    QVector<StudioCanvas2DLayer> m_canvasLayers;
    QVector<QVector<StudioCanvas2DLayer>> m_canvasUndoStack;
    QVector<QVector<StudioCanvas2DLayer>> m_canvasRedoStack;
    bool m_syncingLayerUi = false;

    // Tab 3: Storyboard (ArtCraft)
    QListWidget *m_shotList = nullptr;
    QLineEdit *m_shotTitleEdit = nullptr;
    QLineEdit *m_shotDialogueEdit = nullptr;
    QLineEdit *m_shotActionEdit = nullptr;
    QLineEdit *m_shotPromptEdit = nullptr;
    QDoubleSpinBox *m_shotDurationSpin = nullptr;
    QVector<StudioStoryboardCard> m_storyboardShots;
    bool m_syncingShotUi = false;
};

} // namespace BangOnPro
