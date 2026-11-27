/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "../effects_browser/IntrinsicTransformWidget.h"
#include "../effects_browser/PasteAttributesDialog.h"
#include "../effects_browser/PremierePresetBrowser.h"
#include "../effects_browser/UnifiedEffectsPanel.h"

#include <QJsonArray>
#include <QJsonObject>
#include <QMimeData>
#include <QSettings>
#include <QSignalSpy>
#include <QtTest/QtTest>
#include <cmath>
#include <memory>

using namespace BangOnPro;

class TestEffectsAndControls : public QObject
{
    Q_OBJECT

private:
    static QVector<EffectCatalogEntry> makeSampleCatalog()
    {
        QVector<EffectCatalogEntry> entries;

        EffectCatalogEntry transform;
        transform.id = QStringLiteral("qtblend");
        transform.name = QStringLiteral("Transform");
        transform.description = QStringLiteral("Position, scale, rotation and opacity");
        transform.kind = CatalogAssetKind::VideoEffect;
        entries.append(transform);

        EffectCatalogEntry blur;
        blur.id = QStringLiteral("avfilter.gblur");
        blur.name = QStringLiteral("Gaussian Blur");
        blur.description = QStringLiteral("Smooth directional gaussian blur");
        blur.kind = CatalogAssetKind::VideoEffect;
        entries.append(blur);

        EffectCatalogEntry eq;
        eq.id = QStringLiteral("avfilter.equalizer");
        eq.name = QStringLiteral("Parametric EQ");
        eq.description = QStringLiteral("Sculpt audio frequency bands");
        eq.kind = CatalogAssetKind::AudioEffect;
        entries.append(eq);

        EffectCatalogEntry dissolve;
        dissolve.id = QStringLiteral("luma");
        dissolve.name = QStringLiteral("Dissolve");
        dissolve.description = QStringLiteral("Smooth cross dissolve transition");
        dissolve.kind = CatalogAssetKind::VideoTransition;
        entries.append(dissolve);

        EffectCatalogEntry audioXfade;
        audioXfade.id = QStringLiteral("mix");
        audioXfade.name = QStringLiteral("Constant Power Crossfade");
        audioXfade.description = QStringLiteral("Audio crossfade");
        audioXfade.kind = CatalogAssetKind::AudioTransition;
        entries.append(audioXfade);

        EffectCatalogEntry customLook;
        customLook.id = QStringLiteral("custom_warm_grade");
        customLook.name = QStringLiteral("Warm Studio Grade");
        customLook.description = QStringLiteral("Saved custom colour stack");
        customLook.kind = CatalogAssetKind::CustomPreset;
        entries.append(customLook);

        EffectCatalogEntry legacyFx;
        legacyFx.id = QStringLiteral("old_boxblur_legacy");
        legacyFx.name = QStringLiteral("Old Box Blur - deprecated");
        legacyFx.category = QStringLiteral("Deprecated");
        legacyFx.description = QStringLiteral("Legacy filter kept for old projects");
        legacyFx.kind = CatalogAssetKind::VideoEffect;
        legacyFx.isLegacyOrUnverified = true;
        entries.append(legacyFx);

        EffectCatalogEntry unverifiedFx;
        unverifiedFx.id = QStringLiteral("experimental_wip");
        unverifiedFx.name = QStringLiteral("WIP Filter");
        unverifiedFx.category = QStringLiteral("More checks");
        unverifiedFx.description = QStringLiteral("To be completed");
        unverifiedFx.kind = CatalogAssetKind::VideoEffect;
        entries.append(unverifiedFx);

        return entries;
    }

