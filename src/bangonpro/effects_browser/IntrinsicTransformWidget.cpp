/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "IntrinsicTransformWidget.h"

#include <QCheckBox>
#include <QComboBox>
#include <QDoubleSpinBox>
#include <QFrame>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QIcon>
#include <QLabel>
#include <QLocale>
#include <QSignalBlocker>
#include <QSlider>
#include <QToolButton>
#include <QVBoxLayout>
#include <cmath>
#include <utility>

#if !defined(BANGONPRO_STANDALONE_TEST) && defined(__has_include)
#if __has_include("effects/effectstack/model/effectstackmodel.hpp") && __has_include("assets/keyframes/model/keyframemodellist.hpp") &&                        \
                                                                                     __has_include("kdenlivesettings.h")
#define BANGONPRO_HAS_KDENLIVE_CORE 1
#include "assets/keyframes/model/keyframemodellist.hpp"
#include "assets/model/assetparametermodel.hpp"
#include "core.h"
#include "effects/effectstack/model/effectitemmodel.hpp"
#include "effects/effectstack/model/effectstackmodel.hpp"
#include <KLocalizedString>
#endif
#endif

namespace BangOnPro {

namespace {

constexpr double kPi = 3.14159265358979323846;

QString formatCompactNumber(double value, int maxDecimals)
{
    QString s = QLocale::c().toString(value, 'f', maxDecimals);
    if (s.contains(QLatin1Char('.'))) {
        while (s.endsWith(QLatin1Char('0'))) {
            s.chop(1);
        }
        if (s.endsWith(QLatin1Char('.'))) {
            s.chop(1);
        }
    }
    if (s == QStringLiteral("-0")) {
        s = QStringLiteral("0");
    }
    return s;
}

} // namespace

IntrinsicTransformWidget::IntrinsicTransformWidget(QWidget *parent)
    : QWidget(parent)
{
    setObjectName(QStringLiteral("BangOnProIntrinsicTransformWidget"));
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Maximum);
    buildUi();
    setNeutralUiValues();
    updateKeyframeButtonsState();
}

