/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "../effects_browser/PremierePresetBrowser.h"

#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSignalSpy>
#include <QTemporaryDir>
#include <QtTest/QtTest>

using namespace BangOnPro;

class TestPremierePresetBrowser : public QObject
{
    Q_OBJECT

private:
    static QJsonObject makeSampleReport()
    {
        QJsonObject pExact;
        pExact.insert(QStringLiteral("preset_id"), QStringLiteral("finzar_zoom_1"));
        pExact.insert(QStringLiteral("name"), QStringLiteral("Finzar Punch Zoom"));
        pExact.insert(QStringLiteral("pack"), QStringLiteral("Finzar"));
        pExact.insert(QStringLiteral("attribution"), QStringLiteral("Finzar Pack"));
        pExact.insert(QStringLiteral("folder_path"), QJsonArray{QStringLiteral("Transforms"), QStringLiteral("Zooms")});
        pExact.insert(QStringLiteral("source_file"), QStringLiteral("finzar.prfpset"));
        pExact.insert(QStringLiteral("preset_type"), QStringLiteral("scale"));
        pExact.insert(QStringLiteral("compatibility_status"), QStringLiteral("exact_validated"));
        pExact.insert(QStringLiteral("notes"), QJsonArray{QStringLiteral("Mapped cleanly to qtblend")});
        pExact.insert(QStringLiteral("effect_chain"), QJsonArray{});
        pExact.insert(QStringLiteral("missing_dependencies"), QJsonArray{});
        QJsonObject flt;
        flt.insert(QStringLiteral("service"), QStringLiteral("qtblend"));
        flt.insert(QStringLiteral("properties"), QJsonObject{{QStringLiteral("rect"), QStringLiteral("0=0 0 1920 1080 1")}});
        pExact.insert(QStringLiteral("mlt_filters"), QJsonArray{flt});
        pExact.insert(QStringLiteral("kdenlive_effect_xml"), QStringLiteral("<effectgroup id=\"Finzar Punch Zoom\"/>"));
        pExact.insert(QStringLiteral("original_xml_preserved"), true);

        QJsonObject pUnsupported;
        pUnsupported.insert(QStringLiteral("preset_id"), QStringLiteral("mettle_vr_1"));
        pUnsupported.insert(QStringLiteral("name"), QStringLiteral("Mettle VR Chromatic"));
        pUnsupported.insert(QStringLiteral("pack"), QStringLiteral("ViralFX"));
        pUnsupported.insert(QStringLiteral("attribution"), QStringLiteral("Mettle"));
        pUnsupported.insert(QStringLiteral("folder_path"), QJsonArray{QStringLiteral("VR")});
        pUnsupported.insert(QStringLiteral("source_file"), QStringLiteral("viralfx.prfpset"));
        pUnsupported.insert(QStringLiteral("preset_type"), QStringLiteral("scale"));
        pUnsupported.insert(QStringLiteral("compatibility_status"), QStringLiteral("unsupported"));
        pUnsupported.insert(QStringLiteral("notes"), QJsonArray{QStringLiteral("Proprietary Mettle VR plugin not licensed")});
        pUnsupported.insert(QStringLiteral("effect_chain"), QJsonArray{});
        pUnsupported.insert(QStringLiteral("missing_dependencies"),
                            QJsonArray{QJsonObject{{QStringLiteral("kind"), QStringLiteral("plugin")}, {QStringLiteral("name"), QStringLiteral("Mettle VR")}}});
        pUnsupported.insert(QStringLiteral("mlt_filters"), QJsonArray{});
        pUnsupported.insert(QStringLiteral("kdenlive_effect_xml"), QJsonValue::Null);
        pUnsupported.insert(QStringLiteral("original_xml_preserved"), true);

        QJsonObject report;
        report.insert(QStringLiteral("schema_version"), QStringLiteral("1.0"));
        report.insert(QStringLiteral("generated_at"), QStringLiteral("2026-11-23T00:00:00Z"));
        report.insert(QStringLiteral("source_archive"), QJsonObject{{QStringLiteral("name"), QStringLiteral("synthetic.zip")},
                                                                    {QStringLiteral("sha256"), QStringLiteral("abcdef0123456789")},
                                                                    {QStringLiteral("file_count"), 2}});
        report.insert(QStringLiteral("summary"), QJsonObject{{QStringLiteral("total_presets"), 2},
                                                             {QStringLiteral("distinct_effects"), 2},
                                                             {QStringLiteral("exact_validated"), 1},
                                                             {QStringLiteral("approximated"), 0},
                                                             {QStringLiteral("unsupported"), 1},
                                                             {QStringLiteral("missing_dependency"), 0}});
        report.insert(QStringLiteral("presets"), QJsonArray{pExact, pUnsupported});
        return report;
    }

private Q_SLOTS:
    void testLoadReportRetainsUnsupportedPresets()
    {
        PremierePresetBrowser browser;
        QString err;
        QVERIFY2(browser.loadReportJson(makeSampleReport(), &err), qPrintable(err));
        QCOMPARE(browser.presetCount(), 2);
        QCOMPARE(browser.visiblePresetCount(), 2);

        PresetDisplayEntry unsupported;
        QVERIFY(browser.findPresetById(QStringLiteral("mettle_vr_1"), &unsupported));
        QCOMPARE(unsupported.record.status, PresetCompatibilityStatus::Unsupported);
    }