    static QVector<PresetDisplayEntry> makeSamplePremierePresets()
    {
        QVector<PresetDisplayEntry> list;

        PresetDisplayEntry okPreset;
        okPreset.record.presetId = QStringLiteral("pr_punch_zoom");
        okPreset.record.name = QStringLiteral("Punch Zoom In");
        okPreset.record.pack = QStringLiteral("Finzar");
        okPreset.record.status = PresetCompatibilityStatus::ExactValidated;
        okPreset.record.mltFilters = QJsonArray{QJsonObject{{QStringLiteral("service"), QStringLiteral("qtblend")}}};
        okPreset.description = QStringLiteral("Fast punch-in zoom");
        list.append(okPreset);

        PresetDisplayEntry badPreset;
        badPreset.record.presetId = QStringLiteral("pr_mettle_vr");
        badPreset.record.name = QStringLiteral("Mettle VR Glow");
        badPreset.record.pack = QStringLiteral("ViralFX");
        badPreset.record.status = PresetCompatibilityStatus::Unsupported;
        badPreset.record.mltFilters = QJsonArray{};
        list.append(badPreset);

        return list;
    }

private Q_SLOTS:
    void testUnifiedEffectsSearchFilterLegacyAndPersistence()
    {
        const QString org = QStringLiteral("BangOnProUnitTest");
        const QString app = QStringLiteral("UnifiedEffectsPanelTest");
        {
            QSettings clearSettings(org, app);
            clearSettings.clear();
            clearSettings.sync();
        }

        UnifiedEffectsPanel panel(nullptr, org, app, false);
        panel.populateFromEntries(makeSampleCatalog());
        panel.setPremierePresets(makeSamplePremierePresets());

        // Legacy/deprecated/unverified/unsupported entries must be hidden by default
        QVERIFY(!panel.showLegacyAndUnverified());
        const QStringList defaultVisible = panel.visibleEntryIds();
        QVERIFY(defaultVisible.contains(QStringLiteral("qtblend")));
        QVERIFY(defaultVisible.contains(QStringLiteral("avfilter.gblur")));
        QVERIFY(defaultVisible.contains(QStringLiteral("avfilter.equalizer")));
        QVERIFY(defaultVisible.contains(QStringLiteral("luma")));
        QVERIFY(defaultVisible.contains(QStringLiteral("mix")));
        QVERIFY(defaultVisible.contains(QStringLiteral("custom_warm_grade")));
        QVERIFY(defaultVisible.contains(QStringLiteral("pr_punch_zoom")));
        QVERIFY(!defaultVisible.contains(QStringLiteral("old_boxblur_legacy")));
        QVERIFY(!defaultVisible.contains(QStringLiteral("experimental_wip")));
        QVERIFY(!defaultVisible.contains(QStringLiteral("pr_mettle_vr")));

        // Toggling "Show legacy & unverified" reveals them
        panel.setShowLegacyAndUnverified(true);
        const QStringList allVisible = panel.visibleEntryIds();
        QVERIFY(allVisible.contains(QStringLiteral("old_boxblur_legacy")));
        QVERIFY(allVisible.contains(QStringLiteral("experimental_wip")));
        QVERIFY(allVisible.contains(QStringLiteral("pr_mettle_vr")));
        panel.setShowLegacyAndUnverified(false);

        // Search by name, category, description, and ID
        panel.setSearchQuery(QStringLiteral("Gaussian"), true);
        QCOMPARE(panel.visibleEntryIds(), QStringList{QStringLiteral("avfilter.gblur")});

        panel.setSearchQuery(QStringLiteral("Audio Dynamics"), true);
        QCOMPARE(panel.visibleEntryIds(), QStringList{QStringLiteral("avfilter.equalizer")});

        panel.setSearchQuery(QStringLiteral("cross dissolve"), true);
        QCOMPARE(panel.visibleEntryIds(), QStringList{QStringLiteral("luma")});

        panel.setSearchQuery(QStringLiteral("pr_punch_zoom"), true);
        QCOMPARE(panel.visibleEntryIds(), QStringList{QStringLiteral("pr_punch_zoom")});

        panel.setSearchQuery(QString(), true);

        // Scope filter pills
        panel.setScopeFilter(EffectsScopeFilter::Video);
        QCOMPARE(panel.visibleEntryCount(), 2);

        panel.setScopeFilter(EffectsScopeFilter::Audio);
        QCOMPARE(panel.visibleEntryIds(), QStringList{QStringLiteral("avfilter.equalizer")});

        panel.setScopeFilter(EffectsScopeFilter::Transitions);
        QCOMPARE(panel.visibleEntryCount(), 2);

        panel.setScopeFilter(EffectsScopeFilter::Presets);
        QCOMPARE(panel.visibleEntryCount(), 2);

        // Favourites & Recent + signals + persistence
        panel.setScopeFilter(EffectsScopeFilter::All);
        panel.setFavorite(QStringLiteral("avfilter.gblur"), true);
        panel.setScopeFilter(EffectsScopeFilter::Favourites);
        QCOMPARE(panel.visibleEntryIds(), QStringList{QStringLiteral("avfilter.gblur")});

        QSignalSpy fxSpy(&panel, &UnifiedEffectsPanel::applyEffectRequested);
        QSignalSpy trSpy(&panel, &UnifiedEffectsPanel::applyTransitionRequested);
        QSignalSpy prSpy(&panel, &UnifiedEffectsPanel::applyPremierePresetRequested);

        QVERIFY(panel.applyEntry(QStringLiteral("qtblend")));
        QVERIFY(panel.applyEntry(QStringLiteral("luma")));
        QVERIFY(panel.applyEntry(QStringLiteral("pr_punch_zoom")));
        QCOMPARE(fxSpy.count(), 1);
        QCOMPARE(trSpy.count(), 1);
        QCOMPARE(prSpy.count(), 1);

        panel.setScopeFilter(EffectsScopeFilter::Recent);
        // Most recent first
        QCOMPARE(panel.visibleEntryIds(), (QStringList{QStringLiteral("pr_punch_zoom"), QStringLiteral("luma"), QStringLiteral("qtblend")}));

        // Reload in a fresh panel instance and confirm QSettings persistence
        UnifiedEffectsPanel reloaded(nullptr, org, app, false);
        reloaded.populateFromEntries(makeSampleCatalog());
        reloaded.setPremierePresets(makeSamplePremierePresets());
        QVERIFY(reloaded.isFavorite(QStringLiteral("avfilter.gblur")));
        QCOMPARE(reloaded.recentIds(), (QStringList{QStringLiteral("pr_punch_zoom"), QStringLiteral("luma"), QStringLiteral("qtblend")}));

        // Clean up test QSettings
        QSettings cleanup(org, app);
        cleanup.clear();
        cleanup.sync();
    }