void IntrinsicTransformWidget::buildUi()
{
    setObjectName(QStringLiteral("intrinsicTransformWidget"));
    setStyleSheet(QStringLiteral("QWidget#intrinsicTransformWidget { background-color: #141416; }"));
    auto *rootLayout = new QVBoxLayout(this);
    rootLayout->setContentsMargins(4, 4, 4, 4);
    rootLayout->setSpacing(0);

    auto *card = new QFrame(this);
    card->setObjectName(QStringLiteral("intrinsicTransformCard"));
    card->setFrameShape(QFrame::StyledPanel);
    card->setStyleSheet(QStringLiteral("QFrame#intrinsicTransformCard {"
                                       "  border: 1px solid #2A2A2D;"
                                       "  border-radius: 4px;"
                                       "  background-color: #18181A;"
                                       "}"
                                       "QFrame#intrinsicTransformCard QLabel {"
                                       "  color: #B4B4BC;"
                                       "  background: transparent;"
                                       "}"));
    auto *cardLayout = new QVBoxLayout(card);
    cardLayout->setContentsMargins(6, 4, 6, 6);
    cardLayout->setSpacing(4);

    // Header row
    auto *headerRow = new QHBoxLayout();
    headerRow->setContentsMargins(0, 0, 0, 0);
    headerRow->setSpacing(4);

    m_collapseBtn = new QToolButton(card);
    m_collapseBtn->setObjectName(QStringLiteral("intrinsicCollapseBtn"));
    m_collapseBtn->setCheckable(true);
    m_collapseBtn->setChecked(true);
    m_collapseBtn->setAutoRaise(true);
    m_collapseBtn->setText(QStringLiteral("▾ Motion / Transform"));
    m_collapseBtn->setToolButtonStyle(Qt::ToolButtonTextOnly);
    QFont headerFont = m_collapseBtn->font();
    headerFont.setBold(true);
    m_collapseBtn->setFont(headerFont);
    connect(m_collapseBtn, &QToolButton::toggled, this, &IntrinsicTransformWidget::onCollapseToggled);

    m_enableCheck = new QCheckBox(card);
    m_enableCheck->setObjectName(QStringLiteral("intrinsicEnableCheck"));
    m_enableCheck->setChecked(true);
    m_enableCheck->setToolTip(QStringLiteral("Enable / disable Motion & Transform"));
    connect(m_enableCheck, &QCheckBox::toggled, this, &IntrinsicTransformWidget::onEnableToggled);

    m_prevKfrBtn = new QToolButton(card);
    m_prevKfrBtn->setObjectName(QStringLiteral("intrinsicPrevKfrBtn"));
    m_prevKfrBtn->setAutoRaise(true);
    m_prevKfrBtn->setText(QStringLiteral("◀"));
    m_prevKfrBtn->setToolTip(QStringLiteral("Previous keyframe"));
    connect(m_prevKfrBtn, &QToolButton::clicked, this, &IntrinsicTransformWidget::goToPreviousKeyframe);

    m_toggleKfrBtn = new QToolButton(card);
    m_toggleKfrBtn->setObjectName(QStringLiteral("intrinsicToggleKfrBtn"));
    m_toggleKfrBtn->setAutoRaise(true);
    m_toggleKfrBtn->setCheckable(true);
    m_toggleKfrBtn->setText(QStringLiteral("◆"));
    m_toggleKfrBtn->setToolTip(QStringLiteral("Add / remove keyframe at playhead"));
    connect(m_toggleKfrBtn, &QToolButton::clicked, this, &IntrinsicTransformWidget::toggleKeyframeAtCurrentFrame);

    m_nextKfrBtn = new QToolButton(card);
    m_nextKfrBtn->setObjectName(QStringLiteral("intrinsicNextKfrBtn"));
    m_nextKfrBtn->setAutoRaise(true);
    m_nextKfrBtn->setText(QStringLiteral("▶"));
    m_nextKfrBtn->setToolTip(QStringLiteral("Next keyframe"));
    connect(m_nextKfrBtn, &QToolButton::clicked, this, &IntrinsicTransformWidget::goToNextKeyframe);

    m_resetBtn = new QToolButton(card);
    m_resetBtn->setObjectName(QStringLiteral("intrinsicResetBtn"));
    m_resetBtn->setAutoRaise(true);
    const QIcon resetIcon = QIcon::fromTheme(QStringLiteral("view-refresh"));
    if (!resetIcon.isNull()) {
        m_resetBtn->setIcon(resetIcon);
    } else {
        m_resetBtn->setText(QStringLiteral("↺"));
    }
    m_resetBtn->setToolTip(QStringLiteral("Reset Motion / Transform to defaults"));
    connect(m_resetBtn, &QToolButton::clicked, this, &IntrinsicTransformWidget::resetTransform);

    headerRow->addWidget(m_collapseBtn, 1);
    headerRow->addWidget(m_enableCheck, 0);
    headerRow->addWidget(m_prevKfrBtn, 0);
    headerRow->addWidget(m_toggleKfrBtn, 0);
    headerRow->addWidget(m_nextKfrBtn, 0);
    headerRow->addWidget(m_resetBtn, 0);
    cardLayout->addLayout(headerRow);

    // Body controls
    m_bodyFrame = new QFrame(card);
    m_bodyFrame->setFrameShape(QFrame::NoFrame);
    auto *grid = new QGridLayout(m_bodyFrame);
    grid->setContentsMargins(2, 4, 2, 2);
    grid->setHorizontalSpacing(6);
    grid->setVerticalSpacing(4);

    int row = 0;

    // Position X / Y
    grid->addWidget(new QLabel(QStringLiteral("Position"), m_bodyFrame), row, 0);
    auto *posRow = new QHBoxLayout();
    posRow->setContentsMargins(0, 0, 0, 0);
    posRow->setSpacing(4);

    m_posXSpin = new QDoubleSpinBox(m_bodyFrame);
    m_posXSpin->setObjectName(QStringLiteral("intrinsicPosXSpin"));
    m_posXSpin->setRange(-10000.0, 10000.0);
    m_posXSpin->setDecimals(1);
    m_posXSpin->setSingleStep(1.0);
    m_posXSpin->setPrefix(QStringLiteral("X "));
    m_posXSpin->setSuffix(QStringLiteral(" px"));

    m_posYSpin = new QDoubleSpinBox(m_bodyFrame);
    m_posYSpin->setObjectName(QStringLiteral("intrinsicPosYSpin"));
    m_posYSpin->setRange(-10000.0, 10000.0);
    m_posYSpin->setDecimals(1);
    m_posYSpin->setSingleStep(1.0);
    m_posYSpin->setPrefix(QStringLiteral("Y "));
    m_posYSpin->setSuffix(QStringLiteral(" px"));

    posRow->addWidget(m_posXSpin, 1);
    posRow->addWidget(m_posYSpin, 1);
    grid->addLayout(posRow, row, 1, 1, 2);
    ++row;

    // Position quick scrub sliders
    auto *posSliderRow = new QHBoxLayout();
    posSliderRow->setContentsMargins(0, 0, 0, 0);
    posSliderRow->setSpacing(4);
    m_posXSlider = new QSlider(Qt::Horizontal, m_bodyFrame);
    m_posXSlider->setRange(-1920, 1920);
    m_posXSlider->setValue(0);
    m_posYSlider = new QSlider(Qt::Horizontal, m_bodyFrame);
    m_posYSlider->setRange(-1080, 1080);
    m_posYSlider->setValue(0);
    posSliderRow->addWidget(m_posXSlider, 1);
    posSliderRow->addWidget(m_posYSlider, 1);
    grid->addLayout(posSliderRow, row, 1, 1, 2);
    ++row;

    // Scale (%) + Aspect Ratio lock
    grid->addWidget(new QLabel(QStringLiteral("Scale"), m_bodyFrame), row, 0);
    m_scaleSlider = new QSlider(Qt::Horizontal, m_bodyFrame);
    m_scaleSlider->setObjectName(QStringLiteral("intrinsicScaleSlider"));
    m_scaleSlider->setRange(1, 500);
    m_scaleSlider->setValue(100);

    auto *scaleSpinRow = new QHBoxLayout();
    scaleSpinRow->setContentsMargins(0, 0, 0, 0);
    scaleSpinRow->setSpacing(4);

    m_scaleXSpin = new QDoubleSpinBox(m_bodyFrame);
    m_scaleXSpin->setObjectName(QStringLiteral("intrinsicScaleXSpin"));
    m_scaleXSpin->setRange(0.1, 1000.0);
    m_scaleXSpin->setDecimals(1);
    m_scaleXSpin->setSingleStep(1.0);
    m_scaleXSpin->setSuffix(QStringLiteral(" %"));

    m_aspectLockBtn = new QToolButton(m_bodyFrame);
    m_aspectLockBtn->setObjectName(QStringLiteral("intrinsicAspectLockBtn"));
    m_aspectLockBtn->setCheckable(true);
    m_aspectLockBtn->setChecked(true);
    m_aspectLockBtn->setAutoRaise(true);
    m_aspectLockBtn->setText(QStringLiteral("🔒"));
    m_aspectLockBtn->setToolTip(QStringLiteral("Lock uniform Scale X / Scale Y aspect ratio"));

    m_scaleYSpin = new QDoubleSpinBox(m_bodyFrame);
    m_scaleYSpin->setObjectName(QStringLiteral("intrinsicScaleYSpin"));
    m_scaleYSpin->setRange(0.1, 1000.0);
    m_scaleYSpin->setDecimals(1);
    m_scaleYSpin->setSingleStep(1.0);
    m_scaleYSpin->setSuffix(QStringLiteral(" %"));
    m_scaleYSpin->setEnabled(false);

    scaleSpinRow->addWidget(m_scaleXSpin, 1);
    scaleSpinRow->addWidget(m_aspectLockBtn, 0);
    scaleSpinRow->addWidget(m_scaleYSpin, 1);

    grid->addWidget(m_scaleSlider, row, 1);
    grid->addLayout(scaleSpinRow, row, 2);
    ++row;

    // Rotation (°)
    grid->addWidget(new QLabel(QStringLiteral("Rotation"), m_bodyFrame), row, 0);
    m_rotationSlider = new QSlider(Qt::Horizontal, m_bodyFrame);
    m_rotationSlider->setObjectName(QStringLiteral("intrinsicRotationSlider"));
    m_rotationSlider->setRange(-360, 360);
    m_rotationSlider->setValue(0);

    m_rotationSpin = new QDoubleSpinBox(m_bodyFrame);
    m_rotationSpin->setObjectName(QStringLiteral("intrinsicRotationSpin"));
    m_rotationSpin->setRange(-360.0, 360.0);
    m_rotationSpin->setDecimals(1);
    m_rotationSpin->setSingleStep(1.0);
    m_rotationSpin->setSuffix(QStringLiteral(" °"));

    grid->addWidget(m_rotationSlider, row, 1);
    grid->addWidget(m_rotationSpin, row, 2);
    ++row;

    // Anchor Point X / Y (%)
    grid->addWidget(new QLabel(QStringLiteral("Anchor"), m_bodyFrame), row, 0);
    auto *anchorRow = new QHBoxLayout();
    anchorRow->setContentsMargins(0, 0, 0, 0);
    anchorRow->setSpacing(4);

    m_anchorXSpin = new QDoubleSpinBox(m_bodyFrame);
    m_anchorXSpin->setObjectName(QStringLiteral("intrinsicAnchorXSpin"));
    m_anchorXSpin->setRange(-200.0, 300.0);
    m_anchorXSpin->setDecimals(1);
    m_anchorXSpin->setSingleStep(1.0);
    m_anchorXSpin->setPrefix(QStringLiteral("X "));
    m_anchorXSpin->setSuffix(QStringLiteral(" %"));

    m_anchorYSpin = new QDoubleSpinBox(m_bodyFrame);
    m_anchorYSpin->setObjectName(QStringLiteral("intrinsicAnchorYSpin"));
    m_anchorYSpin->setRange(-200.0, 300.0);
    m_anchorYSpin->setDecimals(1);
    m_anchorYSpin->setSingleStep(1.0);
    m_anchorYSpin->setPrefix(QStringLiteral("Y "));
    m_anchorYSpin->setSuffix(QStringLiteral(" %"));

    anchorRow->addWidget(m_anchorXSpin, 1);
    anchorRow->addWidget(m_anchorYSpin, 1);
    grid->addLayout(anchorRow, row, 1, 1, 2);
    ++row;

    // Opacity (%)
    grid->addWidget(new QLabel(QStringLiteral("Opacity"), m_bodyFrame), row, 0);
    m_opacitySlider = new QSlider(Qt::Horizontal, m_bodyFrame);
    m_opacitySlider->setObjectName(QStringLiteral("intrinsicOpacitySlider"));
    m_opacitySlider->setRange(0, 100);
    m_opacitySlider->setValue(100);

    m_opacitySpin = new QDoubleSpinBox(m_bodyFrame);
    m_opacitySpin->setObjectName(QStringLiteral("intrinsicOpacitySpin"));
    m_opacitySpin->setRange(0.0, 100.0);
    m_opacitySpin->setDecimals(1);
    m_opacitySpin->setSingleStep(1.0);
    m_opacitySpin->setSuffix(QStringLiteral(" %"));

    grid->addWidget(m_opacitySlider, row, 1);
    grid->addWidget(m_opacitySpin, row, 2);
    ++row;

    // Blend Mode / Compositing (exact MLT qtblend compositing codes from data/effects/qtblend.xml)
    grid->addWidget(new QLabel(QStringLiteral("Blend Mode"), m_bodyFrame), row, 0);
    m_blendModeCombo = new QComboBox(m_bodyFrame);
    m_blendModeCombo->setObjectName(QStringLiteral("intrinsicBlendModeCombo"));
    m_blendModeCombo->addItem(QStringLiteral("Normal (Over)"), 0);
    m_blendModeCombo->addItem(QStringLiteral("Multiply"), 13);
    m_blendModeCombo->addItem(QStringLiteral("Screen"), 14);
    m_blendModeCombo->addItem(QStringLiteral("Overlay"), 15);
    m_blendModeCombo->addItem(QStringLiteral("Add"), 12);
    m_blendModeCombo->addItem(QStringLiteral("Darken"), 16);
    m_blendModeCombo->addItem(QStringLiteral("Lighten"), 17);
    m_blendModeCombo->addItem(QStringLiteral("Soft Light"), 21);
    m_blendModeCombo->addItem(QStringLiteral("Hard Light"), 20);
    m_blendModeCombo->addItem(QStringLiteral("Difference"), 22);
    grid->addWidget(m_blendModeCombo, row, 1, 1, 2);

    grid->setColumnStretch(0, 0);
    grid->setColumnStretch(1, 1);
    grid->setColumnStretch(2, 1);

    cardLayout->addWidget(m_bodyFrame);
    rootLayout->addWidget(card);

    // Connect control signals
    connect(m_posXSpin, QOverload<double>::of(&QDoubleSpinBox::valueChanged), this, [this](double v) {
        if (!m_syncingUi) {
            QSignalBlocker bk(m_posXSlider);
            m_posXSlider->setValue(qBound(m_posXSlider->minimum(), qRound(v), m_posXSlider->maximum()));
            onPositionEdited();
        }
    });
    connect(m_posYSpin, QOverload<double>::of(&QDoubleSpinBox::valueChanged), this, [this](double v) {
        if (!m_syncingUi) {
            QSignalBlocker bk(m_posYSlider);
            m_posYSlider->setValue(qBound(m_posYSlider->minimum(), qRound(v), m_posYSlider->maximum()));
            onPositionEdited();
        }
    });
    connect(m_posXSlider, &QSlider::valueChanged, this, [this](int v) {
        if (!m_syncingUi) {
            QSignalBlocker bk(m_posXSpin);
            m_posXSpin->setValue(static_cast<double>(v));
            onPositionEdited();
        }
    });
    connect(m_posYSlider, &QSlider::valueChanged, this, [this](int v) {
        if (!m_syncingUi) {
            QSignalBlocker bk(m_posYSpin);
            m_posYSpin->setValue(static_cast<double>(v));
            onPositionEdited();
        }
    });

    connect(m_scaleXSpin, QOverload<double>::of(&QDoubleSpinBox::valueChanged), this, &IntrinsicTransformWidget::onScaleXEdited);
    connect(m_scaleYSpin, QOverload<double>::of(&QDoubleSpinBox::valueChanged), this, &IntrinsicTransformWidget::onScaleYEdited);
    connect(m_scaleSlider, &QSlider::valueChanged, this, &IntrinsicTransformWidget::onScaleSliderMoved);
    connect(m_aspectLockBtn, &QToolButton::toggled, this, &IntrinsicTransformWidget::onAspectLockToggled);

    connect(m_rotationSpin, QOverload<double>::of(&QDoubleSpinBox::valueChanged), this, &IntrinsicTransformWidget::onRotationSpinEdited);
    connect(m_rotationSlider, &QSlider::valueChanged, this, &IntrinsicTransformWidget::onRotationSliderMoved);

    connect(m_anchorXSpin, QOverload<double>::of(&QDoubleSpinBox::valueChanged), this, [this](double) { onAnchorEdited(); });
    connect(m_anchorYSpin, QOverload<double>::of(&QDoubleSpinBox::valueChanged), this, [this](double) { onAnchorEdited(); });

    connect(m_opacitySpin, QOverload<double>::of(&QDoubleSpinBox::valueChanged), this, &IntrinsicTransformWidget::onOpacitySpinEdited);
    connect(m_opacitySlider, &QSlider::valueChanged, this, &IntrinsicTransformWidget::onOpacitySliderMoved);

    connect(m_blendModeCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, &IntrinsicTransformWidget::onBlendModeChanged);
}