    void testFavoritesCustomStackAndMimePayload()
    {
        PremierePresetBrowser browser;
        QVERIFY(browser.loadReportJson(makeSampleReport()));

        browser.setFavorite(QStringLiteral("finzar_zoom_1"), true);
        QVERIFY(browser.isFavorite(QStringLiteral("finzar_zoom_1")));
        QCOMPARE(browser.favoritePresetIds().size(), 1);

        QVERIFY(browser.saveCustomStack(QStringLiteral("Creator Punch"), QStringList{QStringLiteral("finzar_zoom_1")}));
        QCOMPARE(browser.customStacks().size(), 1);
        QCOMPARE(browser.customStacks().first().combinedMltFilters.size(), 1);

        const QByteArray mime = browser.buildMimePayloadForPreset(QStringLiteral("finzar_zoom_1"));
        QVERIFY(!mime.isEmpty());
        const QJsonObject payload = QJsonDocument::fromJson(mime).object();
        QCOMPARE(payload.value(QStringLiteral("preset_id")).toString(), QStringLiteral("finzar_zoom_1"));
    }

    void testUserStatePersistenceAndParamEditor()
    {
        QTemporaryDir tmpDir;
        QVERIFY(tmpDir.isValid());
        const QString statePath = tmpDir.filePath(QStringLiteral("preset_user_state.json"));

        {
            PremierePresetBrowser browser;
            QVERIFY(browser.loadReportJson(makeSampleReport()));
            browser.setFavorite(QStringLiteral("finzar_zoom_1"), true);
            QVERIFY(browser.saveCustomStack(QStringLiteral("Broadcast Stack"), QStringList{QStringLiteral("finzar_zoom_1")}));
            QVERIFY(browser.saveUserState(statePath));
        }

        {
            PremierePresetBrowser reloaded;
            QVERIFY(reloaded.loadReportJson(makeSampleReport()));
            QVERIFY(reloaded.loadUserState(statePath));
            QVERIFY(reloaded.isFavorite(QStringLiteral("finzar_zoom_1")));
            QCOMPARE(reloaded.customStacks().size(), 1);
            QCOMPARE(reloaded.customStacks().first().name, QStringLiteral("Broadcast Stack"));
        }
    }
};

QTEST_MAIN(TestPremierePresetBrowser)
#include "test_premiere_preset_browser.moc"
