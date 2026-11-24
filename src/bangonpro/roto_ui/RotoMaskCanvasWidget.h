/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include <QColor>
#include <QImage>
#include <QJsonArray>
#include <QJsonObject>
#include <QPointF>
#include <QString>
#include <QVector>
#include <QWidget>

namespace BangOnPro {

enum class RotoTool {
    AiSelectPositive = 0,
    AiSelectNegative = 1,
    Polygon = 2,
    Bezier = 3,
    Freehand = 4,
    Brush = 5,
    Eraser = 6
};

enum class RotoPreviewMode {
    MaskOverlay = 0,
    IsolatedAlpha = 1,
    BeforeAfterSplit = 2,
    FinalComposite = 3
};

struct RotoBrushStroke
{
    QVector<QPointF> points;
    double radius = 16.0;
    bool erase = false;
};

/**
 * @brief Interactive Qt5/Qt6 2D canvas for Bang On Pro Rotoscoping & Masking.
 *
 * Supports AI +/- point selection, polygon, cubic Bézier, freehand, and brush/eraser
 * strokes over video frames with live Mask Overlay, Isolated Alpha, Before/After Split,
 * and Final Composite preview modes.
 */
class RotoMaskCanvasWidget : public QWidget
{
    Q_OBJECT

public:
    explicit RotoMaskCanvasWidget(QWidget *parent = nullptr);
    ~RotoMaskCanvasWidget() override = default;

    void setFrameImage(const QImage &image);
    QImage frameImage() const;

    void setAlphaMaskImage(const QImage &alphaMask);
    QImage computedAlphaMask() const;

    void setActiveTool(RotoTool tool);
    RotoTool activeTool() const;

    void setPreviewMode(RotoPreviewMode mode);
    RotoPreviewMode previewMode() const;

    void setFeatherPx(double feather);
    double featherPx() const;

    void setExpandPx(double expand);
    double expandPx() const;

    void setInverted(bool inverted);
    bool isInverted() const;

    void setBrushRadius(double radius);
    double brushRadius() const;

    void setSplitRatio(double ratio);
    double splitRatio() const;

    // Programmatic geometry helpers (for UI tools and automated tests)
    void addPositivePoint(const QPointF &imgPoint);
    void addNegativePoint(const QPointF &imgPoint);
    void addPolygonVertex(const QPointF &imgPoint);
    void addBrushStroke(const RotoBrushStroke &stroke);
    void clearGeometry();

    const QVector<QPointF> &positivePoints() const { return m_positivePoints; }
    const QVector<QPointF> &negativePoints() const { return m_negativePoints; }
    const QVector<QPointF> &polygonVertices() const { return m_polygonVertices; }
    const QVector<RotoBrushStroke> &brushStrokes() const { return m_brushStrokes; }

    /** @brief Export active mask geometry in roto_mask_v1 schema shape JSON format. */
    QJsonObject exportMaskGeometryJson() const;

    /** @brief Load mask geometry from a roto_mask_v1 shape JSON object. */
    void loadMaskGeometryJson(const QJsonObject &shapeObj);

    QSize sizeHint() const override { return QSize(480, 270); }

Q_SIGNALS:
    void maskGeometryChanged();
    void aiPromptAdded(const QPointF &imgPoint, bool positive);

protected:
    void paintEvent(QPaintEvent *event) override;
    void mousePressEvent(QMouseEvent *event) override;
    void mouseMoveEvent(QMouseEvent *event) override;
    void mouseReleaseEvent(QMouseEvent *event) override;

private:
    QRectF targetImageRect() const;
    QPointF widgetToImage(const QPointF &widgetPt) const;
    QPointF imageToWidget(const QPointF &imgPt) const;
    QImage synthesizeLocalAlphaMask() const;
    void ensureDefaultFrame();

    QImage m_frameImage;
    QImage m_externalAlphaMask;
    RotoTool m_activeTool = RotoTool::Polygon;
    RotoPreviewMode m_previewMode = RotoPreviewMode::MaskOverlay;
    double m_featherPx = 0.0;
    double m_expandPx = 0.0;
    bool m_inverted = false;
    double m_brushRadius = 18.0;
    double m_splitRatio = 0.5;

    QVector<QPointF> m_positivePoints;
    QVector<QPointF> m_negativePoints;
    QVector<QPointF> m_polygonVertices;
    QVector<RotoBrushStroke> m_brushStrokes;

    bool m_isDrawing = false;
    int m_dragVertexIndex = -1;
};

} // namespace BangOnPro
