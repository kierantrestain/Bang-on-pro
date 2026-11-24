/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "roto_ui/RotoMaskCanvasWidget.h"
#include "roto_ui/RotoStudioDock.h"

#include <QJsonArray>
#include <QJsonObject>
#include <QSignalSpy>
#include <QtTest>

using namespace BangOnPro;

class TestRotoStudio : public QObject
{
    Q_OBJECT

private Q_SLOTS:
    void testCanvasDrawingToolsAndPreviewModes()
    {
        RotoMaskCanvasWidget canvas;
        QImage frame(320, 180, QImage::Format_RGB32);
        frame.fill(QColor(30, 60, 110));
        canvas.setFrameImage(frame);

        canvas.setActiveTool(RotoTool::Polygon);
        canvas.addPolygonVertex(QPointF(60, 40));
        canvas.addPolygonVertex(QPointF(260, 40));
        canvas.addPolygonVertex(QPointF(260, 140));
        canvas.addPolygonVertex(QPointF(60, 140));
        QCOMPARE(canvas.polygonVertices().size(), 4);

        const QImage alpha = canvas.computedAlphaMask();
        QCOMPARE(alpha.size(), QSize(320, 180));
        QVERIFY(qGray(alpha.pixel(160, 90)) > 200);
        QVERIFY(qGray(alpha.pixel(5, 5)) < 20);

        // Inversion flips foreground/background matte
        canvas.setInverted(true);
        const QImage invAlpha = canvas.computedAlphaMask();
        QVERIFY(qGray(invAlpha.pixel(160, 90)) < 20);
        QVERIFY(qGray(invAlpha.pixel(5, 5)) > 200);
        canvas.setInverted(false);

        // AI +/- point prompts
        QSignalSpy aiSpy(&canvas, &RotoMaskCanvasWidget::aiPromptAdded);
        canvas.addPositivePoint(QPointF(160, 90));
        canvas.addNegativePoint(QPointF(10, 10));
        QCOMPARE(aiSpy.count(), 2);
        QCOMPARE(canvas.positivePoints().size(), 1);
        QCOMPARE(canvas.negativePoints().size(), 1);

        // Brush strokes
        RotoBrushStroke st;
        st.points = {QPointF(100, 90), QPointF(200, 90)};
        st.radius = 12.0;
        st.erase = false;
        canvas.addBrushStroke(st);
        QCOMPARE(canvas.brushStrokes().size(), 1);

        // Verify all 4 preview modes render without error
        for (const RotoPreviewMode mode : {RotoPreviewMode::MaskOverlay, RotoPreviewMode::IsolatedAlpha,
                                           RotoPreviewMode::BeforeAfterSplit, RotoPreviewMode::FinalComposite}) {
            canvas.setPreviewMode(mode);
            const QPixmap rendered = canvas.grab();
            QVERIFY(!rendered.isNull());
        }
    }

    void testRotoStudioMultiMaskKeyframesAndArtCraftTabs()
    {
        RotoStudioDock dock;
        QCOMPARE(dock.maskCount(), 1);

        dock.canvasWidget()->addPolygonVertex(QPointF(40, 40));
        dock.canvasWidget()->addPolygonVertex(QPointF(200, 40));
        dock.canvasWidget()->addPolygonVertex(QPointF(200, 140));
        dock.setCurrentFrame(10);
        dock.addKeyframeAtCurrentFrame();

        // Add & duplicate mask, set boolean subtract + invert
        dock.addMaskLayer(QStringLiteral("Hole Mask"));
        QCOMPARE(dock.maskCount(), 2);
        dock.setMaskCombineMode(1, QStringLiteral("subtract"));
        dock.setMaskInverted(1, true);
        QVERIFY(dock.duplicateSelectedMask());
        QCOMPARE(dock.maskCount(), 3);
        QVERIFY(dock.removeSelectedMask());
        QCOMPARE(dock.maskCount(), 2);

        const QJsonObject session = dock.exportRotoSessionJson();
        QCOMPARE(session.value(QStringLiteral("schema_version")).toString(), QStringLiteral("1.0"));
        QCOMPARE(session.value(QStringLiteral("masks")).toArray().size(), 2);

        // ArtCraft 2D Composite layers & undo/redo
        const int initialLayers = dock.canvas2DLayerCount();
        dock.addCanvas2DLayer(QStringLiteral("Cutout Subject"), QStringLiteral("cutout"));
        QCOMPARE(dock.canvas2DLayerCount(), initialLayers + 1);
        QVERIFY(dock.canUndoCanvas2D());
        QVERIFY(dock.undoCanvas2D());
        QCOMPARE(dock.canvas2DLayerCount(), initialLayers);
        QVERIFY(dock.redoCanvas2D());
        QCOMPARE(dock.canvas2DLayerCount(), initialLayers + 1);

        // ArtCraft Storyboard shots & reorder
        const int initialShots = dock.storyboardShotCount();
        dock.addStoryboardShot(QStringLiteral("Close-up Reaction"), 2.5, QStringLiteral("That worked!"), QStringLiteral("Cut to guest"));
        QCOMPARE(dock.storyboardShotCount(), initialShots + 1);
        QVERIFY(dock.moveSelectedStoryboardShot(-1));
        const QJsonObject sb = dock.exportStoryboardJson();
        QCOMPARE(sb.value(QStringLiteral("shots")).toArray().size(), initialShots + 1);

        // Honest neural vs OpenCV fallback status reporting
        QVERIFY(dock.segmentationBackendStatusText().contains(QStringLiteral("OpenCV")));
        dock.setSegmentationBackendStatus(QStringLiteral("MobileSAM ONNX"), true, QStringLiteral("CPUExecutionProvider"));
        QVERIFY(dock.segmentationBackendStatusText().contains(QStringLiteral("MobileSAM ONNX")));
        QVERIFY(dock.segmentationBackendStatusText().contains(QStringLiteral("Neural")));
        dock.setSegmentationBackendStatus(QStringLiteral("OpenCV GrabCut"), false, QStringLiteral("weights not downloaded"));
        QVERIFY(dock.segmentationBackendStatusText().contains(QStringLiteral("Fallback")));
    }
};

QTEST_MAIN(TestRotoStudio)
#include "test_roto_studio.moc"