// =============================================================================
// Pure Anchor-Compensated Geometry Math (public static, unit-tested directly)
// =============================================================================

CompensatedRect IntrinsicTransformWidget::computeCompensatedRect(double frameW, double frameH, double posX, double posY, double scaleXPercent,
                                                                 double scaleYPercent, double rotationDeg, double anchorXNorm, double anchorYNorm)
{
    CompensatedRect r;
    const double safeW = frameW > 0.0 ? frameW : 1920.0;
    const double safeH = frameH > 0.0 ? frameH : 1080.0;

    r.width = safeW * (scaleXPercent / 100.0);
    r.height = safeH * (scaleYPercent / 100.0);

    // Vector from anchor point to clip geometric center (0.5, 0.5) in scaled clip space
    const double dxLocal = (0.5 - anchorXNorm) * r.width;
    const double dyLocal = (0.5 - anchorYNorm) * r.height;

    // Rotate around the anchor point in screen coordinates (+X right, +Y down)
    const double theta = rotationDeg * kPi / 180.0;
    const double c = std::cos(theta);
    const double s = std::sin(theta);
    const double dxRot = dxLocal * c - dyLocal * s;
    const double dyRot = dxLocal * s + dyLocal * c;

    // Destination center on canvas so that MLT qtblend (rotate_center=1) pivots around anchor
    const double centerX = anchorXNorm * safeW + posX + dxRot;
    const double centerY = anchorYNorm * safeH + posY + dyRot;

    r.x = centerX - r.width * 0.5;
    r.y = centerY - r.height * 0.5;
    return r;
}

