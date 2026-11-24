/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "RotoStudioDock.h"

#include <QCheckBox>
#include <QComboBox>
#include <QDoubleSpinBox>
#include <QFileDialog>
#include <QFormLayout>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QInputDialog>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QPushButton>
#include <QSlider>
#include <QSpinBox>
#include <QTabWidget>
#include <QUuid>
#include <QVBoxLayout>

namespace BangOnPro {

RotoStudioDock::RotoStudioDock(QWidget *parent)
    : QWidget(parent)
{
    setObjectName(QStringLiteral("bangonproRotoStudioWidget"));
    setWindowTitle(QStringLiteral("Bang On Pro — Roto, Masking & 2D Studio"));
    buildUi();

    // Seed an initial editable mask, 2D plate layer, and opening storyboard shot
    addMaskLayer(QStringLiteral("Subject Mask 1"));
    addCanvas2DLayer(QStringLiteral("Studio Backdrop"), QStringLiteral("gradient"));
    addStoryboardShot(QStringLiteral("Opening Establishing Shot"), 3.0, QString(), QStringLiteral("Wide studio push-in"));
}

void RotoStudioDock::buildUi()
{
    auto *root = new QVBoxLayout(this);
    root->setContentsMargins(6, 6, 6, 6);
    root->setSpacing(6);

    auto *header = new QLabel(QStringLiteral("<b>Bang On Pro Roto & 2D Studio</b> • Native Masking + Selective ArtCraft 2D/Storyboard"), this);
    header->setWordWrap(true);
    root->addWidget(header);

    m_tabs = new QTabWidget(this);
    m_tabs->setObjectName(QStringLiteral("rotoStudioTabs"));
    m_tabs->addTab(buildRotoTab(), QStringLiteral("Rotoscope && Mask"));
    m_tabs->addTab(buildCanvas2DTab(), QStringLiteral("2D Composite"));
    m_tabs->addTab(buildStoryboardTab(), QStringLiteral("Storyboard && Plates"));
    root->addWidget(m_tabs, 1);
}

QWidget *RotoStudioDock::buildRotoTab()
{
    auto *tab = new QWidget(this);
    auto *layout = new QVBoxLayout(tab);
    layout->setContentsMargins(4, 4, 4, 4);
    layout->setSpacing(6);

    // Tool & Preview toolbar
    auto *toolRow = new QHBoxLayout();
    m_toolCombo = new QComboBox(tab);
    m_toolCombo->setObjectName(QStringLiteral("rotoToolCombo"));
    m_toolCombo->addItem(QStringLiteral("AI Select (+)"), static_cast<int>(RotoTool::AiSelectPositive));
    m_toolCombo->addItem(QStringLiteral("AI Subtract (-)"), static_cast<int>(RotoTool::AiSelectNegative));
    m_toolCombo->addItem(QStringLiteral("Polygon Mask"), static_cast<int>(RotoTool::Polygon));
    m_toolCombo->addItem(QStringLiteral("Bézier Spline"), static_cast<int>(RotoTool::Bezier));
    m_toolCombo->addItem(QStringLiteral("Freehand Lasso"), static_cast<int>(RotoTool::Freehand));
    m_toolCombo->addItem(QStringLiteral("Brush Paint"), static_cast<int>(RotoTool::Brush));
    m_toolCombo->addItem(QStringLiteral("Brush Eraser"), static_cast<int>(RotoTool::Eraser));
    m_toolCombo->setCurrentIndex(2);

    m_previewCombo = new QComboBox(tab);
    m_previewCombo->setObjectName(QStringLiteral("rotoPreviewCombo"));
    m_previewCombo->addItem(QStringLiteral("Mask Overlay"), static_cast<int>(RotoPreviewMode::MaskOverlay));
    m_previewCombo->addItem(QStringLiteral("Isolated Alpha"), static_cast<int>(RotoPreviewMode::IsolatedAlpha));
    m_previewCombo->addItem(QStringLiteral("Before / After Split"), static_cast<int>(RotoPreviewMode::BeforeAfterSplit));
    m_previewCombo->addItem(QStringLiteral("Final Composite"), static_cast<int>(RotoPreviewMode::FinalComposite));

    m_backgroundCombo = new QComboBox(tab);
    m_backgroundCombo->setObjectName(QStringLiteral("rotoBackgroundCombo"));
    m_backgroundCombo->addItem(QStringLiteral("Transparent Cutout"), QStringLiteral("transparent"));
    m_backgroundCombo->addItem(QStringLiteral("Solid Color BG"), QStringLiteral("solid_color"));
    m_backgroundCombo->addItem(QStringLiteral("Blur Background"), QStringLiteral("blur_bg"));
    m_backgroundCombo->addItem(QStringLiteral("Replace Background"), QStringLiteral("replace_image"));
    m_backgroundCombo->addItem(QStringLiteral("Selective Effect"), QStringLiteral("selective_effect"));

    toolRow->addWidget(m_toolCombo, 1);
    toolRow->addWidget(m_previewCombo, 1);
    toolRow->addWidget(m_backgroundCombo, 1);
    layout->addLayout(toolRow);

    // Interactive Canvas
    m_canvas = new RotoMaskCanvasWidget(tab);
    layout->addWidget(m_canvas, 2);

    // Keyframe & Tracking Row
    auto *trackRow = new QHBoxLayout();
    auto *trackBackBtn = new QPushButton(QStringLiteral("◀ Track Backward"), tab);
    trackBackBtn->setObjectName(QStringLiteral("rotoTrackBackBtn"));
    auto *trackFwdBtn = new QPushButton(QStringLiteral("Track Forward ▶"), tab);
    trackFwdBtn->setObjectName(QStringLiteral("rotoTrackFwdBtn"));
    auto *aiSegBtn = new QPushButton(QStringLiteral("AI Segment"), tab);
    aiSegBtn->setObjectName(QStringLiteral("rotoAiSegmentBtn"));
    m_frameSpin = new QSpinBox(tab);
    m_frameSpin->setObjectName(QStringLiteral("rotoFrameSpin"));
    m_frameSpin->setRange(0, 1000000);
    m_frameSpin->setPrefix(QStringLiteral("F "));
    auto *addKfBtn = new QPushButton(QStringLiteral("+ Keyframe"), tab);
    addKfBtn->setObjectName(QStringLiteral("rotoAddKeyframeBtn"));

    trackRow->addWidget(trackBackBtn);
    trackRow->addWidget(trackFwdBtn);
    trackRow->addWidget(aiSegBtn);
    trackRow->addWidget(m_frameSpin);
    trackRow->addWidget(addKfBtn);
    layout->addLayout(trackRow);

    // Multi-Mask Stack & Controls
    auto *maskRow = new QHBoxLayout();
    m_maskList = new QListWidget(tab);
    m_maskList->setObjectName(QStringLiteral("rotoMaskList"));
    m_maskList->setMaximumHeight(95);

    auto *maskBtns = new QVBoxLayout();
    auto *addMaskBtn = new QPushButton(QStringLiteral("+ Mask"), tab);
    auto *dupMaskBtn = new QPushButton(QStringLiteral("Duplicate"), tab);
    auto *delMaskBtn = new QPushButton(QStringLiteral("Delete"), tab);
    maskBtns->addWidget(addMaskBtn);
    maskBtns->addWidget(dupMaskBtn);
    maskBtns->addWidget(delMaskBtn);
    maskBtns->addStretch(1);

    maskRow->addWidget(m_maskList, 1);
    maskRow->addLayout(maskBtns);
    layout->addLayout(maskRow);

    // Refinement & Boolean Controls
    auto *form = new QGridLayout();
    m_combineCombo = new QComboBox(tab);
    m_combineCombo->setObjectName(QStringLiteral("rotoCombineCombo"));
    m_combineCombo->addItem(QStringLiteral("Add (Union)"), QStringLiteral("add"));
    m_combineCombo->addItem(QStringLiteral("Subtract"), QStringLiteral("subtract"));
    m_combineCombo->addItem(QStringLiteral("Intersect"), QStringLiteral("intersect"));
    m_combineCombo->addItem(QStringLiteral("XOR"), QStringLiteral("xor"));

    m_invertCheck = new QCheckBox(QStringLiteral("Invert Mask"), tab);
    m_invertCheck->setObjectName(QStringLiteral("rotoInvertCheck"));

    m_featherSlider = new QSlider(Qt::Horizontal, tab);
    m_featherSlider->setRange(0, 60);
    m_expandSlider = new QSlider(Qt::Horizontal, tab);
    m_expandSlider->setRange(-30, 30);
    m_edgeRefineSlider = new QSlider(Qt::Horizontal, tab);
    m_edgeRefineSlider->setRange(0, 100);
    m_edgeRefineSlider->setValue(25);
    m_opacitySlider = new QSlider(Qt::Horizontal, tab);
    m_opacitySlider->setRange(0, 100);
    m_opacitySlider->setValue(100);

    form->addWidget(new QLabel(QStringLiteral("Mode:"), tab), 0, 0);
    form->addWidget(m_combineCombo, 0, 1);
    form->addWidget(m_invertCheck, 0, 2);
    form->addWidget(new QLabel(QStringLiteral("Feather:"), tab), 1, 0);
    form->addWidget(m_featherSlider, 1, 1, 1, 2);
    form->addWidget(new QLabel(QStringLiteral("Expand / Contract:"), tab), 2, 0);
    form->addWidget(m_expandSlider, 2, 1, 1, 2);
    form->addWidget(new QLabel(QStringLiteral("Hair / Edge Refine:"), tab), 3, 0);
    form->addWidget(m_edgeRefineSlider, 3, 1, 1, 2);
    form->addWidget(new QLabel(QStringLiteral("Opacity:"), tab), 4, 0);
    form->addWidget(m_opacitySlider, 4, 1, 1, 2);
    layout->addLayout(form);

    // Commit to Timeline Row
    auto *commitRow = new QHBoxLayout();
    m_rotoStatusLabel = new QLabel(QStringLiteral("Ready — local SAM/OpenCV/Spline engine"), tab);
    auto *commitBtn = new QPushButton(QStringLiteral("Commit Mask to Native Timeline"), tab);
    commitBtn->setObjectName(QStringLiteral("rotoCommitTimelineBtn"));
    commitBtn->setProperty("bangOnProPrimary", true);
    commitRow->addWidget(m_rotoStatusLabel, 1);
    commitRow->addWidget(commitBtn);
    layout->addLayout(commitRow);

    // Connections
    connect(m_toolCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, &RotoStudioDock::handleToolComboChanged);
    connect(m_previewCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, &RotoStudioDock::handlePreviewComboChanged);
    connect(m_maskList, &QListWidget::currentRowChanged, this, &RotoStudioDock::handleMaskSelectionChanged);
    connect(addMaskBtn, &QPushButton::clicked, this, [this]() { addMaskLayer(); });
    connect(dupMaskBtn, &QPushButton::clicked, this, &RotoStudioDock::duplicateSelectedMask);
    connect(delMaskBtn, &QPushButton::clicked, this, &RotoStudioDock::removeSelectedMask);
    connect(m_combineCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, &RotoStudioDock::handleMaskParamsChanged);
    connect(m_invertCheck, &QCheckBox::toggled, this, &RotoStudioDock::handleMaskParamsChanged);
    connect(m_featherSlider, &QSlider::valueChanged, this, &RotoStudioDock::handleMaskParamsChanged);
    connect(m_expandSlider, &QSlider::valueChanged, this, &RotoStudioDock::handleMaskParamsChanged);
    connect(m_edgeRefineSlider, &QSlider::valueChanged, this, &RotoStudioDock::handleMaskParamsChanged);
    connect(m_opacitySlider, &QSlider::valueChanged, this, &RotoStudioDock::handleMaskParamsChanged);
    connect(m_canvas, &RotoMaskCanvasWidget::maskGeometryChanged, this, &RotoStudioDock::handleCanvasGeometryChanged);
    connect(addKfBtn, &QPushButton::clicked, this, &RotoStudioDock::handleAddKeyframeClicked);
    connect(commitBtn, &QPushButton::clicked, this, &RotoStudioDock::handleCommitMaskClicked);
    connect(trackBackBtn, &QPushButton::clicked, this, [this]() { Q_EMIT trackMaskRequested(QStringLiteral("backward"), exportRotoSessionJson()); });
    connect(trackFwdBtn, &QPushButton::clicked, this, [this]() { Q_EMIT trackMaskRequested(QStringLiteral("forward"), exportRotoSessionJson()); });
    connect(aiSegBtn, &QPushButton::clicked, this, [this]() { Q_EMIT aiSegmentRequested(exportRotoSessionJson()); });

    return tab;
}

QWidget *RotoStudioDock::buildCanvas2DTab()
{
    auto *tab = new QWidget(this);
    auto *layout = new QVBoxLayout(tab);
    layout->setContentsMargins(4, 4, 4, 4);
    layout->setSpacing(6);

    auto *info = new QLabel(QStringLiteral("2D Layer Compositor (selectively adapted from ArtCraft MIT/Apache-2.0)"), tab);
    info->setWordWrap(true);
    layout->addWidget(info);

    m_layerList = new QListWidget(tab);
    m_layerList->setObjectName(QStringLiteral("canvas2DLayerList"));
    layout->addWidget(m_layerList, 1);

    auto *btnRow = new QHBoxLayout();
    auto *addPlateBtn = new QPushButton(QStringLiteral("+ Plate"), tab);
    auto *addTextBtn = new QPushButton(QStringLiteral("+ Text"), tab);
    auto *upBtn = new QPushButton(QStringLiteral("▲"), tab);
    auto *downBtn = new QPushButton(QStringLiteral("▼"), tab);
    m_layerUndoBtn = new QPushButton(QStringLiteral("Undo"), tab);
    m_layerRedoBtn = new QPushButton(QStringLiteral("Redo"), tab);
    btnRow->addWidget(addPlateBtn);
    btnRow->addWidget(addTextBtn);
    btnRow->addWidget(upBtn);
    btnRow->addWidget(downBtn);
    btnRow->addWidget(m_layerUndoBtn);
    btnRow->addWidget(m_layerRedoBtn);
    layout->addLayout(btnRow);

    auto *propForm = new QFormLayout();
    m_layerBlendCombo = new QComboBox(tab);
    m_layerBlendCombo->addItems({QStringLiteral("normal"), QStringLiteral("multiply"), QStringLiteral("screen"), QStringLiteral("overlay")});
    m_layerOpacitySlider = new QSlider(Qt::Horizontal, tab);
    m_layerOpacitySlider->setRange(0, 100);
    m_layerOpacitySlider->setValue(100);
    m_layerVisibleCheck = new QCheckBox(QStringLiteral("Visible"), tab);
    m_layerVisibleCheck->setChecked(true);
    propForm->addRow(QStringLiteral("Blend Mode:"), m_layerBlendCombo);
    propForm->addRow(QStringLiteral("Opacity:"), m_layerOpacitySlider);
    propForm->addRow(QString(), m_layerVisibleCheck);
    layout->addLayout(propForm);

    auto *sendRow = new QHBoxLayout();
    auto *sendBinBtn = new QPushButton(QStringLiteral("Flatten → Media Bin"), tab);
    auto *sendTimelineBtn = new QPushButton(QStringLiteral("Flatten → Bin & Timeline"), tab);
    sendTimelineBtn->setProperty("bangOnProPrimary", true);
    sendRow->addWidget(sendBinBtn);
    sendRow->addWidget(sendTimelineBtn);
    layout->addLayout(sendRow);

    connect(addPlateBtn, &QPushButton::clicked, this, [this]() {
        addCanvas2DLayer(QStringLiteral("Layer %1").arg(m_canvasLayers.size() + 1), QStringLiteral("solid"));
    });
    connect(addTextBtn, &QPushButton::clicked, this, [this]() {
        addCanvas2DLayer(QStringLiteral("Title Overlay %1").arg(m_canvasLayers.size() + 1), QStringLiteral("text"), QString(), QStringLiteral("BANG ON PRO"));
    });
    connect(upBtn, &QPushButton::clicked, this, [this]() { moveSelectedCanvas2DLayer(-1); });
    connect(downBtn, &QPushButton::clicked, this, [this]() { moveSelectedCanvas2DLayer(1); });
    connect(m_layerUndoBtn, &QPushButton::clicked, this, &RotoStudioDock::undoCanvas2D);
    connect(m_layerRedoBtn, &QPushButton::clicked, this, &RotoStudioDock::redoCanvas2D);
    connect(m_layerList, &QListWidget::currentRowChanged, this, &RotoStudioDock::handleCanvasLayerSelectionChanged);
    connect(m_layerBlendCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, &RotoStudioDock::handleCanvasLayerPropsChanged);
    connect(m_layerOpacitySlider, &QSlider::valueChanged, this, &RotoStudioDock::handleCanvasLayerPropsChanged);
    connect(m_layerVisibleCheck, &QCheckBox::toggled, this, &RotoStudioDock::handleCanvasLayerPropsChanged);
    connect(sendBinBtn, &QPushButton::clicked, this, [this]() { Q_EMIT send2DCompositeToMediaBinRequested(exportCanvas2DJson(), false); });
    connect(sendTimelineBtn, &QPushButton::clicked, this, [this]() { Q_EMIT send2DCompositeToMediaBinRequested(exportCanvas2DJson(), true); });

    return tab;
}

QWidget *RotoStudioDock::buildStoryboardTab()
{
    auto *tab = new QWidget(this);
    auto *layout = new QVBoxLayout(tab);
    layout->setContentsMargins(4, 4, 4, 4);
    layout->setSpacing(6);

    auto *info = new QLabel(QStringLiteral("Storyboard Planner & Local Plates (selectively adapted from ArtCraft MIT/Apache-2.0)"), tab);
    info->setWordWrap(true);
    layout->addWidget(info);

    m_shotList = new QListWidget(tab);
    m_shotList->setObjectName(QStringLiteral("storyboardShotList"));
    layout->addWidget(m_shotList, 1);

    auto *btnRow = new QHBoxLayout();
    auto *addShotBtn = new QPushButton(QStringLiteral("+ Shot"), tab);
    auto *delShotBtn = new QPushButton(QStringLiteral("Delete"), tab);
    auto *upShotBtn = new QPushButton(QStringLiteral("▲"), tab);
    auto *downShotBtn = new QPushButton(QStringLiteral("▼"), tab);
    btnRow->addWidget(addShotBtn);
    btnRow->addWidget(delShotBtn);
    btnRow->addWidget(upShotBtn);
    btnRow->addWidget(downShotBtn);
    layout->addLayout(btnRow);

    auto *form = new QFormLayout();
    m_shotTitleEdit = new QLineEdit(tab);
    m_shotDialogueEdit = new QLineEdit(tab);
    m_shotActionEdit = new QLineEdit(tab);
    m_shotPromptEdit = new QLineEdit(tab);
    m_shotDurationSpin = new QDoubleSpinBox(tab);
    m_shotDurationSpin->setRange(0.25, 120.0);
    m_shotDurationSpin->setSuffix(QStringLiteral(" s"));
    m_shotDurationSpin->setValue(3.0);

    form->addRow(QStringLiteral("Title:"), m_shotTitleEdit);
    form->addRow(QStringLiteral("Dialogue:"), m_shotDialogueEdit);
    form->addRow(QStringLiteral("Action:"), m_shotActionEdit);
    form->addRow(QStringLiteral("Visual Prompt:"), m_shotPromptEdit);
    form->addRow(QStringLiteral("Duration:"), m_shotDurationSpin);
    layout->addLayout(form);

    auto *exportBtn = new QPushButton(QStringLiteral("Send Storyboard Animatic → Media Bin & Timeline"), tab);
    exportBtn->setObjectName(QStringLiteral("exportStoryboardBtn"));
    exportBtn->setProperty("bangOnProPrimary", true);
    layout->addWidget(exportBtn);

    connect(addShotBtn, &QPushButton::clicked, this, [this]() { addStoryboardShot(); });
    connect(delShotBtn, &QPushButton::clicked, this, &RotoStudioDock::deleteSelectedStoryboardShot);
    connect(upShotBtn, &QPushButton::clicked, this, [this]() { moveSelectedStoryboardShot(-1); });
    connect(downShotBtn, &QPushButton::clicked, this, [this]() { moveSelectedStoryboardShot(1); });
    connect(m_shotList, &QListWidget::currentRowChanged, this, &RotoStudioDock::handleStoryboardSelectionChanged);
    connect(m_shotTitleEdit, &QLineEdit::textEdited, this, &RotoStudioDock::handleStoryboardPropsChanged);
    connect(m_shotDialogueEdit, &QLineEdit::textEdited, this, &RotoStudioDock::handleStoryboardPropsChanged);
    connect(m_shotActionEdit, &QLineEdit::textEdited, this, &RotoStudioDock::handleStoryboardPropsChanged);
    connect(m_shotPromptEdit, &QLineEdit::textEdited, this, &RotoStudioDock::handleStoryboardPropsChanged);
    connect(m_shotDurationSpin, QOverload<double>::of(&QDoubleSpinBox::valueChanged), this, &RotoStudioDock::handleStoryboardPropsChanged);
    connect(exportBtn, &QPushButton::clicked, this, [this]() { Q_EMIT sendStoryboardToTimelineRequested(exportStoryboardJson()); });

    return tab;
}

QString RotoStudioDock::addMaskLayer(const QString &name)
{
    StudioMaskLayerEntry entry;
    entry.id = QStringLiteral("mask_") + QUuid::createUuid().toString(QUuid::WithoutBraces).left(8);
    entry.name = name.isEmpty() ? QStringLiteral("Mask %1").arg(m_masks.size() + 1) : name;
    m_masks.append(entry);
    m_selectedMaskIdx = m_masks.size() - 1;
    refreshMaskList();
    syncUiFromSelectedMask();
    return entry.id;
}

bool RotoStudioDock::duplicateSelectedMask()
{
    if (m_selectedMaskIdx < 0 || m_selectedMaskIdx >= m_masks.size()) {
        return false;
    }
    StudioMaskLayerEntry copy = m_masks.at(m_selectedMaskIdx);
    copy.id = QStringLiteral("mask_") + QUuid::createUuid().toString(QUuid::WithoutBraces).left(8);
    copy.name += QStringLiteral(" Copy");
    m_masks.append(copy);
    m_selectedMaskIdx = m_masks.size() - 1;
    refreshMaskList();
    syncUiFromSelectedMask();
    return true;
}

bool RotoStudioDock::removeSelectedMask()
{
    if (m_selectedMaskIdx < 0 || m_selectedMaskIdx >= m_masks.size()) {
        return false;
    }
    m_masks.removeAt(m_selectedMaskIdx);
    m_selectedMaskIdx = qMin(m_selectedMaskIdx, m_masks.size() - 1);
    refreshMaskList();
    syncUiFromSelectedMask();
    return true;
}

void RotoStudioDock::setMaskCombineMode(int index, const QString &mode)
{
    if (index < 0 || index >= m_masks.size()) {
        return;
    }
    m_masks[index].combineMode = mode;
    refreshMaskList();
    if (index == m_selectedMaskIdx) {
        syncUiFromSelectedMask();
    }
}

void RotoStudioDock::setMaskInverted(int index, bool inverted)
{
    if (index < 0 || index >= m_masks.size()) {
        return;
    }
    m_masks[index].inverted = inverted;
    refreshMaskList();
    if (index == m_selectedMaskIdx) {
        syncUiFromSelectedMask();
    }
}

void RotoStudioDock::addKeyframeAtCurrentFrame()
{
    if (m_selectedMaskIdx < 0 || m_selectedMaskIdx >= m_masks.size()) {
        return;
    }
    const QJsonObject geom = m_canvas->exportMaskGeometryJson();
    m_masks[m_selectedMaskIdx].geometryJson = geom;
    m_masks[m_selectedMaskIdx].keyframedPaths.insert(currentFrame(), geom.value(QStringLiteral("vertices")).toArray());
    m_rotoStatusLabel->setText(QStringLiteral("Keyframed mask '%1' at frame %2").arg(m_masks[m_selectedMaskIdx].name).arg(currentFrame()));
}

int RotoStudioDock::currentFrame() const
{
    return m_frameSpin ? m_frameSpin->value() : 0;
}

void RotoStudioDock::setCurrentFrame(int frame)
{
    if (m_frameSpin) {
        m_frameSpin->setValue(qMax(0, frame));
    }
}

QJsonObject RotoStudioDock::exportRotoSessionJson() const
{
    QJsonObject session;
    session.insert(QStringLiteral("schema_version"), QStringLiteral("1.0"));
    session.insert(QStringLiteral("session_id"), QStringLiteral("studio_roto_1"));
    session.insert(QStringLiteral("clip_path"), QStringLiteral("active_clip"));
    session.insert(QStringLiteral("fps"), 25.0);
    session.insert(QStringLiteral("width"), m_canvas ? qMax(1, m_canvas->frameImage().width()) : 1920);
    session.insert(QStringLiteral("height"), m_canvas ? qMax(1, m_canvas->frameImage().height()) : 1080);

    QString previewStr = QStringLiteral("overlay");
    if (m_canvas) {
        switch (m_canvas->previewMode()) {
        case RotoPreviewMode::MaskOverlay:
            previewStr = QStringLiteral("overlay");
            break;
        case RotoPreviewMode::IsolatedAlpha:
            previewStr = QStringLiteral("alpha");
            break;
        case RotoPreviewMode::BeforeAfterSplit:
            previewStr = QStringLiteral("before_after");
            break;
        case RotoPreviewMode::FinalComposite:
            previewStr = QStringLiteral("composite");
            break;
        }
    }
    session.insert(QStringLiteral("preview_mode"), previewStr);
    session.insert(QStringLiteral("background_mode"), m_backgroundCombo ? m_backgroundCombo->currentData().toString() : QStringLiteral("transparent"));

    QJsonArray masksArr;
    for (int i = 0; i < m_masks.size(); ++i) {
        const StudioMaskLayerEntry &m = m_masks.at(i);
        QJsonObject geom = (i == m_selectedMaskIdx && m_canvas) ? m_canvas->exportMaskGeometryJson() : m.geometryJson;
        geom.insert(QStringLiteral("id"), m.id);
        geom.insert(QStringLiteral("name"), m.name);
        if (!geom.contains(QStringLiteral("tool"))) {
            geom.insert(QStringLiteral("tool"), QStringLiteral("polygon"));
        }
        geom.insert(QStringLiteral("combine_mode"), m.combineMode);
        geom.insert(QStringLiteral("inverted"), m.inverted);
        geom.insert(QStringLiteral("feather_px"), m.featherPx);
        geom.insert(QStringLiteral("expand_px"), m.expandPx);
        geom.insert(QStringLiteral("edge_refine_strength"), m.edgeRefine);
        geom.insert(QStringLiteral("opacity"), m.opacity);
        QJsonObject kfs;
        for (auto it = m.keyframedPaths.constBegin(); it != m.keyframedPaths.constEnd(); ++it) {
            kfs.insert(QString::number(it.key()), it.value());
        }
        geom.insert(QStringLiteral("keyframed_paths"), kfs);
        masksArr.append(geom);
    }
    session.insert(QStringLiteral("masks"), masksArr);
    return session;
}

void RotoStudioDock::refreshMaskList()
{
    m_syncingMaskUi = true;
    m_maskList->clear();
    for (const StudioMaskLayerEntry &m : m_masks) {
        const QString invTag = m.inverted ? QStringLiteral(" [INV]") : QString();
        m_maskList->addItem(QStringLiteral("%1 (%2)%3").arg(m.name, m.combineMode, invTag));
    }
    if (m_selectedMaskIdx >= 0 && m_selectedMaskIdx < m_maskList->count()) {
        m_maskList->setCurrentRow(m_selectedMaskIdx);
    }
    m_syncingMaskUi = false;
}

void RotoStudioDock::syncUiFromSelectedMask()
{
    m_syncingMaskUi = true;
    if (m_selectedMaskIdx >= 0 && m_selectedMaskIdx < m_masks.size()) {
        const StudioMaskLayerEntry &m = m_masks.at(m_selectedMaskIdx);
        const int idx = m_combineCombo->findData(m.combineMode);
        if (idx >= 0) {
            m_combineCombo->setCurrentIndex(idx);
        }
        m_invertCheck->setChecked(m.inverted);
        m_featherSlider->setValue(static_cast<int>(m.featherPx));
        m_expandSlider->setValue(static_cast<int>(m.expandPx));
        m_edgeRefineSlider->setValue(static_cast<int>(m.edgeRefine * 100.0));
        m_opacitySlider->setValue(static_cast<int>(m.opacity * 100.0));
        m_canvas->loadMaskGeometryJson(m.geometryJson);
        m_canvas->setFeatherPx(m.featherPx);
        m_canvas->setExpandPx(m.expandPx);
        m_canvas->setInverted(m.inverted);
    }
    m_syncingMaskUi = false;
}

void RotoStudioDock::handleMaskSelectionChanged(int row)
{
    if (m_syncingMaskUi || row < 0 || row >= m_masks.size()) {
        return;
    }
    if (m_selectedMaskIdx >= 0 && m_selectedMaskIdx < m_masks.size() && m_canvas) {
        m_masks[m_selectedMaskIdx].geometryJson = m_canvas->exportMaskGeometryJson();
    }
    m_selectedMaskIdx = row;
    syncUiFromSelectedMask();
}

void RotoStudioDock::handleMaskParamsChanged()
{
    if (m_syncingMaskUi || m_selectedMaskIdx < 0 || m_selectedMaskIdx >= m_masks.size()) {
        return;
    }
    StudioMaskLayerEntry &m = m_masks[m_selectedMaskIdx];
    m.combineMode = m_combineCombo->currentData().toString();
    m.inverted = m_invertCheck->isChecked();
    m.featherPx = static_cast<double>(m_featherSlider->value());
    m.expandPx = static_cast<double>(m_expandSlider->value());
    m.edgeRefine = static_cast<double>(m_edgeRefineSlider->value()) / 100.0;
    m.opacity = static_cast<double>(m_opacitySlider->value()) / 100.0;
    m_canvas->setFeatherPx(m.featherPx);
    m_canvas->setExpandPx(m.expandPx);
    m_canvas->setInverted(m.inverted);
    refreshMaskList();
}

void RotoStudioDock::handleToolComboChanged(int index)
{
    if (index >= 0 && m_canvas) {
        m_canvas->setActiveTool(static_cast<RotoTool>(m_toolCombo->itemData(index).toInt()));
    }
}

void RotoStudioDock::handlePreviewComboChanged(int index)
{
    if (index >= 0 && m_canvas) {
        m_canvas->setPreviewMode(static_cast<RotoPreviewMode>(m_previewCombo->itemData(index).toInt()));
    }
}

void RotoStudioDock::handleCanvasGeometryChanged()
{
    if (m_selectedMaskIdx >= 0 && m_selectedMaskIdx < m_masks.size() && m_canvas) {
        m_masks[m_selectedMaskIdx].geometryJson = m_canvas->exportMaskGeometryJson();
    }
}

void RotoStudioDock::handleAddKeyframeClicked()
{
    addKeyframeAtCurrentFrame();
}

void RotoStudioDock::handleCommitMaskClicked()
{
    Q_EMIT commitMaskToTimelineRequested(exportRotoSessionJson());
}

void RotoStudioDock::pushCanvas2DUndoSnapshot()
{
    m_canvasUndoStack.append(m_canvasLayers);
    m_canvasRedoStack.clear();
    if (m_layerUndoBtn) {
        m_layerUndoBtn->setEnabled(!m_canvasUndoStack.isEmpty());
    }
    if (m_layerRedoBtn) {
        m_layerRedoBtn->setEnabled(!m_canvasRedoStack.isEmpty());
    }
}

QString RotoStudioDock::addCanvas2DLayer(const QString &name, const QString &kind, const QString &imagePath, const QString &text)
{
    pushCanvas2DUndoSnapshot();
    StudioCanvas2DLayer layer;
    layer.id = QStringLiteral("layer_") + QUuid::createUuid().toString(QUuid::WithoutBraces).left(8);
    layer.name = name;
    layer.kind = kind;
    layer.imagePath = imagePath;
    layer.text = text;
    m_canvasLayers.append(layer);
    refreshCanvas2DList();
    m_layerList->setCurrentRow(m_canvasLayers.size() - 1);
    return layer.id;
}

bool RotoStudioDock::moveSelectedCanvas2DLayer(int delta)
{
    const int row = m_layerList->currentRow();
    const int target = row + delta;
    if (row < 0 || row >= m_canvasLayers.size() || target < 0 || target >= m_canvasLayers.size()) {
        return false;
    }
    pushCanvas2DUndoSnapshot();
    m_canvasLayers.swapItemsAt(row, target);
    refreshCanvas2DList();
    m_layerList->setCurrentRow(target);
    return true;
}

bool RotoStudioDock::undoCanvas2D()
{
    if (m_canvasUndoStack.isEmpty()) {
        return false;
    }
    m_canvasRedoStack.append(m_canvasLayers);
    m_canvasLayers = m_canvasUndoStack.takeLast();
    refreshCanvas2DList();
    return true;
}

bool RotoStudioDock::redoCanvas2D()
{
    if (m_canvasRedoStack.isEmpty()) {
        return false;
    }
    m_canvasUndoStack.append(m_canvasLayers);
    m_canvasLayers = m_canvasRedoStack.takeLast();
    refreshCanvas2DList();
    return true;
}

QJsonObject RotoStudioDock::exportCanvas2DJson() const
{
    QJsonObject obj;
    obj.insert(QStringLiteral("width"), 1920);
    obj.insert(QStringLiteral("height"), 1080);
    QJsonArray arr;
    for (int i = 0; i < m_canvasLayers.size(); ++i) {
        const StudioCanvas2DLayer &l = m_canvasLayers.at(i);
        QJsonObject lo;
        lo.insert(QStringLiteral("layer_id"), l.id);
        lo.insert(QStringLiteral("name"), l.name);
        lo.insert(QStringLiteral("kind"), l.kind);
        lo.insert(QStringLiteral("blend_mode"), l.blendMode);
        lo.insert(QStringLiteral("opacity"), l.opacity);
        lo.insert(QStringLiteral("visible"), l.visible);
        lo.insert(QStringLiteral("z_index"), i);
        lo.insert(QStringLiteral("image_path"), l.imagePath);
        lo.insert(QStringLiteral("text"), l.text);
        arr.append(lo);
    }
    obj.insert(QStringLiteral("layers"), arr);
    return obj;
}

void RotoStudioDock::refreshCanvas2DList()
{
    m_syncingLayerUi = true;
    const int prevRow = m_layerList->currentRow();
    m_layerList->clear();
    for (const StudioCanvas2DLayer &l : m_canvasLayers) {
        const QString vis = l.visible ? QStringLiteral("●") : QStringLiteral("○");
        m_layerList->addItem(QStringLiteral("%1 %2 [%3 • %4]").arg(vis, l.name, l.kind, l.blendMode));
    }
    if (prevRow >= 0 && prevRow < m_layerList->count()) {
        m_layerList->setCurrentRow(prevRow);
    }
    if (m_layerUndoBtn) {
        m_layerUndoBtn->setEnabled(!m_canvasUndoStack.isEmpty());
    }
    if (m_layerRedoBtn) {
        m_layerRedoBtn->setEnabled(!m_canvasRedoStack.isEmpty());
    }
    m_syncingLayerUi = false;
}

void RotoStudioDock::handleCanvasLayerSelectionChanged(int row)
{
    if (m_syncingLayerUi || row < 0 || row >= m_canvasLayers.size()) {
        return;
    }
    m_syncingLayerUi = true;
    const StudioCanvas2DLayer &l = m_canvasLayers.at(row);
    m_layerBlendCombo->setCurrentText(l.blendMode);
    m_layerOpacitySlider->setValue(static_cast<int>(l.opacity * 100.0));
    m_layerVisibleCheck->setChecked(l.visible);
    m_syncingLayerUi = false;
}

void RotoStudioDock::handleCanvasLayerPropsChanged()
{
    if (m_syncingLayerUi) {
        return;
    }
    const int row = m_layerList->currentRow();
    if (row < 0 || row >= m_canvasLayers.size()) {
        return;
    }
    StudioCanvas2DLayer &l = m_canvasLayers[row];
    l.blendMode = m_layerBlendCombo->currentText();
    l.opacity = static_cast<double>(m_layerOpacitySlider->value()) / 100.0;
    l.visible = m_layerVisibleCheck->isChecked();
    refreshCanvas2DList();
}

QString RotoStudioDock::addStoryboardShot(const QString &title, double durationSec, const QString &dialogue, const QString &action)
{
    StudioStoryboardCard card;
    card.id = QStringLiteral("shot_") + QUuid::createUuid().toString(QUuid::WithoutBraces).left(8);
    card.shotNumber = m_storyboardShots.size() + 1;
    card.title = title.isEmpty() ? QStringLiteral("Shot %1").arg(card.shotNumber) : title;
    card.durationSec = qMax(0.25, durationSec);
    card.dialogue = dialogue;
    card.action = action;
    m_storyboardShots.append(card);
    refreshStoryboardList();
    m_shotList->setCurrentRow(m_storyboardShots.size() - 1);
    return card.id;
}

bool RotoStudioDock::deleteSelectedStoryboardShot()
{
    const int row = m_shotList->currentRow();
    if (row < 0 || row >= m_storyboardShots.size()) {
        return false;
    }
    m_storyboardShots.removeAt(row);
    for (int i = 0; i < m_storyboardShots.size(); ++i) {
        m_storyboardShots[i].shotNumber = i + 1;
    }
    refreshStoryboardList();
    return true;
}

bool RotoStudioDock::moveSelectedStoryboardShot(int delta)
{
    const int row = m_shotList->currentRow();
    const int target = row + delta;
    if (row < 0 || row >= m_storyboardShots.size() || target < 0 || target >= m_storyboardShots.size()) {
        return false;
    }
    m_storyboardShots.swapItemsAt(row, target);
    for (int i = 0; i < m_storyboardShots.size(); ++i) {
        m_storyboardShots[i].shotNumber = i + 1;
    }
    refreshStoryboardList();
    m_shotList->setCurrentRow(target);
    return true;
}

QJsonObject RotoStudioDock::exportStoryboardJson() const
{
    QJsonObject obj;
    obj.insert(QStringLiteral("title"), QStringLiteral("Bang On Pro Storyboard"));
    QJsonArray arr;
    for (const StudioStoryboardCard &c : m_storyboardShots) {
        QJsonObject co;
        co.insert(QStringLiteral("id"), c.id);
        co.insert(QStringLiteral("shot_number"), c.shotNumber);
        co.insert(QStringLiteral("title"), c.title);
        co.insert(QStringLiteral("dialogue"), c.dialogue);
        co.insert(QStringLiteral("action"), c.action);
        co.insert(QStringLiteral("prompt"), c.prompt);
        co.insert(QStringLiteral("duration_sec"), c.durationSec);
        co.insert(QStringLiteral("image_path"), c.imagePath);
        arr.append(co);
    }
    obj.insert(QStringLiteral("shots"), arr);
    return obj;
}

void RotoStudioDock::refreshStoryboardList()
{
    m_syncingShotUi = true;
    const int prevRow = m_shotList->currentRow();
    m_shotList->clear();
    for (const StudioStoryboardCard &c : m_storyboardShots) {
        m_shotList->addItem(QStringLiteral("#%1  %2 (%3s)").arg(c.shotNumber).arg(c.title).arg(c.durationSec, 0, 'f', 1));
    }
    if (prevRow >= 0 && prevRow < m_shotList->count()) {
        m_shotList->setCurrentRow(prevRow);
    }
    m_syncingShotUi = false;
}

void RotoStudioDock::handleStoryboardSelectionChanged(int row)
{
    if (m_syncingShotUi || row < 0 || row >= m_storyboardShots.size()) {
        return;
    }
    m_syncingShotUi = true;
    const StudioStoryboardCard &c = m_storyboardShots.at(row);
    m_shotTitleEdit->setText(c.title);
    m_shotDialogueEdit->setText(c.dialogue);
    m_shotActionEdit->setText(c.action);
    m_shotPromptEdit->setText(c.prompt);
    m_shotDurationSpin->setValue(c.durationSec);
    m_syncingShotUi = false;
}

void RotoStudioDock::handleStoryboardPropsChanged()
{
    if (m_syncingShotUi) {
        return;
    }
    const int row = m_shotList->currentRow();
    if (row < 0 || row >= m_storyboardShots.size()) {
        return;
    }
    StudioStoryboardCard &c = m_storyboardShots[row];
    c.title = m_shotTitleEdit->text();
    c.dialogue = m_shotDialogueEdit->text();
    c.action = m_shotActionEdit->text();
    c.prompt = m_shotPromptEdit->text();
    c.durationSec = m_shotDurationSpin->value();
    refreshStoryboardList();
}

} // namespace BangOnPro