    void testUnifiedEffectsMimePayloads()
    {
        UnifiedEffectsPanel panel(nullptr, QStringLiteral("BangOnProUnitTest"), QStringLiteral("UnifiedEffectsMimeTest"), false);
        panel.populateFromEntries(makeSampleCatalog());
        panel.setPremierePresets(makeSamplePremierePresets());

        // Native effect -> kdenlive/effect
        std::unique_ptr<QMimeData> fxMime(panel.createMimeDataForEntry(QStringLiteral("avfilter.gblur")));
        QVERIFY(fxMime != nullptr);
        QVERIFY(fxMime->hasFormat(QStringLiteral("kdenlive/effect")));
        QCOMPARE(QString::fromUtf8(fxMime->data(QStringLiteral("kdenlive/effect"))), QStringLiteral("avfilter.gblur"));

        // Native transition -> kdenlive/composition
        std::unique_ptr<QMimeData> trMime(panel.createMimeDataForEntry(QStringLiteral("luma")));
        QVERIFY(trMime != nullptr);
        QVERIFY(trMime->hasFormat(QStringLiteral("kdenlive/composition")));
        QCOMPARE(QString::fromUtf8(trMime->data(QStringLiteral("kdenlive/composition"))), QStringLiteral("luma"));

        // Premiere preset with drag ID resolver -> resolved kdenlive/effect + application/x-bangonpro-preset
        panel.setPresetDragIdResolver([](const QString &presetId) -> QString { return QStringLiteral("bop_installed_") + presetId; });
        std::unique_ptr<QMimeData> prMime(panel.createMimeDataForEntry(QStringLiteral("pr_punch_zoom")));
        QVERIFY(prMime != nullptr);
        QVERIFY(prMime->hasFormat(QStringLiteral("kdenlive/effect")));
        QCOMPARE(QString::fromUtf8(prMime->data(QStringLiteral("kdenlive/effect"))), QStringLiteral("bop_installed_pr_punch_zoom"));
        QVERIFY(prMime->hasFormat(QString::fromLatin1(PremierePresetBrowser::kPresetMimeType)));
    }