DecomposedTransform IntrinsicTransformWidget::decomposeRect(double frameW, double frameH, double rectX, double rectY, double rectW, double rectH,
                                                            double rotationDeg, double anchorXNorm, double anchorYNorm)
{
    DecomposedTransform out;
    const double safeW = frameW > 0.0 ? frameW : 1920.0;
    const double safeH = frameH > 0.0 ? frameH : 1080.0;

    out.scaleXPercent = (rectW / safeW) * 100.0;
    out.scaleYPercent = (rectH / safeH) * 100.0;

    const double centerX = rectX + rectW * 0.5;
    const double centerY = rectY + rectH * 0.5;

    const double dxLocal = (0.5 - anchorXNorm) * rectW;
    const double dyLocal = (0.5 - anchorYNorm) * rectH;

    const double theta = rotationDeg * kPi / 180.0;
    const double c = std::cos(theta);
    const double s = std::sin(theta);
    const double dxRot = dxLocal * c - dyLocal * s;
    const double dyRot = dxLocal * s + dyLocal * c;

    out.posX = centerX - dxRot - anchorXNorm * safeW;
    out.posY = centerY - dyRot - anchorYNorm * safeH;
    return out;
}

QString IntrinsicTransformWidget::formatMltRect(double x, double y, double w, double h, double opacity01)
{
    const double clampedOpacity = qBound(0.0, opacity01, 1.0);
    return QStringLiteral("%1 %2 %3 %4 %5").arg(qRound(x)).arg(qRound(y)).arg(qRound(w)).arg(qRound(h)).arg(formatCompactNumber(clampedOpacity, 6));
}

bool IntrinsicTransformWidget::parseMltRect(const QString &rectStr, double *outX, double *outY, double *outW, double *outH, double *outOpacity01)
{
    QString s = rectStr.trimmed();
    if (s.isEmpty()) {
        return false;
    }
    // If keyframe-encoded ("0=x y w h opacity;25=..."), take the first segment value after '='
    const int semiIdx = s.indexOf(QLatin1Char(';'));
    if (semiIdx >= 0) {
        s = s.left(semiIdx).trimmed();
    }
    const int eqIdx = s.indexOf(QLatin1Char('='));
    if (eqIdx >= 0) {
        s = s.mid(eqIdx + 1).trimmed();
    }

    // Handle both space-separated ("x y w h op") and legacy slash/colon ("x/y:wxh:op")
    s.replace(QLatin1Char('/'), QLatin1Char(' '));
    s.replace(QLatin1Char(':'), QLatin1Char(' '));
    s.replace(QLatin1Char('x'), QLatin1Char(' '));

    const QStringList parts = s.split(QLatin1Char(' '), Qt::SkipEmptyParts);
    if (parts.size() < 4) {
        return false;
    }

    bool okX = false;
    bool okY = false;
    bool okW = false;
    bool okH = false;
    const QLocale cLoc = QLocale::c();
    const double x = cLoc.toDouble(parts.at(0), &okX);
    const double y = cLoc.toDouble(parts.at(1), &okY);
    const double w = cLoc.toDouble(parts.at(2), &okW);
    const double h = cLoc.toDouble(parts.at(3), &okH);
    if (!okX || !okY || !okW || !okH) {
        return false;
    }

    double opacity = 1.0;
    if (parts.size() >= 5) {
        bool okOp = false;
        const double parsedOp = cLoc.toDouble(parts.at(4), &okOp);
        if (okOp) {
            opacity = parsedOp > 1.0 ? qBound(0.0, parsedOp / 100.0, 1.0) : qBound(0.0, parsedOp, 1.0);
        }
    }

    if (outX) {
        *outX = x;
    }
    if (outY) {
        *outY = y;
    }
    if (outW) {
        *outW = w;
    }
    if (outH) {
        *outH = h;
    }
    if (outOpacity01) {
        *outOpacity01 = opacity;
    }
    return true;
}

// =============================================================================
// EffectStackModel Binding & Lazy Materialization
// =============================================================================

void IntrinsicTransformWidget::setModel(std::shared_ptr<EffectStackModel> stack, QSize frameSize, int currentFrame)
{
    unsetModel();
    m_stack = std::move(stack);
    m_frameSize = (frameSize.isValid() && frameSize.width() > 0 && frameSize.height() > 0) ? frameSize : QSize(1920, 1080);
    m_currentFrame = currentFrame;

    {
        QSignalBlocker bx(m_posXSlider);
        QSignalBlocker by(m_posYSlider);
        m_posXSlider->setRange(-m_frameSize.width(), m_frameSize.width());
        m_posYSlider->setRange(-m_frameSize.height(), m_frameSize.height());
    }

#ifdef BANGONPRO_HAS_KDENLIVE_CORE
    if (m_stack) {
        connect(m_stack.get(), &EffectStackModel::modelChanged, this, &IntrinsicTransformWidget::syncFromModel);
        connect(m_stack.get(), &EffectStackModel::dataChanged, this, &IntrinsicTransformWidget::syncFromModel);
        bindExistingTransform();
    }
#endif
    syncFromModel();
}

void IntrinsicTransformWidget::unsetModel()
{
#ifdef BANGONPRO_HAS_KDENLIVE_CORE
    if (m_stack) {
        disconnect(m_stack.get(), nullptr, this, nullptr);
    }
    if (m_transformModel) {
        disconnect(m_transformModel.get(), nullptr, this, nullptr);
    }
#endif
    m_transformModel.reset();
    m_stack.reset();
    setNeutralUiValues();
    updateKeyframeButtonsState();
}

void IntrinsicTransformWidget::setCurrentFrame(int frame)
{
    if (m_currentFrame == frame) {
        return;
    }
    m_currentFrame = frame;
    if (m_transformModel) {
        syncFromModel();
    }
}

