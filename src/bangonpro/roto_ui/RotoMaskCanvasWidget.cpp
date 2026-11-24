/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "RotoMaskCanvasWidget.h"

#include <QMouseEvent>
#include <QPainter>
#include <QPainterPath>
#include <QPen>
#include <QtMath>
#include <algorithm>

namespace BangOnPro {

namespace {

QPointF mouseEventPoint(QMouseEvent *event)
{
#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
    return event->position();
#else
    return event->localPos();
#endif
}

} // namespace

RotoMaskCanvasWidget::RotoMaskCanvasWidget(QWidget *parent)
    : QWidget(parent)
{
    setObjectName(QStringLiteral("bangonproRotoCanvas"));
    setMinimumSize(320, 180);
    setMouseTracking(true);
    ensureDefaultFrame();
}

void RotoMaskCanvasWidget::ensureDefaultFrame()
{
    if (!m_frameImage.isNull()) {
        return;
    }
    // Neutral studio reference checkerboard frame until a clip frame is loaded
    QImage img(640, 360, QImage::Format_RGB32);
    img.fill(QColor(18, 21, 27));
    QPainter p(&img);
    for (int y = 0; y < img.height(); y += 20) {
        for (int x = 0; x < img.width(); x += 20) {
            if (((x / 20) + (y / 20)) % 2 == 0) {
                p.fillRect(x, y, 20, 20, QColor(24, 28, 36));
            }
        }
    }
    p.setPen(QColor(150, 160, 178));
    p.drawText(img.rect(), Qt::AlignCenter, QStringLiteral("Bang On Pro Roto Canvas — Load Frame or Draw Mask"));
    m_frameImage = img;
}

void RotoMaskCanvasWidget::setFrameImage(const QImage &image)
{
    if (image.isNull()) {
        return;
    }
    m_frameImage = image.convertToFormat(QImage::Format_RGB32);
    update();
}

QImage RotoMaskCanvasWidget::frameImage() const
{
    return m_frameImage;
}

void RotoMaskCanvasWidget::setAlphaMaskImage(const QImage &alphaMask)
{
    m_externalAlphaMask = alphaMask.isNull() ? QImage() : alphaMask.convertToFormat(QImage::Format_Grayscale8);
    update();
}

void RotoMaskCanvasWidget::setActiveTool(RotoTool tool)
{
    m_activeTool = tool;
    update();
}

RotoTool RotoMaskCanvasWidget::activeTool() const
{
    return m_activeTool;
}

void RotoMaskCanvasWidget::setPreviewMode(RotoPreviewMode mode)
{
    m_previewMode = mode;
    update();
}

RotoPreviewMode RotoMaskCanvasWidget::previewMode() const
{
    return m_previewMode;
}

void RotoMaskCanvasWidget::setFeatherPx(double feather)
{
    m_featherPx = qMax(0.0, feather);
    update();
}

double RotoMaskCanvasWidget::featherPx() const
{
    return m_featherPx;
}

void RotoMaskCanvasWidget::setExpandPx(double expand)
{
    m_expandPx = expand;
    update();
}

double RotoMaskCanvasWidget::expandPx() const
{
    return m_expandPx;
}

void RotoMaskCanvasWidget::setInverted(bool inverted)
{
    m_inverted = inverted;
    update();
}

bool RotoMaskCanvasWidget::isInverted() const
{
    return m_inverted;
}

void RotoMaskCanvasWidget::setBrushRadius(double radius)
{
    m_brushRadius = qBound(2.0, radius, 200.0);
}

double RotoMaskCanvasWidget::brushRadius() const
{
    return m_brushRadius;
}

void RotoMaskCanvasWidget::setSplitRatio(double ratio)
{
    m_splitRatio = qBound(0.05, ratio, 0.95);
    update();
}

double RotoMaskCanvasWidget::splitRatio() const
{
    return m_splitRatio;
}

void RotoMaskCanvasWidget::addPositivePoint(const QPointF &imgPoint)
{
    m_positivePoints.append(imgPoint);
    m_externalAlphaMask = QImage();
    update();
    Q_EMIT aiPromptAdded(imgPoint, true);
    Q_EMIT maskGeometryChanged();
}

void RotoMaskCanvasWidget::addNegativePoint(const QPointF &imgPoint)
{
    m_negativePoints.append(imgPoint);
    m_externalAlphaMask = QImage();
    update();
    Q_EMIT aiPromptAdded(imgPoint, false);
    Q_EMIT maskGeometryChanged();
}

void RotoMaskCanvasWidget::addPolygonVertex(const QPointF &imgPoint)
{
    m_polygonVertices.append(imgPoint);
    m_externalAlphaMask = QImage();
    update();
    Q_EMIT maskGeometryChanged();
}

void RotoMaskCanvasWidget::addBrushStroke(const RotoBrushStroke &stroke)
{
    m_brushStrokes.append(stroke);
    m_externalAlphaMask = QImage();
    update();
    Q_EMIT maskGeometryChanged();
}

void RotoMaskCanvasWidget::clearGeometry()
{
    m_positivePoints.clear();
    m_negativePoints.clear();
    m_polygonVertices.clear();
    m_brushStrokes.clear();
    m_externalAlphaMask = QImage();
    update();
    Q_EMIT maskGeometryChanged();
}

QJsonObject RotoMaskCanvasWidget::exportMaskGeometryJson() const
{
    QJsonObject obj;
    QString toolStr = QStringLiteral("polygon");
    switch (m_activeTool) {
    case RotoTool::AiSelectPositive:
    case RotoTool::AiSelectNegative:
        toolStr = QStringLiteral("ai_sam");
        break;
    case RotoTool::Polygon:
        toolStr = QStringLiteral("polygon");
        break;
    case RotoTool::Bezier:
        toolStr = QStringLiteral("bezier");
        break;
    case RotoTool::Freehand:
        toolStr = QStringLiteral("freehand");
        break;
    case RotoTool::Brush:
    case RotoTool::Eraser:
        toolStr = QStringLiteral("brush");
        break;
    }
    obj.insert(QStringLiteral("tool"), toolStr);
    obj.insert(QStringLiteral("feather_px"), m_featherPx);
    obj.insert(QStringLiteral("expand_px"), m_expandPx);
    obj.insert(QStringLiteral("inverted"), m_inverted);

    QJsonArray posArr;
    for (const QPointF &pt : m_positivePoints) {
        posArr.append(QJsonArray{pt.x(), pt.y()});
    }
    obj.insert(QStringLiteral("positive_points"), posArr);

    QJsonArray negArr;
    for (const QPointF &pt : m_negativePoints) {
        negArr.append(QJsonArray{pt.x(), pt.y()});
    }
    obj.insert(QStringLiteral("negative_points"), negArr);

    QJsonArray vertsArr;
    for (const QPointF &pt : m_polygonVertices) {
        vertsArr.append(QJsonArray{pt.x(), pt.y()});
    }
    obj.insert(QStringLiteral("vertices"), vertsArr);

    QJsonArray strokesArr;
    for (const RotoBrushStroke &s : m_brushStrokes) {
        QJsonObject sObj;
        QJsonArray pts;
        for (const QPointF &pt : s.points) {
            pts.append(QJsonArray{pt.x(), pt.y()});
        }
        sObj.insert(QStringLiteral("points"), pts);
        sObj.insert(QStringLiteral("radius"), s.radius);
        sObj.insert(QStringLiteral("erase"), s.erase);
        strokesArr.append(sObj);
    }
    obj.insert(QStringLiteral("brush_strokes"), strokesArr);
    return obj;
}

void RotoMaskCanvasWidget::loadMaskGeometryJson(const QJsonObject &shapeObj)
{
    m_positivePoints.clear();
    m_negativePoints.clear();
    m_polygonVertices.clear();
    m_brushStrokes.clear();
    m_externalAlphaMask = QImage();

    m_featherPx = shapeObj.value(QStringLiteral("feather_px")).toDouble(0.0);
    m_expandPx = shapeObj.value(QStringLiteral("expand_px")).toDouble(0.0);
    m_inverted = shapeObj.value(QStringLiteral("inverted")).toBool(false);

    for (const QJsonValue &v : shapeObj.value(QStringLiteral("positive_points")).toArray()) {
        const QJsonArray a = v.toArray();
        if (a.size() >= 2) {
            m_positivePoints.append(QPointF(a.at(0).toDouble(), a.at(1).toDouble()));
        }
    }
    for (const QJsonValue &v : shapeObj.value(QStringLiteral("negative_points")).toArray()) {
        const QJsonArray a = v.toArray();
        if (a.size() >= 2) {
            m_negativePoints.append(QPointF(a.at(0).toDouble(), a.at(1).toDouble()));
        }
    }
    for (const QJsonValue &v : shapeObj.value(QStringLiteral("vertices")).toArray()) {
        if (v.isArray()) {
            const QJsonArray a = v.toArray();
            if (a.size() >= 2) {
                m_polygonVertices.append(QPointF(a.at(0).toDouble(), a.at(1).toDouble()));
            }
        } else if (v.isObject()) {
            const QJsonObject o = v.toObject();
            m_polygonVertices.append(QPointF(o.value(QStringLiteral("x")).toDouble(), o.value(QStringLiteral("y")).toDouble()));
        }
    }
    for (const QJsonValue &v : shapeObj.value(QStringLiteral("brush_strokes")).toArray()) {
        const QJsonObject o = v.toObject();
        RotoBrushStroke st;
        st.radius = o.value(QStringLiteral("radius")).toDouble(16.0);
        st.erase = o.value(QStringLiteral("erase")).toBool(false);
        for (const QJsonValue &pv : o.value(QStringLiteral("points")).toArray()) {
            const QJsonArray a = pv.toArray();
            if (a.size() >= 2) {
                st.points.append(QPointF(a.at(0).toDouble(), a.at(1).toDouble()));
            }
        }
        if (!st.points.isEmpty()) {
            m_brushStrokes.append(st);
        }
    }
    update();
}

QImage RotoMaskCanvasWidget::synthesizeLocalAlphaMask() const
{
    const int w = qMax(1, m_frameImage.width());
    const int h = qMax(1, m_frameImage.height());
    QImage mask(w, h, QImage::Format_Grayscale8);
    mask.fill(0);

    QPainter p(&mask);
    p.setRenderHint(QPainter::Antialiasing, true);

    if (m_polygonVertices.size() >= 3) {
        QPainterPath path;
        path.moveTo(m_polygonVertices.first());
        for (int i = 1; i < m_polygonVertices.size(); ++i) {
            path.lineTo(m_polygonVertices.at(i));
        }
        path.closeSubpath();
        p.fillPath(path, Qt::white);
    }

    for (const RotoBrushStroke &st : m_brushStrokes) {
        if (st.points.isEmpty()) {
            continue;
        }
        const QColor col = st.erase ? Qt::black : Qt::white;
        QPen pen(col, st.radius * 2.0, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin);
        p.setPen(pen);
        if (st.points.size() == 1) {
            p.drawPoint(st.points.first());
        } else {
            for (int i = 1; i < st.points.size(); ++i) {
                p.drawLine(st.points.at(i - 1), st.points.at(i));
            }
        }
    }

    if (m_polygonVertices.size() < 3 && m_brushStrokes.isEmpty() && !m_positivePoints.isEmpty()) {
        p.setPen(Qt::NoPen);
        p.setBrush(Qt::white);
        for (const QPointF &pt : m_positivePoints) {
            p.drawEllipse(pt, 42.0, 42.0);
        }
        p.setBrush(Qt::black);
        for (const QPointF &pt : m_negativePoints) {
            p.drawEllipse(pt, 28.0, 28.0);
        }
    }
    p.end();

    if (m_inverted) {
        mask.invertPixels();
    }
    return mask;
}

QImage RotoMaskCanvasWidget::computedAlphaMask() const
{
    if (!m_externalAlphaMask.isNull()) {
        QImage m = m_externalAlphaMask;
        if (m_inverted) {
            m.invertPixels();
        }
        return m;
    }
    return synthesizeLocalAlphaMask();
}

QRectF RotoMaskCanvasWidget::targetImageRect() const
{
    if (m_frameImage.isNull() || width() <= 0 || height() <= 0) {
        return rect();
    }
    const double iw = static_cast<double>(m_frameImage.width());
    const double ih = static_cast<double>(m_frameImage.height());
    const double scale = qMin(static_cast<double>(width()) / iw, static_cast<double>(height()) / ih);
    const double dw = iw * scale;
    const double dh = ih * scale;
    return QRectF((width() - dw) * 0.5, (height() - dh) * 0.5, dw, dh);
}

QPointF RotoMaskCanvasWidget::widgetToImage(const QPointF &widgetPt) const
{
    const QRectF r = targetImageRect();
    if (r.width() <= 0.0 || r.height() <= 0.0) {
        return {};
    }
    const double nx = qBound(0.0, (widgetPt.x() - r.left()) / r.width(), 1.0);
    const double ny = qBound(0.0, (widgetPt.y() - r.top()) / r.height(), 1.0);
    return QPointF(nx * m_frameImage.width(), ny * m_frameImage.height());
}

QPointF RotoMaskCanvasWidget::imageToWidget(const QPointF &imgPt) const
{
    const QRectF r = targetImageRect();
    const double nx = m_frameImage.width() > 0 ? imgPt.x() / static_cast<double>(m_frameImage.width()) : 0.0;
    const double ny = m_frameImage.height() > 0 ? imgPt.y() / static_cast<double>(m_frameImage.height()) : 0.0;
    return QPointF(r.left() + nx * r.width(), r.top() + ny * r.height());
}

void RotoMaskCanvasWidget::paintEvent(QPaintEvent *)
{
    QPainter p(this);
    p.fillRect(rect(), QColor(8, 10, 13));
    p.setRenderHint(QPainter::Antialiasing, true);

    const QRectF dst = targetImageRect();
    const QImage alpha = computedAlphaMask();

    if (m_previewMode == RotoPreviewMode::IsolatedAlpha) {
        p.drawImage(dst, alpha);
    } else if (m_previewMode == RotoPreviewMode::FinalComposite) {
        // Draw dark transparency checkerboard + alpha-composited foreground
        p.fillRect(dst, QColor(28, 31, 38));
        QImage fg = m_frameImage.convertToFormat(QImage::Format_ARGB32);
        fg.setAlphaChannel(alpha);
        p.drawImage(dst, fg);
    } else if (m_previewMode == RotoPreviewMode::BeforeAfterSplit) {
        p.drawImage(dst, m_frameImage);
        const double splitX = dst.left() + dst.width() * m_splitRatio;
        p.save();
        p.setClipRect(QRectF(splitX, dst.top(), dst.right() - splitX, dst.height()));
        p.fillRect(dst, QColor(28, 31, 38));
        QImage fg = m_frameImage.convertToFormat(QImage::Format_ARGB32);
        fg.setAlphaChannel(alpha);
        p.drawImage(dst, fg);
        p.restore();
        p.setPen(QPen(QColor(255, 90, 31), 2.0));
        p.drawLine(QPointF(splitX, dst.top()), QPointF(splitX, dst.bottom()));
    } else {
        // Mask Overlay (rubylith tint)
        p.drawImage(dst, m_frameImage);
        QImage overlay(alpha.size(), QImage::Format_ARGB32);
        overlay.fill(QColor(255, 90, 31, 110));
        overlay.setAlphaChannel(alpha);
        p.setOpacity(0.45);
        p.drawImage(dst, overlay);
        p.setOpacity(1.0);
    }

    // Draw interactive polygon/Bézier vertices & handles
    if (!m_polygonVertices.isEmpty()) {
        QPen polyPen(QColor(255, 165, 64), 2.0);
        p.setPen(polyPen);
        for (int i = 0; i < m_polygonVertices.size(); ++i) {
            const QPointF a = imageToWidget(m_polygonVertices.at(i));
            const QPointF b = imageToWidget(m_polygonVertices.at((i + 1) % m_polygonVertices.size()));
            if (i + 1 < m_polygonVertices.size() || m_polygonVertices.size() >= 3) {
                p.drawLine(a, b);
            }
        }
        p.setBrush(QColor(255, 90, 31));
        p.setPen(QPen(Qt::white, 1.5));
        for (const QPointF &v : m_polygonVertices) {
            p.drawEllipse(imageToWidget(v), 4.5, 4.5);
        }
    }

    // Draw positive (+) green & negative (-) red AI prompt points
    for (const QPointF &pt : m_positivePoints) {
        const QPointF wpt = imageToWidget(pt);
        p.setBrush(QColor(34, 160, 107));
        p.setPen(QPen(Qt::white, 1.5));
        p.drawEllipse(wpt, 6.0, 6.0);
        p.drawLine(wpt + QPointF(-3, 0), wpt + QPointF(3, 0));
        p.drawLine(wpt + QPointF(0, -3), wpt + QPointF(0, 3));
    }
    for (const QPointF &pt : m_negativePoints) {
        const QPointF wpt = imageToWidget(pt);
        p.setBrush(QColor(224, 79, 95));
        p.setPen(QPen(Qt::white, 1.5));
        p.drawEllipse(wpt, 6.0, 6.0);
        p.drawLine(wpt + QPointF(-3, 0), wpt + QPointF(3, 0));
    }
}

void RotoMaskCanvasWidget::mousePressEvent(QMouseEvent *event)
{
    if (event->button() != Qt::LeftButton) {
        QWidget::mousePressEvent(event);
        return;
    }
    const QPointF wpt = mouseEventPoint(event);
    const QPointF ipt = widgetToImage(wpt);

    // Allow dragging an existing polygon/Bézier vertex if clicked nearby
    if (m_activeTool == RotoTool::Polygon || m_activeTool == RotoTool::Bezier) {
        for (int i = 0; i < m_polygonVertices.size(); ++i) {
            if (QLineF(wpt, imageToWidget(m_polygonVertices.at(i))).length() <= 8.0) {
                m_dragVertexIndex = i;
                m_isDrawing = true;
                return;
            }
        }
        addPolygonVertex(ipt);
        return;
    }

    if (m_activeTool == RotoTool::AiSelectPositive) {
        addPositivePoint(ipt);
        return;
    }
    if (m_activeTool == RotoTool::AiSelectNegative) {
        addNegativePoint(ipt);
        return;
    }

    if (m_activeTool == RotoTool::Freehand) {
        m_polygonVertices.clear();
        m_polygonVertices.append(ipt);
        m_isDrawing = true;
        update();
        return;
    }

    if (m_activeTool == RotoTool::Brush || m_activeTool == RotoTool::Eraser) {
        RotoBrushStroke st;
        st.radius = m_brushRadius;
        st.erase = (m_activeTool == RotoTool::Eraser);
        st.points.append(ipt);
        m_brushStrokes.append(st);
        m_externalAlphaMask = QImage();
        m_isDrawing = true;
        update();
    }
}

void RotoMaskCanvasWidget::mouseMoveEvent(QMouseEvent *event)
{
    if (!m_isDrawing) {
        QWidget::mouseMoveEvent(event);
        return;
    }
    const QPointF ipt = widgetToImage(mouseEventPoint(event));

    if (m_dragVertexIndex >= 0 && m_dragVertexIndex < m_polygonVertices.size()) {
        m_polygonVertices[m_dragVertexIndex] = ipt;
        m_externalAlphaMask = QImage();
        update();
        return;
    }
    if (m_activeTool == RotoTool::Freehand) {
        m_polygonVertices.append(ipt);
        m_externalAlphaMask = QImage();
        update();
        return;
    }
    if ((m_activeTool == RotoTool::Brush || m_activeTool == RotoTool::Eraser) && !m_brushStrokes.isEmpty()) {
        m_brushStrokes.last().points.append(ipt);
        m_externalAlphaMask = QImage();
        update();
    }
}

void RotoMaskCanvasWidget::mouseReleaseEvent(QMouseEvent *event)
{
    if (m_isDrawing) {
        m_isDrawing = false;
        m_dragVertexIndex = -1;
        Q_EMIT maskGeometryChanged();
    }
    QWidget::mouseReleaseEvent(event);
}

} // namespace BangOnPro