    void testIntrinsicTransformMathRoundtripAndAnchorPivot()
    {
        const double frameW = 1920.0;
        const double frameH = 1080.0;

        // 1. Neutral transform
        {
            const CompensatedRect r = IntrinsicTransformWidget::computeCompensatedRect(frameW, frameH, 0.0, 0.0, 100.0, 100.0, 0.0, 0.5, 0.5);
            QVERIFY(std::fabs(r.x - 0.0) < 1e-6);
            QVERIFY(std::fabs(r.y - 0.0) < 1e-6);
            QVERIFY(std::fabs(r.width - 1920.0) < 1e-6);
            QVERIFY(std::fabs(r.height - 1080.0) < 1e-6);
        }

        // 2. Non-center anchor (0.0, 0.0) scaled 200% keeps top-left pinned at (0, 0)
        {
            const CompensatedRect r = IntrinsicTransformWidget::computeCompensatedRect(frameW, frameH, 0.0, 0.0, 200.0, 200.0, 0.0, 0.0, 0.0);
            QVERIFY(std::fabs(r.x - 0.0) < 1e-6);
            QVERIFY(std::fabs(r.y - 0.0) < 1e-6);
            QVERIFY(std::fabs(r.width - 3840.0) < 1e-6);
            QVERIFY(std::fabs(r.height - 2160.0) < 1e-6);
        }

        // 3. Top-left anchor (0.0, 0.0) rotated +90 deg keeps top-left corner at (100, 50)
        {
            const double posX = 100.0;
            const double posY = 50.0;
            const CompensatedRect r = IntrinsicTransformWidget::computeCompensatedRect(frameW, frameH, posX, posY, 100.0, 100.0, 90.0, 0.0, 0.0);
            const double cx = r.x + r.width * 0.5;
            const double cy = r.y + r.height * 0.5;
            // After +90 deg rotation around (cx, cy), local top-left (-w/2, -h/2) maps to (+h/2, -w/2)
            const double topLeftAfterRotX = cx + r.height * 0.5;
            const double topLeftAfterRotY = cy - r.width * 0.5;
            QVERIFY(std::fabs(topLeftAfterRotX - posX) < 1e-6);
            QVERIFY(std::fabs(topLeftAfterRotY - posY) < 1e-6);
        }

        // 4. Arbitrary anchor (0.25, 0.75) + non-uniform scale + rotation roundtrip
        {
            const double posX = -142.5;
            const double posY = 88.25;
            const double sx = 145.0;
            const double sy = 82.5;
            const double rot = 37.5;
            const double ax = 0.25;
            const double ay = 0.75;

            const CompensatedRect r = IntrinsicTransformWidget::computeCompensatedRect(frameW, frameH, posX, posY, sx, sy, rot, ax, ay);
            const DecomposedTransform d = IntrinsicTransformWidget::decomposeRect(frameW, frameH, r.x, r.y, r.width, r.height, rot, ax, ay);
            QVERIFY(std::fabs(d.posX - posX) < 1e-6);
            QVERIFY(std::fabs(d.posY - posY) < 1e-6);
            QVERIFY(std::fabs(d.scaleXPercent - sx) < 1e-6);
            QVERIFY(std::fabs(d.scaleYPercent - sy) < 1e-6);
        }

        // 5. formatMltRect & parseMltRect
        {
            const QString formatted = IntrinsicTransformWidget::formatMltRect(-120.4, 45.6, 960.0, 540.0, 0.75);
            QCOMPARE(formatted, QStringLiteral("-120 46 960 540 0.75"));

            double x = 0.0;
            double y = 0.0;
            double w = 0.0;
            double h = 0.0;
            double op = 0.0;
            QVERIFY(IntrinsicTransformWidget::parseMltRect(formatted, &x, &y, &w, &h, &op));
            QCOMPARE(x, -120.0);
            QCOMPARE(y, 46.0);
            QCOMPARE(w, 960.0);
            QCOMPARE(h, 540.0);
            QVERIFY(std::fabs(op - 0.75) < 1e-6);

            // Keyframe-prefixed string
            QVERIFY(IntrinsicTransformWidget::parseMltRect(QStringLiteral("0=10 20 1920 1080 0.5;25=0 0 1920 1080 1"), &x, &y, &w, &h, &op));
            QCOMPARE(x, 10.0);
            QCOMPARE(y, 20.0);
            QCOMPARE(w, 1920.0);
            QCOMPARE(h, 1080.0);
            QVERIFY(std::fabs(op - 0.5) < 1e-6);
        }
    }