bool IntrinsicTransformWidget::hasMaterializedTransform() const
{
    return m_transformModel != nullptr;
}

void IntrinsicTransformWidget::bindExistingTransform()
{
#ifdef BANGONPRO_HAS_KDENLIVE_CORE
    if (!m_stack) {
        m_transformModel.reset();
        return;
    }
    std::shared_ptr<AssetParameterModel> existing = m_stack->getAssetModelById(QStringLiteral("qtblend"));
    if (existing == m_transformModel) {
        return;
    }
    if (m_transformModel) {
        disconnect(m_transformModel.get(), nullptr, this, nullptr);
    }
    m_transformModel = std::move(existing);
    if (m_transformModel) {
        connect(m_transformModel.get(), &AssetParameterModel::modelChanged, this, &IntrinsicTransformWidget::syncFromModel);
        connect(m_transformModel.get(), &AssetParameterModel::enabledChange, this, &IntrinsicTransformWidget::syncFromModel);
    }
#endif
}

bool IntrinsicTransformWidget::ensureMaterializedTransform()
{
#ifdef BANGONPRO_HAS_KDENLIVE_CORE
    if (m_transformModel) {
        return true;
    }
    if (!m_stack) {
        return false;
    }
    bindExistingTransform();
    if (m_transformModel) {
        return true;
    }
    // Lazily append a native undo-backed MLT qtblend effect on first edit
    if (!m_stack->appendEffect(QStringLiteral("qtblend"), true)) {
        return false;
    }
    bindExistingTransform();
    if (!m_transformModel) {
        return false;
    }
    m_transformModel->setParameter(QStringLiteral("rotate_center"), 1, false);
    if (!m_transformModel->getKeyframeModel()) {
        m_transformModel->prepareKeyframes();
    }
    return true;
#else
    return false;
#endif
}

void IntrinsicTransformWidget::syncFromModel()
{
    if (m_syncingUi) {
        return;
    }

#ifdef BANGONPRO_HAS_KDENLIVE_CORE
    if (m_stack) {
        bindExistingTransform();
    }
    if (!m_transformModel) {
        setNeutralUiValues();
        updateKeyframeButtonsState();
        return;
    }

    m_syncingUi = true;

    double anchorXNorm = 0.5;
    double anchorYNorm = 0.5;
    if (Mlt::Properties *props = m_transformModel->getAsset()) {
        const char *axStr = props->get("kdenlive:anchor_x");
        const char *ayStr = props->get("kdenlive:anchor_y");
        if (axStr != nullptr && axStr[0] != '\0') {
            bool ok = false;
            const double ax = QLocale::c().toDouble(QString::fromLatin1(axStr), &ok);
            if (ok) {
                anchorXNorm = ax;
            }
        }
        if (ayStr != nullptr && ayStr[0] != '\0') {
            bool ok = false;
            const double ay = QLocale::c().toDouble(QString::fromLatin1(ayStr), &ok);
            if (ok) {
                anchorYNorm = ay;
            }
        }
    }

    QString rectStr = m_transformModel->getParam(QStringLiteral("rect"));
    QString rotStr = m_transformModel->getParam(QStringLiteral("rotation"));
    const int compCode = m_transformModel->getParam(QStringLiteral("compositing")).toInt();

    auto kfr = m_transformModel->getKeyframeModel();
    if (kfr && !kfr->isEmpty()) {
        const QPersistentModelIndex rectIdx(m_transformModel->getParamIndexFromName(QStringLiteral("rect")));
        const QPersistentModelIndex rotIdx(m_transformModel->getParamIndexFromName(QStringLiteral("rotation")));
        if (rectIdx.isValid() && kfr->getKeyModel(rectIdx) != nullptr) {
            const QString interpRect = kfr->getInterpolatedValue(m_currentFrame, rectIdx).toString();
            if (!interpRect.isEmpty()) {
                rectStr = interpRect;
            }
        }
        if (rotIdx.isValid() && kfr->getKeyModel(rotIdx) != nullptr) {
            const QString interpRot = kfr->getInterpolatedValue(m_currentFrame, rotIdx).toString();
            if (!interpRot.isEmpty()) {
                rotStr = interpRot;
            }
        }
    }

    double rotDeg = 0.0;
    {
        QString rClean = rotStr.trimmed();
        const int semi = rClean.indexOf(QLatin1Char(';'));
        if (semi >= 0) {
            rClean = rClean.left(semi);
        }
        const int eq = rClean.indexOf(QLatin1Char('='));
        if (eq >= 0) {
            rClean = rClean.mid(eq + 1);
        }
        bool okRot = false;
        const double parsedRot = QLocale::c().toDouble(rClean, &okRot);
        if (okRot) {
            rotDeg = parsedRot;
        }
    }

    double rx = 0.0;
    double ry = 0.0;
    double rw = static_cast<double>(m_frameSize.width());
    double rh = static_cast<double>(m_frameSize.height());
    double op01 = 1.0;
    parseMltRect(rectStr, &rx, &ry, &rw, &rh, &op01);

    const DecomposedTransform dec =
        decomposeRect(static_cast<double>(m_frameSize.width()), static_cast<double>(m_frameSize.height()), rx, ry, rw, rh, rotDeg, anchorXNorm, anchorYNorm);

    const bool uniform = std::fabs(dec.scaleXPercent - dec.scaleYPercent) < 0.25;

    {
        QSignalBlocker b1(m_posXSpin);
        QSignalBlocker b2(m_posYSpin);
        QSignalBlocker b3(m_posXSlider);
        QSignalBlocker b4(m_posYSlider);
        QSignalBlocker b5(m_scaleXSpin);
        QSignalBlocker b6(m_scaleYSpin);
        QSignalBlocker b7(m_scaleSlider);
        QSignalBlocker b8(m_aspectLockBtn);
        QSignalBlocker b9(m_rotationSpin);
        QSignalBlocker b10(m_rotationSlider);
        QSignalBlocker b11(m_anchorXSpin);
        QSignalBlocker b12(m_anchorYSpin);
        QSignalBlocker b13(m_opacitySpin);
        QSignalBlocker b14(m_opacitySlider);
        QSignalBlocker b15(m_blendModeCombo);
        QSignalBlocker b16(m_enableCheck);

        m_posXSpin->setValue(dec.posX);
        m_posYSpin->setValue(dec.posY);
        m_posXSlider->setValue(qBound(m_posXSlider->minimum(), qRound(dec.posX), m_posXSlider->maximum()));
        m_posYSlider->setValue(qBound(m_posYSlider->minimum(), qRound(dec.posY), m_posYSlider->maximum()));

        m_aspectLockBtn->setChecked(uniform);
        m_scaleYSpin->setEnabled(!uniform);
        m_scaleXSpin->setValue(dec.scaleXPercent);
        m_scaleYSpin->setValue(dec.scaleYPercent);
        m_scaleSlider->setValue(qBound(m_scaleSlider->minimum(), qRound(dec.scaleXPercent), m_scaleSlider->maximum()));

        m_rotationSpin->setValue(rotDeg);
        m_rotationSlider->setValue(qBound(m_rotationSlider->minimum(), qRound(rotDeg), m_rotationSlider->maximum()));

        m_anchorXSpin->setValue(anchorXNorm * 100.0);
        m_anchorYSpin->setValue(anchorYNorm * 100.0);

        const double opPct = op01 * 100.0;
        m_opacitySpin->setValue(opPct);
        m_opacitySlider->setValue(qBound(0, qRound(opPct), 100));

        const int blendIdx = m_blendModeCombo->findData(compCode);
        m_blendModeCombo->setCurrentIndex(blendIdx >= 0 ? blendIdx : 0);

        auto effItem = std::static_pointer_cast<EffectItemModel>(m_transformModel);
        if (effItem) {
            m_enableCheck->setChecked(effItem->isEnabled());
        }
    }

    m_syncingUi = false;
    updateKeyframeButtonsState();
#else
    updateKeyframeButtonsState();
#endif
}

