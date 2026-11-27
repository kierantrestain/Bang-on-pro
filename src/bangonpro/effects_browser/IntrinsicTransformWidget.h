/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include <QSize>
#include <QString>
#include <QWidget>
#include <memory>

class QCheckBox;
class QComboBox;
class QDoubleSpinBox;
class QFrame;
class QPushButton;
class QSlider;
class QToolButton;

class AssetParameterModel;
class EffectStackModel;

namespace BangOnPro {

struct CompensatedRect
{
    double x = 0.0;
    double y = 0.0;
    double width = 1920.0;
    double height = 1080.0;
};

struct DecomposedTransform
{
    double posX = 0.0;
    double posY = 0.0;
    double scaleXPercent = 100.0;
    double scaleYPercent = 100.0;
};

/**
 * @brief Collapsible "Motion / Transform" inspector card pinned at the top of Effect Controls.
 *
 * Displays neutral defaults (Position 0,0; Scale 100%; Rotation 0 deg; Anchor 50%,50%;
 * Opacity 100%; Normal blend) without mutating the clip until the user makes an edit
 * or inserts a keyframe, at which point it lazily materializes a native undo-backed
 * MLT `qtblend` effect on the clip's `EffectStackModel`.
 */
class IntrinsicTransformWidget : public QWidget
{
    Q_OBJECT

public:
    explicit IntrinsicTransformWidget(QWidget *parent = nullptr);
    ~IntrinsicTransformWidget() override = default;

    /** @brief Bind to @p stack (scans for existing `qtblend`, or shows neutral defaults lazily). */
    void setModel(std::shared_ptr<EffectStackModel> stack, QSize frameSize, int currentFrame = 0);

    /** @brief Detach from any active effect stack and reset to neutral UI state. */
    void unsetModel();

    /** @brief Update the clip-local frame used when reading/writing keyframes. */
    void setCurrentFrame(int frame);

    /** @brief Returns true if a native `qtblend` effect is currently bound/materialized. */
    bool hasMaterializedTransform() const;

    /**
     * @brief Compute MLT `qtblend` rect `(x, y, width, height)` compensated so scaling
     * and rotation (with `rotate_center=1`) pivot accurately around `(anchorXNorm, anchorYNorm)`.
     */
    static CompensatedRect computeCompensatedRect(double frameW, double frameH, double posX, double posY, double scaleXPercent, double scaleYPercent,
                                                  double rotationDeg, double anchorXNorm, double anchorYNorm);

    /**
     * @brief Exact inverse of computeCompensatedRect(): recover `(posX, posY, scaleXPercent, scaleYPercent)`
     * from an MLT `qtblend` rect given `(rotationDeg, anchorXNorm, anchorYNorm)`.
     */
    static DecomposedTransform decomposeRect(double frameW, double frameH, double rectX, double rectY, double rectW, double rectH, double rotationDeg,
                                             double anchorXNorm, double anchorYNorm);

    /** @brief Format `(x, y, w, h, opacity01)` into MLT `qtblend` `rect` string ("x y w h opacity"). */
    static QString formatMltRect(double x, double y, double w, double h, double opacity01);

    /** @brief Parse MLT `qtblend` `rect` string (plain or `frame=x y w h opacity`) into components. */
    static bool parseMltRect(const QString &rectStr, double *outX, double *outY, double *outW, double *outH, double *outOpacity01 = nullptr);

    // State accessors & programmatic setters (also used by unit tests)
    bool isExpanded() const;
    void setExpanded(bool expanded);

    bool isTransformEnabled() const;
    void setTransformEnabled(bool enabled);

    double positionX() const;
    double positionY() const;
    void setPosition(double x, double y);

    double scaleXPercent() const;
    double scaleYPercent() const;
    bool isAspectRatioLocked() const;
    void setAspectRatioLocked(bool locked);
    void setScalePercent(double scaleX, double scaleY);

    double rotationDegrees() const;
    void setRotationDegrees(double degrees);

    double anchorXPercent() const;
    double anchorYPercent() const;
    void setAnchorPercent(double xPercent, double yPercent);

    double opacityPercent() const;
    void setOpacityPercent(double opacityPercent);

    int blendModeCode() const;
    void setBlendModeCode(int mltCompositingCode);

    QSize frameSize() const;

public Q_SLOTS:
    void resetTransform();
    void toggleKeyframeAtCurrentFrame();
    void goToPreviousKeyframe();
    void goToNextKeyframe();

Q_SIGNALS:
    void seekToPos(int frame);
    void transformChanged();

private Q_SLOTS:
    void onCollapseToggled(bool expanded);
    void onEnableToggled(bool enabled);
    void onPositionEdited();
    void onScaleXEdited(double val);
    void onScaleYEdited(double val);
    void onScaleSliderMoved(int val);
    void onAspectLockToggled(bool locked);
    void onRotationSpinEdited(double val);
    void onRotationSliderMoved(int val);
    void onAnchorEdited();
    void onOpacitySpinEdited(double val);
    void onOpacitySliderMoved(int val);
    void onBlendModeChanged(int index);
    void syncFromModel();

private:
    void buildUi();
    void setNeutralUiValues();
    void bindExistingTransform();
    bool ensureMaterializedTransform();
    void commitTransformToModel();
    void updateKeyframeButtonsState();

    std::shared_ptr<EffectStackModel> m_stack;
    std::shared_ptr<AssetParameterModel> m_transformModel;
    QSize m_frameSize = QSize(1920, 1080);
    int m_currentFrame = 0;
    bool m_syncingUi = false;

    // Header controls
    QToolButton *m_collapseBtn = nullptr;
    QCheckBox *m_enableCheck = nullptr;
    QToolButton *m_prevKfrBtn = nullptr;
    QToolButton *m_toggleKfrBtn = nullptr;
    QToolButton *m_nextKfrBtn = nullptr;
    QToolButton *m_resetBtn = nullptr;

    // Collapsible body
    QFrame *m_bodyFrame = nullptr;
    QDoubleSpinBox *m_posXSpin = nullptr;
    QDoubleSpinBox *m_posYSpin = nullptr;
    QSlider *m_posXSlider = nullptr;
    QSlider *m_posYSlider = nullptr;

    QSlider *m_scaleSlider = nullptr;
    QDoubleSpinBox *m_scaleXSpin = nullptr;
    QDoubleSpinBox *m_scaleYSpin = nullptr;
    QToolButton *m_aspectLockBtn = nullptr;

    QSlider *m_rotationSlider = nullptr;
    QDoubleSpinBox *m_rotationSpin = nullptr;

    QDoubleSpinBox *m_anchorXSpin = nullptr;
    QDoubleSpinBox *m_anchorYSpin = nullptr;

    QSlider *m_opacitySlider = nullptr;
    QDoubleSpinBox *m_opacitySpin = nullptr;

    QComboBox *m_blendModeCombo = nullptr;
};

} // namespace BangOnPro