    void testIntrinsicTransformWidgetDefaultsAndReset()
    {
        IntrinsicTransformWidget widget;
        QVERIFY(!widget.hasMaterializedTransform());
        QVERIFY(widget.isTransformEnabled());
        QCOMPARE(widget.positionX(), 0.0);
        QCOMPARE(widget.positionY(), 0.0);
        QCOMPARE(widget.scaleXPercent(), 100.0);
        QCOMPARE(widget.scaleYPercent(), 100.0);
        QVERIFY(widget.isAspectRatioLocked());
        QCOMPARE(widget.rotationDegrees(), 0.0);
        QCOMPARE(widget.anchorXPercent(), 50.0);
        QCOMPARE(widget.anchorYPercent(), 50.0);
        QCOMPARE(widget.opacityPercent(), 100.0);
        QCOMPARE(widget.blendModeCode(), 0);

        widget.setPosition(120.0, -45.0);
        widget.setAspectRatioLocked(false);
        widget.setScalePercent(150.0, 90.0);
        widget.setRotationDegrees(22.5);
        widget.setAnchorPercent(25.0, 75.0);
        widget.setOpacityPercent(60.0);
        widget.setBlendModeCode(13); // Multiply

        QCOMPARE(widget.positionX(), 120.0);
        QCOMPARE(widget.positionY(), -45.0);
        QCOMPARE(widget.scaleXPercent(), 150.0);
        QCOMPARE(widget.scaleYPercent(), 90.0);
        QCOMPARE(widget.rotationDegrees(), 22.5);
        QCOMPARE(widget.anchorXPercent(), 25.0);
        QCOMPARE(widget.anchorYPercent(), 75.0);
        QCOMPARE(widget.opacityPercent(), 60.0);
        QCOMPARE(widget.blendModeCode(), 13);

        widget.resetTransform();
        QCOMPARE(widget.positionX(), 0.0);
        QCOMPARE(widget.positionY(), 0.0);
        QCOMPARE(widget.scaleXPercent(), 100.0);
        QCOMPARE(widget.scaleYPercent(), 100.0);
        QVERIFY(widget.isAspectRatioLocked());
        QCOMPARE(widget.rotationDegrees(), 0.0);
        QCOMPARE(widget.anchorXPercent(), 50.0);
        QCOMPARE(widget.anchorYPercent(), 50.0);
        QCOMPARE(widget.opacityPercent(), 100.0);
        QCOMPARE(widget.blendModeCode(), 0);
    }

    void testPasteAttributesDialogSelectiveFiltering()
    {
        const QString sampleXml = QStringLiteral("<effects parentIn=\"0\">"
                                                 "<effect id=\"qtblend\"><property name=\"rect\">0 0 1920 1080 1</property></effect>"
                                                 "<effect id=\"avfilter.gblur\"><property name=\"av.sigma\">4.0</property></effect>"
                                                 "<effect id=\"lift_gamma_gain\"><property name=\"lift_r\">0.1</property></effect>"
                                                 "</effects>");

        const QVector<AttributeItem> parsed = PasteAttributesDialog::parseEffectsXml(sampleXml);
        QCOMPARE(parsed.size(), 3);
        QCOMPARE(parsed.at(0).effectId, QStringLiteral("qtblend"));
        QVERIFY(parsed.at(0).isIntrinsicTransform);
        QCOMPARE(parsed.at(1).effectId, QStringLiteral("avfilter.gblur"));
        QVERIFY(!parsed.at(1).isIntrinsicTransform);
        QCOMPARE(parsed.at(2).effectId, QStringLiteral("lift_gamma_gain"));

        PasteAttributesDialog dlg(parsed);
        QCOMPARE(dlg.selectedItems().size(), 3);
        QVERIFY(!dlg.clearExistingBeforePaste());

        dlg.selectNone();
        QCOMPARE(dlg.selectedItems().size(), 0);
        QVERIFY(dlg.buildCombinedEffectsXml().isEmpty());

        dlg.setItemSelected(0, true);
        dlg.setItemSelected(2, true);
        QCOMPARE(dlg.selectedItems().size(), 2);
        QCOMPARE(dlg.selectedEffectXmls().size(), 2);

        const QString combined = dlg.buildCombinedEffectsXml();
        QVERIFY(combined.contains(QStringLiteral("qtblend")));
        QVERIFY(!combined.contains(QStringLiteral("avfilter.gblur")));
        QVERIFY(combined.contains(QStringLiteral("lift_gamma_gain")));

        dlg.selectAll();
        QCOMPARE(dlg.selectedItems().size(), 3);

        dlg.setClearExistingBeforePaste(true);
        QVERIFY(dlg.clearExistingBeforePaste());
    }
};

QTEST_MAIN(TestEffectsAndControls)
#include "test_effects_and_controls.moc"