void IntrinsicTransformWidget::commitTransformToModel()
{
    if (m_syncingUi) {
        return;
    }

#ifdef BANGONPRO_HAS_KDENLIVE_CORE
    if (m_stack && !ensureMaterializedTransform()) {
        return;
    }
    if (m_transformModel) {
        m_syncingUi = true;

        const double anchorXNorm = m_anchorXSpin->value() / 100.0;
        const double anchorYNorm = m_anchorYSpin->value() / 100.0;
        const double scaleX = m_scaleXSpin->value();
        const double scaleY = m_aspectLockBtn->isChecked() ? scaleX : m_scaleYSpin->value();
        const double rotDeg = m_rotationSpin->value();
        const double op01 = m_opacitySpin->value() / 100.0;

        const CompensatedRect comp = computeCompensatedRect(static_cast<double>(m_frameSize.width()), static_cast<double>(m_frameSize.height()),
                                                            m_posXSpin->value(), m_posYSpin->value(), scaleX, scaleY, rotDeg, anchorXNorm, anchorYNorm);
        const QString rectStr = formatMltRect(comp.x, comp.y, comp.width, comp.height, op01);
        const QString rotStr = formatCompactNumber(rotDeg, 3);
        const int blendCode = m_blendModeCombo->currentData().toInt();
        const int distortFlag = (!m_aspectLockBtn->isChecked() && std::fabs(scaleX - scaleY) >= 0.05) ? 1 : 0;

        // Persist anchor point metadata on MLT filter properties for XML save/reopen fidelity
        if (Mlt::Properties *props = m_transformModel->getAsset()) {
            props->set("kdenlive:anchor_x", formatCompactNumber(anchorXNorm, 6).toLatin1().constData());
            props->set("kdenlive:anchor_y", formatCompactNumber(anchorYNorm, 6).toLatin1().constData());
        }

        m_transformModel->setParameter(QStringLiteral("rotate_center"), 1, false);
        m_transformModel->setParameter(QStringLiteral("distort"), distortFlag, false);
        m_transformModel->setParameter(QStringLiteral("compositing"), blendCode, false);

        auto kfr = m_transformModel->getKeyframeModel();
        const QModelIndex rectIdx = m_transformModel->getParamIndexFromName(QStringLiteral("rect"));
        const QModelIndex rotIdx = m_transformModel->getParamIndexFromName(QStringLiteral("rotation"));
        const QPersistentModelIndex rectPIdx(rectIdx);
        const QPersistentModelIndex rotPIdx(rotIdx);
        if (kfr && !kfr->isEmpty() && rectIdx.isValid() && rotIdx.isValid() && kfr->getKeyModel(rectPIdx) != nullptr && kfr->getKeyModel(rotPIdx) != nullptr &&
            pCore) {
            const GenTime targetPos = kfr->singleKeyframe() ? kfr->getPosAtIndex(0) : GenTime(m_currentFrame, pCore->getCurrentFps());
            const QString prevRect = kfr->getInterpolatedValue(targetPos, rectPIdx).toString();
            const QString prevRot = kfr->getInterpolatedValue(targetPos, rotPIdx).toString();
            kfr->updateMultiKeyframe(targetPos, QStringList{prevRect, prevRot}, QStringList{rectStr, rotStr}, QList<QModelIndex>{rectIdx, rotIdx});
        } else {
            m_transformModel->setParameter(QStringLiteral("rect"), rectStr, false);
            m_transformModel->setParameter(QStringLiteral("rotation"), rotStr, true);
        }

        m_syncingUi = false;
        updateKeyframeButtonsState();
    }
#endif

    Q_EMIT transformChanged();
}

void IntrinsicTransformWidget::setNeutralUiValues()
{
    m_syncingUi = true;
    {
        QSignalBlocker b1(m_posXSpin);
        QSignalBlocker b2(m_posYSpin);
        QSignalBlocker b3(m_posXSlider);
        QSignalBlocker b4(m_posYSlider);
        QSignalBlocker b5(m_scaleXSpin);
        QSignalBlocker b6(m_scaleYSpin);
        QSignalBlocker b7(m_scaleSlider);
        QSignalBlocker b8(m_aspectLockBtn);
        QSignalBlocker b9(m_rotationSpin);
        QSignalBlocker b10(m_rotationSlider);
        QSignalBlocker b11(m_anchorXSpin);
        QSignalBlocker b12(m_anchorYSpin);
        QSignalBlocker b13(m_opacitySpin);
        QSignalBlocker b14(m_opacitySlider);
        QSignalBlocker b15(m_blendModeCombo);
        QSignalBlocker b16(m_enableCheck);

        m_enableCheck->setChecked(true);
        m_posXSpin->setValue(0.0);
        m_posYSpin->setValue(0.0);
        m_posXSlider->setValue(0);
        m_posYSlider->setValue(0);
        m_aspectLockBtn->setChecked(true);
        m_scaleYSpin->setEnabled(false);
        m_scaleXSpin->setValue(100.0);
        m_scaleYSpin->setValue(100.0);
        m_scaleSlider->setValue(100);
        m_rotationSpin->setValue(0.0);
        m_rotationSlider->setValue(0);
        m_anchorXSpin->setValue(50.0);
        m_anchorYSpin->setValue(50.0);
        m_opacitySpin->setValue(100.0);
        m_opacitySlider->setValue(100);
        m_blendModeCombo->setCurrentIndex(0);
    }
    m_syncingUi = false;
}

