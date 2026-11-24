/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "TimelineCommandEngine.h"

#include <QDateTime>
#include <QJsonDocument>
#include <QSet>
#include <QXmlStreamWriter>

#include <algorithm>
#include <cmath>
#include <limits>

namespace BangOnPro {

namespace {

bool jsonValueIsStrictInteger(const QJsonValue &val, qint64 *out = nullptr)
{
    if (!val.isDouble()) {
        return false;
    }
    const double d = val.toDouble();
    if (!std::isfinite(d) || std::floor(d) != d) {
        return false;
    }
    if (d < static_cast<double>(std::numeric_limits<qint64>::min()) || d > static_cast<double>(std::numeric_limits<qint64>::max())) {
        return false;
    }
    if (out != nullptr) {
        *out = static_cast<qint64>(d);
    }
    return true;
}

bool jsonValueIsStrictInt(const QJsonValue &val, int *out = nullptr)
{
    qint64 wide = 0;
    if (!jsonValueIsStrictInteger(val, &wide)) {
        return false;
    }
    if (wide < std::numeric_limits<int>::min() || wide > std::numeric_limits<int>::max()) {
        return false;
    }
    if (out != nullptr) {
        *out = static_cast<int>(wide);
    }
    return true;
}

QJsonObject frameRangeToJson(const FrameRange &fr)
{
    QJsonObject obj;
    obj.insert(QStringLiteral("in_frame"), fr.inFrame);
    obj.insert(QStringLiteral("out_frame"), fr.outFrame);
    obj.insert(QStringLiteral("timeline_start"), fr.timelineStart);
    obj.insert(QStringLiteral("fps_num"), fr.fpsNum);
    obj.insert(QStringLiteral("fps_den"), fr.fpsDen);
    return obj;
}

bool frameRangeFromJson(const QJsonObject &obj, FrameRange *frOut, QString *errorOut)
{
    FrameRange fr;
    if (obj.contains(QStringLiteral("in_frame"))) {
        if (!jsonValueIsStrictInt(obj.value(QStringLiteral("in_frame")), &fr.inFrame)) {
            if (errorOut != nullptr) {
                *errorOut = QStringLiteral("frame_range.in_frame must be an integer");
            }
            return false;
        }
    }
    if (obj.contains(QStringLiteral("out_frame"))) {
        if (!jsonValueIsStrictInt(obj.value(QStringLiteral("out_frame")), &fr.outFrame)) {
            if (errorOut != nullptr) {
                *errorOut = QStringLiteral("frame_range.out_frame must be an integer");
            }
            return false;
        }
    }
    if (obj.contains(QStringLiteral("timeline_start"))) {
        if (!jsonValueIsStrictInt(obj.value(QStringLiteral("timeline_start")), &fr.timelineStart)) {
            if (errorOut != nullptr) {
                *errorOut = QStringLiteral("frame_range.timeline_start must be an integer");
            }
            return false;
        }
    }
    if (obj.contains(QStringLiteral("fps_num"))) {
        if (!jsonValueIsStrictInt(obj.value(QStringLiteral("fps_num")), &fr.fpsNum)) {
            if (errorOut != nullptr) {
                *errorOut = QStringLiteral("frame_range.fps_num must be an integer");
            }
            return false;
        }
    }
    if (obj.contains(QStringLiteral("fps_den"))) {
        if (!jsonValueIsStrictInt(obj.value(QStringLiteral("fps_den")), &fr.fpsDen)) {
            if (errorOut != nullptr) {
                *errorOut = QStringLiteral("frame_range.fps_den must be an integer");
            }
            return false;
        }
    }
    *frOut = fr;
    return true;
}

QJsonObject keyframeMapToJson(const QMap<QString, QVector<KeyframeRecord>> &map)
{
    QJsonObject obj;
    for (auto it = map.cbegin(); it != map.cend(); ++it) {
        QJsonArray arr;
        for (const KeyframeRecord &kf : it.value()) {
            QJsonObject kfObj;
            kfObj.insert(QStringLiteral("frame"), kf.frame);
            kfObj.insert(QStringLiteral("value"), kf.value);
            kfObj.insert(QStringLiteral("interpolation"), kf.interpolation);
            arr.append(kfObj);
        }
        obj.insert(it.key(), arr);
    }
    return obj;
}

QMap<QString, QVector<KeyframeRecord>> keyframeMapFromJson(const QJsonObject &obj)
{
    QMap<QString, QVector<KeyframeRecord>> result;
    for (auto it = obj.constBegin(); it != obj.constEnd(); ++it) {
        QVector<KeyframeRecord> list;
        const QJsonArray arr = it.value().toArray();
        list.reserve(arr.size());
        for (const QJsonValue &v : arr) {
            const QJsonObject kfObj = v.toObject();
            KeyframeRecord kf;
            kf.frame = kfObj.value(QStringLiteral("frame")).toInt();
            kf.value = kfObj.value(QStringLiteral("value"));
            kf.interpolation = kfObj.value(QStringLiteral("interpolation")).toString(QStringLiteral("linear"));
            list.append(kf);
        }
        result.insert(it.key(), list);
    }
    return result;
}

QJsonArray effectsToJson(const QVector<EffectInstance> &effects)
{
    QJsonArray arr;
    for (const EffectInstance &fx : effects) {
        QJsonObject obj;
        obj.insert(QStringLiteral("instance_id"), fx.instanceId);
        obj.insert(QStringLiteral("effect_id"), fx.effectId);
        obj.insert(QStringLiteral("is_audio_filter"), fx.isAudioFilter);
        obj.insert(QStringLiteral("enabled"), fx.enabled);
        obj.insert(QStringLiteral("parameters"), fx.parameters);
        obj.insert(QStringLiteral("keyframes"), keyframeMapToJson(fx.keyframes));
        arr.append(obj);
    }
    return arr;
}

QVector<EffectInstance> effectsFromJson(const QJsonArray &arr)
{
    QVector<EffectInstance> list;
    list.reserve(arr.size());
    for (const QJsonValue &v : arr) {
        const QJsonObject obj = v.toObject();
        EffectInstance fx;
        fx.instanceId = obj.value(QStringLiteral("instance_id")).toString();
        fx.effectId = obj.value(QStringLiteral("effect_id")).toString();
        fx.isAudioFilter = obj.value(QStringLiteral("is_audio_filter")).toBool(false);
        fx.enabled = obj.value(QStringLiteral("enabled")).toBool(true);
        fx.parameters = obj.value(QStringLiteral("parameters")).toObject();
        fx.keyframes = keyframeMapFromJson(obj.value(QStringLiteral("keyframes")).toObject());
        list.append(fx);
    }
    return list;
}

void upsertKeyframe(QMap<QString, QVector<KeyframeRecord>> &map, const QString &paramName, const KeyframeRecord &record)
{
    QVector<KeyframeRecord> &list = map[paramName];
    for (KeyframeRecord &existing : list) {
        if (existing.frame == record.frame) {
            existing.value = record.value;
            existing.interpolation = record.interpolation;
            return;
        }
    }
    list.append(record);
    std::sort(list.begin(), list.end(), [](const KeyframeRecord &a, const KeyframeRecord &b) { return a.frame < b.frame; });
}

QString firstNonEmptyStringParam(const QJsonObject &params, const QStringList &keys)
{
    for (const QString &key : keys) {
        const QJsonValue val = params.value(key);
        if (val.isString()) {
            const QString s = val.toString().trimmed();
            if (!s.isEmpty()) {
                return s;
            }
        }
    }
    return {};
}

bool extractAudioGainDb(const QJsonObject &params, double *gainDbOut, QString *errorOut)
{
    static const QStringList dbKeys = {QStringLiteral("gain_db"), QStringLiteral("level_db"), QStringLiteral("db")};
    for (const QString &key : dbKeys) {
        if (params.contains(key)) {
            const QJsonValue v = params.value(key);
            if (!v.isDouble()) {
                if (errorOut != nullptr) {
                    *errorOut = QStringLiteral("Parameter '%1' must be numeric").arg(key);
                }
                return false;
            }
            const double db = v.toDouble();
            if (!std::isfinite(db) || db < -96.0 || db > 24.0) {
                if (errorOut != nullptr) {
                    *errorOut = QStringLiteral("Audio gain_db (%1) out of valid range [-96.0, 24.0]").arg(db);
                }
                return false;
            }
            *gainDbOut = db;
            return true;
        }
    }

    static const QStringList linearKeys = {QStringLiteral("gain"), QStringLiteral("level")};
    for (const QString &key : linearKeys) {
        if (params.contains(key)) {
            const QJsonValue v = params.value(key);
            if (!v.isDouble()) {
                if (errorOut != nullptr) {
                    *errorOut = QStringLiteral("Parameter '%1' must be numeric").arg(key);
                }
                return false;
            }
            const double linear = v.toDouble();
            if (!std::isfinite(linear) || linear < 0.0 || linear > 16.0) {
                if (errorOut != nullptr) {
                    *errorOut = QStringLiteral("Linear audio gain (%1) out of valid range [0.0, 16.0]").arg(linear);
                }
                return false;
            }
            *gainDbOut = (linear <= 0.0) ? -96.0 : qBound(-96.0, 20.0 * std::log10(linear), 24.0);
            return true;
        }
    }

    if (errorOut != nullptr) {
        *errorOut = QStringLiteral("set_audio_level requires gain_db, level_db, db, gain, or level parameter");
    }
    return false;
}

bool commandStatusFromString(const QString &s, CommandStatus *statusOut)
{
    if (s == QStringLiteral("ok")) {
        *statusOut = CommandStatus::Ok;
        return true;
    }
    if (s == QStringLiteral("validation_error")) {
        *statusOut = CommandStatus::ValidationError;
        return true;
    }
    if (s == QStringLiteral("revision_conflict")) {
        *statusOut = CommandStatus::RevisionConflict;
        return true;
    }
    if (s == QStringLiteral("execution_failed")) {
        *statusOut = CommandStatus::ExecutionFailed;
        return true;
    }
    if (s == QStringLiteral("rolled_back")) {
        *statusOut = CommandStatus::RolledBack;
        return true;
    }
    if (s == QStringLiteral("pending_approval")) {
        *statusOut = CommandStatus::PendingApproval;
        return true;
    }
    return false;
}

QString extractTrackParamString(const QJsonObject &params, const QString &key)
{
    if (!params.contains(key)) {
        return {};
    }
    const QJsonValue v = params.value(key);
    if (v.isString()) {
        return v.toString().trimmed();
    }
    int numeric = 0;
    if (jsonValueIsStrictInt(v, &numeric)) {
        return QString::number(numeric);
    }
    return {};
}

bool isValidMaskType(const QString &maskType)
{
    return maskType == QStringLiteral("spline") || maskType == QStringLiteral("polygon") || maskType == QStringLiteral("bezier") ||
           maskType == QStringLiteral("matte_file") || maskType == QStringLiteral("ai_sam");
}

bool isValidMaskOperation(const QString &operation)
{
    return operation == QStringLiteral("write_on_clear") || operation == QStringLiteral("add") || operation == QStringLiteral("subtract") ||
           operation == QStringLiteral("intersect");
}

bool extractSpeedPercent(const QJsonObject &params, double *speedPercentOut, QString *errorOut)
{
    if (params.contains(QStringLiteral("speed_percent"))) {
        const QJsonValue v = params.value(QStringLiteral("speed_percent"));
        if (!v.isDouble()) {
            if (errorOut != nullptr) {
                *errorOut = QStringLiteral("set_clip_speed params.speed_percent must be numeric");
            }
            return false;
        }
        const double sp = v.toDouble();
        if (!std::isfinite(sp) || std::abs(sp) < 1.0 || std::abs(sp) > 10000.0) {
            if (errorOut != nullptr) {
                *errorOut = QStringLiteral("set_clip_speed speed_percent (%1) must have magnitude within [1.0, 10000.0]").arg(sp);
            }
            return false;
        }
        if (speedPercentOut != nullptr) {
            *speedPercentOut = sp;
        }
        return true;
    }

    if (params.contains(QStringLiteral("speed"))) {
        const QJsonValue v = params.value(QStringLiteral("speed"));
        if (!v.isDouble()) {
            if (errorOut != nullptr) {
                *errorOut = QStringLiteral("set_clip_speed params.speed must be numeric");
            }
            return false;
        }
        const double ratio = v.toDouble();
        const double sp = ratio * 100.0;
        if (!std::isfinite(sp) || std::abs(sp) < 1.0 || std::abs(sp) > 10000.0) {
            if (errorOut != nullptr) {
                *errorOut = QStringLiteral("set_clip_speed speed ratio (%1) out of valid range").arg(ratio);
            }
            return false;
        }
        if (speedPercentOut != nullptr) {
            *speedPercentOut = sp;
        }
        return true;
    }

    if (errorOut != nullptr) {
        *errorOut = QStringLiteral("set_clip_speed requires params.speed_percent or params.speed");
    }
    return false;
}

QString synthesizeRotoscopingSplineJson(const QJsonObject &params)
{
    if (params.contains(QStringLiteral("splines_json"))) {
        const QJsonValue sj = params.value(QStringLiteral("splines_json"));
        if (sj.isString()) {
            const QString s = sj.toString().trimmed();
            if (!s.isEmpty()) {
                return s;
            }
        } else if (sj.isObject()) {
            return QString::fromUtf8(QJsonDocument(sj.toObject()).toJson(QJsonDocument::Compact));
        } else if (sj.isArray()) {
            QJsonObject wrapped;
            wrapped.insert(QStringLiteral("0"), sj.toArray());
            return QString::fromUtf8(QJsonDocument(wrapped).toJson(QJsonDocument::Compact));
        }
    }

    const QJsonArray keyframes = params.value(QStringLiteral("keyframes")).toArray();
    if (keyframes.isEmpty()) {
        return {};
    }

    QJsonObject splineObj;
    for (const QJsonValue &kfVal : keyframes) {
        const QJsonObject kf = kfVal.toObject();
        const int frame = qMax(0, kf.value(QStringLiteral("frame")).toInt(0));
        const QJsonArray rawPoints = kf.value(QStringLiteral("points")).toArray();
        QJsonArray bpoints;
        for (const QJsonValue &ptVal : rawPoints) {
            if (ptVal.isArray()) {
                const QJsonArray arr = ptVal.toArray();
                if (arr.size() == 3 && arr.at(0).isArray() && arr.at(1).isArray() && arr.at(2).isArray()) {
                    bpoints.append(arr);
                } else if (arr.size() >= 2 && arr.at(0).isDouble() && arr.at(1).isDouble()) {
                    QJsonArray xy;
                    xy.append(arr.at(0).toDouble());
                    xy.append(arr.at(1).toDouble());
                    QJsonArray bp;
                    bp.append(xy);
                    bp.append(xy);
                    bp.append(xy);
                    bpoints.append(bp);
                }
            } else if (ptVal.isObject()) {
                const QJsonObject po = ptVal.toObject();
                const double x = po.value(QStringLiteral("x")).toDouble(0.5);
                const double y = po.value(QStringLiteral("y")).toDouble(0.5);
                QJsonArray h1;
                h1.append(po.value(QStringLiteral("h1x")).toDouble(x));
                h1.append(po.value(QStringLiteral("h1y")).toDouble(y));
                QJsonArray p;
                p.append(x);
                p.append(y);
                QJsonArray h2;
                h2.append(po.value(QStringLiteral("h2x")).toDouble(x));
                h2.append(po.value(QStringLiteral("h2y")).toDouble(y));
                QJsonArray bp;
                bp.append(h1);
                bp.append(p);
                bp.append(h2);
                bpoints.append(bp);
            }
        }
        if (!bpoints.isEmpty()) {
            splineObj.insert(QString::number(frame), bpoints);
        }
    }
    if (splineObj.isEmpty()) {
        return {};
    }
    return QString::fromUtf8(QJsonDocument(splineObj).toJson(QJsonDocument::Compact));
}

QString buildLowerThirdTitleXml(const QString &speakerName, const QString &subtitle, const QString &templateName, const QString &position, int width,
                                int height, int durationFrames, const QJsonObject &style)
{
    const int safeW = qMax(320, width);
    const int safeH = qMax(180, height);
    const int safeDur = qMax(1, durationFrames);

    const int boxW = qRound(safeW * 0.42);
    const int boxH = subtitle.trimmed().isEmpty() ? qRound(safeH * 0.085) : qRound(safeH * 0.125);
    const int marginX = qRound(safeW * 0.05);
    const int marginY = qRound(safeH * 0.08);

    int posX = marginX;
    int posY = safeH - marginY - boxH;
    const QString normPos = position.trimmed().toLower();
    if (normPos == QStringLiteral("bottom_center")) {
        posX = (safeW - boxW) / 2;
        posY = safeH - marginY - boxH;
    } else if (normPos == QStringLiteral("bottom_right")) {
        posX = safeW - marginX - boxW;
        posY = safeH - marginY - boxH;
    } else if (normPos == QStringLiteral("top_left")) {
        posX = marginX;
        posY = marginY;
    } else if (normPos == QStringLiteral("top_center")) {
        posX = (safeW - boxW) / 2;
        posY = marginY;
    } else if (normPos == QStringLiteral("top_right")) {
        posX = safeW - marginX - boxW;
        posY = marginY;
    }

    const QString accentColor = style.value(QStringLiteral("accent_color")).toString(
        templateName == QStringLiteral("broadcast_bar") ? QStringLiteral("231,76,60,255") : QStringLiteral("61,174,233,255"));
    const QString bgColor = style.value(QStringLiteral("bg_color")).toString(
        templateName == QStringLiteral("minimal") ? QStringLiteral("15,23,42,165") : QStringLiteral("15,23,42,220"));
    const QString textColor = style.value(QStringLiteral("text_color")).toString(QStringLiteral("255,255,255,255"));
    const QString subColor = style.value(QStringLiteral("subtitle_color")).toString(QStringLiteral("203,213,225,255"));
    const QString fontFamily = style.value(QStringLiteral("font")).toString(QStringLiteral("Sans Serif"));

    QString xml;
    QXmlStreamWriter w(&xml);
    w.setAutoFormatting(false);
    w.writeStartElement(QStringLiteral("kdenlivetitle"));
    w.writeAttribute(QStringLiteral("width"), QString::number(safeW));
    w.writeAttribute(QStringLiteral("height"), QString::number(safeH));
    w.writeAttribute(QStringLiteral("duration"), QString::number(safeDur));
    w.writeAttribute(QStringLiteral("out"), QString::number(safeDur - 1));
    w.writeAttribute(QStringLiteral("LC_NUMERIC"), QStringLiteral("C"));

    // Backdrop rectangle
    w.writeStartElement(QStringLiteral("item"));
    w.writeAttribute(QStringLiteral("type"), QStringLiteral("QGraphicsRectItem"));
    w.writeAttribute(QStringLiteral("z-index"), QStringLiteral("0"));
    w.writeStartElement(QStringLiteral("position"));
    w.writeAttribute(QStringLiteral("x"), QString::number(posX));
    w.writeAttribute(QStringLiteral("y"), QString::number(posY));
    w.writeEndElement();
    w.writeStartElement(QStringLiteral("content"));
    w.writeAttribute(QStringLiteral("rect"), QStringLiteral("0,0,%1,%2").arg(boxW).arg(boxH));
    w.writeAttribute(QStringLiteral("brushcolor"), bgColor);
    w.writeAttribute(QStringLiteral("pencolor"), QStringLiteral("0,0,0,0"));
    w.writeAttribute(QStringLiteral("penwidth"), QStringLiteral("0"));
    w.writeEndElement();
    w.writeEndElement();

    // Accent strip
    const int accentW = qMax(6, qRound(safeW * 0.005));
    w.writeStartElement(QStringLiteral("item"));
    w.writeAttribute(QStringLiteral("type"), QStringLiteral("QGraphicsRectItem"));
    w.writeAttribute(QStringLiteral("z-index"), QStringLiteral("1"));
    w.writeStartElement(QStringLiteral("position"));
    w.writeAttribute(QStringLiteral("x"), QString::number(posX));
    w.writeAttribute(QStringLiteral("y"), QString::number(posY));
    w.writeEndElement();
    w.writeStartElement(QStringLiteral("content"));
    w.writeAttribute(QStringLiteral("rect"), QStringLiteral("0,0,%1,%2").arg(accentW).arg(boxH));
    w.writeAttribute(QStringLiteral("brushcolor"), accentColor);
    w.writeAttribute(QStringLiteral("pencolor"), QStringLiteral("0,0,0,0"));
    w.writeAttribute(QStringLiteral("penwidth"), QStringLiteral("0"));
    w.writeEndElement();
    w.writeEndElement();

    // Speaker name text item
    const int padX = accentW + qRound(safeW * 0.015);
    const int nameSize = qMax(18, qRound(safeH * 0.038));
    w.writeStartElement(QStringLiteral("item"));
    w.writeAttribute(QStringLiteral("type"), QStringLiteral("QGraphicsTextItem"));
    w.writeAttribute(QStringLiteral("z-index"), QStringLiteral("2"));
    w.writeStartElement(QStringLiteral("position"));
    w.writeAttribute(QStringLiteral("x"), QString::number(posX + padX));
    w.writeAttribute(QStringLiteral("y"), QString::number(posY + qRound(boxH * 0.14)));
    w.writeEndElement();
    w.writeStartElement(QStringLiteral("content"));
    w.writeAttribute(QStringLiteral("font"), fontFamily);
    w.writeAttribute(QStringLiteral("font-pixel-size"), QString::number(nameSize));
    w.writeAttribute(QStringLiteral("font-weight"), QStringLiteral("75"));
    w.writeAttribute(QStringLiteral("font-color"), textColor);
    w.writeCharacters(speakerName);
    w.writeEndElement();
    w.writeEndElement();

    // Optional subtitle text item
    if (!subtitle.trimmed().isEmpty()) {
        const int subSize = qMax(14, qRound(safeH * 0.025));
        w.writeStartElement(QStringLiteral("item"));
        w.writeAttribute(QStringLiteral("type"), QStringLiteral("QGraphicsTextItem"));
        w.writeAttribute(QStringLiteral("z-index"), QStringLiteral("3"));
        w.writeStartElement(QStringLiteral("position"));
        w.writeAttribute(QStringLiteral("x"), QString::number(posX + padX));
        w.writeAttribute(QStringLiteral("y"), QString::number(posY + qRound(boxH * 0.56)));
        w.writeEndElement();
        w.writeStartElement(QStringLiteral("content"));
        w.writeAttribute(QStringLiteral("font"), fontFamily);
        w.writeAttribute(QStringLiteral("font-pixel-size"), QString::number(subSize));
        w.writeAttribute(QStringLiteral("font-weight"), QStringLiteral("50"));
        w.writeAttribute(QStringLiteral("font-color"), subColor);
        w.writeCharacters(subtitle);
        w.writeEndElement();
        w.writeEndElement();
    }

    w.writeEndElement(); // </kdenlivetitle>
    return xml;
}

} // namespace

// ============================================================================
// InMemoryTimelineBackend
// ============================================================================

InMemoryTimelineBackend::InMemoryTimelineBackend() = default;

QString InMemoryTimelineBackend::backendName() const
{
    return QStringLiteral("in_memory");
}

bool InMemoryTimelineBackend::beginTransaction(const QString &label, QString *errorOut)
{
    Q_UNUSED(label);
    if (m_inTransaction) {
        if (errorOut != nullptr) {
            *errorOut = QStringLiteral("A transaction is already active on InMemoryTimelineBackend");
        }
        return false;
    }
    m_transactionBackup = m_state;
    m_inTransaction = true;
    return true;
}

bool InMemoryTimelineBackend::commitTransaction(const QString &label, QString *errorOut)
{
    Q_UNUSED(label);
    if (!m_inTransaction) {
        if (errorOut != nullptr) {
            *errorOut = QStringLiteral("No active transaction to commit");
        }
        return false;
    }
    m_inTransaction = false;
    m_transactionBackup = State{};
    return true;
}

bool InMemoryTimelineBackend::rollbackTransaction(QString *errorOut)
{
    if (!m_inTransaction) {
        if (errorOut != nullptr) {
            *errorOut = QStringLiteral("No active transaction to roll back");
        }
        return false;
    }
    m_state = m_transactionBackup;
    m_transactionBackup = State{};
    m_inTransaction = false;
    return true;
}

void InMemoryTimelineBackend::ensureSequenceExists(const QString &sequenceId)
{
    if (sequenceId.isEmpty()) {
        return;
    }
    if (!m_state.sequences.contains(sequenceId)) {
        SequenceRecord seq;
        seq.sequenceId = sequenceId;
        seq.name = sequenceId;
        m_state.sequences.insert(sequenceId, seq);
    }
    if (m_state.activeSequenceId.isEmpty()) {
        m_state.activeSequenceId = sequenceId;
    }
}

TimelineOperationResult InMemoryTimelineBackend::executeOperation(const TimelineOperation &op, const QString &sequenceId)
{
    if (!m_inTransaction) {
        TimelineOperationResult res;
        res.opId = op.opId;
        res.applied = false;
        res.message = QStringLiteral("Cannot execute operation outside an active transaction");
        return res;
    }

    if (op.type == QStringLiteral("import_media")) {
        return handleImportMedia(op);
    }
    if (op.type == QStringLiteral("sync_clips")) {
        return handleSyncClips(op, sequenceId);
    }
    if (op.type == QStringLiteral("create_sequence")) {
        return handleCreateSequence(op, sequenceId);
    }
    if (op.type == QStringLiteral("create_track")) {
        return handleCreateTrack(op, sequenceId);
    }
    if (op.type == QStringLiteral("place_clip")) {
        return handlePlaceClip(op, sequenceId);
    }
    if (op.type == QStringLiteral("move_clip")) {
        return handleMoveClip(op);
    }
    if (op.type == QStringLiteral("trim_clip")) {
        return handleTrimClip(op);
    }
    if (op.type == QStringLiteral("split_clip")) {
        return handleSplitClip(op);
    }
    if (op.type == QStringLiteral("delete_clip")) {
        return handleDeleteClip(op);
    }
    if (op.type == QStringLiteral("select_camera")) {
        return handleSelectCamera(op, sequenceId);
    }
    if (op.type == QStringLiteral("add_effect")) {
        return handleAddEffect(op, sequenceId, false);
    }
    if (op.type == QStringLiteral("set_effect_param")) {
        return handleSetEffectParam(op, sequenceId);
    }
    if (op.type == QStringLiteral("set_keyframe")) {
        return handleSetKeyframe(op, sequenceId);
    }
    if (op.type == QStringLiteral("set_audio_level")) {
        return handleSetAudioLevel(op, sequenceId);
    }
    if (op.type == QStringLiteral("add_audio_filter")) {
        return handleAddEffect(op, sequenceId, true);
    }
    if (op.type == QStringLiteral("add_caption")) {
        return handleAddCaption(op, sequenceId);
    }
    if (op.type == QStringLiteral("update_caption")) {
        return handleUpdateCaption(op, sequenceId);
    }
    if (op.type == QStringLiteral("delete_caption")) {
        return handleDeleteCaption(op);
    }
    if (op.type == QStringLiteral("add_marker")) {
        return handleAddMarker(op, sequenceId);
    }
    if (op.type == QStringLiteral("apply_mask")) {
        return handleApplyMask(op, sequenceId);
    }
    if (op.type == QStringLiteral("add_transition")) {
        return handleAddTransition(op, sequenceId);
    }
    if (op.type == QStringLiteral("add_lower_third")) {
        return handleAddLowerThird(op, sequenceId);
    }
    if (op.type == QStringLiteral("set_clip_speed")) {
        return handleSetClipSpeed(op);
    }
    if (op.type == QStringLiteral("export_sequence")) {
        return handleExportSequence(op, sequenceId);
    }

    TimelineOperationResult res;
    res.opId = op.opId;
    res.applied = false;
    res.message = QStringLiteral("Unsupported operation type '%1' in InMemoryTimelineBackend").arg(op.type);
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleImportMedia(const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    const QString sourcePath = firstNonEmptyStringParam(op.params, {QStringLiteral("path"), QStringLiteral("uri"), QStringLiteral("source_path")});
    if (sourcePath.isEmpty() && op.mediaRef.trimmed().isEmpty()) {
        res.message = QStringLiteral("import_media requires media_ref or params.path/uri");
        return res;
    }

    QString mediaRef = op.mediaRef.trimmed();
    if (mediaRef.isEmpty()) {
        mediaRef = QStringLiteral("media-%1").arg(m_state.nextMediaId++);
    }
    const bool allowReplace = op.params.value(QStringLiteral("replace")).toBool(false);
    if (m_state.mediaBin.contains(mediaRef) && !allowReplace) {
        res.message = QStringLiteral("Media ref '%1' already exists in bin").arg(mediaRef);
        return res;
    }

    MediaAsset asset;
    asset.mediaRef = mediaRef;
    asset.sourcePath = sourcePath.isEmpty() ? mediaRef : sourcePath;
    asset.name = op.params.value(QStringLiteral("name")).toString(mediaRef);
    asset.mediaType = op.params.value(QStringLiteral("media_type")).toString(QStringLiteral("av"));
    asset.durationFrames = op.params.value(QStringLiteral("duration_frames"))
                               .toInt(op.frameRange.outFrame > op.frameRange.inFrame ? (op.frameRange.outFrame - op.frameRange.inFrame) : 0);
    if (asset.durationFrames < 0) {
        res.message = QStringLiteral("import_media duration_frames cannot be negative");
        return res;
    }
    asset.fpsNum = op.frameRange.fpsNum > 0 ? op.frameRange.fpsNum : 25;
    asset.fpsDen = op.frameRange.fpsDen > 0 ? op.frameRange.fpsDen : 1;
    asset.metadata = op.params.value(QStringLiteral("metadata")).toObject();

    m_state.mediaBin.insert(mediaRef, asset);

    res.applied = true;
    res.createdId = mediaRef;
    res.message = QStringLiteral("Imported media '%1'").arg(mediaRef);
    res.metadata.insert(QStringLiteral("source_path"), asset.sourcePath);
    res.metadata.insert(QStringLiteral("duration_frames"), asset.durationFrames);
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleSyncClips(const TimelineOperation &op, const QString &sequenceId)
{
    TimelineOperationResult res;
    res.opId = op.opId;
    ensureSequenceExists(sequenceId);

    const QJsonArray clipIdsJson = op.params.value(QStringLiteral("clip_ids")).toArray();
    if (clipIdsJson.size() < 2) {
        res.message = QStringLiteral("sync_clips requires params.clip_ids with at least 2 clips");
        return res;
    }

    QStringList clipIds;
    clipIds.reserve(clipIdsJson.size());
    for (const QJsonValue &val : clipIdsJson) {
        const QString cid = val.toString().trimmed();
        if (cid.isEmpty() || !m_state.clips.contains(cid)) {
            res.message = QStringLiteral("sync_clips references unknown clip '%1'").arg(cid);
            return res;
        }
        clipIds.append(cid);
    }

    const QString refClipId = op.params.value(QStringLiteral("reference_clip_id")).toString(clipIds.first());
    if (!m_state.clips.contains(refClipId)) {
        res.message = QStringLiteral("sync_clips reference_clip_id '%1' does not exist").arg(refClipId);
        return res;
    }

    const int refStart = m_state.clips.value(refClipId).frameRange.timelineStart;
    const QJsonObject offsetsObj = op.params.value(QStringLiteral("offsets")).toObject();

    SyncGroup group;
    group.syncGroupId = QStringLiteral("sync-%1").arg(m_state.nextSyncGroupId++);
    group.sequenceId = sequenceId;
    group.method = op.params.value(QStringLiteral("sync_method")).toString(QStringLiteral("audio_waveform"));
    group.referenceClipId = refClipId;

    for (const QString &cid : clipIds) {
        const int offset = offsetsObj.value(cid).toInt(0);
        const int newStart = refStart + offset;
        if (newStart < 0) {
            res.message = QStringLiteral("sync_clips computed negative timeline_start (%1) for clip '%2'").arg(newStart).arg(cid);
            return res;
        }
        m_state.clips[cid].frameRange.timelineStart = newStart;
        group.clipOffsets.insert(cid, offset);
    }

    m_state.syncGroups.insert(group.syncGroupId, group);

    res.applied = true;
    res.createdId = group.syncGroupId;
    res.message = QStringLiteral("Synchronized %1 clips via %2").arg(clipIds.size()).arg(group.method);
    res.metadata.insert(QStringLiteral("reference_clip_id"), refClipId);
    res.metadata.insert(QStringLiteral("synced_count"), clipIds.size());
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleCreateSequence(const TimelineOperation &op, const QString &sequenceId)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    QString targetSeqId = op.params.value(QStringLiteral("sequence_id")).toString().trimmed();
    if (targetSeqId.isEmpty()) {
        targetSeqId = !sequenceId.isEmpty() && !m_state.sequences.contains(sequenceId) ? sequenceId : QStringLiteral("seq-%1").arg(m_state.nextSequenceId++);
    }
    const bool allowExisting = op.params.value(QStringLiteral("allow_existing")).toBool(false);
    if (m_state.sequences.contains(targetSeqId) && !allowExisting) {
        res.message = QStringLiteral("Sequence '%1' already exists").arg(targetSeqId);
        return res;
    }

    SequenceRecord seq;
    seq.sequenceId = targetSeqId;
    seq.name = op.params.value(QStringLiteral("name")).toString(targetSeqId);
    seq.fpsNum = op.params.value(QStringLiteral("fps_num")).toInt(op.frameRange.fpsNum > 0 ? op.frameRange.fpsNum : 25);
    seq.fpsDen = op.params.value(QStringLiteral("fps_den")).toInt(op.frameRange.fpsDen > 0 ? op.frameRange.fpsDen : 1);
    seq.width = op.params.value(QStringLiteral("width")).toInt(1920);
    seq.height = op.params.value(QStringLiteral("height")).toInt(1080);

    if (seq.fpsNum < 1 || seq.fpsDen < 1 || seq.width < 1 || seq.height < 1) {
        res.message = QStringLiteral("create_sequence requires positive fps and frame dimensions");
        return res;
    }

    m_state.sequences.insert(targetSeqId, seq);
    if (op.params.value(QStringLiteral("set_active")).toBool(true) || m_state.activeSequenceId.isEmpty()) {
        m_state.activeSequenceId = targetSeqId;
    }

    res.applied = true;
    res.createdId = targetSeqId;
    res.message = QStringLiteral("Created sequence '%1'").arg(targetSeqId);
    res.metadata.insert(QStringLiteral("width"), seq.width);
    res.metadata.insert(QStringLiteral("height"), seq.height);
    res.metadata.insert(QStringLiteral("fps_num"), seq.fpsNum);
    res.metadata.insert(QStringLiteral("fps_den"), seq.fpsDen);
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleCreateTrack(const TimelineOperation &op, const QString &sequenceId)
{
    TimelineOperationResult res;
    res.opId = op.opId;
    ensureSequenceExists(sequenceId);

    const QString kind = firstNonEmptyStringParam(op.params, {QStringLiteral("kind"), QStringLiteral("track_type")});
    const QString resolvedKind = kind.isEmpty() ? QStringLiteral("video") : kind.toLower();
    if (resolvedKind != QStringLiteral("video") && resolvedKind != QStringLiteral("audio") && resolvedKind != QStringLiteral("subtitle")) {
        res.message = QStringLiteral("Invalid track kind '%1' (expected video, audio, or subtitle)").arg(resolvedKind);
        return res;
    }

    QString trackId = op.trackId.trimmed();
    if (trackId.isEmpty()) {
        trackId = QStringLiteral("track-%1").arg(m_state.nextTrackId++);
    }
    if (m_state.tracks.contains(trackId)) {
        res.message = QStringLiteral("Track '%1' already exists").arg(trackId);
        return res;
    }

    TimelineTrack track;
    track.trackId = trackId;
    track.sequenceId = sequenceId;
    track.kind = resolvedKind;
    track.name = op.params.value(QStringLiteral("name")).toString(trackId);
    track.index = op.params.value(QStringLiteral("index")).toInt(m_state.tracks.size());
    track.locked = op.params.value(QStringLiteral("locked")).toBool(false);
    track.muted = op.params.value(QStringLiteral("muted")).toBool(false);
    track.cameraId = op.params.value(QStringLiteral("camera_id")).toString();

    m_state.tracks.insert(trackId, track);

    res.applied = true;
    res.createdId = trackId;
    res.message = QStringLiteral("Created %1 track '%2'").arg(resolvedKind, trackId);
    res.metadata.insert(QStringLiteral("kind"), resolvedKind);
    res.metadata.insert(QStringLiteral("name"), track.name);
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handlePlaceClip(const TimelineOperation &op, const QString &sequenceId)
{
    TimelineOperationResult res;
    res.opId = op.opId;
    ensureSequenceExists(sequenceId);

    const QString trackId = op.trackId.trimmed();
    if (trackId.isEmpty() || !m_state.tracks.contains(trackId)) {
        res.message = QStringLiteral("Target track '%1' does not exist").arg(trackId);
        return res;
    }
    const TimelineTrack &track = m_state.tracks.value(trackId);
    if (track.locked) {
        res.message = QStringLiteral("Target track '%1' is locked").arg(trackId);
        return res;
    }

    const QString mediaRef = op.mediaRef.trimmed();
    const bool hasGenerator = op.params.contains(QStringLiteral("generator"));
    if (!hasGenerator && (mediaRef.isEmpty() || !m_state.mediaBin.contains(mediaRef))) {
        res.message = QStringLiteral("Media ref '%1' does not exist in bin").arg(mediaRef);
        return res;
    }

    QString clipId = op.clipId.trimmed();
    if (clipId.isEmpty()) {
        clipId = QStringLiteral("clip-%1").arg(m_state.nextClipId++);
    }
    if (m_state.clips.contains(clipId)) {
        res.message = QStringLiteral("Clip '%1' already exists").arg(clipId);
        return res;
    }

    FrameRange fr = op.frameRange;
    if (fr.outFrame == fr.inFrame) {
        const int explicitDuration = op.params.value(QStringLiteral("duration_frames")).toInt(0);
        if (explicitDuration > 0) {
            fr.outFrame = fr.inFrame + explicitDuration;
        } else if (m_state.mediaBin.contains(mediaRef) && m_state.mediaBin.value(mediaRef).durationFrames > 0) {
            fr.outFrame = fr.inFrame + m_state.mediaBin.value(mediaRef).durationFrames;
        } else {
            res.message = QStringLiteral("place_clip requires positive duration (out_frame > in_frame or duration_frames > 0)");
            return res;
        }
    }
    if (fr.outFrame <= fr.inFrame) {
        res.message = QStringLiteral("place_clip frame_range must have out_frame > in_frame");
        return res;
    }

    if (m_state.mediaBin.contains(mediaRef)) {
        const MediaAsset &asset = m_state.mediaBin.value(mediaRef);
        if (asset.durationFrames > 0 && fr.outFrame > asset.durationFrames && !op.params.value(QStringLiteral("allow_extend")).toBool(false)) {
            res.message = QStringLiteral("Requested out_frame (%1) exceeds media '%2' duration (%3)").arg(fr.outFrame).arg(mediaRef).arg(asset.durationFrames);
            return res;
        }
    }

    const int newStart = fr.timelineStart;
    const int newEnd = newStart + (fr.outFrame - fr.inFrame);
    const bool allowOverlap = op.params.value(QStringLiteral("allow_overlap")).toBool(false);
    if (!allowOverlap) {
        for (const TimelineClip &existing : m_state.clips) {
            if (existing.trackId != trackId) {
                continue;
            }
            const int exStart = existing.frameRange.timelineStart;
            const int exEnd = exStart + existing.duration();
            if (newStart < exEnd && newEnd > exStart) {
                res.message = QStringLiteral("Clip overlaps existing clip '%1' on track '%2'").arg(existing.clipId, trackId);
                return res;
            }
        }
    }

    TimelineClip clip;
    clip.clipId = clipId;
    clip.sequenceId = sequenceId;
    clip.trackId = trackId;
    clip.mediaRef = mediaRef;
    clip.name = op.params.value(QStringLiteral("name")).toString(mediaRef.isEmpty() ? clipId : mediaRef);
    clip.frameRange = fr;
    clip.cameraId = op.params.value(QStringLiteral("camera_id")).toString(track.cameraId);
    clip.audioGainDb = op.params.value(QStringLiteral("audio_gain_db")).toDouble(0.0);
    clip.metadata = op.params.value(QStringLiteral("metadata")).toObject();

    m_state.clips.insert(clipId, clip);

    res.applied = true;
    res.createdId = clipId;
    res.message = QStringLiteral("Placed clip '%1' on track '%2' at frame %3").arg(clipId, trackId).arg(newStart);
    res.metadata.insert(QStringLiteral("track_id"), trackId);
    res.metadata.insert(QStringLiteral("timeline_start"), newStart);
    res.metadata.insert(QStringLiteral("duration_frames"), clip.duration());
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleMoveClip(const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    const QString clipId = op.clipId.trimmed();
    if (!m_state.clips.contains(clipId)) {
        res.message = QStringLiteral("Clip '%1' does not exist").arg(clipId);
        return res;
    }

    TimelineClip &clip = m_state.clips[clipId];
    if (m_state.tracks.value(clip.trackId).locked) {
        res.message = QStringLiteral("Source track '%1' is locked").arg(clip.trackId);
        return res;
    }

    const QString targetTrackId = op.trackId.trimmed().isEmpty() ? clip.trackId : op.trackId.trimmed();
    if (!m_state.tracks.contains(targetTrackId)) {
        res.message = QStringLiteral("Target track '%1' does not exist").arg(targetTrackId);
        return res;
    }
    if (m_state.tracks.value(targetTrackId).locked) {
        res.message = QStringLiteral("Target track '%1' is locked").arg(targetTrackId);
        return res;
    }

    int targetStart = op.frameRange.timelineStart;
    if (op.params.contains(QStringLiteral("timeline_start"))) {
        if (!jsonValueIsStrictInt(op.params.value(QStringLiteral("timeline_start")), &targetStart) || targetStart < 0) {
            res.message = QStringLiteral("move_clip params.timeline_start must be a non-negative integer");
            return res;
        }
    }

    const int targetEnd = targetStart + clip.duration();
    const bool allowOverlap = op.params.value(QStringLiteral("allow_overlap")).toBool(false);
    if (!allowOverlap) {
        for (const TimelineClip &existing : m_state.clips) {
            if (existing.clipId == clipId || existing.trackId != targetTrackId) {
                continue;
            }
            const int exStart = existing.frameRange.timelineStart;
            const int exEnd = exStart + existing.duration();
            if (targetStart < exEnd && targetEnd > exStart) {
                res.message = QStringLiteral("Move destination overlaps clip '%1' on track '%2'").arg(existing.clipId, targetTrackId);
                return res;
            }
        }
    }

    clip.trackId = targetTrackId;
    clip.frameRange.timelineStart = targetStart;

    res.applied = true;
    res.createdId = clipId;
    res.message = QStringLiteral("Moved clip '%1' to track '%2' frame %3").arg(clipId, targetTrackId).arg(targetStart);
    res.metadata.insert(QStringLiteral("track_id"), targetTrackId);
    res.metadata.insert(QStringLiteral("timeline_start"), targetStart);
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleTrimClip(const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    const QString clipId = op.clipId.trimmed();
    if (!m_state.clips.contains(clipId)) {
        res.message = QStringLiteral("Clip '%1' does not exist").arg(clipId);
        return res;
    }

    TimelineClip &clip = m_state.clips[clipId];
    if (m_state.tracks.value(clip.trackId).locked) {
        res.message = QStringLiteral("Track '%1' is locked").arg(clip.trackId);
        return res;
    }

    int newIn = op.frameRange.inFrame;
    int newOut = op.frameRange.outFrame;
    if (newIn == 0 && newOut == 0) {
        newIn = op.params.value(QStringLiteral("in_frame")).toInt(clip.frameRange.inFrame);
        newOut = op.params.value(QStringLiteral("out_frame")).toInt(clip.frameRange.outFrame);
        if (op.params.contains(QStringLiteral("duration_frames"))) {
            newOut = newIn + op.params.value(QStringLiteral("duration_frames")).toInt(0);
        }
    }

    if (newIn < 0 || newOut <= newIn) {
        res.message = QStringLiteral("trim_clip requires 0 <= in_frame < out_frame (got in=%1, out=%2)").arg(newIn).arg(newOut);
        return res;
    }

    if (m_state.mediaBin.contains(clip.mediaRef)) {
        const MediaAsset &asset = m_state.mediaBin.value(clip.mediaRef);
        if (asset.durationFrames > 0 && newOut > asset.durationFrames && !op.params.value(QStringLiteral("allow_extend")).toBool(false)) {
            res.message = QStringLiteral("Trim out_frame (%1) exceeds media '%2' duration (%3)").arg(newOut).arg(clip.mediaRef).arg(asset.durationFrames);
            return res;
        }
    }

    int newStart = clip.frameRange.timelineStart;
    if (op.params.contains(QStringLiteral("timeline_start"))) {
        if (!jsonValueIsStrictInt(op.params.value(QStringLiteral("timeline_start")), &newStart) || newStart < 0) {
            res.message = QStringLiteral("trim_clip params.timeline_start must be a non-negative integer");
            return res;
        }
    } else if (op.frameRange.timelineStart > 0) {
        newStart = op.frameRange.timelineStart;
    }

    clip.frameRange.inFrame = newIn;
    clip.frameRange.outFrame = newOut;
    clip.frameRange.timelineStart = newStart;

    res.applied = true;
    res.createdId = clipId;
    res.message = QStringLiteral("Trimmed clip '%1' to [%2, %3)").arg(clipId).arg(newIn).arg(newOut);
    res.metadata.insert(QStringLiteral("in_frame"), newIn);
    res.metadata.insert(QStringLiteral("out_frame"), newOut);
    res.metadata.insert(QStringLiteral("duration_frames"), clip.duration());
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleSplitClip(const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    const QString clipId = op.clipId.trimmed();
    if (!m_state.clips.contains(clipId)) {
        res.message = QStringLiteral("Clip '%1' does not exist").arg(clipId);
        return res;
    }

    TimelineClip &leftClip = m_state.clips[clipId];
    if (m_state.tracks.value(leftClip.trackId).locked) {
        res.message = QStringLiteral("Track '%1' is locked").arg(leftClip.trackId);
        return res;
    }

    int splitFrame = -1;
    if (op.params.contains(QStringLiteral("split_frame"))) {
        if (!jsonValueIsStrictInt(op.params.value(QStringLiteral("split_frame")), &splitFrame)) {
            res.message = QStringLiteral("split_clip params.split_frame must be an integer");
            return res;
        }
    } else if (op.params.contains(QStringLiteral("position"))) {
        if (!jsonValueIsStrictInt(op.params.value(QStringLiteral("position")), &splitFrame)) {
            res.message = QStringLiteral("split_clip params.position must be an integer");
            return res;
        }
    } else if (op.params.contains(QStringLiteral("timeline_frame"))) {
        if (!jsonValueIsStrictInt(op.params.value(QStringLiteral("timeline_frame")), &splitFrame)) {
            res.message = QStringLiteral("split_clip params.timeline_frame must be an integer");
            return res;
        }
    } else if (op.params.contains(QStringLiteral("offset_frames"))) {
        int offset = 0;
        if (!jsonValueIsStrictInt(op.params.value(QStringLiteral("offset_frames")), &offset)) {
            res.message = QStringLiteral("split_clip params.offset_frames must be an integer");
            return res;
        }
        splitFrame = leftClip.frameRange.timelineStart + offset;
    } else if (op.frameRange.timelineStart > 0) {
        splitFrame = op.frameRange.timelineStart;
    }

    const int clipStart = leftClip.frameRange.timelineStart;
    const int clipEnd = clipStart + leftClip.duration();
    if (splitFrame <= clipStart || splitFrame >= clipEnd) {
        res.message = QStringLiteral("Split frame %1 is outside clip '%2' interior range (%3, %4)").arg(splitFrame).arg(clipId).arg(clipStart).arg(clipEnd);
        return res;
    }

    QString rightClipId = op.params.value(QStringLiteral("new_clip_id")).toString().trimmed();
    if (rightClipId.isEmpty()) {
        rightClipId = QStringLiteral("clip-%1").arg(m_state.nextClipId++);
    }
    if (m_state.clips.contains(rightClipId)) {
        res.message = QStringLiteral("Right split clip ID '%1' already exists").arg(rightClipId);
        return res;
    }

    const int leftDuration = splitFrame - clipStart;
    const int oldOut = leftClip.frameRange.outFrame;
    const int splitSourceFrame = leftClip.frameRange.inFrame + leftDuration;

    TimelineClip rightClip = leftClip;
    rightClip.clipId = rightClipId;
    rightClip.frameRange.inFrame = splitSourceFrame;
    rightClip.frameRange.outFrame = oldOut;
    rightClip.frameRange.timelineStart = splitFrame;
    for (EffectInstance &fx : rightClip.effects) {
        fx.instanceId = QStringLiteral("fx-%1").arg(m_state.nextEffectId++);
    }

    leftClip.frameRange.outFrame = splitSourceFrame;

    m_state.clips.insert(rightClipId, rightClip);

    res.applied = true;
    res.createdId = rightClipId;
    res.message = QStringLiteral("Split clip '%1' at frame %2 into '%1' and '%3'").arg(clipId).arg(splitFrame).arg(rightClipId);
    res.metadata.insert(QStringLiteral("left_clip_id"), clipId);
    res.metadata.insert(QStringLiteral("right_clip_id"), rightClipId);
    res.metadata.insert(QStringLiteral("split_frame"), splitFrame);
    res.metadata.insert(QStringLiteral("left_duration"), leftDuration);
    res.metadata.insert(QStringLiteral("right_duration"), rightClip.duration());
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleDeleteClip(const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    const QString clipId = op.clipId.trimmed();
    if (!m_state.clips.contains(clipId)) {
        res.message = QStringLiteral("Clip '%1' does not exist").arg(clipId);
        return res;
    }
    const QString trackId = m_state.clips.value(clipId).trackId;
    if (m_state.tracks.value(trackId).locked) {
        res.message = QStringLiteral("Track '%1' is locked").arg(trackId);
        return res;
    }

    m_state.clips.remove(clipId);

    res.applied = true;
    res.message = QStringLiteral("Deleted clip '%1'").arg(clipId);
    res.metadata.insert(QStringLiteral("deleted_clip_id"), clipId);
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleSelectCamera(const TimelineOperation &op, const QString &sequenceId)
{
    TimelineOperationResult res;
    res.opId = op.opId;
    ensureSequenceExists(sequenceId);

    QString cameraId = firstNonEmptyStringParam(op.params, {QStringLiteral("camera_id"), QStringLiteral("angle")});
    if (cameraId.isEmpty() && op.params.contains(QStringLiteral("camera_index"))) {
        int camIdx = -1;
        if (!jsonValueIsStrictInt(op.params.value(QStringLiteral("camera_index")), &camIdx) || camIdx < 0) {
            res.message = QStringLiteral("select_camera params.camera_index must be a non-negative integer");
            return res;
        }
        cameraId = QStringLiteral("cam-%1").arg(camIdx);
    }
    if (cameraId.isEmpty()) {
        res.message = QStringLiteral("select_camera requires params.camera_id, params.camera_index, or params.angle");
        return res;
    }

    const QString clipId = op.clipId.trimmed();
    if (!clipId.isEmpty()) {
        if (!m_state.clips.contains(clipId)) {
            res.message = QStringLiteral("Clip '%1' does not exist").arg(clipId);
            return res;
        }
        m_state.clips[clipId].cameraId = cameraId;
    }

    const QString trackId = op.trackId.trimmed();
    if (!trackId.isEmpty()) {
        if (!m_state.tracks.contains(trackId)) {
            res.message = QStringLiteral("Track '%1' does not exist").arg(trackId);
            return res;
        }
        m_state.tracks[trackId].cameraId = cameraId;
    }

    int timelineFrame = op.frameRange.timelineStart;
    if (op.params.contains(QStringLiteral("frame"))) {
        if (!jsonValueIsStrictInt(op.params.value(QStringLiteral("frame")), &timelineFrame) || timelineFrame < 0) {
            res.message = QStringLiteral("select_camera params.frame must be a non-negative integer");
            return res;
        }
    }

    CameraSelection sel;
    sel.selectionId = QStringLiteral("camsel-%1").arg(m_state.nextCameraSelectionId++);
    sel.sequenceId = sequenceId;
    sel.trackId = trackId;
    sel.clipId = clipId;
    sel.cameraId = cameraId;
    sel.timelineFrame = timelineFrame;
    sel.metadata = op.params;

    m_state.cameraSelections.insert(sel.selectionId, sel);

    res.applied = true;
    res.createdId = sel.selectionId;
    res.message = QStringLiteral("Selected camera '%1' at frame %2").arg(cameraId).arg(timelineFrame);
    res.metadata.insert(QStringLiteral("camera_id"), cameraId);
    res.metadata.insert(QStringLiteral("timeline_frame"), timelineFrame);
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleAddEffect(const TimelineOperation &op, const QString &sequenceId, bool asAudioFilter)
{
    TimelineOperationResult res;
    res.opId = op.opId;
    ensureSequenceExists(sequenceId);

    const QStringList idKeys = asAudioFilter
                                   ? QStringList{QStringLiteral("filter_id"), QStringLiteral("filter"), QStringLiteral("effect_service"),
                                                 QStringLiteral("mlt_service"), QStringLiteral("service"), QStringLiteral("effect_id"), QStringLiteral("effect")}
                                   : QStringList{QStringLiteral("effect_id"), QStringLiteral("effect_service"), QStringLiteral("mlt_service"),
                                                 QStringLiteral("service"), QStringLiteral("effect"), QStringLiteral("filter_id"), QStringLiteral("filter")};
    const QString effectId = firstNonEmptyStringParam(op.params, idKeys);
    if (effectId.isEmpty()) {
        res.message = asAudioFilter ? QStringLiteral("add_audio_filter requires params.filter_id/filter/effect_service/mlt_service")
                                    : QStringLiteral("add_effect requires params.effect_id/effect/effect_service/mlt_service");
        return res;
    }

    EffectInstance fx;
    fx.instanceId = op.params.value(QStringLiteral("instance_id")).toString().trimmed();
    if (fx.instanceId.isEmpty()) {
        fx.instanceId = QStringLiteral("%1-%2").arg(asAudioFilter ? QStringLiteral("afx") : QStringLiteral("fx")).arg(m_state.nextEffectId++);
    }
    fx.effectId = effectId;
    fx.isAudioFilter = asAudioFilter;
    fx.enabled = op.params.value(QStringLiteral("enabled")).toBool(true);
    fx.parameters = op.params.value(QStringLiteral("parameters")).toObject();

    const QString clipId = op.clipId.trimmed();
    const QString trackId = op.trackId.trimmed();

    if (!clipId.isEmpty()) {
        if (!m_state.clips.contains(clipId)) {
            res.message = QStringLiteral("Clip '%1' does not exist").arg(clipId);
            return res;
        }
        m_state.clips[clipId].effects.append(fx);
    } else if (!trackId.isEmpty()) {
        if (!m_state.tracks.contains(trackId)) {
            res.message = QStringLiteral("Track '%1' does not exist").arg(trackId);
            return res;
        }
        m_state.tracks[trackId].effects.append(fx);
    } else {
        m_state.sequences[sequenceId].masterEffects.append(fx);
    }

    res.applied = true;
    res.createdId = fx.instanceId;
    res.message = QStringLiteral("Added %1 '%2' (%3)").arg(asAudioFilter ? QStringLiteral("audio filter") : QStringLiteral("effect"), effectId, fx.instanceId);
    res.metadata.insert(QStringLiteral("effect_id"), effectId);
    res.metadata.insert(QStringLiteral("instance_id"), fx.instanceId);
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleSetEffectParam(const TimelineOperation &op, const QString &sequenceId)
{
    TimelineOperationResult res;
    res.opId = op.opId;
    ensureSequenceExists(sequenceId);

    QVector<EffectInstance> *targetEffects = nullptr;
    const QString clipId = op.clipId.trimmed();
    const QString trackId = op.trackId.trimmed();
    if (!clipId.isEmpty()) {
        if (!m_state.clips.contains(clipId)) {
            res.message = QStringLiteral("Clip '%1' does not exist").arg(clipId);
            return res;
        }
        targetEffects = &m_state.clips[clipId].effects;
    } else if (!trackId.isEmpty()) {
        if (!m_state.tracks.contains(trackId)) {
            res.message = QStringLiteral("Track '%1' does not exist").arg(trackId);
            return res;
        }
        targetEffects = &m_state.tracks[trackId].effects;
    } else {
        targetEffects = &m_state.sequences[sequenceId].masterEffects;
    }

    const QString instanceId = op.params.value(QStringLiteral("effect_instance_id")).toString().trimmed();
    const QString effectId = firstNonEmptyStringParam(op.params, {QStringLiteral("effect_id"), QStringLiteral("filter_id")});
    if (instanceId.isEmpty() && effectId.isEmpty()) {
        res.message = QStringLiteral("set_effect_param requires params.effect_instance_id or params.effect_id");
        return res;
    }

    EffectInstance *matched = nullptr;
    for (EffectInstance &fx : *targetEffects) {
        if ((!instanceId.isEmpty() && fx.instanceId == instanceId) || (instanceId.isEmpty() && fx.effectId == effectId)) {
            matched = &fx;
            break;
        }
    }
    if (matched == nullptr) {
        res.message = QStringLiteral("Target effect '%1' not found").arg(instanceId.isEmpty() ? effectId : instanceId);
        return res;
    }

    const QString paramName = firstNonEmptyStringParam(op.params, {QStringLiteral("param_name"), QStringLiteral("parameter")});
    const QJsonObject batchParams = op.params.value(QStringLiteral("parameters")).toObject();
    if (paramName.isEmpty() && batchParams.isEmpty()) {
        res.message = QStringLiteral("set_effect_param requires params.param_name + params.value or params.parameters");
        return res;
    }

    if (!paramName.isEmpty()) {
        if (!op.params.contains(QStringLiteral("value"))) {
            res.message = QStringLiteral("set_effect_param requires params.value for '%1'").arg(paramName);
            return res;
        }
        matched->parameters.insert(paramName, op.params.value(QStringLiteral("value")));
    }
    for (auto it = batchParams.constBegin(); it != batchParams.constEnd(); ++it) {
        matched->parameters.insert(it.key(), it.value());
    }

    res.applied = true;
    res.createdId = matched->instanceId;
    res.message = QStringLiteral("Updated parameters on effect '%1'").arg(matched->instanceId);
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleSetKeyframe(const TimelineOperation &op, const QString &sequenceId)
{
    TimelineOperationResult res;
    res.opId = op.opId;
    ensureSequenceExists(sequenceId);

    const QString paramName = firstNonEmptyStringParam(op.params, {QStringLiteral("param_name"), QStringLiteral("parameter")});
    if (paramName.isEmpty()) {
        res.message = QStringLiteral("set_keyframe requires params.param_name or params.parameter");
        return res;
    }
    if (!op.params.contains(QStringLiteral("value"))) {
        res.message = QStringLiteral("set_keyframe requires params.value");
        return res;
    }

    int frame = op.frameRange.inFrame > 0 ? op.frameRange.inFrame : op.frameRange.timelineStart;
    if (op.params.contains(QStringLiteral("frame"))) {
        if (!jsonValueIsStrictInt(op.params.value(QStringLiteral("frame")), &frame) || frame < 0) {
            res.message = QStringLiteral("set_keyframe params.frame must be a non-negative integer");
            return res;
        }
    }

    KeyframeRecord kf;
    kf.frame = frame;
    kf.value = op.params.value(QStringLiteral("value"));
    kf.interpolation = op.params.value(QStringLiteral("interpolation")).toString(QStringLiteral("linear"));

    const QString instanceId = op.params.value(QStringLiteral("effect_instance_id")).toString().trimmed();
    const QString effectId = firstNonEmptyStringParam(op.params, {QStringLiteral("effect_id"), QStringLiteral("filter_id")});

    QVector<EffectInstance> *effectsList = nullptr;
    QMap<QString, QVector<KeyframeRecord>> *intrinsicMap = nullptr;
    const QString clipId = op.clipId.trimmed();
    const QString trackId = op.trackId.trimmed();
    if (!clipId.isEmpty()) {
        if (!m_state.clips.contains(clipId)) {
            res.message = QStringLiteral("Clip '%1' does not exist").arg(clipId);
            return res;
        }
        effectsList = &m_state.clips[clipId].effects;
        intrinsicMap = &m_state.clips[clipId].intrinsicKeyframes;
    } else if (!trackId.isEmpty()) {
        if (!m_state.tracks.contains(trackId)) {
            res.message = QStringLiteral("Track '%1' does not exist").arg(trackId);
            return res;
        }
        effectsList = &m_state.tracks[trackId].effects;
        intrinsicMap = &m_state.tracks[trackId].intrinsicKeyframes;
    } else {
        effectsList = &m_state.sequences[sequenceId].masterEffects;
        intrinsicMap = &m_state.sequences[sequenceId].masterKeyframes;
    }

    if (!instanceId.isEmpty() || !effectId.isEmpty()) {
        EffectInstance *matched = nullptr;
        for (EffectInstance &fx : *effectsList) {
            if ((!instanceId.isEmpty() && fx.instanceId == instanceId) || (instanceId.isEmpty() && fx.effectId == effectId)) {
                matched = &fx;
                break;
            }
        }
        if (matched == nullptr) {
            res.message = QStringLiteral("Target effect '%1' not found for keyframe").arg(instanceId.isEmpty() ? effectId : instanceId);
            return res;
        }
        upsertKeyframe(matched->keyframes, paramName, kf);
        res.createdId = matched->instanceId;
    } else {
        upsertKeyframe(*intrinsicMap, paramName, kf);
        res.createdId = !clipId.isEmpty() ? clipId : (!trackId.isEmpty() ? trackId : sequenceId);
    }

    res.applied = true;
    res.message = QStringLiteral("Set keyframe '%1' at frame %2").arg(paramName).arg(frame);
    res.metadata.insert(QStringLiteral("param_name"), paramName);
    res.metadata.insert(QStringLiteral("frame"), frame);
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleSetAudioLevel(const TimelineOperation &op, const QString &sequenceId)
{
    TimelineOperationResult res;
    res.opId = op.opId;
    ensureSequenceExists(sequenceId);

    double gainDb = 0.0;
    QString err;
    if (!extractAudioGainDb(op.params, &gainDb, &err)) {
        res.message = err;
        return res;
    }

    const QString clipId = op.clipId.trimmed();
    const QString trackId = op.trackId.trimmed();
    if (!clipId.isEmpty()) {
        if (!m_state.clips.contains(clipId)) {
            res.message = QStringLiteral("Clip '%1' does not exist").arg(clipId);
            return res;
        }
        TimelineClip &clip = m_state.clips[clipId];
        clip.audioGainDb = gainDb;
        if (op.params.value(QStringLiteral("keyframe")).toBool(false) || op.frameRange.timelineStart > 0) {
            AudioLevelPoint pt;
            pt.frame = op.frameRange.timelineStart;
            pt.gainDb = gainDb;
            clip.audioAutomation.append(pt);
        }
        res.createdId = clipId;
    } else if (!trackId.isEmpty()) {
        if (!m_state.tracks.contains(trackId)) {
            res.message = QStringLiteral("Track '%1' does not exist").arg(trackId);
            return res;
        }
        m_state.tracks[trackId].audioGainDb = gainDb;
        res.createdId = trackId;
    } else {
        m_state.sequences[sequenceId].masterGainDb = gainDb;
        res.createdId = sequenceId;
    }

    res.applied = true;
    res.message = QStringLiteral("Set audio level to %1 dB").arg(gainDb, 0, 'f', 2);
    res.metadata.insert(QStringLiteral("gain_db"), gainDb);
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleAddCaption(const TimelineOperation &op, const QString &sequenceId)
{
    TimelineOperationResult res;
    res.opId = op.opId;
    ensureSequenceExists(sequenceId);

    const QString text = op.params.value(QStringLiteral("text")).toString().trimmed();
    if (text.isEmpty()) {
        res.message = QStringLiteral("add_caption requires non-empty params.text");
        return res;
    }

    const QString trackId = op.trackId.trimmed();
    if (!trackId.isEmpty() && !m_state.tracks.contains(trackId)) {
        res.message = QStringLiteral("Caption track '%1' does not exist").arg(trackId);
        return res;
    }

    FrameRange fr = op.frameRange;
    if (fr.outFrame == fr.inFrame) {
        const int durationFrames = op.params.value(QStringLiteral("duration_frames")).toInt(0);
        if (durationFrames > 0) {
            fr.outFrame = fr.inFrame + durationFrames;
        } else {
            res.message = QStringLiteral("add_caption requires positive duration (out_frame > in_frame or params.duration_frames > 0)");
            return res;
        }
    }

    TimelineCaption cap;
    cap.captionId = op.params.value(QStringLiteral("caption_id")).toString().trimmed();
    if (cap.captionId.isEmpty()) {
        cap.captionId = QStringLiteral("cap-%1").arg(m_state.nextCaptionId++);
    }
    if (m_state.captions.contains(cap.captionId)) {
        res.message = QStringLiteral("Caption '%1' already exists").arg(cap.captionId);
        return res;
    }
    cap.sequenceId = sequenceId;
    cap.trackId = trackId;
    cap.frameRange = fr;
    cap.text = text;
    cap.speaker = op.params.value(QStringLiteral("speaker")).toString();
    cap.language = op.params.value(QStringLiteral("language")).toString(QStringLiteral("en"));
    cap.style = op.params.value(QStringLiteral("style")).toObject();

    m_state.captions.insert(cap.captionId, cap);

    res.applied = true;
    res.createdId = cap.captionId;
    res.message = QStringLiteral("Added caption '%1'").arg(cap.captionId);
    res.metadata.insert(QStringLiteral("text"), cap.text);
    res.metadata.insert(QStringLiteral("timeline_start"), cap.frameRange.timelineStart);
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleAddMarker(const TimelineOperation &op, const QString &sequenceId)
{
    TimelineOperationResult res;
    res.opId = op.opId;
    ensureSequenceExists(sequenceId);

    const QString clipId = op.clipId.trimmed();
    if (!clipId.isEmpty() && !m_state.clips.contains(clipId)) {
        res.message = QStringLiteral("Clip '%1' does not exist").arg(clipId);
        return res;
    }

    int frame = op.frameRange.timelineStart > 0 ? op.frameRange.timelineStart : op.frameRange.inFrame;
    if (op.params.contains(QStringLiteral("frame"))) {
        if (!jsonValueIsStrictInt(op.params.value(QStringLiteral("frame")), &frame) || frame < 0) {
            res.message = QStringLiteral("add_marker params.frame must be a non-negative integer");
            return res;
        }
    }

    TimelineMarker marker;
    marker.markerId = op.params.value(QStringLiteral("marker_id")).toString().trimmed();
    if (marker.markerId.isEmpty()) {
        marker.markerId = QStringLiteral("mkr-%1").arg(m_state.nextMarkerId++);
    }
    if (m_state.markers.contains(marker.markerId)) {
        res.message = QStringLiteral("Marker '%1' already exists").arg(marker.markerId);
        return res;
    }
    marker.sequenceId = sequenceId;
    marker.clipId = clipId;
    marker.frame = frame;
    marker.label = firstNonEmptyStringParam(op.params, {QStringLiteral("label"), QStringLiteral("comment"), QStringLiteral("name")});
    if (marker.label.isEmpty()) {
        marker.label = op.rationale.trimmed().isEmpty() ? marker.markerId : op.rationale.trimmed();
    }
    marker.category = op.params.value(QStringLiteral("category")).toString(QStringLiteral("editorial"));
    marker.color = op.params.value(QStringLiteral("color")).toString(QStringLiteral("#3daee9"));

    m_state.markers.insert(marker.markerId, marker);

    res.applied = true;
    res.createdId = marker.markerId;
    res.message = QStringLiteral("Added marker '%1' at frame %2").arg(marker.label).arg(frame);
    res.metadata.insert(QStringLiteral("frame"), frame);
    res.metadata.insert(QStringLiteral("category"), marker.category);
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleUpdateCaption(const TimelineOperation &op, const QString &sequenceId)
{
    TimelineOperationResult res;
    res.opId = op.opId;
    ensureSequenceExists(sequenceId);

    QString captionId = firstNonEmptyStringParam(op.params, {QStringLiteral("caption_id"), QStringLiteral("subtitle_id")});
    if (captionId.isEmpty()) {
        captionId = op.clipId.trimmed();
    }

    if (captionId.isEmpty() && (op.params.contains(QStringLiteral("original_start_frame")) || op.params.contains(QStringLiteral("start_frame")))) {
        int lookupStart = -1;
        const QString key = op.params.contains(QStringLiteral("original_start_frame")) ? QStringLiteral("original_start_frame") : QStringLiteral("start_frame");
        if (jsonValueIsStrictInt(op.params.value(key), &lookupStart)) {
            for (const TimelineCaption &cap : std::as_const(m_state.captions)) {
                if (cap.frameRange.timelineStart == lookupStart) {
                    captionId = cap.captionId;
                    break;
                }
            }
        }
    }

    if (captionId.isEmpty() || !m_state.captions.contains(captionId)) {
        res.message = QStringLiteral("Caption '%1' does not exist").arg(captionId);
        return res;
    }

    TimelineCaption &cap = m_state.captions[captionId];
    if (!cap.trackId.isEmpty() && m_state.tracks.contains(cap.trackId) && m_state.tracks.value(cap.trackId).locked) {
        res.message = QStringLiteral("Caption track '%1' is locked").arg(cap.trackId);
        return res;
    }

    bool updatedAny = false;
    if (op.params.contains(QStringLiteral("text"))) {
        if (!op.params.value(QStringLiteral("text")).isString()) {
            res.message = QStringLiteral("update_caption params.text must be a string");
            return res;
        }
        const QString newText = op.params.value(QStringLiteral("text")).toString().trimmed();
        if (newText.isEmpty()) {
            res.message = QStringLiteral("update_caption params.text must be non-empty");
            return res;
        }
        cap.text = newText;
        updatedAny = true;
    }

    if (op.params.contains(QStringLiteral("speaker"))) {
        cap.speaker = op.params.value(QStringLiteral("speaker")).toString();
        updatedAny = true;
    }
    if (op.params.contains(QStringLiteral("language"))) {
        cap.language = op.params.value(QStringLiteral("language")).toString();
        updatedAny = true;
    }
    if (op.params.contains(QStringLiteral("style")) && op.params.value(QStringLiteral("style")).isObject()) {
        const QJsonObject styleUpdates = op.params.value(QStringLiteral("style")).toObject();
        for (auto it = styleUpdates.constBegin(); it != styleUpdates.constEnd(); ++it) {
            cap.style.insert(it.key(), it.value());
        }
        updatedAny = true;
    }

    int newStart = cap.frameRange.timelineStart;
    bool timingRequested = false;
    if (op.params.contains(QStringLiteral("start_frame"))) {
        if (!jsonValueIsStrictInt(op.params.value(QStringLiteral("start_frame")), &newStart) || newStart < 0) {
            res.message = QStringLiteral("update_caption params.start_frame must be a non-negative integer");
            return res;
        }
        timingRequested = true;
    } else if (op.frameRange.timelineStart > 0 || op.frameRange.outFrame > op.frameRange.inFrame) {
        newStart = op.frameRange.timelineStart;
        timingRequested = true;
    }

    int newDuration = qMax(1, cap.frameRange.outFrame - cap.frameRange.inFrame);
    if (op.params.contains(QStringLiteral("end_frame"))) {
        int endFrame = 0;
        if (!jsonValueIsStrictInt(op.params.value(QStringLiteral("end_frame")), &endFrame) || endFrame <= newStart) {
            res.message = QStringLiteral("update_caption params.end_frame must be an integer > start_frame");
            return res;
        }
        newDuration = endFrame - newStart;
        timingRequested = true;
    } else if (op.params.contains(QStringLiteral("duration_frames"))) {
        if (!jsonValueIsStrictInt(op.params.value(QStringLiteral("duration_frames")), &newDuration) || newDuration <= 0) {
            res.message = QStringLiteral("update_caption params.duration_frames must be a positive integer");
            return res;
        }
        timingRequested = true;
    } else if (op.frameRange.outFrame > op.frameRange.inFrame) {
        newDuration = op.frameRange.outFrame - op.frameRange.inFrame;
        timingRequested = true;
    }

    if (timingRequested) {
        cap.frameRange.timelineStart = newStart;
        cap.frameRange.inFrame = 0;
        cap.frameRange.outFrame = newDuration;
        updatedAny = true;
    }

    if (!updatedAny) {
        res.message = QStringLiteral("update_caption requires at least one updated field (text, timing, speaker, language, or style)");
        return res;
    }

    res.applied = true;
    res.createdId = captionId;
    res.message = QStringLiteral("Updated caption '%1'").arg(captionId);
    res.metadata.insert(QStringLiteral("text"), cap.text);
    res.metadata.insert(QStringLiteral("timeline_start"), cap.frameRange.timelineStart);
    res.metadata.insert(QStringLiteral("duration_frames"), cap.frameRange.outFrame - cap.frameRange.inFrame);
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleDeleteCaption(const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    QString captionId = firstNonEmptyStringParam(op.params, {QStringLiteral("caption_id"), QStringLiteral("subtitle_id")});
    if (captionId.isEmpty()) {
        captionId = op.clipId.trimmed();
    }

    if (captionId.isEmpty() && op.params.contains(QStringLiteral("start_frame"))) {
        int lookupStart = -1;
        if (jsonValueIsStrictInt(op.params.value(QStringLiteral("start_frame")), &lookupStart)) {
            for (const TimelineCaption &cap : std::as_const(m_state.captions)) {
                if (cap.frameRange.timelineStart == lookupStart) {
                    captionId = cap.captionId;
                    break;
                }
            }
        }
    }

    if (captionId.isEmpty() || !m_state.captions.contains(captionId)) {
        res.message = QStringLiteral("Caption '%1' does not exist").arg(captionId);
        return res;
    }

    const TimelineCaption existing = m_state.captions.value(captionId);
    if (!existing.trackId.isEmpty() && m_state.tracks.contains(existing.trackId) && m_state.tracks.value(existing.trackId).locked) {
        res.message = QStringLiteral("Caption track '%1' is locked").arg(existing.trackId);
        return res;
    }

    m_state.captions.remove(captionId);

    res.applied = true;
    res.createdId = captionId;
    res.message = QStringLiteral("Deleted caption '%1'").arg(captionId);
    res.metadata.insert(QStringLiteral("deleted_caption_id"), captionId);
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleApplyMask(const TimelineOperation &op, const QString &sequenceId)
{
    TimelineOperationResult res;
    res.opId = op.opId;
    ensureSequenceExists(sequenceId);

    const QString clipId = op.clipId.trimmed();
    const QString trackId = op.trackId.trimmed();
    if (clipId.isEmpty() && trackId.isEmpty()) {
        res.message = QStringLiteral("apply_mask requires non-empty clip_id or track_id");
        return res;
    }
    if (!clipId.isEmpty()) {
        if (!m_state.clips.contains(clipId)) {
            res.message = QStringLiteral("Clip '%1' does not exist").arg(clipId);
            return res;
        }
        const QString clipTrackId = m_state.clips.value(clipId).trackId;
        if (m_state.tracks.contains(clipTrackId) && m_state.tracks.value(clipTrackId).locked) {
            res.message = QStringLiteral("Track '%1' is locked").arg(clipTrackId);
            return res;
        }
    } else if (!m_state.tracks.contains(trackId)) {
        res.message = QStringLiteral("Track '%1' does not exist").arg(trackId);
        return res;
    } else if (m_state.tracks.value(trackId).locked) {
        res.message = QStringLiteral("Track '%1' is locked").arg(trackId);
        return res;
    }

    const QString maskType = op.params.value(QStringLiteral("mask_type")).toString(QStringLiteral("spline")).trimmed().toLower();
    if (!isValidMaskType(maskType)) {
        res.message = QStringLiteral("Invalid mask_type '%1'").arg(maskType);
        return res;
    }

    const QString operation = op.params.value(QStringLiteral("operation")).toString(QStringLiteral("write_on_clear")).trimmed().toLower();
    if (!isValidMaskOperation(operation)) {
        res.message = QStringLiteral("Invalid mask operation '%1'").arg(operation);
        return res;
    }

    const double feather = op.params.value(QStringLiteral("feather")).toDouble(0.0);
    const double expand = op.params.value(QStringLiteral("expand")).toDouble(0.0);
    if (!std::isfinite(feather) || feather < 0.0 || feather > 100.0) {
        res.message = QStringLiteral("apply_mask feather (%1) must be within [0.0, 100.0]").arg(feather);
        return res;
    }
    if (!std::isfinite(expand) || expand < -50.0 || expand > 50.0) {
        res.message = QStringLiteral("apply_mask expand (%1) must be within [-50.0, 50.0]").arg(expand);
        return res;
    }
    const bool invert = op.params.value(QStringLiteral("invert")).toBool(false);

    const QString mattePath = op.params.value(QStringLiteral("matte_path")).toString().trimmed();
    if (mattePath.contains(QChar(u'\0'))) {
        res.message = QStringLiteral("apply_mask matte_path contains invalid null byte");
        return res;
    }
    const QString splinesJson = synthesizeRotoscopingSplineJson(op.params);
    const QJsonArray keyframes = op.params.value(QStringLiteral("keyframes")).toArray();

    const QString requestedService =
        firstNonEmptyStringParam(op.params, {QStringLiteral("mlt_service"), QStringLiteral("filter_id"), QStringLiteral("effect_service")});
    if (maskType == QStringLiteral("matte_file") && mattePath.isEmpty()) {
        res.message = QStringLiteral("apply_mask with mask_type='matte_file' requires non-empty params.matte_path");
        return res;
    }
    if (mattePath.isEmpty() && splinesJson.isEmpty() && keyframes.isEmpty() && requestedService.isEmpty()) {
        res.message = QStringLiteral("apply_mask requires splines_json, matte_path, keyframes, or explicit alphaops service");
        return res;
    }

    QString mltService = QStringLiteral("rotoscoping");
    if (!mattePath.isEmpty() || maskType == QStringLiteral("matte_file")) {
        mltService = QStringLiteral("shape");
    } else if (requestedService == QStringLiteral("alphaops") || requestedService == QStringLiteral("frei0r.alpha0ps")) {
        mltService = QStringLiteral("frei0r.alpha0ps");
    } else if (!requestedService.isEmpty()) {
        mltService = requestedService;
    }

    QString maskId = op.params.value(QStringLiteral("mask_id")).toString().trimmed();
    if (maskId.isEmpty()) {
        maskId = QStringLiteral("mask-%1").arg(m_state.nextMaskId++);
    }
    const bool allowReplace = op.params.value(QStringLiteral("replace")).toBool(false);
    if (m_state.masks.contains(maskId) && !allowReplace) {
        res.message = QStringLiteral("Mask '%1' already exists").arg(maskId);
        return res;
    }

    EffectInstance fx;
    fx.instanceId = QStringLiteral("maskfx-%1").arg(m_state.nextEffectId++);
    fx.effectId = mltService;
    fx.isAudioFilter = false;
    fx.enabled = op.params.value(QStringLiteral("enabled")).toBool(true);
    fx.parameters.insert(QStringLiteral("mask_id"), maskId);
    fx.parameters.insert(QStringLiteral("mask_type"), maskType);
    fx.parameters.insert(QStringLiteral("feather"), feather);
    fx.parameters.insert(QStringLiteral("expand"), expand);
    fx.parameters.insert(QStringLiteral("invert"), invert);
    fx.parameters.insert(QStringLiteral("operation"), operation);
    if (!splinesJson.isEmpty()) {
        fx.parameters.insert(QStringLiteral("spline"), splinesJson);
    }
    if (!mattePath.isEmpty()) {
        fx.parameters.insert(QStringLiteral("resource"), mattePath);
    }

    if (!clipId.isEmpty()) {
        m_state.clips[clipId].effects.append(fx);
    } else {
        m_state.tracks[trackId].effects.append(fx);
    }

    TimelineMask mask;
    mask.maskId = maskId;
    mask.sequenceId = sequenceId;
    mask.clipId = clipId;
    mask.trackId = trackId;
    mask.maskType = maskType;
    mask.mltService = mltService;
    mask.splinesJson = splinesJson;
    mask.mattePath = mattePath;
    mask.feather = feather;
    mask.expand = expand;
    mask.invert = invert;
    mask.operation = operation;
    mask.keyframes = keyframes;
    mask.effectInstanceId = fx.instanceId;

    m_state.masks.insert(maskId, mask);

    res.applied = true;
    res.createdId = maskId;
    res.message = QStringLiteral("Applied %1 mask '%2' via MLT service '%3'").arg(maskType, maskId, mltService);
    res.metadata.insert(QStringLiteral("mask_type"), maskType);
    res.metadata.insert(QStringLiteral("mlt_service"), mltService);
    res.metadata.insert(QStringLiteral("effect_instance_id"), fx.instanceId);
    res.metadata.insert(QStringLiteral("feather"), feather);
    res.metadata.insert(QStringLiteral("expand"), expand);
    res.metadata.insert(QStringLiteral("invert"), invert);
    res.metadata.insert(QStringLiteral("operation"), operation);
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleAddTransition(const TimelineOperation &op, const QString &sequenceId)
{
    TimelineOperationResult res;
    res.opId = op.opId;
    ensureSequenceExists(sequenceId);

    const QString transitionId =
        firstNonEmptyStringParam(op.params, {QStringLiteral("transition_id"), QStringLiteral("transition_type"), QStringLiteral("mlt_service"),
                                             QStringLiteral("service")});
    if (transitionId.isEmpty()) {
        res.message = QStringLiteral("add_transition requires params.transition_id");
        return res;
    }

    QString bTrackId = extractTrackParamString(op.params, QStringLiteral("b_track"));
    if (bTrackId.isEmpty()) {
        bTrackId = op.trackId.trimmed();
    }
    const QString aTrackId = extractTrackParamString(op.params, QStringLiteral("a_track"));

    if (bTrackId.isEmpty() || !m_state.tracks.contains(bTrackId)) {
        res.message = QStringLiteral("Transition b_track '%1' does not exist").arg(bTrackId);
        return res;
    }
    if (m_state.tracks.value(bTrackId).locked) {
        res.message = QStringLiteral("Transition b_track '%1' is locked").arg(bTrackId);
        return res;
    }
    if (!aTrackId.isEmpty() && aTrackId != QStringLiteral("0") && aTrackId.compare(QStringLiteral("black"), Qt::CaseInsensitive) != 0) {
        if (!m_state.tracks.contains(aTrackId)) {
            res.message = QStringLiteral("Transition a_track '%1' does not exist").arg(aTrackId);
            return res;
        }
    }

    int inFrame = op.frameRange.timelineStart;
    int outFrame = 0;
    if (op.params.contains(QStringLiteral("in_frame"))) {
        if (!jsonValueIsStrictInt(op.params.value(QStringLiteral("in_frame")), &inFrame) || inFrame < 0) {
            res.message = QStringLiteral("add_transition params.in_frame must be a non-negative integer");
            return res;
        }
    } else if (op.frameRange.inFrame > 0 && op.frameRange.timelineStart == 0) {
        inFrame = op.frameRange.inFrame;
    }

    if (op.params.contains(QStringLiteral("out_frame"))) {
        if (!jsonValueIsStrictInt(op.params.value(QStringLiteral("out_frame")), &outFrame) || outFrame <= inFrame) {
            res.message = QStringLiteral("add_transition params.out_frame must be an integer > in_frame");
            return res;
        }
    } else if (op.params.contains(QStringLiteral("duration_frames"))) {
        int dur = 0;
        if (!jsonValueIsStrictInt(op.params.value(QStringLiteral("duration_frames")), &dur) || dur <= 0) {
            res.message = QStringLiteral("add_transition params.duration_frames must be a positive integer");
            return res;
        }
        outFrame = inFrame + dur;
    } else if (op.frameRange.outFrame > op.frameRange.inFrame) {
        outFrame = inFrame + (op.frameRange.outFrame - op.frameRange.inFrame);
    } else {
        res.message = QStringLiteral("add_transition requires positive duration (params.in_frame/out_frame, duration_frames, or frame_range)");
        return res;
    }

    QString instanceId = op.params.value(QStringLiteral("composition_id")).toString().trimmed();
    if (instanceId.isEmpty()) {
        instanceId = QStringLiteral("tr-%1").arg(m_state.nextTransitionId++);
    }
    if (m_state.transitions.contains(instanceId)) {
        res.message = QStringLiteral("Transition '%1' already exists").arg(instanceId);
        return res;
    }

    TimelineTransition tr;
    tr.transitionInstanceId = instanceId;
    tr.transitionId = transitionId;
    tr.sequenceId = sequenceId;
    tr.aTrackId = aTrackId;
    tr.bTrackId = bTrackId;
    tr.inFrame = inFrame;
    tr.outFrame = outFrame;
    tr.properties = op.params.value(QStringLiteral("properties")).toObject();

    m_state.transitions.insert(instanceId, tr);

    res.applied = true;
    res.createdId = instanceId;
    res.message = QStringLiteral("Added transition '%1' (%2) on track '%3' [%4, %5)").arg(transitionId, instanceId, bTrackId).arg(inFrame).arg(outFrame);
    res.metadata.insert(QStringLiteral("transition_id"), transitionId);
    res.metadata.insert(QStringLiteral("a_track"), aTrackId);
    res.metadata.insert(QStringLiteral("b_track"), bTrackId);
    res.metadata.insert(QStringLiteral("in_frame"), inFrame);
    res.metadata.insert(QStringLiteral("out_frame"), outFrame);
    res.metadata.insert(QStringLiteral("duration_frames"), tr.duration());
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleAddLowerThird(const TimelineOperation &op, const QString &sequenceId)
{
    TimelineOperationResult res;
    res.opId = op.opId;
    ensureSequenceExists(sequenceId);

    const QString speakerName = firstNonEmptyStringParam(op.params, {QStringLiteral("speaker_name"), QStringLiteral("title"), QStringLiteral("name")});
    if (speakerName.isEmpty()) {
        res.message = QStringLiteral("add_lower_third requires non-empty params.speaker_name");
        return res;
    }

    const QString trackId = op.trackId.trimmed();
    if (!trackId.isEmpty()) {
        if (!m_state.tracks.contains(trackId)) {
            res.message = QStringLiteral("Lower-third target track '%1' does not exist").arg(trackId);
            return res;
        }
        if (m_state.tracks.value(trackId).locked) {
            res.message = QStringLiteral("Lower-third target track '%1' is locked").arg(trackId);
            return res;
        }
    }

    FrameRange fr = op.frameRange;
    if (fr.outFrame == fr.inFrame) {
        const int dur = op.params.contains(QStringLiteral("duration_frames")) ? op.params.value(QStringLiteral("duration_frames")).toInt(0) : 125;
        if (dur <= 0) {
            res.message = QStringLiteral("add_lower_third requires positive duration");
            return res;
        }
        fr.outFrame = fr.inFrame + dur;
    }

    QString ltId = firstNonEmptyStringParam(op.params, {QStringLiteral("lower_third_id")});
    if (ltId.isEmpty()) {
        ltId = op.clipId.trimmed();
    }
    if (ltId.isEmpty()) {
        ltId = QStringLiteral("lt-%1").arg(m_state.nextLowerThirdId++);
    }
    if (m_state.lowerThirds.contains(ltId)) {
        res.message = QStringLiteral("Lower third '%1' already exists").arg(ltId);
        return res;
    }

    const QString subtitle = op.params.value(QStringLiteral("subtitle")).toString().trimmed();
    const QString templateName = op.params.value(QStringLiteral("template")).toString(QStringLiteral("modern_clean")).trimmed();
    QString position = QStringLiteral("bottom_left");
    if (op.params.value(QStringLiteral("position")).isString()) {
        const QString p = op.params.value(QStringLiteral("position")).toString().trimmed();
        if (!p.isEmpty()) {
            position = p;
        }
    } else if (op.params.value(QStringLiteral("position")).isObject()) {
        position = QString::fromUtf8(QJsonDocument(op.params.value(QStringLiteral("position")).toObject()).toJson(QJsonDocument::Compact));
    }
    const QJsonObject style = op.params.value(QStringLiteral("style")).toObject();

    const SequenceRecord seq = m_state.sequences.value(sequenceId);
    const int durationFrames = qMax(1, fr.outFrame - fr.inFrame);
    const QString titleXml = buildLowerThirdTitleXml(speakerName, subtitle, templateName, position, seq.width, seq.height, durationFrames, style);

    TimelineLowerThird lt;
    lt.lowerThirdId = ltId;
    lt.sequenceId = sequenceId;
    lt.trackId = trackId;
    lt.clipId = op.clipId.trimmed().isEmpty() ? ltId : op.clipId.trimmed();
    lt.frameRange = fr;
    lt.speakerName = speakerName;
    lt.subtitle = subtitle;
    lt.templateName = templateName;
    lt.position = position;
    lt.titleXml = titleXml;
    lt.style = style;

    m_state.lowerThirds.insert(ltId, lt);

    res.applied = true;
    res.createdId = ltId;
    res.message = QStringLiteral("Added lower third '%1' for '%2'").arg(ltId, speakerName);
    res.metadata.insert(QStringLiteral("speaker_name"), speakerName);
    res.metadata.insert(QStringLiteral("subtitle"), subtitle);
    res.metadata.insert(QStringLiteral("template"), templateName);
    res.metadata.insert(QStringLiteral("position"), position);
    res.metadata.insert(QStringLiteral("timeline_start"), fr.timelineStart);
    res.metadata.insert(QStringLiteral("duration_frames"), durationFrames);
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleSetClipSpeed(const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    const QString clipId = op.clipId.trimmed();
    if (!m_state.clips.contains(clipId)) {
        res.message = QStringLiteral("Clip '%1' does not exist").arg(clipId);
        return res;
    }

    TimelineClip &clip = m_state.clips[clipId];
    if (m_state.tracks.contains(clip.trackId) && m_state.tracks.value(clip.trackId).locked) {
        res.message = QStringLiteral("Track '%1' is locked").arg(clip.trackId);
        return res;
    }

    double newSpeedPercent = 100.0;
    QString err;
    if (!extractSpeedPercent(op.params, &newSpeedPercent, &err)) {
        res.message = err;
        return res;
    }

    const bool pitchCompensate = op.params.value(QStringLiteral("pitch_compensate")).toBool(true);
    const bool changeDuration = op.params.value(QStringLiteral("change_duration")).toBool(true);

    const double oldSpeedPercent = clip.speedPercent;
    const int oldDuration = qMax(1, clip.duration());
    clip.speedPercent = newSpeedPercent;
    clip.pitchCompensate = pitchCompensate;

    if (changeDuration) {
        const double ratio = std::abs(oldSpeedPercent) / std::abs(newSpeedPercent);
        const int newDuration = qMax(1, static_cast<int>(std::llround(static_cast<double>(oldDuration) * ratio)));
        clip.frameRange.outFrame = clip.frameRange.inFrame + newDuration;
    }

    res.applied = true;
    res.createdId = clipId;
    res.message = QStringLiteral("Set clip '%1' speed to %2%").arg(clipId).arg(newSpeedPercent, 0, 'f', 2);
    res.metadata.insert(QStringLiteral("previous_speed_percent"), oldSpeedPercent);
    res.metadata.insert(QStringLiteral("speed_percent"), newSpeedPercent);
    res.metadata.insert(QStringLiteral("pitch_compensate"), pitchCompensate);
    res.metadata.insert(QStringLiteral("duration_frames"), clip.duration());
    return res;
}

TimelineOperationResult InMemoryTimelineBackend::handleExportSequence(const TimelineOperation &op, const QString &sequenceId)
{
    TimelineOperationResult res;
    res.opId = op.opId;
    ensureSequenceExists(sequenceId);

    const QString outputPath = firstNonEmptyStringParam(op.params, {QStringLiteral("output_path"), QStringLiteral("destination")});
    if (outputPath.isEmpty()) {
        res.message = QStringLiteral("export_sequence requires params.output_path or params.destination");
        return res;
    }
    if (outputPath.contains(QChar(u'\0'))) {
        res.message = QStringLiteral("export_sequence output_path contains invalid null byte");
        return res;
    }

    ExportRequestRecord req;
    req.exportId = QStringLiteral("exp-%1").arg(m_state.nextExportId++);
    req.sequenceId = sequenceId;
    req.outputPath = outputPath;
    req.preset = firstNonEmptyStringParam(op.params, {QStringLiteral("preset"), QStringLiteral("profile")});
    if (req.preset.isEmpty()) {
        req.preset = QStringLiteral("mp4_h264_default");
    }
    req.frameRange = op.frameRange;
    req.params = op.params;
    req.status = QStringLiteral("queued");

    m_state.exportRequests.insert(req.exportId, req);

    res.applied = true;
    res.createdId = req.exportId;
    res.message = QStringLiteral("Queued export '%1' for sequence '%2'").arg(req.exportId, sequenceId);
    res.metadata.insert(QStringLiteral("output_path"), req.outputPath);
    res.metadata.insert(QStringLiteral("preset"), req.preset);
    return res;
}

QJsonObject InMemoryTimelineBackend::captureStateSnapshot() const
{
    return serializeState(m_state);
}

bool InMemoryTimelineBackend::restoreStateSnapshot(const QJsonObject &snapshot, QString *errorOut)
{
    State restored;
    if (!deserializeState(snapshot, &restored, errorOut)) {
        return false;
    }
    m_state = std::move(restored);
    m_inTransaction = false;
    m_transactionBackup = State{};
    return true;
}

QJsonObject InMemoryTimelineBackend::serializeState(const State &state)
{
    QJsonObject root;
    root.insert(QStringLiteral("active_sequence_id"), state.activeSequenceId);

    QJsonObject counters;
    counters.insert(QStringLiteral("media"), state.nextMediaId);
    counters.insert(QStringLiteral("sequence"), state.nextSequenceId);
    counters.insert(QStringLiteral("track"), state.nextTrackId);
    counters.insert(QStringLiteral("clip"), state.nextClipId);
    counters.insert(QStringLiteral("effect"), state.nextEffectId);
    counters.insert(QStringLiteral("caption"), state.nextCaptionId);
    counters.insert(QStringLiteral("marker"), state.nextMarkerId);
    counters.insert(QStringLiteral("camera_selection"), state.nextCameraSelectionId);
    counters.insert(QStringLiteral("sync_group"), state.nextSyncGroupId);
    counters.insert(QStringLiteral("export"), state.nextExportId);
    root.insert(QStringLiteral("counters"), counters);

    QJsonArray seqArr;
    for (const SequenceRecord &s : state.sequences) {
        QJsonObject o;
        o.insert(QStringLiteral("sequence_id"), s.sequenceId);
        o.insert(QStringLiteral("name"), s.name);
        o.insert(QStringLiteral("fps_num"), s.fpsNum);
        o.insert(QStringLiteral("fps_den"), s.fpsDen);
        o.insert(QStringLiteral("width"), s.width);
        o.insert(QStringLiteral("height"), s.height);
        o.insert(QStringLiteral("master_gain_db"), s.masterGainDb);
        o.insert(QStringLiteral("master_effects"), effectsToJson(s.masterEffects));
        o.insert(QStringLiteral("master_keyframes"), keyframeMapToJson(s.masterKeyframes));
        seqArr.append(o);
    }
    root.insert(QStringLiteral("sequences"), seqArr);

    QJsonArray mediaArr;
    for (const MediaAsset &m : state.mediaBin) {
        QJsonObject o;
        o.insert(QStringLiteral("media_ref"), m.mediaRef);
        o.insert(QStringLiteral("source_path"), m.sourcePath);
        o.insert(QStringLiteral("name"), m.name);
        o.insert(QStringLiteral("media_type"), m.mediaType);
        o.insert(QStringLiteral("duration_frames"), m.durationFrames);
        o.insert(QStringLiteral("fps_num"), m.fpsNum);
        o.insert(QStringLiteral("fps_den"), m.fpsDen);
        o.insert(QStringLiteral("metadata"), m.metadata);
        mediaArr.append(o);
    }
    root.insert(QStringLiteral("media_bin"), mediaArr);

    QJsonArray trackArr;
    for (const TimelineTrack &t : state.tracks) {
        QJsonObject o;
        o.insert(QStringLiteral("track_id"), t.trackId);
        o.insert(QStringLiteral("sequence_id"), t.sequenceId);
        o.insert(QStringLiteral("name"), t.name);
        o.insert(QStringLiteral("kind"), t.kind);
        o.insert(QStringLiteral("index"), t.index);
        o.insert(QStringLiteral("locked"), t.locked);
        o.insert(QStringLiteral("muted"), t.muted);
        o.insert(QStringLiteral("audio_gain_db"), t.audioGainDb);
        o.insert(QStringLiteral("camera_id"), t.cameraId);
        o.insert(QStringLiteral("effects"), effectsToJson(t.effects));
        o.insert(QStringLiteral("intrinsic_keyframes"), keyframeMapToJson(t.intrinsicKeyframes));
        trackArr.append(o);
    }
    root.insert(QStringLiteral("tracks"), trackArr);

    QJsonArray clipArr;
    for (const TimelineClip &c : state.clips) {
        QJsonObject o;
        o.insert(QStringLiteral("clip_id"), c.clipId);
        o.insert(QStringLiteral("sequence_id"), c.sequenceId);
        o.insert(QStringLiteral("track_id"), c.trackId);
        o.insert(QStringLiteral("media_ref"), c.mediaRef);
        o.insert(QStringLiteral("name"), c.name);
        o.insert(QStringLiteral("frame_range"), frameRangeToJson(c.frameRange));
        o.insert(QStringLiteral("camera_id"), c.cameraId);
        o.insert(QStringLiteral("audio_gain_db"), c.audioGainDb);
        QJsonArray autoArr;
        for (const AudioLevelPoint &pt : c.audioAutomation) {
            QJsonObject po;
            po.insert(QStringLiteral("frame"), pt.frame);
            po.insert(QStringLiteral("gain_db"), pt.gainDb);
            autoArr.append(po);
        }
        o.insert(QStringLiteral("audio_automation"), autoArr);
        o.insert(QStringLiteral("effects"), effectsToJson(c.effects));
        o.insert(QStringLiteral("intrinsic_keyframes"), keyframeMapToJson(c.intrinsicKeyframes));
        o.insert(QStringLiteral("metadata"), c.metadata);
        clipArr.append(o);
    }
    root.insert(QStringLiteral("clips"), clipArr);

    QJsonArray capArr;
    for (const TimelineCaption &cap : state.captions) {
        QJsonObject o;
        o.insert(QStringLiteral("caption_id"), cap.captionId);
        o.insert(QStringLiteral("sequence_id"), cap.sequenceId);
        o.insert(QStringLiteral("track_id"), cap.trackId);
        o.insert(QStringLiteral("frame_range"), frameRangeToJson(cap.frameRange));
        o.insert(QStringLiteral("text"), cap.text);
        o.insert(QStringLiteral("speaker"), cap.speaker);
        o.insert(QStringLiteral("language"), cap.language);
        o.insert(QStringLiteral("style"), cap.style);
        capArr.append(o);
    }
    root.insert(QStringLiteral("captions"), capArr);

    QJsonArray markerArr;
    for (const TimelineMarker &m : state.markers) {
        QJsonObject o;
        o.insert(QStringLiteral("marker_id"), m.markerId);
        o.insert(QStringLiteral("sequence_id"), m.sequenceId);
        o.insert(QStringLiteral("clip_id"), m.clipId);
        o.insert(QStringLiteral("frame"), m.frame);
        o.insert(QStringLiteral("label"), m.label);
        o.insert(QStringLiteral("category"), m.category);
        o.insert(QStringLiteral("color"), m.color);
        markerArr.append(o);
    }
    root.insert(QStringLiteral("markers"), markerArr);

    QJsonArray camArr;
    for (const CameraSelection &cs : state.cameraSelections) {
        QJsonObject o;
        o.insert(QStringLiteral("selection_id"), cs.selectionId);
        o.insert(QStringLiteral("sequence_id"), cs.sequenceId);
        o.insert(QStringLiteral("track_id"), cs.trackId);
        o.insert(QStringLiteral("clip_id"), cs.clipId);
        o.insert(QStringLiteral("camera_id"), cs.cameraId);
        o.insert(QStringLiteral("timeline_frame"), cs.timelineFrame);
        o.insert(QStringLiteral("metadata"), cs.metadata);
        camArr.append(o);
    }
    root.insert(QStringLiteral("camera_selections"), camArr);

    QJsonArray syncArr;
    for (const SyncGroup &sg : state.syncGroups) {
        QJsonObject o;
        o.insert(QStringLiteral("sync_group_id"), sg.syncGroupId);
        o.insert(QStringLiteral("sequence_id"), sg.sequenceId);
        o.insert(QStringLiteral("method"), sg.method);
        o.insert(QStringLiteral("reference_clip_id"), sg.referenceClipId);
        QJsonObject offsets;
        for (auto it = sg.clipOffsets.cbegin(); it != sg.clipOffsets.cend(); ++it) {
            offsets.insert(it.key(), it.value());
        }
        o.insert(QStringLiteral("clip_offsets"), offsets);
        syncArr.append(o);
    }
    root.insert(QStringLiteral("sync_groups"), syncArr);

    QJsonArray expArr;
    for (const ExportRequestRecord &er : state.exportRequests) {
        QJsonObject o;
        o.insert(QStringLiteral("export_id"), er.exportId);
        o.insert(QStringLiteral("sequence_id"), er.sequenceId);
        o.insert(QStringLiteral("output_path"), er.outputPath);
        o.insert(QStringLiteral("preset"), er.preset);
        o.insert(QStringLiteral("frame_range"), frameRangeToJson(er.frameRange));
        o.insert(QStringLiteral("params"), er.params);
        o.insert(QStringLiteral("status"), er.status);
        expArr.append(o);
    }
    root.insert(QStringLiteral("export_requests"), expArr);

    QJsonArray maskArr;
    for (const TimelineMask &mk : state.masks) {
        QJsonObject o;
        o.insert(QStringLiteral("mask_id"), mk.maskId);
        o.insert(QStringLiteral("sequence_id"), mk.sequenceId);
        o.insert(QStringLiteral("clip_id"), mk.clipId);
        o.insert(QStringLiteral("track_id"), mk.trackId);
        o.insert(QStringLiteral("mask_type"), mk.maskType);
        o.insert(QStringLiteral("mlt_service"), mk.mltService);
        o.insert(QStringLiteral("splines_json"), mk.splinesJson);
        o.insert(QStringLiteral("matte_path"), mk.mattePath);
        o.insert(QStringLiteral("feather"), mk.feather);
        o.insert(QStringLiteral("expand"), mk.expand);
        o.insert(QStringLiteral("invert"), mk.invert);
        o.insert(QStringLiteral("operation"), mk.operation);
        o.insert(QStringLiteral("keyframes"), mk.keyframes);
        o.insert(QStringLiteral("effect_instance_id"), mk.effectInstanceId);
        maskArr.append(o);
    }
    root.insert(QStringLiteral("masks"), maskArr);

    QJsonArray trArr;
    for (const TimelineTransition &tr : state.transitions) {
        QJsonObject o;
        o.insert(QStringLiteral("transition_instance_id"), tr.transitionInstanceId);
        o.insert(QStringLiteral("transition_id"), tr.transitionId);
        o.insert(QStringLiteral("sequence_id"), tr.sequenceId);
        o.insert(QStringLiteral("a_track_id"), tr.aTrackId);
        o.insert(QStringLiteral("b_track_id"), tr.bTrackId);
        o.insert(QStringLiteral("in_frame"), tr.inFrame);
        o.insert(QStringLiteral("out_frame"), tr.outFrame);
        o.insert(QStringLiteral("properties"), tr.properties);
        trArr.append(o);
    }
    root.insert(QStringLiteral("transitions"), trArr);

    QJsonArray ltArr;
    for (const TimelineLowerThird &lt : state.lowerThirds) {
        QJsonObject o;
        o.insert(QStringLiteral("lower_third_id"), lt.lowerThirdId);
        o.insert(QStringLiteral("sequence_id"), lt.sequenceId);
        o.insert(QStringLiteral("track_id"), lt.trackId);
        o.insert(QStringLiteral("clip_id"), lt.clipId);
        o.insert(QStringLiteral("frame_range"), frameRangeToJson(lt.frameRange));
        o.insert(QStringLiteral("speaker_name"), lt.speakerName);
        o.insert(QStringLiteral("subtitle"), lt.subtitle);
        o.insert(QStringLiteral("template_name"), lt.templateName);
        o.insert(QStringLiteral("position"), lt.position);
        o.insert(QStringLiteral("title_xml"), lt.titleXml);
        o.insert(QStringLiteral("style"), lt.style);
        ltArr.append(o);
    }
    root.insert(QStringLiteral("lower_thirds"), ltArr);

    return root;
}

bool InMemoryTimelineBackend::deserializeState(const QJsonObject &obj, State *stateOut, QString *errorOut)
{
    if (stateOut == nullptr) {
        if (errorOut != nullptr) {
            *errorOut = QStringLiteral("Null stateOut pointer");
        }
        return false;
    }

    State s;
    s.activeSequenceId = obj.value(QStringLiteral("active_sequence_id")).toString();

    const QJsonObject counters = obj.value(QStringLiteral("counters")).toObject();
    s.nextMediaId = static_cast<qint64>(counters.value(QStringLiteral("media")).toDouble(1.0));
    s.nextSequenceId = static_cast<qint64>(counters.value(QStringLiteral("sequence")).toDouble(1.0));
    s.nextTrackId = static_cast<qint64>(counters.value(QStringLiteral("track")).toDouble(1.0));
    s.nextClipId = static_cast<qint64>(counters.value(QStringLiteral("clip")).toDouble(1.0));
    s.nextEffectId = static_cast<qint64>(counters.value(QStringLiteral("effect")).toDouble(1.0));
    s.nextCaptionId = static_cast<qint64>(counters.value(QStringLiteral("caption")).toDouble(1.0));
    s.nextMarkerId = static_cast<qint64>(counters.value(QStringLiteral("marker")).toDouble(1.0));
    s.nextCameraSelectionId = static_cast<qint64>(counters.value(QStringLiteral("camera_selection")).toDouble(1.0));
    s.nextSyncGroupId = static_cast<qint64>(counters.value(QStringLiteral("sync_group")).toDouble(1.0));
    s.nextExportId = static_cast<qint64>(counters.value(QStringLiteral("export")).toDouble(1.0));

    for (const QJsonValue &v : obj.value(QStringLiteral("sequences")).toArray()) {
        const QJsonObject o = v.toObject();
        SequenceRecord seq;
        seq.sequenceId = o.value(QStringLiteral("sequence_id")).toString();
        seq.name = o.value(QStringLiteral("name")).toString();
        seq.fpsNum = o.value(QStringLiteral("fps_num")).toInt(25);
        seq.fpsDen = o.value(QStringLiteral("fps_den")).toInt(1);
        seq.width = o.value(QStringLiteral("width")).toInt(1920);
        seq.height = o.value(QStringLiteral("height")).toInt(1080);
        seq.masterGainDb = o.value(QStringLiteral("master_gain_db")).toDouble(0.0);
        seq.masterEffects = effectsFromJson(o.value(QStringLiteral("master_effects")).toArray());
        seq.masterKeyframes = keyframeMapFromJson(o.value(QStringLiteral("master_keyframes")).toObject());
        s.sequences.insert(seq.sequenceId, seq);
    }

    for (const QJsonValue &v : obj.value(QStringLiteral("media_bin")).toArray()) {
        const QJsonObject o = v.toObject();
        MediaAsset m;
        m.mediaRef = o.value(QStringLiteral("media_ref")).toString();
        m.sourcePath = o.value(QStringLiteral("source_path")).toString();
        m.name = o.value(QStringLiteral("name")).toString();
        m.mediaType = o.value(QStringLiteral("media_type")).toString(QStringLiteral("av"));
        m.durationFrames = o.value(QStringLiteral("duration_frames")).toInt(0);
        m.fpsNum = o.value(QStringLiteral("fps_num")).toInt(25);
        m.fpsDen = o.value(QStringLiteral("fps_den")).toInt(1);
        m.metadata = o.value(QStringLiteral("metadata")).toObject();
        s.mediaBin.insert(m.mediaRef, m);
    }

    for (const QJsonValue &v : obj.value(QStringLiteral("tracks")).toArray()) {
        const QJsonObject o = v.toObject();
        TimelineTrack t;
        t.trackId = o.value(QStringLiteral("track_id")).toString();
        t.sequenceId = o.value(QStringLiteral("sequence_id")).toString();
        t.name = o.value(QStringLiteral("name")).toString();
        t.kind = o.value(QStringLiteral("kind")).toString(QStringLiteral("video"));
        t.index = o.value(QStringLiteral("index")).toInt(0);
        t.locked = o.value(QStringLiteral("locked")).toBool(false);
        t.muted = o.value(QStringLiteral("muted")).toBool(false);
        t.audioGainDb = o.value(QStringLiteral("audio_gain_db")).toDouble(0.0);
        t.cameraId = o.value(QStringLiteral("camera_id")).toString();
        t.effects = effectsFromJson(o.value(QStringLiteral("effects")).toArray());
        t.intrinsicKeyframes = keyframeMapFromJson(o.value(QStringLiteral("intrinsic_keyframes")).toObject());
        s.tracks.insert(t.trackId, t);
    }

    for (const QJsonValue &v : obj.value(QStringLiteral("clips")).toArray()) {
        const QJsonObject o = v.toObject();
        TimelineClip c;
        c.clipId = o.value(QStringLiteral("clip_id")).toString();
        c.sequenceId = o.value(QStringLiteral("sequence_id")).toString();
        c.trackId = o.value(QStringLiteral("track_id")).toString();
        c.mediaRef = o.value(QStringLiteral("media_ref")).toString();
        c.name = o.value(QStringLiteral("name")).toString();
        if (!frameRangeFromJson(o.value(QStringLiteral("frame_range")).toObject(), &c.frameRange, errorOut)) {
            return false;
        }
        c.cameraId = o.value(QStringLiteral("camera_id")).toString();
        c.audioGainDb = o.value(QStringLiteral("audio_gain_db")).toDouble(0.0);
        for (const QJsonValue &pv : o.value(QStringLiteral("audio_automation")).toArray()) {
            const QJsonObject po = pv.toObject();
            AudioLevelPoint pt;
            pt.frame = po.value(QStringLiteral("frame")).toInt(0);
            pt.gainDb = po.value(QStringLiteral("gain_db")).toDouble(0.0);
            c.audioAutomation.append(pt);
        }
        c.effects = effectsFromJson(o.value(QStringLiteral("effects")).toArray());
        c.intrinsicKeyframes = keyframeMapFromJson(o.value(QStringLiteral("intrinsic_keyframes")).toObject());
        c.metadata = o.value(QStringLiteral("metadata")).toObject();
        s.clips.insert(c.clipId, c);
    }

    for (const QJsonValue &v : obj.value(QStringLiteral("captions")).toArray()) {
        const QJsonObject o = v.toObject();
        TimelineCaption cap;
        cap.captionId = o.value(QStringLiteral("caption_id")).toString();
        cap.sequenceId = o.value(QStringLiteral("sequence_id")).toString();
        cap.trackId = o.value(QStringLiteral("track_id")).toString();
        if (!frameRangeFromJson(o.value(QStringLiteral("frame_range")).toObject(), &cap.frameRange, errorOut)) {
            return false;
        }
        cap.text = o.value(QStringLiteral("text")).toString();
        cap.speaker = o.value(QStringLiteral("speaker")).toString();
        cap.language = o.value(QStringLiteral("language")).toString(QStringLiteral("en"));
        cap.style = o.value(QStringLiteral("style")).toObject();
        s.captions.insert(cap.captionId, cap);
    }

    for (const QJsonValue &v : obj.value(QStringLiteral("markers")).toArray()) {
        const QJsonObject o = v.toObject();
        TimelineMarker m;
        m.markerId = o.value(QStringLiteral("marker_id")).toString();
        m.sequenceId = o.value(QStringLiteral("sequence_id")).toString();
        m.clipId = o.value(QStringLiteral("clip_id")).toString();
        m.frame = o.value(QStringLiteral("frame")).toInt(0);
        m.label = o.value(QStringLiteral("label")).toString();
        m.category = o.value(QStringLiteral("category")).toString(QStringLiteral("editorial"));
        m.color = o.value(QStringLiteral("color")).toString(QStringLiteral("#3daee9"));
        s.markers.insert(m.markerId, m);
    }

    for (const QJsonValue &v : obj.value(QStringLiteral("camera_selections")).toArray()) {
        const QJsonObject o = v.toObject();
        CameraSelection cs;
        cs.selectionId = o.value(QStringLiteral("selection_id")).toString();
        cs.sequenceId = o.value(QStringLiteral("sequence_id")).toString();
        cs.trackId = o.value(QStringLiteral("track_id")).toString();
        cs.clipId = o.value(QStringLiteral("clip_id")).toString();
        cs.cameraId = o.value(QStringLiteral("camera_id")).toString();
        cs.timelineFrame = o.value(QStringLiteral("timeline_frame")).toInt(0);
        cs.metadata = o.value(QStringLiteral("metadata")).toObject();
        s.cameraSelections.insert(cs.selectionId, cs);
    }

    for (const QJsonValue &v : obj.value(QStringLiteral("sync_groups")).toArray()) {
        const QJsonObject o = v.toObject();
        SyncGroup sg;
        sg.syncGroupId = o.value(QStringLiteral("sync_group_id")).toString();
        sg.sequenceId = o.value(QStringLiteral("sequence_id")).toString();
        sg.method = o.value(QStringLiteral("method")).toString(QStringLiteral("audio_waveform"));
        sg.referenceClipId = o.value(QStringLiteral("reference_clip_id")).toString();
        const QJsonObject offsets = o.value(QStringLiteral("clip_offsets")).toObject();
        for (auto it = offsets.constBegin(); it != offsets.constEnd(); ++it) {
            sg.clipOffsets.insert(it.key(), it.value().toInt());
        }
        s.syncGroups.insert(sg.syncGroupId, sg);
    }

    for (const QJsonValue &v : obj.value(QStringLiteral("export_requests")).toArray()) {
        const QJsonObject o = v.toObject();
        ExportRequestRecord er;
        er.exportId = o.value(QStringLiteral("export_id")).toString();
        er.sequenceId = o.value(QStringLiteral("sequence_id")).toString();
        er.outputPath = o.value(QStringLiteral("output_path")).toString();
        er.preset = o.value(QStringLiteral("preset")).toString();
        if (!frameRangeFromJson(o.value(QStringLiteral("frame_range")).toObject(), &er.frameRange, errorOut)) {
            return false;
        }
        er.params = o.value(QStringLiteral("params")).toObject();
        er.status = o.value(QStringLiteral("status")).toString(QStringLiteral("queued"));
        s.exportRequests.insert(er.exportId, er);
    }

    for (const QJsonValue &v : obj.value(QStringLiteral("masks")).toArray()) {
        const QJsonObject o = v.toObject();
        TimelineMask mk;
        mk.maskId = o.value(QStringLiteral("mask_id")).toString();
        mk.sequenceId = o.value(QStringLiteral("sequence_id")).toString();
        mk.clipId = o.value(QStringLiteral("clip_id")).toString();
        mk.trackId = o.value(QStringLiteral("track_id")).toString();
        mk.maskType = o.value(QStringLiteral("mask_type")).toString(QStringLiteral("spline"));
        mk.mltService = o.value(QStringLiteral("mlt_service")).toString(QStringLiteral("rotoscoping"));
        mk.splinesJson = o.value(QStringLiteral("splines_json")).toString();
        mk.mattePath = o.value(QStringLiteral("matte_path")).toString();
        mk.feather = o.value(QStringLiteral("feather")).toDouble(0.0);
        mk.expand = o.value(QStringLiteral("expand")).toDouble(0.0);
        mk.invert = o.value(QStringLiteral("invert")).toBool(false);
        mk.operation = o.value(QStringLiteral("operation")).toString(QStringLiteral("write_on_clear"));
        mk.keyframes = o.value(QStringLiteral("keyframes")).toArray();
        mk.effectInstanceId = o.value(QStringLiteral("effect_instance_id")).toString();
        s.masks.insert(mk.maskId, mk);
    }

    for (const QJsonValue &v : obj.value(QStringLiteral("transitions")).toArray()) {
        const QJsonObject o = v.toObject();
        TimelineTransition tr;
        tr.transitionInstanceId = o.value(QStringLiteral("transition_instance_id")).toString();
        tr.transitionId = o.value(QStringLiteral("transition_id")).toString();
        tr.sequenceId = o.value(QStringLiteral("sequence_id")).toString();
        tr.aTrackId = o.value(QStringLiteral("a_track_id")).toString();
        tr.bTrackId = o.value(QStringLiteral("b_track_id")).toString();
        tr.inFrame = o.value(QStringLiteral("in_frame")).toInt(0);
        tr.outFrame = o.value(QStringLiteral("out_frame")).toInt(0);
        tr.properties = o.value(QStringLiteral("properties")).toObject();
        s.transitions.insert(tr.transitionInstanceId, tr);
    }

    for (const QJsonValue &v : obj.value(QStringLiteral("lower_thirds")).toArray()) {
        const QJsonObject o = v.toObject();
        TimelineLowerThird lt;
        lt.lowerThirdId = o.value(QStringLiteral("lower_third_id")).toString();
        lt.sequenceId = o.value(QStringLiteral("sequence_id")).toString();
        lt.trackId = o.value(QStringLiteral("track_id")).toString();
        lt.clipId = o.value(QStringLiteral("clip_id")).toString();
        if (!frameRangeFromJson(o.value(QStringLiteral("frame_range")).toObject(), &lt.frameRange, errorOut)) {
            return false;
        }
        lt.speakerName = o.value(QStringLiteral("speaker_name")).toString();
        lt.subtitle = o.value(QStringLiteral("subtitle")).toString();
        lt.templateName = o.value(QStringLiteral("template_name")).toString(QStringLiteral("modern_clean"));
        lt.position = o.value(QStringLiteral("position")).toString(QStringLiteral("bottom_left"));
        lt.titleXml = o.value(QStringLiteral("title_xml")).toString();
        lt.style = o.value(QStringLiteral("style")).toObject();
        s.lowerThirds.insert(lt.lowerThirdId, lt);
    }

    *stateOut = std::move(s);
    return true;
}

QString InMemoryTimelineBackend::activeSequenceId() const
{
    return m_state.activeSequenceId;
}

bool InMemoryTimelineBackend::hasSequence(const QString &sequenceId) const
{
    return m_state.sequences.contains(sequenceId);
}

bool InMemoryTimelineBackend::hasMedia(const QString &mediaRef) const
{
    return m_state.mediaBin.contains(mediaRef);
}

bool InMemoryTimelineBackend::hasTrack(const QString &trackId) const
{
    return m_state.tracks.contains(trackId);
}

bool InMemoryTimelineBackend::hasClip(const QString &clipId) const
{
    return m_state.clips.contains(clipId);
}

std::optional<SequenceRecord> InMemoryTimelineBackend::sequence(const QString &sequenceId) const
{
    const auto it = m_state.sequences.constFind(sequenceId);
    if (it == m_state.sequences.cend()) {
        return std::nullopt;
    }
    return *it;
}

std::optional<MediaAsset> InMemoryTimelineBackend::media(const QString &mediaRef) const
{
    const auto it = m_state.mediaBin.constFind(mediaRef);
    if (it == m_state.mediaBin.cend()) {
        return std::nullopt;
    }
    return *it;
}

std::optional<TimelineTrack> InMemoryTimelineBackend::track(const QString &trackId) const
{
    const auto it = m_state.tracks.constFind(trackId);
    if (it == m_state.tracks.cend()) {
        return std::nullopt;
    }
    return *it;
}

std::optional<TimelineClip> InMemoryTimelineBackend::clip(const QString &clipId) const
{
    const auto it = m_state.clips.constFind(clipId);
    if (it == m_state.clips.cend()) {
        return std::nullopt;
    }
    return *it;
}

QVector<SequenceRecord> InMemoryTimelineBackend::sequences() const
{
    QVector<SequenceRecord> out;
    out.reserve(m_state.sequences.size());
    for (const SequenceRecord &item : m_state.sequences) {
        out.append(item);
    }
    return out;
}

QVector<MediaAsset> InMemoryTimelineBackend::mediaAssets() const
{
    QVector<MediaAsset> out;
    out.reserve(m_state.mediaBin.size());
    for (const MediaAsset &item : m_state.mediaBin) {
        out.append(item);
    }
    return out;
}

QVector<TimelineTrack> InMemoryTimelineBackend::tracks() const
{
    QVector<TimelineTrack> out;
    out.reserve(m_state.tracks.size());
    for (const TimelineTrack &item : m_state.tracks) {
        out.append(item);
    }
    return out;
}

QVector<TimelineClip> InMemoryTimelineBackend::clips() const
{
    QVector<TimelineClip> out;
    out.reserve(m_state.clips.size());
    for (const TimelineClip &item : m_state.clips) {
        out.append(item);
    }
    return out;
}

QVector<TimelineCaption> InMemoryTimelineBackend::captions() const
{
    QVector<TimelineCaption> out;
    out.reserve(m_state.captions.size());
    for (const TimelineCaption &item : m_state.captions) {
        out.append(item);
    }
    return out;
}

QVector<TimelineMarker> InMemoryTimelineBackend::markers() const
{
    QVector<TimelineMarker> out;
    out.reserve(m_state.markers.size());
    for (const TimelineMarker &item : m_state.markers) {
        out.append(item);
    }
    return out;
}

QVector<CameraSelection> InMemoryTimelineBackend::cameraSelections() const
{
    QVector<CameraSelection> out;
    out.reserve(m_state.cameraSelections.size());
    for (const CameraSelection &item : m_state.cameraSelections) {
        out.append(item);
    }
    return out;
}

QVector<SyncGroup> InMemoryTimelineBackend::syncGroups() const
{
    QVector<SyncGroup> out;
    out.reserve(m_state.syncGroups.size());
    for (const SyncGroup &item : m_state.syncGroups) {
        out.append(item);
    }
    return out;
}

QVector<ExportRequestRecord> InMemoryTimelineBackend::exportRequests() const
{
    QVector<ExportRequestRecord> out;
    out.reserve(m_state.exportRequests.size());
    for (const ExportRequestRecord &item : m_state.exportRequests) {
        out.append(item);
    }
    return out;
}

// ============================================================================
// TimelineCommandEngine
// ============================================================================

TimelineCommandEngine::TimelineCommandEngine(std::shared_ptr<ITimelineBackend> backend, QObject *parent)
    : QObject(parent)
    , m_backend(backend ? std::move(backend) : std::make_shared<InMemoryTimelineBackend>())
{
}

std::shared_ptr<ITimelineBackend> TimelineCommandEngine::backend() const
{
    RecursiveMutexLocker locker(&m_writeMutex);
    return m_backend;
}

qint64 TimelineCommandEngine::currentRevision() const
{
    RecursiveMutexLocker locker(&m_writeMutex);
    return m_revision;
}

bool TimelineCommandEngine::canUndo() const
{
    RecursiveMutexLocker locker(&m_writeMutex);
    return !m_undoStack.isEmpty() || (m_backend && m_backend->canNativeUndo());
}

bool TimelineCommandEngine::canRedo() const
{
    RecursiveMutexLocker locker(&m_writeMutex);
    return !m_redoStack.isEmpty() || (m_backend && m_backend->canNativeRedo());
}

int TimelineCommandEngine::undoDepth() const
{
    RecursiveMutexLocker locker(&m_writeMutex);
    return m_undoStack.size();
}

int TimelineCommandEngine::redoDepth() const
{
    RecursiveMutexLocker locker(&m_writeMutex);
    return m_redoStack.size();
}

QStringList TimelineCommandEngine::supportedOperationTypes()
{
    static const QStringList kOps = {
        QStringLiteral("import_media"),   QStringLiteral("sync_clips"),       QStringLiteral("create_sequence"),
        QStringLiteral("create_track"),   QStringLiteral("place_clip"),       QStringLiteral("move_clip"),
        QStringLiteral("trim_clip"),      QStringLiteral("split_clip"),       QStringLiteral("delete_clip"),
        QStringLiteral("select_camera"),  QStringLiteral("add_effect"),       QStringLiteral("set_effect_param"),
        QStringLiteral("set_keyframe"),   QStringLiteral("set_audio_level"),  QStringLiteral("add_audio_filter"),
        QStringLiteral("add_caption"),    QStringLiteral("update_caption"),   QStringLiteral("delete_caption"),
        QStringLiteral("add_marker"),     QStringLiteral("apply_mask"),       QStringLiteral("add_transition"),
        QStringLiteral("add_lower_third"), QStringLiteral("set_clip_speed"),  QStringLiteral("undo"),
        QStringLiteral("redo"),           QStringLiteral("export_sequence"),
    };
    return kOps;
}

bool TimelineCommandEngine::isSupportedOperationType(const QString &type)
{
    static const QSet<QString> kOpSet = []() {
        QSet<QString> set;
        for (const QString &op : supportedOperationTypes()) {
            set.insert(op);
        }
        return set;
    }();
    return kOpSet.contains(type);
}

bool TimelineCommandEngine::validateBatch(const TimelineCommandBatch &batch, QString *errorMessageOut)
{
    auto fail = [errorMessageOut](const QString &msg) {
        if (errorMessageOut != nullptr) {
            *errorMessageOut = msg;
        }
        return false;
    };

    if (batch.apiVersion != QLatin1String(kTimelineApiVersion)) {
        return fail(QStringLiteral("Unsupported api_version '%1' (expected '%2')").arg(batch.apiVersion, QLatin1String(kTimelineApiVersion)));
    }
    if (batch.requestId.trimmed().isEmpty()) {
        return fail(QStringLiteral("request_id must be a non-empty string"));
    }
    if (batch.projectId.trimmed().isEmpty()) {
        return fail(QStringLiteral("project_id must be a non-empty string"));
    }
    if (batch.sequenceId.trimmed().isEmpty()) {
        return fail(QStringLiteral("sequence_id must be a non-empty string"));
    }
    if (batch.expectedRevision < 0) {
        return fail(QStringLiteral("expected_revision must be >= 0"));
    }
    if (batch.agentId.trimmed().isEmpty()) {
        return fail(QStringLiteral("actor.agent_id must be a non-empty string"));
    }
    if (batch.agentRole.trimmed().isEmpty()) {
        return fail(QStringLiteral("actor.role must be a non-empty string"));
    }
    if (batch.operations.isEmpty()) {
        return fail(QStringLiteral("operations array must contain at least one operation"));
    }

    QSet<QString> seenOpIds;
    bool hasHistoryOp = false;
    for (int i = 0; i < batch.operations.size(); ++i) {
        const TimelineOperation &op = batch.operations.at(i);
        if (op.opId.trimmed().isEmpty()) {
            return fail(QStringLiteral("operations[%1].op_id must be a non-empty string").arg(i));
        }
        if (seenOpIds.contains(op.opId)) {
            return fail(QStringLiteral("Duplicate op_id '%1' in batch").arg(op.opId));
        }
        seenOpIds.insert(op.opId);

        if (!isSupportedOperationType(op.type)) {
            return fail(QStringLiteral("Unknown operation type '%1' in op '%2'").arg(op.type, op.opId));
        }

        if (op.type == QStringLiteral("undo") || op.type == QStringLiteral("redo")) {
            hasHistoryOp = true;
        }

        if (!std::isfinite(op.confidence) || op.confidence < 0.0 || op.confidence > 1.0) {
            return fail(QStringLiteral("Operation '%1' confidence must be within [0.0, 1.0]").arg(op.opId));
        }

        const FrameRange &fr = op.frameRange;
        if (fr.inFrame < 0 || fr.outFrame < 0 || fr.timelineStart < 0) {
            return fail(QStringLiteral("Operation '%1' frame_range values (in_frame, out_frame, timeline_start) must be >= 0").arg(op.opId));
        }
        if (fr.fpsNum < 1 || fr.fpsDen < 1) {
            return fail(QStringLiteral("Operation '%1' frame_range fps_num and fps_den must be >= 1").arg(op.opId));
        }
        if (fr.outFrame < fr.inFrame) {
            return fail(QStringLiteral("Operation '%1' frame_range has out_frame (%2) < in_frame (%3)").arg(op.opId).arg(fr.outFrame).arg(fr.inFrame));
        }

        if (op.type == QStringLiteral("trim_clip") && (fr.inFrame > 0 || fr.outFrame > 0) && fr.outFrame <= fr.inFrame) {
            return fail(QStringLiteral("trim_clip '%1' requires out_frame > in_frame").arg(op.opId));
        }
        if ((op.type == QStringLiteral("move_clip") || op.type == QStringLiteral("trim_clip") || op.type == QStringLiteral("split_clip") ||
             op.type == QStringLiteral("delete_clip")) &&
            op.clipId.trimmed().isEmpty()) {
            return fail(QStringLiteral("Operation '%1' (%2) requires non-empty clip_id").arg(op.opId, op.type));
        }
    }

    if (hasHistoryOp && batch.operations.size() > 1) {
        return fail(QStringLiteral("undo/redo operations cannot be mixed with other operations in the same batch"));
    }

    return true;
}

bool TimelineCommandEngine::batchFromJson(const QJsonObject &obj, TimelineCommandBatch *batchOut, QString *errorMessageOut)
{
    auto fail = [errorMessageOut](const QString &msg) {
        if (errorMessageOut != nullptr) {
            *errorMessageOut = msg;
        }
        return false;
    };

    if (batchOut == nullptr) {
        return fail(QStringLiteral("batchOut pointer must not be null"));
    }

    TimelineCommandBatch batch;

    if (!obj.contains(QStringLiteral("api_version")) || !obj.value(QStringLiteral("api_version")).isString()) {
        return fail(QStringLiteral("Missing or non-string required field 'api_version'"));
    }
    batch.apiVersion = obj.value(QStringLiteral("api_version")).toString();

    if (!obj.contains(QStringLiteral("request_id")) || !obj.value(QStringLiteral("request_id")).isString()) {
        return fail(QStringLiteral("Missing or non-string required field 'request_id'"));
    }
    batch.requestId = obj.value(QStringLiteral("request_id")).toString();

    if (!obj.contains(QStringLiteral("project_id")) || !obj.value(QStringLiteral("project_id")).isString()) {
        return fail(QStringLiteral("Missing or non-string required field 'project_id'"));
    }
    batch.projectId = obj.value(QStringLiteral("project_id")).toString();

    if (!obj.contains(QStringLiteral("sequence_id")) || !obj.value(QStringLiteral("sequence_id")).isString()) {
        return fail(QStringLiteral("Missing or non-string required field 'sequence_id'"));
    }
    batch.sequenceId = obj.value(QStringLiteral("sequence_id")).toString();

    if (!obj.contains(QStringLiteral("expected_revision")) ||
        !jsonValueIsStrictInteger(obj.value(QStringLiteral("expected_revision")), &batch.expectedRevision)) {
        return fail(QStringLiteral("Missing or non-integer required field 'expected_revision'"));
    }

    if (!obj.contains(QStringLiteral("actor")) || !obj.value(QStringLiteral("actor")).isObject()) {
        return fail(QStringLiteral("Missing or non-object required field 'actor'"));
    }
    const QJsonObject actorObj = obj.value(QStringLiteral("actor")).toObject();
    if (!actorObj.contains(QStringLiteral("agent_id")) || !actorObj.value(QStringLiteral("agent_id")).isString()) {
        return fail(QStringLiteral("Missing or non-string required field 'actor.agent_id'"));
    }
    if (!actorObj.contains(QStringLiteral("role")) || !actorObj.value(QStringLiteral("role")).isString()) {
        return fail(QStringLiteral("Missing or non-string required field 'actor.role'"));
    }
    batch.agentId = actorObj.value(QStringLiteral("agent_id")).toString();
    batch.agentRole = actorObj.value(QStringLiteral("role")).toString();
    if (actorObj.contains(QStringLiteral("requires_approval"))) {
        if (!actorObj.value(QStringLiteral("requires_approval")).isBool()) {
            return fail(QStringLiteral("Field 'actor.requires_approval' must be a boolean"));
        }
        batch.requiresApproval = actorObj.value(QStringLiteral("requires_approval")).toBool();
    } else {
        batch.requiresApproval = true;
    }

    if (obj.contains(QStringLiteral("transaction_label"))) {
        if (!obj.value(QStringLiteral("transaction_label")).isString()) {
            return fail(QStringLiteral("Field 'transaction_label' must be a string"));
        }
        batch.transactionLabel = obj.value(QStringLiteral("transaction_label")).toString();
    }

    if (obj.contains(QStringLiteral("dry_run"))) {
        if (!obj.value(QStringLiteral("dry_run")).isBool()) {
            return fail(QStringLiteral("Field 'dry_run' must be a boolean"));
        }
        batch.dryRun = obj.value(QStringLiteral("dry_run")).toBool();
    }

    if (!obj.contains(QStringLiteral("operations")) || !obj.value(QStringLiteral("operations")).isArray()) {
        return fail(QStringLiteral("Missing or non-array required field 'operations'"));
    }
    const QJsonArray opsArr = obj.value(QStringLiteral("operations")).toArray();
    batch.operations.reserve(opsArr.size());

    for (int i = 0; i < opsArr.size(); ++i) {
        if (!opsArr.at(i).isObject()) {
            return fail(QStringLiteral("operations[%1] must be a JSON object").arg(i));
        }
        const QJsonObject opObj = opsArr.at(i).toObject();
        TimelineOperation op;

        if (!opObj.contains(QStringLiteral("op_id")) || !opObj.value(QStringLiteral("op_id")).isString()) {
            return fail(QStringLiteral("operations[%1].op_id must be a string").arg(i));
        }
        op.opId = opObj.value(QStringLiteral("op_id")).toString();

        if (!opObj.contains(QStringLiteral("type")) || !opObj.value(QStringLiteral("type")).isString()) {
            return fail(QStringLiteral("operations[%1].type must be a string").arg(i));
        }
        op.type = opObj.value(QStringLiteral("type")).toString();

        if (opObj.contains(QStringLiteral("media_ref"))) {
            if (!opObj.value(QStringLiteral("media_ref")).isString()) {
                return fail(QStringLiteral("operations[%1].media_ref must be a string").arg(i));
            }
            op.mediaRef = opObj.value(QStringLiteral("media_ref")).toString();
        }
        if (opObj.contains(QStringLiteral("clip_id"))) {
            if (!opObj.value(QStringLiteral("clip_id")).isString()) {
                return fail(QStringLiteral("operations[%1].clip_id must be a string").arg(i));
            }
            op.clipId = opObj.value(QStringLiteral("clip_id")).toString();
        }
        if (opObj.contains(QStringLiteral("track_id"))) {
            if (!opObj.value(QStringLiteral("track_id")).isString()) {
                return fail(QStringLiteral("operations[%1].track_id must be a string").arg(i));
            }
            op.trackId = opObj.value(QStringLiteral("track_id")).toString();
        }
        if (opObj.contains(QStringLiteral("frame_range"))) {
            if (!opObj.value(QStringLiteral("frame_range")).isObject()) {
                return fail(QStringLiteral("operations[%1].frame_range must be a JSON object").arg(i));
            }
            QString frErr;
            if (!frameRangeFromJson(opObj.value(QStringLiteral("frame_range")).toObject(), &op.frameRange, &frErr)) {
                return fail(QStringLiteral("operations[%1]: %2").arg(i).arg(frErr));
            }
        }
        if (opObj.contains(QStringLiteral("params"))) {
            if (!opObj.value(QStringLiteral("params")).isObject()) {
                return fail(QStringLiteral("operations[%1].params must be a JSON object").arg(i));
            }
            op.params = opObj.value(QStringLiteral("params")).toObject();
        }
        if (opObj.contains(QStringLiteral("confidence"))) {
            if (!opObj.value(QStringLiteral("confidence")).isDouble()) {
                return fail(QStringLiteral("operations[%1].confidence must be a number").arg(i));
            }
            op.confidence = opObj.value(QStringLiteral("confidence")).toDouble();
        }
        if (opObj.contains(QStringLiteral("rationale"))) {
            if (!opObj.value(QStringLiteral("rationale")).isString()) {
                return fail(QStringLiteral("operations[%1].rationale must be a string").arg(i));
            }
            op.rationale = opObj.value(QStringLiteral("rationale")).toString();
        }

        batch.operations.append(op);
    }

    *batchOut = std::move(batch);
    return true;
}

QJsonObject TimelineCommandEngine::batchToJson(const TimelineCommandBatch &batch)
{
    QJsonObject obj;
    obj.insert(QStringLiteral("api_version"), batch.apiVersion);
    obj.insert(QStringLiteral("request_id"), batch.requestId);
    obj.insert(QStringLiteral("project_id"), batch.projectId);
    obj.insert(QStringLiteral("sequence_id"), batch.sequenceId);
    obj.insert(QStringLiteral("expected_revision"), batch.expectedRevision);

    QJsonObject actor;
    actor.insert(QStringLiteral("agent_id"), batch.agentId);
    actor.insert(QStringLiteral("role"), batch.agentRole);
    actor.insert(QStringLiteral("requires_approval"), batch.requiresApproval);
    obj.insert(QStringLiteral("actor"), actor);

    obj.insert(QStringLiteral("transaction_label"), batch.transactionLabel);
    obj.insert(QStringLiteral("dry_run"), batch.dryRun);

    QJsonArray opsArr;
    for (const TimelineOperation &op : batch.operations) {
        QJsonObject opObj;
        opObj.insert(QStringLiteral("op_id"), op.opId);
        opObj.insert(QStringLiteral("type"), op.type);
        if (!op.mediaRef.isEmpty()) {
            opObj.insert(QStringLiteral("media_ref"), op.mediaRef);
        }
        if (!op.clipId.isEmpty()) {
            opObj.insert(QStringLiteral("clip_id"), op.clipId);
        }
        if (!op.trackId.isEmpty()) {
            opObj.insert(QStringLiteral("track_id"), op.trackId);
        }
        opObj.insert(QStringLiteral("frame_range"), frameRangeToJson(op.frameRange));
        if (!op.params.isEmpty()) {
            opObj.insert(QStringLiteral("params"), op.params);
        }
        opObj.insert(QStringLiteral("confidence"), op.confidence);
        if (!op.rationale.isEmpty()) {
            opObj.insert(QStringLiteral("rationale"), op.rationale);
        }
        opsArr.append(opObj);
    }
    obj.insert(QStringLiteral("operations"), opsArr);
    return obj;
}

QJsonObject TimelineCommandEngine::responseToJson(const TimelineCommandResponse &response)
{
    QJsonObject obj;
    obj.insert(QStringLiteral("api_version"), response.apiVersion);
    obj.insert(QStringLiteral("request_id"), response.requestId);
    obj.insert(QStringLiteral("status"), commandStatusToString(response.status));
    obj.insert(QStringLiteral("previous_revision"), response.previousRevision);
    obj.insert(QStringLiteral("current_revision"), response.currentRevision);
    if (!response.errorMessage.isEmpty()) {
        obj.insert(QStringLiteral("error_message"), response.errorMessage);
    }

    QJsonArray resultsArr;
    for (const TimelineOperationResult &r : response.results) {
        QJsonObject ro;
        ro.insert(QStringLiteral("op_id"), r.opId);
        ro.insert(QStringLiteral("applied"), r.applied);
        if (!r.createdId.isEmpty()) {
            ro.insert(QStringLiteral("created_id"), r.createdId);
        }
        if (!r.message.isEmpty()) {
            ro.insert(QStringLiteral("message"), r.message);
        }
        if (!r.metadata.isEmpty()) {
            ro.insert(QStringLiteral("metadata"), r.metadata);
        }
        resultsArr.append(ro);
    }
    obj.insert(QStringLiteral("results"), resultsArr);
    return obj;
}

bool TimelineCommandEngine::responseFromJson(const QJsonObject &obj, TimelineCommandResponse *responseOut, QString *errorMessageOut)
{
    auto fail = [errorMessageOut](const QString &msg) {
        if (errorMessageOut != nullptr) {
            *errorMessageOut = msg;
        }
        return false;
    };

    if (responseOut == nullptr) {
        return fail(QStringLiteral("responseOut pointer must not be null"));
    }

    TimelineCommandResponse res;
    res.apiVersion = obj.value(QStringLiteral("api_version")).toString(QString::fromLatin1(kTimelineApiVersion));
    res.requestId = obj.value(QStringLiteral("request_id")).toString();
    if (!commandStatusFromString(obj.value(QStringLiteral("status")).toString(), &res.status)) {
        return fail(QStringLiteral("Invalid or missing response status"));
    }
    res.previousRevision = static_cast<qint64>(obj.value(QStringLiteral("previous_revision")).toDouble(0.0));
    res.currentRevision = static_cast<qint64>(obj.value(QStringLiteral("current_revision")).toDouble(0.0));
    res.errorMessage = obj.value(QStringLiteral("error_message")).toString();

    for (const QJsonValue &v : obj.value(QStringLiteral("results")).toArray()) {
        const QJsonObject ro = v.toObject();
        TimelineOperationResult r;
        r.opId = ro.value(QStringLiteral("op_id")).toString();
        r.applied = ro.value(QStringLiteral("applied")).toBool(false);
        r.createdId = ro.value(QStringLiteral("created_id")).toString();
        r.message = ro.value(QStringLiteral("message")).toString();
        r.metadata = ro.value(QStringLiteral("metadata")).toObject();
        res.results.append(r);
    }

    *responseOut = std::move(res);
    return true;
}

TimelineCommandResponse TimelineCommandEngine::executeBatch(const TimelineCommandBatch &batch)
{
    // Phase 1: Stateless contract validation runs outside the writer mutex so concurrent
    // callers can validate payloads without serializing against timeline mutations.
    QString validationErr;
    if (!validateBatch(batch, &validationErr)) {
        RecursiveMutexLocker locker(&m_writeMutex);
        TimelineCommandResponse response;
        response.apiVersion = QString::fromLatin1(kTimelineApiVersion);
        response.requestId = batch.requestId;
        response.status = CommandStatus::ValidationError;
        response.previousRevision = m_revision;
        response.currentRevision = m_revision;
        response.errorMessage = validationErr;
        appendActivityLogLocked(batch, response);
        Q_EMIT activityLogUpdated();
        return response;
    }

    // Phase 2: Acquire single authorized writer lock for concurrency check and execution.
    RecursiveMutexLocker locker(&m_writeMutex);
    return executeBatchLocked(batch, false);
}

QByteArray TimelineCommandEngine::executeJson(const QByteArray &requestJson)
{
    QJsonParseError parseError;
    const QJsonDocument doc = QJsonDocument::fromJson(requestJson, &parseError);
    if (parseError.error != QJsonParseError::NoError || !doc.isObject()) {
        TimelineCommandResponse errResp;
        errResp.apiVersion = QString::fromLatin1(kTimelineApiVersion);
        errResp.status = CommandStatus::ValidationError;
        {
            RecursiveMutexLocker locker(&m_writeMutex);
            errResp.previousRevision = m_revision;
            errResp.currentRevision = m_revision;
        }
        errResp.errorMessage = parseError.error != QJsonParseError::NoError
                                   ? QStringLiteral("Malformed JSON payload: %1 (offset %2)").arg(parseError.errorString()).arg(parseError.offset)
                                   : QStringLiteral("JSON payload root must be an object");
        return QJsonDocument(responseToJson(errResp)).toJson(QJsonDocument::Compact);
    }

    const QJsonObject rootObj = doc.object();
    TimelineCommandBatch batch;
    QString schemaErr;
    if (!batchFromJson(rootObj, &batch, &schemaErr)) {
        TimelineCommandResponse errResp;
        errResp.apiVersion = QString::fromLatin1(kTimelineApiVersion);
        errResp.requestId = rootObj.value(QStringLiteral("request_id")).toString();
        errResp.status = CommandStatus::ValidationError;
        {
            RecursiveMutexLocker locker(&m_writeMutex);
            errResp.previousRevision = m_revision;
            errResp.currentRevision = m_revision;
        }
        errResp.errorMessage = schemaErr;
        return QJsonDocument(responseToJson(errResp)).toJson(QJsonDocument::Compact);
    }

    const TimelineCommandResponse response = executeBatch(batch);
    return QJsonDocument(responseToJson(response)).toJson(QJsonDocument::Compact);
}

TimelineCommandResponse TimelineCommandEngine::executeBatchLocked(const TimelineCommandBatch &batch, bool bypassApprovalGate)
{
    TimelineCommandResponse response;
    response.apiVersion = QString::fromLatin1(kTimelineApiVersion);
    response.requestId = batch.requestId;
    response.previousRevision = m_revision;
    response.currentRevision = m_revision;

    if (!m_backend) {
        response.status = CommandStatus::ExecutionFailed;
        response.errorMessage = QStringLiteral("No timeline backend configured");
        appendActivityLogLocked(batch, response);
        Q_EMIT activityLogUpdated();
        return response;
    }

    // Optimistic concurrency conflict detection.
    if (batch.expectedRevision != m_revision) {
        response.status = CommandStatus::RevisionConflict;
        response.errorMessage =
            QStringLiteral("Revision conflict: expected revision %1, but current revision is %2").arg(batch.expectedRevision).arg(m_revision);
        appendActivityLogLocked(batch, response);
        Q_EMIT activityLogUpdated();
        return response;
    }

    // Approval staging gate (non-dry-run batches requiring user approval).
    if (batch.requiresApproval && !bypassApprovalGate && !batch.dryRun) {
        for (const PendingProposal &existing : m_pendingProposals) {
            if (existing.batch.requestId == batch.requestId) {
                response.status = CommandStatus::ValidationError;
                response.errorMessage = QStringLiteral("Pending proposal with request_id '%1' already exists").arg(batch.requestId);
                appendActivityLogLocked(batch, response);
                Q_EMIT activityLogUpdated();
                return response;
            }
        }

        PendingProposal proposal;
        proposal.batch = batch;
        proposal.stagedAtRevision = m_revision;
        proposal.stagedAtUtc = currentUtcTimestamp();
        proposal.proposedDiff = computeProposalDiffLocked(batch, &proposal.previewResults);
        m_pendingProposals.append(proposal);

        response.status = CommandStatus::PendingApproval;
        response.results.reserve(batch.operations.size());
        for (const TimelineOperation &op : batch.operations) {
            TimelineOperationResult r;
            r.opId = op.opId;
            r.applied = false;
            r.message = QStringLiteral("Staged for user approval");
            r.metadata.insert(QStringLiteral("type"), op.type);
            r.metadata.insert(QStringLiteral("staged"), true);
            response.results.append(r);
        }

        appendActivityLogLocked(batch, response, QStringLiteral("Proposal staged for user approval"));
        Q_EMIT proposalStaged(batch.requestId, batch.transactionLabel);
        Q_EMIT activityLogUpdated();
        return response;
    }

    // Dedicated handling for single-operation history commands ("undo" / "redo").
    if (batch.operations.size() == 1 && (batch.operations.first().type == QStringLiteral("undo") || batch.operations.first().type == QStringLiteral("redo"))) {
        const TimelineOperation &op = batch.operations.first();
        const bool isUndo = (op.type == QStringLiteral("undo"));
        if (batch.dryRun) {
            const bool available = isUndo ? canUndo() : canRedo();
            TimelineOperationResult opRes;
            opRes.opId = op.opId;
            opRes.applied = available;
            opRes.message = available ? QStringLiteral("Dry-run %1 valid").arg(op.type) : QStringLiteral("No transaction available to %1").arg(op.type);
            opRes.metadata.insert(QStringLiteral("dry_run"), true);
            response.results.append(opRes);
            response.status = available ? CommandStatus::Ok : CommandStatus::ExecutionFailed;
            if (!available) {
                response.errorMessage = opRes.message;
            }
            appendActivityLogLocked(batch, response);
            Q_EMIT activityLogUpdated();
            return response;
        }
        TimelineCommandResponse histResp = isUndo ? undoLastTransaction(batch.agentId) : redoLastTransaction(batch.agentId);
        histResp.requestId = batch.requestId;
        if (histResp.results.isEmpty()) {
            TimelineOperationResult opRes;
            opRes.opId = op.opId;
            opRes.applied = (histResp.status == CommandStatus::Ok);
            opRes.message = histResp.status == CommandStatus::Ok ? QStringLiteral("Executed %1").arg(op.type) : histResp.errorMessage;
            histResp.results.append(opRes);
        } else {
            histResp.results[0].opId = op.opId;
        }
        return histResp;
    }

    // Begin atomic backend transaction.
    QJsonObject beforeSnapshot;
    const bool useSnapshots = m_backend->supportsSnapshots();
    if (useSnapshots) {
        beforeSnapshot = m_backend->captureStateSnapshot();
    }

    QString txErr;
    if (!m_backend->beginTransaction(batch.transactionLabel, &txErr)) {
        response.status = CommandStatus::ExecutionFailed;
        response.errorMessage = txErr.isEmpty() ? QStringLiteral("Failed to begin backend transaction") : txErr;
        appendActivityLogLocked(batch, response);
        Q_EMIT activityLogUpdated();
        return response;
    }

    response.results.reserve(batch.operations.size());
    bool failed = false;
    QString failureMessage;
    for (const TimelineOperation &op : batch.operations) {
        TimelineOperationResult opRes = m_backend->executeOperation(op, batch.sequenceId);
        if (opRes.opId.isEmpty()) {
            opRes.opId = op.opId;
        }
        response.results.append(opRes);
        if (!opRes.applied) {
            failed = true;
            failureMessage = QStringLiteral("Operation '%1' (%2) failed: %3").arg(op.opId, op.type, opRes.message);
            break;
        }
    }

    if (failed) {
        QString rbErr;
        const bool rolledBack = m_backend->rollbackTransaction(&rbErr);
        for (int i = 0; i < response.results.size(); ++i) {
            if (response.results[i].applied) {
                response.results[i].applied = false;
                response.results[i].metadata.insert(QStringLiteral("rolled_back"), true);
            }
        }
        response.status = rolledBack ? CommandStatus::RolledBack : CommandStatus::ExecutionFailed;
        response.previousRevision = m_revision;
        response.currentRevision = m_revision;
        response.errorMessage = rolledBack ? failureMessage : QStringLiteral("%1 (rollback error: %2)").arg(failureMessage, rbErr);
        appendActivityLogLocked(batch, response);
        Q_EMIT transactionRolledBack(batch.requestId, response.errorMessage);
        Q_EMIT activityLogUpdated();
        return response;
    }

    if (batch.dryRun) {
        QString rbErr;
        if (!m_backend->rollbackTransaction(&rbErr)) {
            response.status = CommandStatus::ExecutionFailed;
            response.errorMessage = QStringLiteral("Dry-run cleanup rollback failed: %1").arg(rbErr);
            appendActivityLogLocked(batch, response);
            Q_EMIT activityLogUpdated();
            return response;
        }
        for (TimelineOperationResult &r : response.results) {
            r.metadata.insert(QStringLiteral("dry_run"), true);
        }
        response.status = CommandStatus::Ok;
        response.previousRevision = m_revision;
        response.currentRevision = m_revision;
        appendActivityLogLocked(batch, response, QStringLiteral("Dry-run validation succeeded"));
        Q_EMIT activityLogUpdated();
        return response;
    }

    QString commitErr;
    if (!m_backend->commitTransaction(batch.transactionLabel, &commitErr)) {
        QString rbErr;
        m_backend->rollbackTransaction(&rbErr);
        for (TimelineOperationResult &r : response.results) {
            r.applied = false;
            r.metadata.insert(QStringLiteral("rolled_back"), true);
        }
        response.status = CommandStatus::RolledBack;
        response.previousRevision = m_revision;
        response.currentRevision = m_revision;
        response.errorMessage = commitErr.isEmpty() ? QStringLiteral("Backend commit failed") : commitErr;
        appendActivityLogLocked(batch, response);
        Q_EMIT transactionRolledBack(batch.requestId, response.errorMessage);
        Q_EMIT activityLogUpdated();
        return response;
    }

    // Commit succeeded: advance revision and push undo record.
    const qint64 prevRev = m_revision;
    ++m_revision;

    UndoRecord undoRec;
    undoRec.requestId = batch.requestId;
    undoRec.projectId = batch.projectId;
    undoRec.sequenceId = batch.sequenceId;
    undoRec.agentId = batch.agentId;
    undoRec.agentRole = batch.agentRole;
    undoRec.transactionLabel = batch.transactionLabel;
    undoRec.usesSnapshot = useSnapshots;
    if (useSnapshots) {
        undoRec.beforeSnapshot = beforeSnapshot;
        undoRec.afterSnapshot = m_backend->captureStateSnapshot();
    }
    m_undoStack.append(std::move(undoRec));
    m_redoStack.clear();

    response.status = CommandStatus::Ok;
    response.previousRevision = prevRev;
    response.currentRevision = m_revision;

    appendActivityLogLocked(batch, response);
    Q_EMIT revisionChanged(m_revision);
    Q_EMIT transactionCommitted(batch.requestId, m_revision, batch.transactionLabel);
    Q_EMIT activityLogUpdated();
    return response;
}

QJsonObject TimelineCommandEngine::computeProposalDiffLocked(const TimelineCommandBatch &batch, QVector<TimelineOperationResult> *previewResultsOut)
{
    QJsonObject diff;
    diff.insert(QStringLiteral("operation_count"), batch.operations.size());
    diff.insert(QStringLiteral("transaction_label"), batch.transactionLabel);

    QJsonArray opSummaries;
    for (const TimelineOperation &op : batch.operations) {
        QJsonObject s;
        s.insert(QStringLiteral("op_id"), op.opId);
        s.insert(QStringLiteral("type"), op.type);
        if (!op.trackId.isEmpty()) {
            s.insert(QStringLiteral("track_id"), op.trackId);
        }
        if (!op.clipId.isEmpty()) {
            s.insert(QStringLiteral("clip_id"), op.clipId);
        }
        if (!op.mediaRef.isEmpty()) {
            s.insert(QStringLiteral("media_ref"), op.mediaRef);
        }
        if (!op.rationale.isEmpty()) {
            s.insert(QStringLiteral("rationale"), op.rationale);
        }
        s.insert(QStringLiteral("confidence"), op.confidence);
        opSummaries.append(s);
    }
    diff.insert(QStringLiteral("operations"), opSummaries);

    if (!m_backend || !m_backend->supportsTransactionalDryRun()) {
        diff.insert(QStringLiteral("preview_simulated"), false);
        return diff;
    }

    const bool hasSnap = m_backend->supportsSnapshots();
    const QJsonObject beforeSnap = hasSnap ? m_backend->captureStateSnapshot() : QJsonObject{};

    QString txErr;
    if (!m_backend->beginTransaction(QStringLiteral("Preview: ") + batch.transactionLabel, &txErr)) {
        diff.insert(QStringLiteral("preview_simulated"), false);
        diff.insert(QStringLiteral("preview_error"), txErr);
        return diff;
    }

    bool previewValid = true;
    QString previewErr;
    QVector<TimelineOperationResult> previews;
    previews.reserve(batch.operations.size());
    for (const TimelineOperation &op : batch.operations) {
        if (op.type == QStringLiteral("undo") || op.type == QStringLiteral("redo")) {
            TimelineOperationResult r;
            r.opId = op.opId;
            r.applied = (op.type == QStringLiteral("undo")) ? canUndo() : canRedo();
            r.message = r.applied ? QStringLiteral("Preview %1 ready").arg(op.type) : QStringLiteral("Nothing to %1").arg(op.type);
            previews.append(r);
            if (!r.applied) {
                previewValid = false;
                previewErr = r.message;
                break;
            }
            continue;
        }
        TimelineOperationResult r = m_backend->executeOperation(op, batch.sequenceId);
        previews.append(r);
        if (!r.applied) {
            previewValid = false;
            previewErr = r.message;
            break;
        }
    }

    if (previewValid && hasSnap) {
        const QJsonObject afterSnap = m_backend->captureStateSnapshot();
        QJsonObject countsDelta;
        const QStringList collections = {QStringLiteral("media_bin"),   QStringLiteral("tracks"),         QStringLiteral("clips"),
                                         QStringLiteral("captions"),    QStringLiteral("markers"),        QStringLiteral("camera_selections"),
                                         QStringLiteral("sync_groups"), QStringLiteral("export_requests")};
        for (const QString &key : collections) {
            const int beforeCount = beforeSnap.value(key).toArray().size();
            const int afterCount = afterSnap.value(key).toArray().size();
            if (beforeCount != afterCount) {
                QJsonObject d;
                d.insert(QStringLiteral("before"), beforeCount);
                d.insert(QStringLiteral("after"), afterCount);
                d.insert(QStringLiteral("delta"), afterCount - beforeCount);
                countsDelta.insert(key, d);
            }
        }
        diff.insert(QStringLiteral("collection_deltas"), countsDelta);
    }

    m_backend->rollbackTransaction();

    diff.insert(QStringLiteral("preview_simulated"), true);
    diff.insert(QStringLiteral("preview_valid"), previewValid);
    if (!previewErr.isEmpty()) {
        diff.insert(QStringLiteral("preview_error"), previewErr);
    }
    if (previewResultsOut != nullptr) {
        *previewResultsOut = std::move(previews);
    }
    return diff;
}

QVector<PendingProposal> TimelineCommandEngine::pendingProposals() const
{
    RecursiveMutexLocker locker(&m_writeMutex);
    return m_pendingProposals;
}

QJsonArray TimelineCommandEngine::pendingProposalsJson() const
{
    RecursiveMutexLocker locker(&m_writeMutex);
    QJsonArray arr;
    for (const PendingProposal &p : m_pendingProposals) {
        arr.append(serializeProposal(p));
    }
    return arr;
}

TimelineCommandResponse TimelineCommandEngine::approveProposal(const QString &requestId, bool rebaseToCurrentRevision)
{
    RecursiveMutexLocker locker(&m_writeMutex);

    int idx = -1;
    for (int i = 0; i < m_pendingProposals.size(); ++i) {
        if (m_pendingProposals.at(i).batch.requestId == requestId) {
            idx = i;
            break;
        }
    }

    if (idx < 0) {
        TimelineCommandResponse resp;
        resp.apiVersion = QString::fromLatin1(kTimelineApiVersion);
        resp.requestId = requestId;
        resp.status = CommandStatus::ValidationError;
        resp.previousRevision = m_revision;
        resp.currentRevision = m_revision;
        resp.errorMessage = QStringLiteral("Pending proposal '%1' not found").arg(requestId);
        return resp;
    }

    TimelineCommandBatch batch = m_pendingProposals.at(idx).batch;
    if (rebaseToCurrentRevision) {
        batch.expectedRevision = m_revision;
    } else if (batch.expectedRevision != m_revision) {
        TimelineCommandResponse resp;
        resp.apiVersion = QString::fromLatin1(kTimelineApiVersion);
        resp.requestId = requestId;
        resp.status = CommandStatus::RevisionConflict;
        resp.previousRevision = m_revision;
        resp.currentRevision = m_revision;
        resp.errorMessage =
            QStringLiteral("Proposal '%1' targeted revision %2, but current revision is %3").arg(requestId).arg(batch.expectedRevision).arg(m_revision);
        appendActivityLogLocked(batch, resp, resp.errorMessage);
        Q_EMIT activityLogUpdated();
        return resp;
    }

    m_pendingProposals.removeAt(idx);
    batch.requiresApproval = false;

    const TimelineCommandResponse resp = executeBatchLocked(batch, true);
    Q_EMIT proposalResolved(requestId, true);
    return resp;
}

TimelineCommandResponse TimelineCommandEngine::approveBatchWithSelection(const QString &requestId, const QSet<QString> &acceptedOpIds,
                                                                         const QString &approvedBy, bool rebaseToCurrentRevision)
{
    RecursiveMutexLocker locker(&m_writeMutex);

    int idx = -1;
    for (int i = 0; i < m_pendingProposals.size(); ++i) {
        if (m_pendingProposals.at(i).batch.requestId == requestId) {
            idx = i;
            break;
        }
    }

    if (idx < 0) {
        TimelineCommandResponse resp;
        resp.apiVersion = QString::fromLatin1(kTimelineApiVersion);
        resp.requestId = requestId;
        resp.status = CommandStatus::ValidationError;
        resp.previousRevision = m_revision;
        resp.currentRevision = m_revision;
        resp.errorMessage = QStringLiteral("Pending proposal '%1' not found").arg(requestId);
        return resp;
    }

    TimelineCommandBatch batch = m_pendingProposals.at(idx).batch;
    if (rebaseToCurrentRevision) {
        batch.expectedRevision = m_revision;
    } else if (batch.expectedRevision != m_revision) {
        TimelineCommandResponse resp;
        resp.apiVersion = QString::fromLatin1(kTimelineApiVersion);
        resp.requestId = requestId;
        resp.status = CommandStatus::RevisionConflict;
        resp.previousRevision = m_revision;
        resp.currentRevision = m_revision;
        resp.errorMessage =
            QStringLiteral("Proposal '%1' targeted revision %2, but current revision is %3").arg(requestId).arg(batch.expectedRevision).arg(m_revision);
        appendActivityLogLocked(batch, resp, resp.errorMessage);
        Q_EMIT activityLogUpdated();
        return resp;
    }

    QVector<TimelineOperation> filteredOps;
    filteredOps.reserve(batch.operations.size());
    for (const TimelineOperation &op : std::as_const(batch.operations)) {
        if (acceptedOpIds.contains(op.opId)) {
            filteredOps.append(op);
        }
    }

    if (filteredOps.isEmpty()) {
        TimelineCommandResponse resp;
        resp.apiVersion = QString::fromLatin1(kTimelineApiVersion);
        resp.requestId = requestId;
        resp.status = CommandStatus::ValidationError;
        resp.previousRevision = m_revision;
        resp.currentRevision = m_revision;
        resp.errorMessage = QStringLiteral("No accepted operations selected for proposal '%1'").arg(requestId);
        return resp;
    }

    const int originalCount = batch.operations.size();
    m_pendingProposals.removeAt(idx);
    batch.operations = filteredOps;
    batch.requiresApproval = false;
    if (filteredOps.size() < originalCount) {
        const QString approver = approvedBy.trimmed().isEmpty() ? QStringLiteral("user") : approvedBy.trimmed();
        batch.transactionLabel =
            QStringLiteral("%1 (%2/%3 ops approved by %4)").arg(batch.transactionLabel).arg(filteredOps.size()).arg(originalCount).arg(approver);
    }

    const TimelineCommandResponse resp = executeBatchLocked(batch, true);
    Q_EMIT proposalResolved(requestId, true);
    return resp;
}

bool TimelineCommandEngine::rejectProposal(const QString &requestId, const QString &reason)
{
    RecursiveMutexLocker locker(&m_writeMutex);

    for (int i = 0; i < m_pendingProposals.size(); ++i) {
        if (m_pendingProposals.at(i).batch.requestId == requestId) {
            const PendingProposal removed = m_pendingProposals.takeAt(i);
            const QString msg = reason.trimmed().isEmpty() ? QStringLiteral("Proposal rejected by user")
                                                           : QStringLiteral("Proposal rejected by user: %1").arg(reason.trimmed());
            appendActivityLogSimpleLocked(removed.batch.requestId, removed.batch.projectId, removed.batch.sequenceId, removed.batch.agentId,
                                          removed.batch.agentRole, removed.batch.transactionLabel, CommandStatus::RolledBack, m_revision, m_revision,
                                          removed.batch.operations.size(), false, msg);
            Q_EMIT proposalResolved(requestId, false);
            Q_EMIT activityLogUpdated();
            return true;
        }
    }
    return false;
}

TimelineCommandResponse TimelineCommandEngine::undoLastTransaction(const QString &actorId)
{
    RecursiveMutexLocker locker(&m_writeMutex);

    TimelineCommandResponse resp;
    resp.apiVersion = QString::fromLatin1(kTimelineApiVersion);
    resp.requestId = QStringLiteral("undo-rev-%1").arg(m_revision);
    resp.previousRevision = m_revision;
    resp.currentRevision = m_revision;

    if (m_undoStack.isEmpty()) {
        if (m_backend && m_backend->canNativeUndo()) {
            QString err;
            if (!m_backend->nativeUndo(&err)) {
                resp.status = CommandStatus::ExecutionFailed;
                resp.errorMessage = err.isEmpty() ? QStringLiteral("Backend native undo failed") : err;
                return resp;
            }
            const qint64 prevRev = m_revision;
            ++m_revision;
            resp.status = CommandStatus::Ok;
            resp.previousRevision = prevRev;
            resp.currentRevision = m_revision;
            appendActivityLogSimpleLocked(resp.requestId, QString(), QString(), actorId, QStringLiteral("user"), QStringLiteral("Undo"), CommandStatus::Ok,
                                          prevRev, m_revision, 1, false, QStringLiteral("Undid native backend transaction"));
            Q_EMIT revisionChanged(m_revision);
            Q_EMIT activityLogUpdated();
            return resp;
        }
        resp.status = CommandStatus::ExecutionFailed;
        resp.errorMessage = QStringLiteral("No transaction available to undo");
        return resp;
    }

    UndoRecord rec = m_undoStack.last();
    QString err;
    bool ok = false;
    if (rec.usesSnapshot) {
        ok = m_backend->restoreStateSnapshot(rec.beforeSnapshot, &err);
    } else if (m_backend && m_backend->canNativeUndo()) {
        ok = m_backend->nativeUndo(&err);
    } else {
        err = QStringLiteral("Backend does not support restoring previous transaction state");
    }

    if (!ok) {
        resp.status = CommandStatus::ExecutionFailed;
        resp.errorMessage = err.isEmpty() ? QStringLiteral("Failed to undo transaction") : err;
        return resp;
    }

    m_undoStack.removeLast();
    m_redoStack.append(rec);

    const qint64 prevRev = m_revision;
    ++m_revision;

    resp.status = CommandStatus::Ok;
    resp.previousRevision = prevRev;
    resp.currentRevision = m_revision;

    TimelineOperationResult opRes;
    opRes.opId = QStringLiteral("undo");
    opRes.applied = true;
    opRes.createdId = rec.requestId;
    opRes.message = QStringLiteral("Undid transaction '%1' (%2)").arg(rec.transactionLabel, rec.requestId);
    resp.results.append(opRes);

    appendActivityLogSimpleLocked(resp.requestId, rec.projectId, rec.sequenceId, actorId, QStringLiteral("user"),
                                  QStringLiteral("Undo: %1").arg(rec.transactionLabel), CommandStatus::Ok, prevRev, m_revision, 1, false, opRes.message);
    Q_EMIT revisionChanged(m_revision);
    Q_EMIT activityLogUpdated();
    return resp;
}

TimelineCommandResponse TimelineCommandEngine::redoLastTransaction(const QString &actorId)
{
    RecursiveMutexLocker locker(&m_writeMutex);

    TimelineCommandResponse resp;
    resp.apiVersion = QString::fromLatin1(kTimelineApiVersion);
    resp.requestId = QStringLiteral("redo-rev-%1").arg(m_revision);
    resp.previousRevision = m_revision;
    resp.currentRevision = m_revision;

    if (m_redoStack.isEmpty()) {
        if (m_backend && m_backend->canNativeRedo()) {
            QString err;
            if (!m_backend->nativeRedo(&err)) {
                resp.status = CommandStatus::ExecutionFailed;
                resp.errorMessage = err.isEmpty() ? QStringLiteral("Backend native redo failed") : err;
                return resp;
            }
            const qint64 prevRev = m_revision;
            ++m_revision;
            resp.status = CommandStatus::Ok;
            resp.previousRevision = prevRev;
            resp.currentRevision = m_revision;
            appendActivityLogSimpleLocked(resp.requestId, QString(), QString(), actorId, QStringLiteral("user"), QStringLiteral("Redo"), CommandStatus::Ok,
                                          prevRev, m_revision, 1, false, QStringLiteral("Redid native backend transaction"));
            Q_EMIT revisionChanged(m_revision);
            Q_EMIT activityLogUpdated();
            return resp;
        }
        resp.status = CommandStatus::ExecutionFailed;
        resp.errorMessage = QStringLiteral("No transaction available to redo");
        return resp;
    }

    UndoRecord rec = m_redoStack.last();
    QString err;
    bool ok = false;
    if (rec.usesSnapshot) {
        ok = m_backend->restoreStateSnapshot(rec.afterSnapshot, &err);
    } else if (m_backend && m_backend->canNativeRedo()) {
        ok = m_backend->nativeRedo(&err);
    } else {
        err = QStringLiteral("Backend does not support restoring redone transaction state");
    }

    if (!ok) {
        resp.status = CommandStatus::ExecutionFailed;
        resp.errorMessage = err.isEmpty() ? QStringLiteral("Failed to redo transaction") : err;
        return resp;
    }

    m_redoStack.removeLast();
    m_undoStack.append(rec);

    const qint64 prevRev = m_revision;
    ++m_revision;

    resp.status = CommandStatus::Ok;
    resp.previousRevision = prevRev;
    resp.currentRevision = m_revision;

    TimelineOperationResult opRes;
    opRes.opId = QStringLiteral("redo");
    opRes.applied = true;
    opRes.createdId = rec.requestId;
    opRes.message = QStringLiteral("Redid transaction '%1' (%2)").arg(rec.transactionLabel, rec.requestId);
    resp.results.append(opRes);

    appendActivityLogSimpleLocked(resp.requestId, rec.projectId, rec.sequenceId, actorId, QStringLiteral("user"),
                                  QStringLiteral("Redo: %1").arg(rec.transactionLabel), CommandStatus::Ok, prevRev, m_revision, 1, false, opRes.message);
    Q_EMIT revisionChanged(m_revision);
    Q_EMIT activityLogUpdated();
    return resp;
}

QVector<ActivityLogEntry> TimelineCommandEngine::activityLog() const
{
    RecursiveMutexLocker locker(&m_writeMutex);
    return m_activityLog;
}

QJsonArray TimelineCommandEngine::activityLogJson() const
{
    RecursiveMutexLocker locker(&m_writeMutex);
    QJsonArray arr;
    for (const ActivityLogEntry &entry : m_activityLog) {
        arr.append(serializeActivityEntry(entry));
    }
    return arr;
}

QJsonObject TimelineCommandEngine::stateSnapshotJson() const
{
    RecursiveMutexLocker locker(&m_writeMutex);
    QJsonObject obj;
    obj.insert(QStringLiteral("api_version"), QString::fromLatin1(kTimelineApiVersion));
    obj.insert(QStringLiteral("revision"), m_revision);
    obj.insert(QStringLiteral("backend"), m_backend ? m_backend->backendName() : QStringLiteral("none"));
    obj.insert(QStringLiteral("can_undo"), canUndo());
    obj.insert(QStringLiteral("can_redo"), canRedo());
    obj.insert(QStringLiteral("pending_proposal_count"), m_pendingProposals.size());
    if (m_backend && m_backend->supportsSnapshots()) {
        obj.insert(QStringLiteral("state"), m_backend->captureStateSnapshot());
    }
    return obj;
}

void TimelineCommandEngine::appendActivityLogLocked(const TimelineCommandBatch &batch, const TimelineCommandResponse &response, const QString &overrideMessage)
{
    ActivityLogEntry entry;
    entry.entryId = m_nextLogEntryId++;
    entry.timestampUtc = currentUtcTimestamp();
    entry.requestId = batch.requestId;
    entry.projectId = batch.projectId;
    entry.sequenceId = batch.sequenceId;
    entry.agentId = batch.agentId;
    entry.agentRole = batch.agentRole;
    entry.transactionLabel = batch.transactionLabel;
    entry.status = response.status;
    entry.previousRevision = response.previousRevision;
    entry.currentRevision = response.currentRevision;
    entry.operationCount = batch.operations.size();
    entry.dryRun = batch.dryRun;
    entry.message = !overrideMessage.isEmpty() ? overrideMessage : response.errorMessage;
    for (const TimelineOperationResult &r : response.results) {
        QJsonObject s;
        s.insert(QStringLiteral("op_id"), r.opId);
        s.insert(QStringLiteral("applied"), r.applied);
        if (!r.createdId.isEmpty()) {
            s.insert(QStringLiteral("created_id"), r.createdId);
        }
        if (!r.message.isEmpty()) {
            s.insert(QStringLiteral("message"), r.message);
        }
        entry.operationSummaries.append(s);
    }
    m_activityLog.append(std::move(entry));
}

void TimelineCommandEngine::appendActivityLogSimpleLocked(const QString &requestId, const QString &projectId, const QString &sequenceId, const QString &agentId,
                                                          const QString &agentRole, const QString &label, CommandStatus status, qint64 prevRev, qint64 currRev,
                                                          int opCount, bool dryRun, const QString &message)
{
    ActivityLogEntry entry;
    entry.entryId = m_nextLogEntryId++;
    entry.timestampUtc = currentUtcTimestamp();
    entry.requestId = requestId;
    entry.projectId = projectId;
    entry.sequenceId = sequenceId;
    entry.agentId = agentId;
    entry.agentRole = agentRole;
    entry.transactionLabel = label;
    entry.status = status;
    entry.previousRevision = prevRev;
    entry.currentRevision = currRev;
    entry.operationCount = opCount;
    entry.dryRun = dryRun;
    entry.message = message;
    m_activityLog.append(std::move(entry));
}

QString TimelineCommandEngine::currentUtcTimestamp()
{
    return QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs);
}

QJsonObject TimelineCommandEngine::serializeProposal(const PendingProposal &proposal)
{
    QJsonObject obj;
    obj.insert(QStringLiteral("request_id"), proposal.batch.requestId);
    obj.insert(QStringLiteral("staged_at_revision"), proposal.stagedAtRevision);
    obj.insert(QStringLiteral("staged_at_utc"), proposal.stagedAtUtc);
    obj.insert(QStringLiteral("batch"), batchToJson(proposal.batch));
    obj.insert(QStringLiteral("proposed_diff"), proposal.proposedDiff);
    return obj;
}

QJsonObject TimelineCommandEngine::serializeActivityEntry(const ActivityLogEntry &entry)
{
    QJsonObject obj;
    obj.insert(QStringLiteral("entry_id"), entry.entryId);
    obj.insert(QStringLiteral("timestamp_utc"), entry.timestampUtc);
    obj.insert(QStringLiteral("request_id"), entry.requestId);
    obj.insert(QStringLiteral("project_id"), entry.projectId);
    obj.insert(QStringLiteral("sequence_id"), entry.sequenceId);
    obj.insert(QStringLiteral("agent_id"), entry.agentId);
    obj.insert(QStringLiteral("agent_role"), entry.agentRole);
    obj.insert(QStringLiteral("transaction_label"), entry.transactionLabel);
    obj.insert(QStringLiteral("status"), commandStatusToString(entry.status));
    obj.insert(QStringLiteral("previous_revision"), entry.previousRevision);
    obj.insert(QStringLiteral("current_revision"), entry.currentRevision);
    obj.insert(QStringLiteral("operation_count"), entry.operationCount);
    obj.insert(QStringLiteral("dry_run"), entry.dryRun);
    if (!entry.message.isEmpty()) {
        obj.insert(QStringLiteral("message"), entry.message);
    }
    if (!entry.operationSummaries.isEmpty()) {
        obj.insert(QStringLiteral("operations"), entry.operationSummaries);
    }
    return obj;
}

} // namespace BangOnPro
