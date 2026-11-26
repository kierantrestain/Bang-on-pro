/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "KdenliveTimelineBridge.h"

#ifdef BANGONPRO_WITH_KDENLIVE_CORE

#include "assets/model/assetparametermodel.hpp"
#include "bin/clipcreator.hpp"
#include "bin/model/markerlistmodel.hpp"
#include "bin/model/subtitlemodel.hpp"
#include "bin/projectclip.h"
#include "bin/projectfolder.h"
#include "bin/projectitemmodel.h"
#include "core.h"
#include "doc/docundostack.hpp"
#include "doc/kdenlivedoc.h"
#include "effects/effectsrepository.hpp"
#include "effects/effectstack/model/effectstackmodel.hpp"
#include "project/projectmanager.h"
#include "timeline2/model/timelinefunctions.hpp"
#include "timeline2/model/timelineitemmodel.hpp"
#include "utils/gentime.h"

#include <mlt++/MltProperties.h>

#include <QCryptographicHash>
#include <QFileInfo>
#include <QJsonDocument>
#include <QRegularExpression>
#include <QUuid>
#include <algorithm>
#include <cmath>

namespace BangOnPro {

namespace {

QString firstNonEmptyString(const QJsonObject &params, const QStringList &keys)
{
    for (const QString &key : keys) {
        const QJsonValue v = params.value(key);
        if (v.isString()) {
            const QString s = v.toString().trimmed();
            if (!s.isEmpty()) {
                return s;
            }
        }
    }
    return {};
}

QString jsonValueToParamString(const QJsonValue &val)
{
    if (val.isString()) {
        return val.toString();
    }
    if (val.isBool()) {
        return val.toBool() ? QStringLiteral("1") : QStringLiteral("0");
    }
    if (val.isDouble()) {
        return QString::number(val.toDouble(), 'g', 12);
    }
    if (val.isObject()) {
        return QString::fromUtf8(QJsonDocument(val.toObject()).toJson(QJsonDocument::Compact));
    }
    if (val.isArray()) {
        return QString::fromUtf8(QJsonDocument(val.toArray()).toJson(QJsonDocument::Compact));
    }
    return {};
}

} // namespace

KdenliveTimelineBridge::KdenliveTimelineBridge(std::shared_ptr<TimelineItemModel> explicitTimeline)
    : m_explicitTimeline(std::move(explicitTimeline))
{
}

QString KdenliveTimelineBridge::backendName() const
{
    return QStringLiteral("kdenlive_core");
}

void KdenliveTimelineBridge::setTimelineOverride(std::shared_ptr<TimelineItemModel> timeline)
{
    m_explicitTimeline = std::move(timeline);
}

QString KdenliveTimelineBridge::currentProjectId() const
{
    if (!Core::self()) {
        return {};
    }
    if (KdenliveDoc *doc = Core::self()->currentDoc(); doc != nullptr) {
        const QString docId = doc->getDocumentProperty(QStringLiteral("documentid")).trimmed();
        if (!docId.isEmpty()) {
            return docId;
        }
        const QString urlStr = doc->url().toLocalFile().trimmed();
        if (!urlStr.isEmpty()) {
            return urlStr;
        }
    }
    return {};
}

QString KdenliveTimelineBridge::currentSequenceId() const
{
    if (!Core::self()) {
        return {};
    }
    if (KdenliveDoc *doc = Core::self()->currentDoc(); doc != nullptr) {
        if (!doc->activeUuid.isNull()) {
            return doc->activeUuid.toString(QUuid::WithoutBraces);
        }
    }
    return {};
}

TimelineStateFingerprint KdenliveTimelineBridge::captureStateFingerprint(const QString &sequenceId) const
{
    TimelineStateFingerprint fp;
    fp.projectId = currentProjectId();
    fp.sequenceId = sequenceId.trimmed().isEmpty() ? currentSequenceId() : sequenceId.trimmed();

    std::shared_ptr<TimelineItemModel> timeline = resolveTimeline(fp.sequenceId);
    if (!timeline) {
        fp.valid = false;
        return fp;
    }

    QVector<QPair<int, int>> orderedTracks;
    const auto allTrackIds = timeline->getAllTracksIds();
    orderedTracks.reserve(static_cast<int>(allTrackIds.size()));
    for (int tid : allTrackIds) {
        orderedTracks.append({timeline->getTrackPosition(tid), tid});
    }
    std::sort(orderedTracks.begin(), orderedTracks.end(), [](const QPair<int, int> &a, const QPair<int, int> &b) { return a.first < b.first; });

    QSet<int> seenClipIds;
    for (const auto &pair : orderedTracks) {
        const int tid = pair.second;
        const QString tidStr = QString::number(tid);
        const QString tag = timeline->getTrackTagById(tid);
        const QString kind = timeline->isAudioTrack(tid) ? QStringLiteral("audio") : QStringLiteral("video");
        fp.trackIds.append(tidStr);
        fp.trackSignatures.insert(tidStr, QStringLiteral("%1|%2|%3").arg(kind, tag).arg(pair.first));
        if (!tag.isEmpty()) {
            fp.trackSignatures.insert(tag, QStringLiteral("%1|%2|%3").arg(kind, tidStr).arg(pair.first));
        }

        const auto items = timeline->getItemsInRange(tid, 0, -1, false);
        for (int itemId : items) {
            if (!timeline->isClip(itemId) || seenClipIds.contains(itemId)) {
                continue;
            }
            seenClipIds.insert(itemId);
            const QString cidStr = QString::number(itemId);
            const int pos = timeline->getClipPosition(itemId);
            const int inFrame = timeline->getClipIn(itemId);
            const int dur = timeline->getClipPlaytime(itemId);
            const QString binId = timeline->getClipBinId(itemId);
            fp.clipIds.append(cidStr);
            fp.clipSignatures.insert(cidStr, QStringLiteral("%1|%2|%3|%4|%5").arg(tid).arg(pos).arg(inFrame).arg(dur).arg(binId));
        }
    }

    // Include reverse alias entries so logical IDs created by previous AI batches are also tracked.
    for (auto it = m_trackAliases.cbegin(); it != m_trackAliases.cend(); ++it) {
        if (timeline->isTrack(it.value())) {
            const QString aliasSig = fp.trackSignatures.value(QString::number(it.value()));
            if (!aliasSig.isEmpty()) {
                fp.trackSignatures.insert(it.key(), aliasSig);
            }
        }
    }
    for (auto it = m_clipAliases.cbegin(); it != m_clipAliases.cend(); ++it) {
        if (timeline->isClip(it.value())) {
            const QString aliasSig = fp.clipSignatures.value(QString::number(it.value()));
            if (!aliasSig.isEmpty()) {
                fp.clipSignatures.insert(it.key(), aliasSig);
            }
        }
    }

    fp.trackIds.sort();
    fp.clipIds.sort();

    QCryptographicHash hasher(QCryptographicHash::Sha256);
    hasher.addData(QByteArrayLiteral("proj="));
    hasher.addData(fp.projectId.toUtf8());
    hasher.addData(QByteArrayLiteral("\nseq="));
    hasher.addData(fp.sequenceId.toUtf8());
    for (auto it = fp.trackSignatures.cbegin(); it != fp.trackSignatures.cend(); ++it) {
        hasher.addData(QByteArrayLiteral("\nt="));
        hasher.addData(it.key().toUtf8());
        hasher.addData(QByteArrayLiteral(":"));
        hasher.addData(it.value().toUtf8());
    }
    for (auto it = fp.clipSignatures.cbegin(); it != fp.clipSignatures.cend(); ++it) {
        hasher.addData(QByteArrayLiteral("\nc="));
        hasher.addData(it.key().toUtf8());
        hasher.addData(QByteArrayLiteral(":"));
        hasher.addData(it.value().toUtf8());
    }
    fp.digest = QString::fromLatin1(hasher.result().toHex());
    fp.valid = true;
    return fp;
}

QJsonObject KdenliveTimelineBridge::captureReadOnlyStateJson(const QString &sequenceId) const
{
    const QString seqId =
        sequenceId.trimmed().isEmpty() ? (currentSequenceId().isEmpty() ? QStringLiteral("seq_main") : currentSequenceId()) : sequenceId.trimmed();
    std::shared_ptr<TimelineItemModel> timeline = resolveTimeline(seqId);
    if (!timeline) {
        return {};
    }

    QJsonObject root;
    root.insert(QStringLiteral("active_sequence_id"), seqId);

    QJsonArray sequencesArr;
    QJsonObject seqObj;
    seqObj.insert(QStringLiteral("sequence_id"), seqId);
    seqObj.insert(QStringLiteral("name"), QStringLiteral("Active Sequence"));
    const double fps = Core::self() ? Core::self()->getCurrentFps() : 25.0;
    seqObj.insert(QStringLiteral("fps_num"), qMax(1, static_cast<int>(std::lround(fps))));
    seqObj.insert(QStringLiteral("fps_den"), 1);
    seqObj.insert(QStringLiteral("width"), 1920);
    seqObj.insert(QStringLiteral("height"), 1080);
    sequencesArr.append(seqObj);
    root.insert(QStringLiteral("sequences"), sequencesArr);

    QJsonArray mediaArr;
    QSet<QString> addedMedia;
    if (Core::self() && Core::self()->projectItemModel()) {
        const auto binIds = Core::self()->projectItemModel()->getAllClipIds();
        for (const QString &bid : binIds) {
            auto binClip = Core::self()->projectItemModel()->getClipByBinID(bid);
            if (!binClip) {
                continue;
            }
            const QString srcPath = binClip->url();
            if (srcPath.isEmpty()) {
                continue;
            }
            QJsonObject m;
            m.insert(QStringLiteral("media_ref"), bid);
            m.insert(QStringLiteral("source_path"), srcPath);
            m.insert(QStringLiteral("name"), binClip->clipName());
            m.insert(QStringLiteral("media_type"), QStringLiteral("av"));
            m.insert(QStringLiteral("duration_frames"), static_cast<int>(binClip->frameDuration()));
            mediaArr.append(m);
            addedMedia.insert(bid);
        }
    }

    QJsonArray tracksArr;
    QJsonArray clipsArr;
    QVector<QPair<int, int>> orderedTracks;
    for (int tid : timeline->getAllTracksIds()) {
        orderedTracks.append({timeline->getTrackPosition(tid), tid});
    }
    std::sort(orderedTracks.begin(), orderedTracks.end(), [](const QPair<int, int> &a, const QPair<int, int> &b) { return a.first < b.first; });

    QSet<int> seenClipIds;
    int vIdx = 1;
    int aIdx = 1;
    for (const auto &pair : orderedTracks) {
        const int tid = pair.second;
        const bool isAudio = timeline->isAudioTrack(tid);
        const QString tag = timeline->getTrackTagById(tid);
        const QString logicalTag = !tag.isEmpty() ? tag : (isAudio ? QStringLiteral("A%1").arg(aIdx) : QStringLiteral("V%1").arg(vIdx));
        if (isAudio) {
            ++aIdx;
        } else {
            ++vIdx;
        }

        QJsonObject t;
        t.insert(QStringLiteral("track_id"), logicalTag);
        t.insert(QStringLiteral("sequence_id"), seqId);
        t.insert(QStringLiteral("name"), logicalTag);
        t.insert(QStringLiteral("kind"), isAudio ? QStringLiteral("audio") : QStringLiteral("video"));
        t.insert(QStringLiteral("index"), pair.first);
        tracksArr.append(t);

        const auto items = timeline->getItemsInRange(tid, 0, -1, false);
        for (int itemId : items) {
            if (!timeline->isClip(itemId) || seenClipIds.contains(itemId)) {
                continue;
            }
            seenClipIds.insert(itemId);
            const int pos = timeline->getClipPosition(itemId);
            const int inFrame = timeline->getClipIn(itemId);
            const int dur = qMax(1, timeline->getClipPlaytime(itemId));
            const QString binId = timeline->getClipBinId(itemId);

            if (!binId.isEmpty() && !addedMedia.contains(binId)) {
                QJsonObject m;
                m.insert(QStringLiteral("media_ref"), binId);
                m.insert(QStringLiteral("name"), timeline->getClipName(itemId));
                m.insert(QStringLiteral("duration_frames"), inFrame + dur);
                mediaArr.append(m);
                addedMedia.insert(binId);
            }

            QJsonObject fr;
            fr.insert(QStringLiteral("in_frame"), inFrame);
            fr.insert(QStringLiteral("out_frame"), inFrame + dur);
            fr.insert(QStringLiteral("timeline_start"), pos);

            QJsonObject c;
            c.insert(QStringLiteral("clip_id"), QString::number(itemId));
            c.insert(QStringLiteral("sequence_id"), seqId);
            c.insert(QStringLiteral("track_id"), logicalTag);
            c.insert(QStringLiteral("media_ref"), binId);
            c.insert(QStringLiteral("name"), timeline->getClipName(itemId));
            c.insert(QStringLiteral("frame_range"), fr);
            clipsArr.append(c);
        }
    }

    root.insert(QStringLiteral("media_bin"), mediaArr);
    root.insert(QStringLiteral("tracks"), tracksArr);
    root.insert(QStringLiteral("clips"), clipsArr);
    root.insert(QStringLiteral("captions"), QJsonArray{});
    root.insert(QStringLiteral("markers"), QJsonArray{});
    root.insert(QStringLiteral("masks"), QJsonArray{});
    root.insert(QStringLiteral("transitions"), QJsonArray{});
    root.insert(QStringLiteral("lower_thirds"), QJsonArray{});
    root.insert(QStringLiteral("camera_selections"), QJsonArray{});
    root.insert(QStringLiteral("sync_groups"), QJsonArray{});
    root.insert(QStringLiteral("export_requests"), QJsonArray{});
    return root;
}

std::shared_ptr<TimelineItemModel> KdenliveTimelineBridge::resolveTimeline(const QString &sequenceId) const
{
    if (m_explicitTimeline) {
        return m_explicitTimeline;
    }
    if (!Core::self()) {
        return nullptr;
    }
    if (KdenliveDoc *doc = Core::self()->currentDoc(); doc != nullptr && !sequenceId.isEmpty()) {
        const QUuid seqUuid(sequenceId);
        if (!seqUuid.isNull()) {
            if (auto seqTimeline = doc->getTimeline(seqUuid, true)) {
                return seqTimeline;
            }
        }
    }
    if (ProjectManager *pm = Core::self()->projectManager(); pm != nullptr) {
        return pm->getTimeline();
    }
    return nullptr;
}

bool KdenliveTimelineBridge::resolveTrackId(const std::shared_ptr<TimelineItemModel> &timeline, const QString &rawTrackId, int *trackIdOut) const
{
    const QString key = rawTrackId.trimmed();
    if (key.isEmpty() || !timeline) {
        return false;
    }
    if (m_trackAliases.contains(key)) {
        const int mapped = m_trackAliases.value(key);
        if (timeline->isTrack(mapped)) {
            *trackIdOut = mapped;
            return true;
        }
    }
    bool ok = false;
    const int numeric = key.toInt(&ok);
    if (ok && timeline->isTrack(numeric)) {
        *trackIdOut = numeric;
        return true;
    }
    // Auto-resolve standard logical track labels (V1, V2, A1, A2, ...) against existing Kdenlive tracks
    static const QRegularExpression kLogicalTrackRe(QStringLiteral("^([VA])(\\d+)$"), QRegularExpression::CaseInsensitiveOption);
    const QRegularExpressionMatch match = kLogicalTrackRe.match(key);
    if (match.hasMatch()) {
        const bool wantAudio = (match.captured(1).toUpper() == QStringLiteral("A"));
        const int logicalIndex = qMax(1, match.captured(2).toInt()) - 1;
        QVector<QPair<int, int>> matchingTracks;
        for (int tid : timeline->getAllTracksIds()) {
            if (timeline->isAudioTrack(tid) == wantAudio) {
                matchingTracks.append({timeline->getTrackPosition(tid), tid});
            }
        }
        std::sort(matchingTracks.begin(), matchingTracks.end(),
                  [](const QPair<int, int> &a, const QPair<int, int> &b) { return a.first < b.first; });
        if (!matchingTracks.isEmpty()) {
            const int clampedIdx = qMin(logicalIndex, matchingTracks.size() - 1);
            *trackIdOut = matchingTracks.at(clampedIdx).second;
            return true;
        }
    }
    return false;
}

bool KdenliveTimelineBridge::resolveClipId(const std::shared_ptr<TimelineItemModel> &timeline, const QString &rawClipId, int *clipIdOut) const
{
    const QString key = rawClipId.trimmed();
    if (key.isEmpty() || !timeline) {
        return false;
    }
    if (m_clipAliases.contains(key)) {
        const int mapped = m_clipAliases.value(key);
        if (timeline->isClip(mapped)) {
            *clipIdOut = mapped;
            return true;
        }
    }
    bool ok = false;
    const int numeric = key.toInt(&ok);
    if (ok && timeline->isClip(numeric)) {
        *clipIdOut = numeric;
        return true;
    }
    return false;
}

QString KdenliveTimelineBridge::resolveBinId(const QString &rawMediaRef) const
{
    const QString key = rawMediaRef.trimmed();
    if (m_mediaAliases.contains(key)) {
        return m_mediaAliases.value(key);
    }
    return key;
}

void KdenliveTimelineBridge::composeUndoRedo(Fun localUndo, Fun localRedo)
{
    Fun prevUndo = m_txUndo;
    m_txUndo = [prevUndo = std::move(prevUndo), localUndo = std::move(localUndo)]() {
        const bool v = localUndo ? localUndo() : true;
        return v && (prevUndo ? prevUndo() : true);
    };
    Fun prevRedo = m_txRedo;
    m_txRedo = [prevRedo = std::move(prevRedo), localRedo = std::move(localRedo)]() {
        const bool v = prevRedo ? prevRedo() : true;
        return v && (localRedo ? localRedo() : true);
    };
}

bool KdenliveTimelineBridge::beginTransaction(const QString &label, QString *errorOut)
{
    Q_UNUSED(label);
    if (m_inTransaction) {
        if (errorOut != nullptr) {
            *errorOut = QStringLiteral("A transaction is already active on KdenliveTimelineBridge");
        }
        return false;
    }
    if (!Core::self()) {
        if (errorOut != nullptr) {
            *errorOut = QStringLiteral("Kdenlive Core singleton is not initialized");
        }
        return false;
    }

    m_txUndo = []() { return true; };
    m_txRedo = []() { return true; };
    m_mediaAliasesBackup = m_mediaAliases;
    m_trackAliasesBackup = m_trackAliases;
    m_clipAliasesBackup = m_clipAliases;
    m_inTransaction = true;
    return true;
}

bool KdenliveTimelineBridge::commitTransaction(const QString &label, QString *errorOut)
{
    if (!m_inTransaction) {
        if (errorOut != nullptr) {
            *errorOut = QStringLiteral("No active Kdenlive transaction to commit");
        }
        return false;
    }
    if (!Core::self()) {
        if (errorOut != nullptr) {
            *errorOut = QStringLiteral("Kdenlive Core singleton is unavailable during commit");
        }
        return false;
    }

    const QString undoText = label.trimmed().isEmpty() ? QStringLiteral("Bang On Pro AI Edit Batch") : label;
    Core::self()->pushUndo(m_txUndo, m_txRedo, undoText);

    m_txUndo = Fun();
    m_txRedo = Fun();
    m_mediaAliasesBackup.clear();
    m_trackAliasesBackup.clear();
    m_clipAliasesBackup.clear();
    m_inTransaction = false;
    return true;
}

bool KdenliveTimelineBridge::rollbackTransaction(QString *errorOut)
{
    if (!m_inTransaction) {
        if (errorOut != nullptr) {
            *errorOut = QStringLiteral("No active Kdenlive transaction to roll back");
        }
        return false;
    }

    bool ok = true;
    if (m_txUndo) {
        ok = m_txUndo();
    }

    m_mediaAliases = m_mediaAliasesBackup;
    m_trackAliases = m_trackAliasesBackup;
    m_clipAliases = m_clipAliasesBackup;
    m_mediaAliasesBackup.clear();
    m_trackAliasesBackup.clear();
    m_clipAliasesBackup.clear();
    m_txUndo = Fun();
    m_txRedo = Fun();
    m_inTransaction = false;

    if (!ok && errorOut != nullptr) {
        *errorOut = QStringLiteral("One or more Kdenlive rollback undo lambdas returned false");
    }
    return ok;
}

bool KdenliveTimelineBridge::canNativeUndo() const
{
    if (!Core::self() || !Core::self()->projectManager() || !Core::self()->projectManager()->current()) {
        return false;
    }
    const std::shared_ptr<DocUndoStack> stack = Core::self()->undoStack();
    return stack && stack->canUndo();
}

bool KdenliveTimelineBridge::canNativeRedo() const
{
    if (!Core::self() || !Core::self()->projectManager() || !Core::self()->projectManager()->current()) {
        return false;
    }
    const std::shared_ptr<DocUndoStack> stack = Core::self()->undoStack();
    return stack && stack->canRedo();
}

bool KdenliveTimelineBridge::nativeUndo(QString *errorOut)
{
    if (!Core::self() || !Core::self()->projectManager() || !Core::self()->projectManager()->current()) {
        if (errorOut != nullptr) {
            *errorOut = QStringLiteral("Kdenlive project document is not initialized");
        }
        return false;
    }
    const std::shared_ptr<DocUndoStack> stack = Core::self()->undoStack();
    if (!stack || !stack->canUndo()) {
        if (errorOut != nullptr) {
            *errorOut = QStringLiteral("Kdenlive DocUndoStack has no command to undo");
        }
        return false;
    }
    stack->undo();
    return true;
}

bool KdenliveTimelineBridge::nativeRedo(QString *errorOut)
{
    if (!Core::self() || !Core::self()->projectManager() || !Core::self()->projectManager()->current()) {
        if (errorOut != nullptr) {
            *errorOut = QStringLiteral("Kdenlive project document is not initialized");
        }
        return false;
    }
    const std::shared_ptr<DocUndoStack> stack = Core::self()->undoStack();
    if (!stack || !stack->canRedo()) {
        if (errorOut != nullptr) {
            *errorOut = QStringLiteral("Kdenlive DocUndoStack has no command to redo");
        }
        return false;
    }
    stack->redo();
    return true;
}

TimelineOperationResult KdenliveTimelineBridge::executeOperation(const TimelineOperation &op, const QString &sequenceId)
{
    TimelineOperationResult res;
    res.opId = op.opId;
    if (!m_inTransaction) {
        res.message = QStringLiteral("Cannot execute Kdenlive operation outside an active transaction");
        return res;
    }

    if (op.type == QStringLiteral("import_media")) {
        return handleImportMedia(op);
    }
    if (op.type == QStringLiteral("create_sequence")) {
        return handleCreateSequence(op);
    }
    if (op.type == QStringLiteral("export_sequence")) {
        return handleExportSequence(op, sequenceId);
    }

    const std::shared_ptr<TimelineItemModel> timeline = resolveTimeline(sequenceId);
    if (!timeline) {
        res.message = QStringLiteral("No active Kdenlive TimelineItemModel for sequence '%1'").arg(sequenceId);
        return res;
    }

    if (op.type == QStringLiteral("create_track")) {
        return handleCreateTrack(timeline, op);
    }
    if (op.type == QStringLiteral("place_clip")) {
        return handlePlaceClip(timeline, op);
    }
    if (op.type == QStringLiteral("move_clip")) {
        return handleMoveClip(timeline, op);
    }
    if (op.type == QStringLiteral("trim_clip")) {
        return handleTrimClip(timeline, op);
    }
    if (op.type == QStringLiteral("split_clip")) {
        return handleSplitClip(timeline, op);
    }
    if (op.type == QStringLiteral("delete_clip")) {
        return handleDeleteClip(timeline, op);
    }
    if (op.type == QStringLiteral("sync_clips")) {
        return handleSyncClips(timeline, op);
    }
    if (op.type == QStringLiteral("select_camera")) {
        return handleSelectCamera(timeline, op);
    }
    if (op.type == QStringLiteral("add_effect")) {
        return handleAddEffect(timeline, op, false);
    }
    if (op.type == QStringLiteral("add_audio_filter")) {
        return handleAddEffect(timeline, op, true);
    }
    if (op.type == QStringLiteral("set_effect_param")) {
        return handleSetEffectParam(timeline, op);
    }
    if (op.type == QStringLiteral("set_keyframe")) {
        return handleSetKeyframe(timeline, op);
    }
    if (op.type == QStringLiteral("set_audio_level")) {
        return handleSetAudioLevel(timeline, op);
    }
    if (op.type == QStringLiteral("add_caption")) {
        return handleAddCaption(timeline, op);
    }
    if (op.type == QStringLiteral("update_caption")) {
        return handleUpdateCaption(timeline, op);
    }
    if (op.type == QStringLiteral("delete_caption")) {
        return handleDeleteCaption(timeline, op);
    }
    if (op.type == QStringLiteral("add_lower_third")) {
        return handleAddLowerThird(timeline, op);
    }
    if (op.type == QStringLiteral("apply_mask")) {
        return handleApplyMask(timeline, op);
    }
    if (op.type == QStringLiteral("add_transition")) {
        return handleAddTransition(timeline, op);
    }
    if (op.type == QStringLiteral("set_clip_speed")) {
        return handleSetClipSpeed(timeline, op);
    }
    if (op.type == QStringLiteral("add_marker")) {
        return handleAddMarker(timeline, op);
    }

    res.message = QStringLiteral("Unsupported operation '%1' in KdenliveTimelineBridge").arg(op.type);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleImportMedia(const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    const QString sourcePath = firstNonEmptyString(op.params, {QStringLiteral("path"), QStringLiteral("uri"), QStringLiteral("source_path")});
    if (sourcePath.isEmpty()) {
        res.message = QStringLiteral("import_media requires params.path or params.uri for Kdenlive bin ingestion");
        return res;
    }

    const QFileInfo fi(sourcePath);
    if (!fi.exists()) {
        res.message = QStringLiteral("Source media file does not exist: '%1'").arg(sourcePath);
        return res;
    }
    const QString canonicalPath = fi.canonicalFilePath();

    const std::shared_ptr<ProjectItemModel> binModel = Core::self()->projectItemModel();
    if (!binModel || !binModel->getRootFolder()) {
        res.message = QStringLiteral("Kdenlive ProjectItemModel is not initialized");
        return res;
    }

    // Deduplicate against existing bin clips by canonical path to prevent duplicate media storage
    for (const QString &existingId : binModel->getAllClipIds()) {
        if (auto existingClip = binModel->getClipByBinID(existingId)) {
            const QString existingCanonical = QFileInfo(existingClip->clipUrl()).canonicalFilePath();
            if (!existingCanonical.isEmpty() && existingCanonical == canonicalPath) {
                if (!op.mediaRef.trimmed().isEmpty()) {
                    m_mediaAliases.insert(op.mediaRef.trimmed(), existingId);
                }
                res.applied = true;
                res.createdId = existingId;
                res.message = QStringLiteral("Reused existing Kdenlive Bin clip id '%1'").arg(existingId);
                return res;
            }
        }
    }

    const QString parentFolder = op.params.value(QStringLiteral("folder_id")).toString(binModel->getRootFolder()->clipId());
    const QString createdBinId = ClipCreator::createClipFromFile(sourcePath, parentFolder, binModel, m_txUndo, m_txRedo);
    if (createdBinId.isEmpty() || createdBinId == QStringLiteral("-1")) {
        res.message = QStringLiteral("Kdenlive ClipCreator failed to import '%1'").arg(sourcePath);
        return res;
    }

    if (!op.mediaRef.trimmed().isEmpty()) {
        m_mediaAliases.insert(op.mediaRef.trimmed(), createdBinId);
    }

    res.applied = true;
    res.createdId = createdBinId;
    res.message = QStringLiteral("Imported media into Kdenlive Bin as id '%1'").arg(createdBinId);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleCreateSequence(const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    const std::shared_ptr<ProjectItemModel> binModel = Core::self()->projectItemModel();
    if (!binModel || !binModel->getRootFolder()) {
        res.message = QStringLiteral("Kdenlive ProjectItemModel is not initialized");
        return res;
    }

    const QString seqName = op.params.value(QStringLiteral("name")).toString(QStringLiteral("AI Sequence"));
    const int videoTracks = qMax(1, op.params.value(QStringLiteral("video_tracks")).toInt(2));
    const int audioTracks = qMax(1, op.params.value(QStringLiteral("audio_tracks")).toInt(2));
    const QString parentFolder = binModel->getRootFolder()->clipId();

    // ClipCreator takes {audio, video} (V1.5 passed them swapped, harmless only while both counts were equal).
    const QString binSeqId = ClipCreator::createPlaylistClipWithUndo(seqName, {audioTracks, videoTracks}, parentFolder, binModel, m_txUndo, m_txRedo);
    if (binSeqId.isEmpty() || binSeqId == QStringLiteral("-1")) {
        res.message = QStringLiteral("Failed to create Kdenlive sequence '%1'").arg(seqName);
        return res;
    }

    res.applied = true;
    res.createdId = binSeqId;
    res.message = QStringLiteral("Created Kdenlive sequence '%1' (bin id '%2')").arg(seqName, binSeqId);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleCreateTrack(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    const QString kind = firstNonEmptyString(op.params, {QStringLiteral("kind"), QStringLiteral("track_type")}).toLower();
    const bool isAudio = (kind == QStringLiteral("audio"));
    const QString trackName = op.params.value(QStringLiteral("name")).toString(op.trackId);
    const int position = op.params.value(QStringLiteral("position")).toInt(-1);

    int createdTrackId = -1;
    if (!timeline->requestTrackInsertion(position, createdTrackId, trackName, isAudio, m_txUndo, m_txRedo)) {
        res.message = QStringLiteral("TimelineItemModel::requestTrackInsertion failed for track '%1'").arg(trackName);
        return res;
    }

    if (!op.trackId.trimmed().isEmpty()) {
        m_trackAliases.insert(op.trackId.trimmed(), createdTrackId);
    }

    res.applied = true;
    res.createdId = QString::number(createdTrackId);
    res.message = QStringLiteral("Inserted Kdenlive %1 track id %2").arg(isAudio ? QStringLiteral("audio") : QStringLiteral("video")).arg(createdTrackId);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handlePlaceClip(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    if (op.params.value(QStringLiteral("clip_kind")).toString() == QStringLiteral("kdenlivetitle")) {
        return handleAddLowerThird(timeline, op);
    }

    int trackId = -1;
    if (!resolveTrackId(timeline, op.trackId, &trackId)) {
        res.message = QStringLiteral("Unknown Kdenlive track_id '%1'").arg(op.trackId);
        return res;
    }

    const QString binId = resolveBinId(op.mediaRef);
    if (binId.isEmpty()) {
        res.message = QStringLiteral("place_clip requires non-empty media_ref");
        return res;
    }

    // Kdenlive inserts an audio+video clip on a video track as a grouped A/V pair unless the bin id carries its
    // stream prefix. The agents place picture (V1) and dialogue (A1) separately, so honour "stream": an untrimmed
    // audio twin would otherwise occupy the audio track and block the next shot.
    const QString stream = op.params.value(QStringLiteral("stream")).toString();
    const QString insertId =
        stream == QLatin1String("video") ? QStringLiteral("V") + binId : (stream == QLatin1String("audio") ? QStringLiteral("A") + binId : binId);
    int createdClipId = -1;
    if (!timeline->requestClipInsertion(insertId, trackId, op.frameRange.timelineStart, createdClipId, false, true, false, m_txUndo, m_txRedo)) {
        res.message = QStringLiteral("TimelineItemModel::requestClipInsertion failed for bin '%1' on track %2").arg(binId).arg(trackId);
        return res;
    }

    // Apply source inFrame trim (left edge) followed by outFrame duration (right edge)
    const int initialPlaytime = timeline->getClipPlaytime(createdClipId);
    if (op.frameRange.inFrame > 0 && initialPlaytime > op.frameRange.inFrame) {
        int leftTrimmedSize = initialPlaytime - op.frameRange.inFrame;
        if (timeline->requestItemResize(createdClipId, leftTrimmedSize, false, false, m_txUndo, m_txRedo)) {
            timeline->requestClipMove(createdClipId, trackId, op.frameRange.timelineStart, true, true, false, true, m_txUndo, m_txRedo);
        }
    }

    if (op.frameRange.outFrame > op.frameRange.inFrame) {
        int requestedDuration = op.frameRange.outFrame - op.frameRange.inFrame;
        if (requestedDuration > 0 && requestedDuration != timeline->getClipPlaytime(createdClipId)) {
            if (!timeline->requestItemResize(createdClipId, requestedDuration, true, false, m_txUndo, m_txRedo)) {
                res.message = QStringLiteral("Inserted clip %1 but failed to resize to duration %2").arg(createdClipId).arg(requestedDuration);
                return res;
            }
        }
    }

    if (!op.clipId.trimmed().isEmpty()) {
        m_clipAliases.insert(op.clipId.trimmed(), createdClipId);
    }

    res.applied = true;
    res.createdId = QString::number(createdClipId);
    res.message = QStringLiteral("Inserted clip %1 on track %2 at frame %3").arg(createdClipId).arg(trackId).arg(op.frameRange.timelineStart);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleMoveClip(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    int clipId = -1;
    if (!resolveClipId(timeline, op.clipId, &clipId)) {
        res.message = QStringLiteral("Unknown Kdenlive clip_id '%1'").arg(op.clipId);
        return res;
    }

    int targetTrackId = timeline->getClipTrackId(clipId);
    if (!op.trackId.trimmed().isEmpty() && !resolveTrackId(timeline, op.trackId, &targetTrackId)) {
        res.message = QStringLiteral("Unknown target track_id '%1'").arg(op.trackId);
        return res;
    }

    const int targetPos =
        op.params.contains(QStringLiteral("timeline_start")) ? op.params.value(QStringLiteral("timeline_start")).toInt(0) : op.frameRange.timelineStart;
    if (!timeline->requestClipMove(clipId, targetTrackId, targetPos, true, true, false, true, m_txUndo, m_txRedo)) {
        res.message = QStringLiteral("TimelineItemModel::requestClipMove failed for clip %1").arg(clipId);
        return res;
    }

    res.applied = true;
    res.createdId = QString::number(clipId);
    res.message = QStringLiteral("Moved clip %1 to track %2 frame %3").arg(clipId).arg(targetTrackId).arg(targetPos);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleTrimClip(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    int clipId = -1;
    if (!resolveClipId(timeline, op.clipId, &clipId)) {
        res.message = QStringLiteral("Unknown Kdenlive clip_id '%1'").arg(op.clipId);
        return res;
    }

    int targetDuration = op.frameRange.outFrame - op.frameRange.inFrame;
    if (targetDuration <= 0) {
        targetDuration = op.params.value(QStringLiteral("duration_frames")).toInt(0);
    }
    if (targetDuration <= 0) {
        res.message = QStringLiteral("trim_clip requires positive duration");
        return res;
    }

    const bool trimRight = op.params.value(QStringLiteral("right")).toBool(true);
    if (!timeline->requestItemResize(clipId, targetDuration, trimRight, false, m_txUndo, m_txRedo)) {
        res.message = QStringLiteral("TimelineItemModel::requestItemResize failed for clip %1").arg(clipId);
        return res;
    }

    res.applied = true;
    res.createdId = QString::number(clipId);
    res.message = QStringLiteral("Resized clip %1 to %2 frames").arg(clipId).arg(targetDuration);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleSplitClip(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    int clipId = -1;
    if (!resolveClipId(timeline, op.clipId, &clipId)) {
        res.message = QStringLiteral("Unknown Kdenlive clip_id '%1'").arg(op.clipId);
        return res;
    }

    int splitFrame = op.frameRange.timelineStart;
    if (op.params.contains(QStringLiteral("split_frame"))) {
        splitFrame = op.params.value(QStringLiteral("split_frame")).toInt(-1);
    } else if (op.params.contains(QStringLiteral("position"))) {
        splitFrame = op.params.value(QStringLiteral("position")).toInt(-1);
    }

    int newClipId = -1;
    if (!TimelineFunctions::processClipCut(timeline, clipId, splitFrame, newClipId, m_txUndo, m_txRedo)) {
        res.message = QStringLiteral("TimelineFunctions::processClipCut failed for clip %1 at frame %2").arg(clipId).arg(splitFrame);
        return res;
    }

    const QString alias = op.params.value(QStringLiteral("new_clip_id")).toString().trimmed();
    if (!alias.isEmpty()) {
        m_clipAliases.insert(alias, newClipId);
    }

    res.applied = true;
    res.createdId = QString::number(newClipId);
    res.message = QStringLiteral("Split clip %1 at frame %2; right clip id is %3").arg(clipId).arg(splitFrame).arg(newClipId);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleDeleteClip(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    int clipId = -1;
    if (!resolveClipId(timeline, op.clipId, &clipId)) {
        res.message = QStringLiteral("Unknown Kdenlive clip_id '%1'").arg(op.clipId);
        return res;
    }

    if (!timeline->requestItemDeletion(clipId, m_txUndo, m_txRedo, false)) {
        res.message = QStringLiteral("TimelineItemModel::requestItemDeletion failed for clip %1").arg(clipId);
        return res;
    }

    res.applied = true;
    res.message = QStringLiteral("Deleted clip %1").arg(clipId);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleSyncClips(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    const QJsonArray clipIds = op.params.value(QStringLiteral("clip_ids")).toArray();
    if (clipIds.size() < 2) {
        res.message = QStringLiteral("sync_clips requires at least 2 clip_ids");
        return res;
    }

    int refCid = -1;
    const QString refKey = op.params.value(QStringLiteral("reference_clip_id")).toString(clipIds.first().toString());
    if (!resolveClipId(timeline, refKey, &refCid)) {
        res.message = QStringLiteral("Unknown reference clip '%1'").arg(refKey);
        return res;
    }
    const int refPos = timeline->getClipPosition(refCid);
    const QJsonObject offsets = op.params.value(QStringLiteral("offsets")).toObject();

    for (const QJsonValue &v : clipIds) {
        const QString key = v.toString();
        int cid = -1;
        if (!resolveClipId(timeline, key, &cid)) {
            res.message = QStringLiteral("Unknown clip '%1' in sync_clips").arg(key);
            return res;
        }
        const int targetPos = qMax(0, refPos + offsets.value(key).toInt(0));
        const int tid = timeline->getClipTrackId(cid);
        if (!timeline->requestClipMove(cid, tid, targetPos, true, true, false, true, m_txUndo, m_txRedo)) {
            res.message = QStringLiteral("Failed to align clip %1 during sync_clips").arg(cid);
            return res;
        }
    }

    res.applied = true;
    res.message = QStringLiteral("Synchronized %1 Kdenlive clips").arg(clipIds.size());
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleSelectCamera(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    const QString cameraId = firstNonEmptyString(op.params, {QStringLiteral("camera_id"), QStringLiteral("angle")});
    if (cameraId.isEmpty() && !op.params.contains(QStringLiteral("camera_index"))) {
        res.message = QStringLiteral("select_camera requires camera_id, angle, or camera_index");
        return res;
    }

    if (!op.clipId.trimmed().isEmpty()) {
        int clipId = -1;
        if (!resolveClipId(timeline, op.clipId, &clipId)) {
            res.message = QStringLiteral("Unknown Kdenlive clip_id '%1'").arg(op.clipId);
            return res;
        }
        const bool enableClip = op.params.value(QStringLiteral("active")).toBool(true);
        const PlaylistState::ClipState desiredState = enableClip ? PlaylistState::VideoOnly : PlaylistState::Disabled;
        if (!TimelineFunctions::changeClipState(timeline, clipId, desiredState, m_txUndo, m_txRedo)) {
            res.message = QStringLiteral("Failed to toggle camera clip state for clip %1").arg(clipId);
            return res;
        }
    }

    res.applied = true;
    res.createdId = cameraId.isEmpty() ? QStringLiteral("cam-%1").arg(op.params.value(QStringLiteral("camera_index")).toInt(0)) : cameraId;
    res.message = QStringLiteral("Applied multicam camera selection '%1'").arg(res.createdId);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleAddEffect(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op,
                                                                bool asAudioFilter)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    // The agents send an MLT service plus their own label (filter_id "flt_a1_highpass"). Use the first candidate that
    // Kdenlive's effect repository knows: an unknown id makes Kdenlive throw (V1.5 aborted the whole app here).
    const QStringList idKeys =
        asAudioFilter ? QStringList{QStringLiteral("mlt_service"), QStringLiteral("effect_id"), QStringLiteral("filter"), QStringLiteral("filter_id")}
                      : QStringList{QStringLiteral("mlt_service"), QStringLiteral("effect_id"), QStringLiteral("effect")};
    QString effectId;
    QStringList tried;
    for (const QString &key : idKeys) {
        const QString candidate = op.params.value(key).toString().trimmed();
        if (candidate.isEmpty()) {
            continue;
        }
        tried << candidate;
        if (EffectsRepository::get()->exists(candidate)) {
            effectId = candidate;
            break;
        }
    }
    if (effectId.isEmpty()) {
        res.message = tried.isEmpty() ? QStringLiteral("Missing effect_id / filter_id parameter")
                                      : QStringLiteral("No installed Kdenlive effect matches %1").arg(tried.join(QStringLiteral(", ")));
        return res;
    }

    std::shared_ptr<EffectStackModel> stack;
    if (!op.clipId.trimmed().isEmpty()) {
        int clipId = -1;
        if (!resolveClipId(timeline, op.clipId, &clipId)) {
            res.message = QStringLiteral("Unknown clip_id '%1'").arg(op.clipId);
            return res;
        }
        stack = timeline->getClipEffectStack(clipId);
    } else if (!op.trackId.trimmed().isEmpty()) {
        int trackId = -1;
        if (!resolveTrackId(timeline, op.trackId, &trackId)) {
            res.message = QStringLiteral("Unknown track_id '%1'").arg(op.trackId);
            return res;
        }
        stack = timeline->getTrackEffectStackModel(trackId);
    } else {
        stack = timeline->getMasterEffectStackModel();
    }

    if (!stack) {
        res.message = QStringLiteral("Target EffectStackModel is unavailable");
        return res;
    }

    if (!stack->appendEffectWithUndo(effectId, m_txUndo, m_txRedo)) {
        res.message = QStringLiteral("EffectStackModel::appendEffectWithUndo failed for '%1'").arg(effectId);
        return res;
    }

    QJsonObject initialParams = op.params.value(QStringLiteral("parameters")).toObject();
    if (initialParams.isEmpty()) {
        initialParams = op.params.value(QStringLiteral("mlt_params")).toObject(); // the department's parameter key
    }
    if (!initialParams.isEmpty()) {
        if (auto assetModel = stack->getAssetModelById(effectId)) {
            for (auto it = initialParams.constBegin(); it != initialParams.constEnd(); ++it) {
                assetModel->setParameter(it.key(), jsonValueToParamString(it.value()), true);
            }
        }
    }

    res.applied = true;
    res.createdId = effectId;
    res.message = QStringLiteral("Appended effect '%1' to Kdenlive EffectStackModel").arg(effectId);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleSetEffectParam(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    const QString effectId = firstNonEmptyString(op.params, {QStringLiteral("effect_id"), QStringLiteral("effect_instance_id"), QStringLiteral("filter_id")});
    const QString paramName = firstNonEmptyString(op.params, {QStringLiteral("param_name"), QStringLiteral("parameter")});
    if (effectId.isEmpty() || paramName.isEmpty() || !op.params.contains(QStringLiteral("value"))) {
        res.message = QStringLiteral("set_effect_param requires effect_id, param_name, and value");
        return res;
    }

    std::shared_ptr<EffectStackModel> stack;
    if (!op.clipId.trimmed().isEmpty()) {
        int clipId = -1;
        if (!resolveClipId(timeline, op.clipId, &clipId)) {
            res.message = QStringLiteral("Unknown clip_id '%1'").arg(op.clipId);
            return res;
        }
        stack = timeline->getClipEffectStack(clipId);
    } else if (!op.trackId.trimmed().isEmpty()) {
        int trackId = -1;
        if (!resolveTrackId(timeline, op.trackId, &trackId)) {
            res.message = QStringLiteral("Unknown track_id '%1'").arg(op.trackId);
            return res;
        }
        stack = timeline->getTrackEffectStackModel(trackId);
    } else {
        stack = timeline->getMasterEffectStackModel();
    }

    if (!stack) {
        res.message = QStringLiteral("Target EffectStackModel is unavailable");
        return res;
    }
    const std::shared_ptr<AssetParameterModel> assetModel = stack->getAssetModelById(effectId);
    if (!assetModel) {
        res.message = QStringLiteral("Effect '%1' not found on target EffectStackModel").arg(effectId);
        return res;
    }

    const QString prevVal = assetModel->getParamFromName(paramName).toString();
    const QString nextVal = jsonValueToParamString(op.params.value(QStringLiteral("value")));
    assetModel->setParameter(paramName, nextVal, true);

    std::weak_ptr<AssetParameterModel> weakAsset = assetModel;
    Fun localUndo = [weakAsset, paramName, prevVal]() {
        if (auto locked = weakAsset.lock()) {
            locked->setParameter(paramName, prevVal, true);
            return true;
        }
        return false;
    };
    Fun localRedo = [weakAsset, paramName, nextVal]() {
        if (auto locked = weakAsset.lock()) {
            locked->setParameter(paramName, nextVal, true);
            return true;
        }
        return false;
    };
    composeUndoRedo(std::move(localUndo), std::move(localRedo));

    res.applied = true;
    res.createdId = effectId;
    res.message = QStringLiteral("Updated effect '%1' parameter '%2'").arg(effectId, paramName);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleSetKeyframe(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    int clipId = -1;
    if (!resolveClipId(timeline, op.clipId, &clipId)) {
        res.message = QStringLiteral("set_keyframe requires valid clip_id on Kdenlive backend");
        return res;
    }

    const std::shared_ptr<EffectStackModel> stack = timeline->getClipEffectStack(clipId);
    if (!stack) {
        res.message = QStringLiteral("Clip %1 has no EffectStackModel").arg(clipId);
        return res;
    }

    const int frame = op.params.contains(QStringLiteral("frame")) ? op.params.value(QStringLiteral("frame")).toInt(0) : op.frameRange.inFrame;
    const double val = op.params.value(QStringLiteral("value")).toDouble(0.0);
    if (!stack->addEffectKeyFrame(frame, val)) {
        res.message = QStringLiteral("EffectStackModel::addEffectKeyFrame failed at frame %1").arg(frame);
        return res;
    }

    std::weak_ptr<EffectStackModel> weakStack = stack;
    Fun localUndo = [weakStack, frame]() {
        if (auto locked = weakStack.lock()) {
            return locked->removeKeyFrame(frame);
        }
        return false;
    };
    Fun localRedo = [weakStack, frame, val]() {
        if (auto locked = weakStack.lock()) {
            return locked->addEffectKeyFrame(frame, val);
        }
        return false;
    };
    composeUndoRedo(std::move(localUndo), std::move(localRedo));

    res.applied = true;
    res.createdId = QString::number(clipId);
    res.message = QStringLiteral("Added keyframe on clip %1 at frame %2").arg(clipId).arg(frame);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleSetAudioLevel(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    std::shared_ptr<EffectStackModel> stack;
    if (!op.clipId.trimmed().isEmpty()) {
        int clipId = -1;
        if (!resolveClipId(timeline, op.clipId, &clipId)) {
            res.message = QStringLiteral("Unknown clip_id '%1'").arg(op.clipId);
            return res;
        }
        stack = timeline->getClipEffectStack(clipId);
    } else if (!op.trackId.trimmed().isEmpty()) {
        int trackId = -1;
        if (!resolveTrackId(timeline, op.trackId, &trackId)) {
            res.message = QStringLiteral("Unknown track_id '%1'").arg(op.trackId);
            return res;
        }
        stack = timeline->getTrackEffectStackModel(trackId);
    } else {
        stack = timeline->getMasterEffectStackModel();
    }

    if (!stack) {
        res.message = QStringLiteral("Target EffectStackModel unavailable for set_audio_level");
        return res;
    }

    const QString volumeFilterId = QStringLiteral("volume");
    if (!stack->hasFilter(volumeFilterId)) {
        if (!stack->appendEffectWithUndo(volumeFilterId, m_txUndo, m_txRedo)) {
            res.message = QStringLiteral("Failed to append MLT volume filter for set_audio_level");
            return res;
        }
    }

    const double gainDb = op.params.value(QStringLiteral("gain_db")).toDouble(op.params.value(QStringLiteral("level_db")).toDouble(0.0));
    if (auto assetModel = stack->getAssetModelById(volumeFilterId)) {
        const QString prevVal = assetModel->getParamFromName(QStringLiteral("level")).toString();
        const QString nextVal = QString::number(gainDb, 'f', 2);
        assetModel->setParameter(QStringLiteral("level"), nextVal, true);

        std::weak_ptr<AssetParameterModel> weakAsset = assetModel;
        Fun localUndo = [weakAsset, prevVal]() {
            if (auto locked = weakAsset.lock()) {
                locked->setParameter(QStringLiteral("level"), prevVal, true);
                return true;
            }
            return false;
        };
        Fun localRedo = [weakAsset, nextVal]() {
            if (auto locked = weakAsset.lock()) {
                locked->setParameter(QStringLiteral("level"), nextVal, true);
                return true;
            }
            return false;
        };
        composeUndoRedo(std::move(localUndo), std::move(localRedo));
    }

    res.applied = true;
    res.message = QStringLiteral("Set MLT volume level to %1 dB").arg(gainDb, 0, 'f', 2);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleAddCaption(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    const QString text = op.params.value(QStringLiteral("text")).toString().trimmed();
    if (text.isEmpty()) {
        res.message = QStringLiteral("add_caption requires non-empty params.text");
        return res;
    }

    std::shared_ptr<SubtitleModel> subModel = timeline->getSubtitleModel();
    if (!subModel) {
        subModel = timeline->createSubtitleModel();
    }
    if (!subModel) {
        res.message = QStringLiteral("Failed to obtain Kdenlive SubtitleModel");
        return res;
    }

    const double fps = Core::self()->getCurrentFps() > 0.0 ? Core::self()->getCurrentFps() : 25.0;
    const int startFrame = op.frameRange.timelineStart;
    const int duration = op.frameRange.outFrame > op.frameRange.inFrame ? (op.frameRange.outFrame - op.frameRange.inFrame)
                                                                        : op.params.value(QStringLiteral("duration_frames")).toInt(50);
    const GenTime startTime(startFrame, fps);
    const GenTime endTime(startFrame + qMax(1, duration), fps);

    if (!subModel->addSubtitle(startTime, endTime, text, m_txUndo, m_txRedo)) {
        res.message = QStringLiteral("SubtitleModel::addSubtitle failed");
        return res;
    }

    res.applied = true;
    res.createdId = QString::number(subModel->getIdForStartPos(startTime));
    res.message = QStringLiteral("Added subtitle caption at frame %1").arg(startFrame);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleUpdateCaption(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    std::shared_ptr<SubtitleModel> subModel = timeline->getSubtitleModel();
    if (!subModel) {
        res.message = QStringLiteral("Timeline has no active SubtitleModel");
        return res;
    }

    const QString newText = op.params.value(QStringLiteral("text")).toString().trimmed();
    if (newText.isEmpty()) {
        res.message = QStringLiteral("update_caption requires non-empty params.text");
        return res;
    }

    int subId = op.params.value(QStringLiteral("caption_id")).toString().toInt();
    if (subId <= 0) {
        const double fps = Core::self()->getCurrentFps() > 0.0 ? Core::self()->getCurrentFps() : 25.0;
        subId = subModel->getIdForStartPos(GenTime(op.frameRange.timelineStart, fps));
    }
    if (subId <= 0) {
        res.message = QStringLiteral("Could not locate target subtitle caption for update_caption");
        return res;
    }

    const QString prevText = subModel->getText(subId);
    if (!subModel->editSubtitle(subId, newText)) {
        res.message = QStringLiteral("SubtitleModel::editSubtitle failed for subtitle %1").arg(subId);
        return res;
    }

    std::weak_ptr<SubtitleModel> weakSub = subModel;
    composeUndoRedo(
        [weakSub, subId, prevText]() {
            if (auto locked = weakSub.lock()) {
                return locked->editSubtitle(subId, prevText);
            }
            return false;
        },
        [weakSub, subId, newText]() {
            if (auto locked = weakSub.lock()) {
                return locked->editSubtitle(subId, newText);
            }
            return false;
        });

    res.applied = true;
    res.createdId = QString::number(subId);
    res.message = QStringLiteral("Updated subtitle caption %1").arg(subId);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleDeleteCaption(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    std::shared_ptr<SubtitleModel> subModel = timeline->getSubtitleModel();
    if (!subModel) {
        res.message = QStringLiteral("Timeline has no active SubtitleModel");
        return res;
    }

    const double fps = Core::self()->getCurrentFps() > 0.0 ? Core::self()->getCurrentFps() : 25.0;
    int subId = op.params.value(QStringLiteral("caption_id")).toString().toInt();
    if (subId <= 0) {
        subId = subModel->getIdForStartPos(GenTime(op.frameRange.timelineStart, fps));
    }
    if (subId <= 0) {
        res.message = QStringLiteral("Could not locate target subtitle caption for delete_caption");
        return res;
    }

    const QString prevText = subModel->getText(subId);
    const GenTime startTime(op.frameRange.timelineStart, fps);
    const int duration = qMax(1, op.frameRange.outFrame - op.frameRange.inFrame);
    const GenTime endTime(op.frameRange.timelineStart + duration, fps);

    if (!subModel->removeSubtitle(subId)) {
        res.message = QStringLiteral("SubtitleModel::removeSubtitle failed for subtitle %1").arg(subId);
        return res;
    }

    std::weak_ptr<SubtitleModel> weakSub = subModel;
    composeUndoRedo(
        [weakSub, subId, startTime, endTime, prevText]() {
            if (auto locked = weakSub.lock()) {
                return locked->addSubtitle(subId, startTime, endTime, prevText);
            }
            return false;
        },
        [weakSub, startTime]() {
            if (auto locked = weakSub.lock()) {
                const int id = locked->getIdForStartPos(startTime);
                return id > 0 ? locked->removeSubtitle(id) : true;
            }
            return false;
        });

    res.applied = true;
    res.createdId = QString::number(subId);
    res.message = QStringLiteral("Deleted subtitle caption %1").arg(subId);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleAddLowerThird(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    const std::shared_ptr<ProjectItemModel> binModel = Core::self()->projectItemModel();
    if (!binModel || !binModel->getRootFolder()) {
        res.message = QStringLiteral("Kdenlive ProjectItemModel is not initialized");
        return res;
    }

    int trackId = -1;
    const QString requestedTrack = op.trackId.trimmed().isEmpty() ? QStringLiteral("V2") : op.trackId;
    if (!resolveTrackId(timeline, requestedTrack, &trackId)) {
        res.message = QStringLiteral("Unknown target track '%1' for lower third").arg(requestedTrack);
        return res;
    }

    const QString speakerName = firstNonEmptyString(op.params, {QStringLiteral("speaker_name"), QStringLiteral("title"), QStringLiteral("name")});
    const QString subtitle = op.params.value(QStringLiteral("subtitle")).toString();
    const QString accentHex = op.params.value(QStringLiteral("accent_hex")).toString(QStringLiteral("#FF5A1F"));
    QString titleXml = op.params.value(QStringLiteral("kdenlive_title_xml")).toString();
    if (titleXml.isEmpty()) {
        const QString escSpeaker = speakerName.toHtmlEscaped();
        const QString escSub = subtitle.toHtmlEscaped();
        titleXml = QStringLiteral(
                       "<kdenlivetitle duration=\"125\" LC_NUMERIC=\"C\" width=\"1920\" height=\"1080\" out=\"124\">"
                       "<item type=\"QGraphicsRectItem\" z-index=\"0\">"
                       "<position x=\"100\" y=\"840\"><transform>1,0,0,0,1,0,0,0,1</transform></position>"
                       "<content brushcolor=\"20,23,29,220\" pencolor=\"0,0,0,0\" penwidth=\"0\" rect=\"0,0,620,120\"/>"
                       "</item>"
                       "<item type=\"QGraphicsRectItem\" z-index=\"1\">"
                       "<position x=\"100\" y=\"840\"><transform>1,0,0,0,1,0,0,0,1</transform></position>"
                       "<content brushcolor=\"%3\" pencolor=\"0,0,0,0\" penwidth=\"0\" rect=\"0,0,10,120\"/>"
                       "</item>"
                       "<item type=\"QGraphicsTextItem\" z-index=\"2\">"
                       "<position x=\"132\" y=\"856\"><transform>1,0,0,0,1,0,0,0,1</transform></position>"
                       "<content font=\"Sans\" font-size=\"40\" font-weight=\"700\" font-color=\"255,255,255,255\">%1</content>"
                       "</item>"
                       "<item type=\"QGraphicsTextItem\" z-index=\"3\">"
                       "<position x=\"132\" y=\"910\"><transform>1,0,0,0,1,0,0,0,1</transform></position>"
                       "<content font=\"Sans\" font-size=\"24\" font-weight=\"500\" font-color=\"200,208,220,255\">%2</content>"
                       "</item>"
                       "</kdenlivetitle>")
                       .arg(escSpeaker, escSub, accentHex);
    }

    const int duration = op.frameRange.outFrame > op.frameRange.inFrame
                             ? (op.frameRange.outFrame - op.frameRange.inFrame)
                             : op.params.value(QStringLiteral("duration_frames")).toInt(125);
    std::unordered_map<QString, QString> props;
    props[QStringLiteral("xmldata")] = titleXml;
    const QString displayName = speakerName.isEmpty() ? QStringLiteral("Lower Third") : QStringLiteral("LT: %1").arg(speakerName);
    const QString binId = ClipCreator::createTitleClip(props, qMax(1, duration), displayName, binModel->getRootFolder()->clipId(), binModel);
    if (binId.isEmpty() || binId == QStringLiteral("-1")) {
        res.message = QStringLiteral("ClipCreator::createTitleClip failed for lower third '%1'").arg(displayName);
        return res;
    }

    int createdClipId = -1;
    if (!timeline->requestClipInsertion(binId, trackId, op.frameRange.timelineStart, createdClipId, false, true, false, m_txUndo, m_txRedo)) {
        res.message = QStringLiteral("Failed to place lower-third clip '%1' onto track %2").arg(binId).arg(trackId);
        return res;
    }

    if (!op.clipId.trimmed().isEmpty()) {
        m_clipAliases.insert(op.clipId.trimmed(), createdClipId);
    }

    res.applied = true;
    res.createdId = QString::number(createdClipId);
    res.message = QStringLiteral("Placed lower-third '%1' on track %2 at frame %3").arg(displayName).arg(trackId).arg(op.frameRange.timelineStart);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleApplyMask(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    std::shared_ptr<EffectStackModel> stack;
    if (!op.clipId.trimmed().isEmpty()) {
        int clipId = -1;
        if (!resolveClipId(timeline, op.clipId, &clipId)) {
            res.message = QStringLiteral("Unknown clip_id '%1' for apply_mask").arg(op.clipId);
            return res;
        }
        stack = timeline->getClipEffectStack(clipId);
    } else if (!op.trackId.trimmed().isEmpty()) {
        int trackId = -1;
        if (!resolveTrackId(timeline, op.trackId, &trackId)) {
            res.message = QStringLiteral("Unknown track_id '%1' for apply_mask").arg(op.trackId);
            return res;
        }
        stack = timeline->getTrackEffectStackModel(trackId);
    } else {
        stack = timeline->getMasterEffectStackModel();
    }

    if (!stack) {
        res.message = QStringLiteral("Target EffectStackModel unavailable for apply_mask");
        return res;
    }

    const QString mattePath = op.params.value(QStringLiteral("matte_path")).toString().trimmed();
    const QJsonObject maskedEffect = op.params.value(QStringLiteral("masked_effect")).toObject();
    const QString maskedId = maskedEffect.value(QStringLiteral("kdenlive_id")).toString(maskedEffect.value(QStringLiteral("mlt_service")).toString()).trimmed();
    // Mattes are white on the subject; MLT's luma shape makes luma above the threshold transparent. The
    // threshold is the midpoint (100 keeps everything) and the selection is the white region unless the
    // operation is inverted ("outside the subject"). Identical to project_builder.py (pixel-checked renders).
    const QString kMatteThreshold = QStringLiteral("50");
    const QString mltMatteInvert = op.params.value(QStringLiteral("invert")).toBool(false) ? QStringLiteral("0") : QStringLiteral("1");
    if (!mattePath.isEmpty() && !maskedId.isEmpty()) {
        // An effect limited to the matte uses Kdenlive's native mask workflow, exactly as the department's
        // draft builder writes it: mask_start (shape matte) -> the masked effect -> mask_apply.
        for (const QString &id : {QStringLiteral("mask_start-shape"), maskedId, QStringLiteral("mask_apply")}) {
            if (!stack->appendEffectWithUndo(id, m_txUndo, m_txRedo)) {
                res.message = QStringLiteral("EffectStackModel::appendEffectWithUndo failed for mask workflow effect '%1'").arg(id);
                return res;
            }
        }
        if (auto start = stack->getAssetModelById(QStringLiteral("mask_start-shape"))) {
            start->setParameter(QStringLiteral("filter.resource"), mattePath, true);
            start->setParameter(QStringLiteral("filter.mix"), kMatteThreshold, true);
            start->setParameter(QStringLiteral("filter.softness"),
                                QString::number(qBound(0.0, op.params.value(QStringLiteral("feather")).toDouble(0.0) / 100.0, 1.0), 'f', 3), true);
            start->setParameter(QStringLiteral("filter.invert"), mltMatteInvert, true);
            start->setParameter(QStringLiteral("filter.use_luminance"), QStringLiteral("1"), true);
        }
        const QJsonObject props = maskedEffect.value(QStringLiteral("properties")).toObject();
        if (auto eff = props.isEmpty() ? nullptr : stack->getAssetModelById(maskedId)) {
            for (auto it = props.constBegin(); it != props.constEnd(); ++it) {
                eff->setParameter(it.key(), jsonValueToParamString(it.value()), true);
            }
        }
        res.applied = true;
        res.createdId = op.params.value(QStringLiteral("mask_id")).toString(QStringLiteral("mask_start-shape"));
        res.message = QStringLiteral("Applied '%1' through the matte '%2' (native mask workflow)").arg(maskedId, res.createdId);
        return res;
    }
    const QString serviceId = !mattePath.isEmpty() ? QStringLiteral("shape") : QStringLiteral("rotoscoping");
    if (!stack->appendEffectWithUndo(serviceId, m_txUndo, m_txRedo)) {
        res.message = QStringLiteral("EffectStackModel::appendEffectWithUndo failed for mask filter '%1'").arg(serviceId);
        return res;
    }

    if (auto assetModel = stack->getAssetModelById(serviceId)) {
        if (serviceId == QStringLiteral("shape")) {
            assetModel->setParameter(QStringLiteral("resource"), mattePath, true);
            assetModel->setParameter(QStringLiteral("use_luminance"), QStringLiteral("1"), true);
            assetModel->setParameter(QStringLiteral("mix"), kMatteThreshold, true);
            assetModel->setParameter(QStringLiteral("softness"), QString::number(qBound(0.0, op.params.value(QStringLiteral("feather")).toDouble(0.0) / 100.0, 1.0), 'f', 3), true);
            assetModel->setParameter(QStringLiteral("invert"), mltMatteInvert, true);
        } else {
            const QString splinesJson = op.params.value(QStringLiteral("splines_json")).toString();
            if (!splinesJson.isEmpty()) {
                assetModel->setParameter(QStringLiteral("spline"), splinesJson, true);
            }
            assetModel->setParameter(QStringLiteral("feather"), QString::number(qMax(0, op.params.value(QStringLiteral("feather")).toInt(0))), true);
            assetModel->setParameter(QStringLiteral("invert"), op.params.value(QStringLiteral("invert")).toBool(false) ? QStringLiteral("1") : QStringLiteral("0"), true);
            const QString opMode = op.params.value(QStringLiteral("operation")).toString(QStringLiteral("write_on_clear"));
            assetModel->setParameter(QStringLiteral("alpha_operation"), opMode, true);
        }
    }

    res.applied = true;
    res.createdId = op.params.value(QStringLiteral("mask_id")).toString(serviceId);
    res.message = QStringLiteral("Applied %1 mask '%2' to native EffectStackModel").arg(serviceId, res.createdId);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleAddTransition(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    const QString transitionId = firstNonEmptyString(op.params, {QStringLiteral("transition_id"), QStringLiteral("mlt_service"), QStringLiteral("type")});
    if (transitionId.isEmpty()) {
        res.message = QStringLiteral("add_transition requires params.transition_id");
        return res;
    }

    int trackId = -1;
    const QString trackRef = firstNonEmptyString(op.params, {QStringLiteral("b_track"), QStringLiteral("track_id")});
    if (!resolveTrackId(timeline, trackRef.isEmpty() ? op.trackId : trackRef, &trackId)) {
        res.message = QStringLiteral("Unknown target track for add_transition");
        return res;
    }

    int aTrack = -1;
    const QString aTrackRef = op.params.value(QStringLiteral("a_track")).toString();
    if (!aTrackRef.isEmpty()) {
        resolveTrackId(timeline, aTrackRef, &aTrack);
    }

    const int inFrame = op.params.contains(QStringLiteral("in_frame")) ? op.params.value(QStringLiteral("in_frame")).toInt(0) : op.frameRange.timelineStart;
    const int outFrame = op.params.contains(QStringLiteral("out_frame")) ? op.params.value(QStringLiteral("out_frame")).toInt(inFrame + 25) : op.frameRange.outFrame;
    const int duration = qMax(1, outFrame - inFrame);

    auto transProps = std::make_unique<Mlt::Properties>();
    const QJsonObject propsObj = op.params.value(QStringLiteral("properties")).toObject();
    for (auto it = propsObj.constBegin(); it != propsObj.constEnd(); ++it) {
        transProps->set(it.key().toUtf8().constData(), jsonValueToParamString(it.value()).toUtf8().constData());
    }

    int compId = -1;
    if (!timeline->requestCompositionInsertion(transitionId, trackId, aTrack, inFrame, duration, std::move(transProps), compId, m_txUndo, m_txRedo)) {
        res.message = QStringLiteral("TimelineItemModel::requestCompositionInsertion failed for transition '%1'").arg(transitionId);
        return res;
    }

    res.applied = true;
    res.createdId = QString::number(compId);
    res.message = QStringLiteral("Inserted transition '%1' (id %2) on track %3 at frame %4").arg(transitionId).arg(compId).arg(trackId).arg(inFrame);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleSetClipSpeed(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    int clipId = -1;
    if (!resolveClipId(timeline, op.clipId, &clipId)) {
        res.message = QStringLiteral("Unknown Kdenlive clip_id '%1' for set_clip_speed").arg(op.clipId);
        return res;
    }

    const double speedPercent = op.params.value(QStringLiteral("speed_percent")).toDouble(100.0);
    if (speedPercent <= 0.0 || speedPercent > 10000.0) {
        res.message = QStringLiteral("speed_percent must be in (0, 10000]");
        return res;
    }
    const bool pitchComp = op.params.value(QStringLiteral("pitch_compensate")).toBool(true);

    if (!timeline->requestClipTimeWarp(clipId, speedPercent / 100.0, pitchComp, true, m_txUndo, m_txRedo)) {
        res.message = QStringLiteral("TimelineItemModel::requestClipTimeWarp failed for clip %1").arg(clipId);
        return res;
    }

    res.applied = true;
    res.createdId = QString::number(clipId);
    res.message = QStringLiteral("Set clip %1 speed to %2%").arg(clipId).arg(speedPercent, 0, 'f', 1);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleAddMarker(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    const std::shared_ptr<MarkerListModel> guides = timeline->getGuideModel();
    if (!guides) {
        res.message = QStringLiteral("Timeline guide MarkerListModel is unavailable");
        return res;
    }

    const double fps = Core::self()->getCurrentFps() > 0.0 ? Core::self()->getCurrentFps() : 25.0;
    const int frame = op.params.contains(QStringLiteral("frame")) ? op.params.value(QStringLiteral("frame")).toInt(0) : op.frameRange.timelineStart;
    const QString label = firstNonEmptyString(op.params, {QStringLiteral("label"), QStringLiteral("comment"), QStringLiteral("name")});
    const int category = op.params.value(QStringLiteral("category_index")).toInt(-1);
    const GenTime pos(frame, fps);

    if (!guides->addMarker(pos, label, category)) {
        res.message = QStringLiteral("MarkerListModel::addMarker failed at frame %1").arg(frame);
        return res;
    }

    std::weak_ptr<MarkerListModel> weakGuides = guides;
    Fun localUndo = [weakGuides, pos]() {
        if (auto locked = weakGuides.lock()) {
            return locked->removeMarker(pos);
        }
        return false;
    };
    Fun localRedo = [weakGuides, pos, label, category]() {
        if (auto locked = weakGuides.lock()) {
            return locked->addMarker(pos, label, category);
        }
        return false;
    };
    composeUndoRedo(std::move(localUndo), std::move(localRedo));

    res.applied = true;
    res.createdId = QString::number(guides->markerIdAtFrame(frame));
    res.message = QStringLiteral("Added timeline guide marker at frame %1").arg(frame);
    return res;
}

TimelineOperationResult KdenliveTimelineBridge::handleExportSequence(const TimelineOperation &op, const QString &sequenceId)
{
    TimelineOperationResult res;
    res.opId = op.opId;

    const QString outputPath = firstNonEmptyString(op.params, {QStringLiteral("output_path"), QStringLiteral("destination")});
    if (outputPath.isEmpty()) {
        res.message = QStringLiteral("export_sequence requires params.output_path or params.destination");
        return res;
    }

    if (KdenliveDoc *doc = Core::self()->currentDoc(); doc != nullptr) {
        doc->setDocumentProperty(QStringLiteral("bangonpro.last_export_sequence"), sequenceId);
        doc->setDocumentProperty(QStringLiteral("bangonpro.last_export_target"), outputPath);
    }

    res.applied = true;
    res.createdId = QStringLiteral("kdenlive-export-%1").arg(sequenceId);
    res.message = QStringLiteral("Recorded headless export request to '%1'").arg(outputPath);
    res.metadata.insert(QStringLiteral("output_path"), outputPath);
    return res;
}

} // namespace BangOnPro

#endif // BANGONPRO_WITH_KDENLIVE_CORE