void IntrinsicTransformWidget::updateKeyframeButtonsState()
{
#ifdef BANGONPRO_HAS_KDENLIVE_CORE
    bool hasAtCurrent = false;
    bool hasPrev = false;
    bool hasNext = false;
    if (m_transformModel && pCore) {
        auto kfr = m_transformModel->getKeyframeModel();
        if (kfr && !kfr->isEmpty()) {
            const GenTime pos(m_currentFrame, pCore->getCurrentFps());
            hasAtCurrent = kfr->hasKeyframe(m_currentFrame);
            bool okPrev = false;
            kfr->getPrevKeyframe(pos, &okPrev);
            hasPrev = okPrev;
            bool okNext = false;
            kfr->getNextKeyframe(pos, &okNext);
            hasNext = okNext;
        }
    }
    if (m_toggleKfrBtn) {
        QSignalBlocker bk(m_toggleKfrBtn);
        m_toggleKfrBtn->setChecked(hasAtCurrent);
    }
    if (m_prevKfrBtn) {
        m_prevKfrBtn->setEnabled(hasPrev);
    }
    if (m_nextKfrBtn) {
        m_nextKfrBtn->setEnabled(hasNext);
    }
#else
    if (m_toggleKfrBtn) {
        QSignalBlocker bk(m_toggleKfrBtn);
        m_toggleKfrBtn->setChecked(false);
    }
    if (m_prevKfrBtn) {
        m_prevKfrBtn->setEnabled(false);
    }
    if (m_nextKfrBtn) {
        m_nextKfrBtn->setEnabled(false);
    }
#endif
}

// =============================================================================
// Public Getters / Setters & Slots
// =============================================================================

bool IntrinsicTransformWidget::isExpanded() const
{
    return m_bodyFrame && m_bodyFrame->isVisible();
}

void IntrinsicTransformWidget::setExpanded(bool expanded)
{
    if (m_collapseBtn) {
        m_collapseBtn->setChecked(expanded);
    }
}

bool IntrinsicTransformWidget::isTransformEnabled() const
{
    return m_enableCheck && m_enableCheck->isChecked();
}

void IntrinsicTransformWidget::setTransformEnabled(bool enabled)
{
    if (m_enableCheck) {
        m_enableCheck->setChecked(enabled);
    }
}

double IntrinsicTransformWidget::positionX() const
{
    return m_posXSpin ? m_posXSpin->value() : 0.0;
}

double IntrinsicTransformWidget::positionY() const
{
    return m_posYSpin ? m_posYSpin->value() : 0.0;
}

void IntrinsicTransformWidget::setPosition(double x, double y)
{
    {
        QSignalBlocker b1(m_posXSpin);
        QSignalBlocker b2(m_posYSpin);
        QSignalBlocker b3(m_posXSlider);
        QSignalBlocker b4(m_posYSlider);
        m_posXSpin->setValue(x);
        m_posYSpin->setValue(y);
        m_posXSlider->setValue(qBound(m_posXSlider->minimum(), qRound(x), m_posXSlider->maximum()));
        m_posYSlider->setValue(qBound(m_posYSlider->minimum(), qRound(y), m_posYSlider->maximum()));
    }
    commitTransformToModel();
}

double IntrinsicTransformWidget::scaleXPercent() const
{
    return m_scaleXSpin ? m_scaleXSpin->value() : 100.0;
}

double IntrinsicTransformWidget::scaleYPercent() const
{
    return m_scaleYSpin ? m_scaleYSpin->value() : 100.0;
}

bool IntrinsicTransformWidget::isAspectRatioLocked() const
{
    return m_aspectLockBtn && m_aspectLockBtn->isChecked();
}

void IntrinsicTransformWidget::setAspectRatioLocked(bool locked)
{
    if (m_aspectLockBtn) {
        m_aspectLockBtn->setChecked(locked);
    }
}

void IntrinsicTransformWidget::setScalePercent(double scaleX, double scaleY)
{
    {
        QSignalBlocker b1(m_scaleXSpin);
        QSignalBlocker b2(m_scaleYSpin);
        QSignalBlocker b3(m_scaleSlider);
        m_scaleXSpin->setValue(scaleX);
        m_scaleYSpin->setValue(m_aspectLockBtn->isChecked() ? scaleX : scaleY);
        m_scaleSlider->setValue(qBound(m_scaleSlider->minimum(), qRound(scaleX), m_scaleSlider->maximum()));
    }
    commitTransformToModel();
}

double IntrinsicTransformWidget::rotationDegrees() const
{
    return m_rotationSpin ? m_rotationSpin->value() : 0.0;
}

void IntrinsicTransformWidget::setRotationDegrees(double degrees)
{
    {
        QSignalBlocker b1(m_rotationSpin);
        QSignalBlocker b2(m_rotationSlider);
        m_rotationSpin->setValue(degrees);
        m_rotationSlider->setValue(qBound(m_rotationSlider->minimum(), qRound(degrees), m_rotationSlider->maximum()));
    }
    commitTransformToModel();
}

double IntrinsicTransformWidget::anchorXPercent() const
{
    return m_anchorXSpin ? m_anchorXSpin->value() : 50.0;
}

double IntrinsicTransformWidget::anchorYPercent() const
{
    return m_anchorYSpin ? m_anchorYSpin->value() : 50.0;
}

void IntrinsicTransformWidget::setAnchorPercent(double xPercent, double yPercent)
{
    {
        QSignalBlocker b1(m_anchorXSpin);
        QSignalBlocker b2(m_anchorYSpin);
        m_anchorXSpin->setValue(xPercent);
        m_anchorYSpin->setValue(yPercent);
    }
    commitTransformToModel();
}

double IntrinsicTransformWidget::opacityPercent() const
{
    return m_opacitySpin ? m_opacitySpin->value() : 100.0;
}

void IntrinsicTransformWidget::setOpacityPercent(double opacityPercent)
{
    {
        QSignalBlocker b1(m_opacitySpin);
        QSignalBlocker b2(m_opacitySlider);
        m_opacitySpin->setValue(opacityPercent);
        m_opacitySlider->setValue(qBound(0, qRound(opacityPercent), 100));
    }
    commitTransformToModel();
}

int IntrinsicTransformWidget::blendModeCode() const
{
    return m_blendModeCombo ? m_blendModeCombo->currentData().toInt() : 0;
}

void IntrinsicTransformWidget::setBlendModeCode(int mltCompositingCode)
{
    if (!m_blendModeCombo) {
        return;
    }
    const int idx = m_blendModeCombo->findData(mltCompositingCode);
    if (idx >= 0) {
        m_blendModeCombo->setCurrentIndex(idx);
    }
}

QSize IntrinsicTransformWidget::frameSize() const
{
    return m_frameSize;
}

void IntrinsicTransformWidget::resetTransform()
{
    setNeutralUiValues();
#ifdef BANGONPRO_HAS_KDENLIVE_CORE
    if (m_transformModel) {
        auto kfr = m_transformModel->getKeyframeModel();
        if (kfr && !kfr->singleKeyframe()) {
            kfr->removeAllKeyframes();
        }
        commitTransformToModel();
    } else {
        Q_EMIT transformChanged();
    }
#else
    Q_EMIT transformChanged();
#endif
}

void IntrinsicTransformWidget::toggleKeyframeAtCurrentFrame()
{
#ifdef BANGONPRO_HAS_KDENLIVE_CORE
    if (!m_stack || !pCore) {
        return;
    }
    if (!ensureMaterializedTransform() || !m_transformModel) {
        return;
    }
    auto kfr = m_transformModel->getKeyframeModel();
    if (!kfr) {
        m_transformModel->prepareKeyframes();
        kfr = m_transformModel->getKeyframeModel();
    }
    if (!kfr) {
        return;
    }
    const GenTime pos(m_currentFrame, pCore->getCurrentFps());
    if (kfr->hasKeyframe(m_currentFrame)) {
        kfr->removeKeyframe(pos);
    } else {
        kfr->addKeyframe(pos, KeyframeType::Linear);
    }
    updateKeyframeButtonsState();
#endif
}

void IntrinsicTransformWidget::goToPreviousKeyframe()
{
#ifdef BANGONPRO_HAS_KDENLIVE_CORE
    if (!m_transformModel || !pCore) {
        return;
    }
    auto kfr = m_transformModel->getKeyframeModel();
    if (!kfr) {
        return;
    }
    bool ok = false;
    const GenTime pos(m_currentFrame, pCore->getCurrentFps());
    const Keyframe prev = kfr->getPrevKeyframe(pos, &ok);
    if (ok) {
        const int targetFrame = prev.first.frames(pCore->getCurrentFps());
        Q_EMIT seekToPos(targetFrame);
    }
#endif
}

void IntrinsicTransformWidget::goToNextKeyframe()
{
#ifdef BANGONPRO_HAS_KDENLIVE_CORE
    if (!m_transformModel || !pCore) {
        return;
    }
    auto kfr = m_transformModel->getKeyframeModel();
    if (!kfr) {
        return;
    }
    bool ok = false;
    const GenTime pos(m_currentFrame, pCore->getCurrentFps());
    const Keyframe next = kfr->getNextKeyframe(pos, &ok);
    if (ok) {
        const int targetFrame = next.first.frames(pCore->getCurrentFps());
        Q_EMIT seekToPos(targetFrame);
    }
#endif
}

void IntrinsicTransformWidget::onCollapseToggled(bool expanded)
{
    if (m_bodyFrame) {
        m_bodyFrame->setVisible(expanded);
    }
    if (m_collapseBtn) {
        m_collapseBtn->setText(expanded ? QStringLiteral("▾ Motion / Transform") : QStringLiteral("▸ Motion / Transform"));
    }
}

void IntrinsicTransformWidget::onEnableToggled(bool enabled)
{
    if (m_bodyFrame) {
        m_bodyFrame->setEnabled(enabled);
    }
    if (m_syncingUi) {
        return;
    }
#ifdef BANGONPRO_HAS_KDENLIVE_CORE
    if (m_transformModel && pCore) {
        auto effItem = std::static_pointer_cast<EffectItemModel>(m_transformModel);
        if (effItem && effItem->isEnabled() != enabled) {
            Fun undo = []() { return true; };
            Fun redo = []() { return true; };
            std::static_pointer_cast<AbstractEffectItem>(effItem)->markEnabled(enabled, undo, redo);
            redo();
            pCore->pushUndo(undo, redo, enabled ? i18n("Enable Transform") : i18n("Disable Transform"));
        }
    }
#endif
    Q_EMIT transformChanged();
}

void IntrinsicTransformWidget::onPositionEdited()
{
    commitTransformToModel();
}

void IntrinsicTransformWidget::onScaleXEdited(double val)
{
    if (m_syncingUi) {
        return;
    }
    {
        QSignalBlocker bs(m_scaleSlider);
        m_scaleSlider->setValue(qBound(m_scaleSlider->minimum(), qRound(val), m_scaleSlider->maximum()));
    }
    if (m_aspectLockBtn && m_aspectLockBtn->isChecked()) {
        QSignalBlocker by(m_scaleYSpin);
        m_scaleYSpin->setValue(val);
    }
    commitTransformToModel();
}

void IntrinsicTransformWidget::onScaleYEdited(double)
{
    if (m_syncingUi) {
        return;
    }
    commitTransformToModel();
}

void IntrinsicTransformWidget::onScaleSliderMoved(int val)
{
    if (m_syncingUi) {
        return;
    }
    {
        QSignalBlocker bx(m_scaleXSpin);
        m_scaleXSpin->setValue(static_cast<double>(val));
        if (m_aspectLockBtn && m_aspectLockBtn->isChecked()) {
            QSignalBlocker by(m_scaleYSpin);
            m_scaleYSpin->setValue(static_cast<double>(val));
        }
    }
    commitTransformToModel();
}

void IntrinsicTransformWidget::onAspectLockToggled(bool locked)
{
    if (m_scaleYSpin) {
        m_scaleYSpin->setEnabled(!locked);
    }
    if (m_aspectLockBtn) {
        m_aspectLockBtn->setText(locked ? QStringLiteral("🔒") : QStringLiteral("🔓"));
    }
    if (!m_syncingUi && locked && m_scaleXSpin && m_scaleYSpin) {
        QSignalBlocker by(m_scaleYSpin);
        m_scaleYSpin->setValue(m_scaleXSpin->value());
        commitTransformToModel();
    }
}

void IntrinsicTransformWidget::onRotationSpinEdited(double val)
{
    if (m_syncingUi) {
        return;
    }
    {
        QSignalBlocker bs(m_rotationSlider);
        m_rotationSlider->setValue(qBound(m_rotationSlider->minimum(), qRound(val), m_rotationSlider->maximum()));
    }
    commitTransformToModel();
}

void IntrinsicTransformWidget::onRotationSliderMoved(int val)
{
    if (m_syncingUi) {
        return;
    }
    {
        QSignalBlocker bs(m_rotationSpin);
        m_rotationSpin->setValue(static_cast<double>(val));
    }
    commitTransformToModel();
}

void IntrinsicTransformWidget::onAnchorEdited()
{
    if (m_syncingUi) {
        return;
    }
    commitTransformToModel();
}

void IntrinsicTransformWidget::onOpacitySpinEdited(double val)
{
    if (m_syncingUi) {
        return;
    }
    {
        QSignalBlocker bs(m_opacitySlider);
        m_opacitySlider->setValue(qBound(0, qRound(val), 100));
    }
    commitTransformToModel();
}

void IntrinsicTransformWidget::onOpacitySliderMoved(int val)
{
    if (m_syncingUi) {
        return;
    }
    {
        QSignalBlocker bs(m_opacitySpin);
        m_opacitySpin->setValue(static_cast<double>(val));
    }
    commitTransformToModel();
}

void IntrinsicTransformWidget::onBlendModeChanged(int)
{
    if (m_syncingUi) {
        return;
    }
    commitTransformToModel();
}

} // namespace BangOnPro
