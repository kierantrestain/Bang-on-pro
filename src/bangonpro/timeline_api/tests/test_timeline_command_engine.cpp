/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "TimelineCommandEngine.h"
#include "TimelineCommandServer.h"

#include <QCoreApplication>
#include <QDir>
#include <QElapsedTimer>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLocalSocket>
#include <QSignalSpy>
#include <QThread>
#include <QtTest/QtTest>

#include <atomic>
#include <memory>
#include <vector>

using namespace BangOnPro;

namespace {

TimelineCommandBatch makeBaseBatch(const QString &requestId, qint64 expectedRevision, bool requiresApproval = false, bool dryRun = false)
{
    TimelineCommandBatch batch;
    batch.apiVersion = QString::fromLatin1(kTimelineApiVersion);
    batch.requestId = requestId;
    batch.projectId = QStringLiteral("proj-bangonpro-01");
    batch.sequenceId = QStringLiteral("seq-main");
    batch.expectedRevision = expectedRevision;
    batch.agentId = QStringLiteral("agent-editor-01");
    batch.agentRole = QStringLiteral("rough_cut_editor");
    batch.requiresApproval = requiresApproval;
    batch.transactionLabel = QStringLiteral("Test Edit Batch");
    batch.dryRun = dryRun;
    return batch;
}

TimelineOperation makeOp(const QString &opId, const QString &type)
{
    TimelineOperation op;
    op.opId = opId;
    op.type = type;
    op.confidence = 0.95;
    op.rationale = QStringLiteral("Unit test operation");
    return op;
}

} // namespace

class TestTimelineCommandEngine : public QObject
{
    Q_OBJECT

private Q_SLOTS:
    void testMultiOpBatchExecution();
    void testMoveSyncDeleteAndJsonHistory();
    void testRevisionIncrementAndConflictDetection();
    void testAtomicRollbackMidBatch();
    void testApprovalWorkflowAcceptAndReject();
    void testProposalConflictAndRebase();
    void testUndoRedoRoundTrip();
    void testDryRunMode();
    void testLocalServerIpcRoundTrip();
    void testServerOversizedPayloadAndPipelining();
    void testValidationRejections();
    void testConcurrentSerializedWrites();
};

void TestTimelineCommandEngine::testMultiOpBatchExecution()
{
    auto backend = std::make_shared<InMemoryTimelineBackend>();
    TimelineCommandEngine engine(backend);

    TimelineCommandBatch batch = makeBaseBatch(QStringLiteral("req-multi-01"), 0, false, false);
    batch.transactionLabel = QStringLiteral("Full Editorial Assembly");

    // 1. create_sequence
    TimelineOperation opSeq = makeOp(QStringLiteral("op-seq"), QStringLiteral("create_sequence"));
    opSeq.params.insert(QStringLiteral("sequence_id"), QStringLiteral("seq-main"));
    opSeq.params.insert(QStringLiteral("name"), QStringLiteral("Episode 01 Master"));
    opSeq.params.insert(QStringLiteral("fps_num"), 25);
    opSeq.params.insert(QStringLiteral("fps_den"), 1);
    opSeq.params.insert(QStringLiteral("width"), 3840);
    opSeq.params.insert(QStringLiteral("height"), 2160);
    batch.operations.append(opSeq);

    // 2. import_media
    TimelineOperation opImport = makeOp(QStringLiteral("op-import"), QStringLiteral("import_media"));
    opImport.mediaRef = QStringLiteral("media-cam-a");
    opImport.params.insert(QStringLiteral("path"), QStringLiteral("/media/episode01/cam_a.mov"));
    opImport.params.insert(QStringLiteral("name"), QStringLiteral("Cam A Wide"));
    opImport.params.insert(QStringLiteral("duration_frames"), 1000);
    batch.operations.append(opImport);

    // 3. create_track (video)
    TimelineOperation opTrackV = makeOp(QStringLiteral("op-track-v1"), QStringLiteral("create_track"));
    opTrackV.trackId = QStringLiteral("V1");
    opTrackV.params.insert(QStringLiteral("kind"), QStringLiteral("video"));
    opTrackV.params.insert(QStringLiteral("name"), QStringLiteral("Camera A Video"));
    batch.operations.append(opTrackV);

    // 4. create_track (audio)
    TimelineOperation opTrackA = makeOp(QStringLiteral("op-track-a1"), QStringLiteral("create_track"));
    opTrackA.trackId = QStringLiteral("A1");
    opTrackA.params.insert(QStringLiteral("kind"), QStringLiteral("audio"));
    opTrackA.params.insert(QStringLiteral("name"), QStringLiteral("Host Dialogue"));
    batch.operations.append(opTrackA);

    // 5. place_clip
    TimelineOperation opPlace = makeOp(QStringLiteral("op-place-1"), QStringLiteral("place_clip"));
    opPlace.mediaRef = QStringLiteral("media-cam-a");
    opPlace.trackId = QStringLiteral("V1");
    opPlace.clipId = QStringLiteral("clip-intro");
    opPlace.frameRange = {0, 300, 0, 25, 1};
    batch.operations.append(opPlace);

    // 6. trim_clip
    TimelineOperation opTrim = makeOp(QStringLiteral("op-trim-1"), QStringLiteral("trim_clip"));
    opTrim.clipId = QStringLiteral("clip-intro");
    opTrim.frameRange = {25, 225, 0, 25, 1};
    batch.operations.append(opTrim);

    // 7. split_clip at timeline frame 100 (left duration 100, right duration 100)
    TimelineOperation opSplit = makeOp(QStringLiteral("op-split-1"), QStringLiteral("split_clip"));
    opSplit.clipId = QStringLiteral("clip-intro");
    opSplit.params.insert(QStringLiteral("split_frame"), 100);
    opSplit.params.insert(QStringLiteral("new_clip_id"), QStringLiteral("clip-intro-part2"));
    batch.operations.append(opSplit);

    // 8. add_effect
    TimelineOperation opFx = makeOp(QStringLiteral("op-fx-1"), QStringLiteral("add_effect"));
    opFx.clipId = QStringLiteral("clip-intro");
    opFx.params.insert(QStringLiteral("effect_id"), QStringLiteral("frei0r.coloradj_RGB"));
    opFx.params.insert(QStringLiteral("instance_id"), QStringLiteral("fx-grade-1"));
    QJsonObject fxParams;
    fxParams.insert(QStringLiteral("R"), 0.55);
    opFx.params.insert(QStringLiteral("parameters"), fxParams);
    batch.operations.append(opFx);

    // 9. set_effect_param
    TimelineOperation opFxParam = makeOp(QStringLiteral("op-fx-param-1"), QStringLiteral("set_effect_param"));
    opFxParam.clipId = QStringLiteral("clip-intro");
    opFxParam.params.insert(QStringLiteral("effect_instance_id"), QStringLiteral("fx-grade-1"));
    opFxParam.params.insert(QStringLiteral("param_name"), QStringLiteral("G"));
    opFxParam.params.insert(QStringLiteral("value"), 0.52);
    batch.operations.append(opFxParam);

    // 10. set_keyframe
    TimelineOperation opKf = makeOp(QStringLiteral("op-kf-1"), QStringLiteral("set_keyframe"));
    opKf.clipId = QStringLiteral("clip-intro");
    opKf.params.insert(QStringLiteral("effect_instance_id"), QStringLiteral("fx-grade-1"));
    opKf.params.insert(QStringLiteral("param_name"), QStringLiteral("R"));
    opKf.params.insert(QStringLiteral("frame"), 50);
    opKf.params.insert(QStringLiteral("value"), 0.65);
    opKf.params.insert(QStringLiteral("interpolation"), QStringLiteral("smooth"));
    batch.operations.append(opKf);

    // 11. set_audio_level
    TimelineOperation opAudio = makeOp(QStringLiteral("op-audio-1"), QStringLiteral("set_audio_level"));
    opAudio.clipId = QStringLiteral("clip-intro");
    opAudio.params.insert(QStringLiteral("gain_db"), -3.5);
    batch.operations.append(opAudio);

    // 12. add_audio_filter
    TimelineOperation opAudioFilter = makeOp(QStringLiteral("op-afx-1"), QStringLiteral("add_audio_filter"));
    opAudioFilter.trackId = QStringLiteral("A1");
    opAudioFilter.params.insert(QStringLiteral("filter_id"), QStringLiteral("avfilter.loudnorm"));
    batch.operations.append(opAudioFilter);

    // 13. add_caption
    TimelineOperation opCap = makeOp(QStringLiteral("op-cap-1"), QStringLiteral("add_caption"));
    opCap.frameRange = {0, 75, 10, 25, 1};
    opCap.params.insert(QStringLiteral("text"), QStringLiteral("Welcome to Bang On Pro"));
    opCap.params.insert(QStringLiteral("speaker"), QStringLiteral("Host"));
    batch.operations.append(opCap);

    // 14. add_marker
    TimelineOperation opMarker = makeOp(QStringLiteral("op-mkr-1"), QStringLiteral("add_marker"));
    opMarker.frameRange.timelineStart = 100;
    opMarker.params.insert(QStringLiteral("label"), QStringLiteral("Beat Drop Cut"));
    opMarker.params.insert(QStringLiteral("category"), QStringLiteral("pacing"));
    batch.operations.append(opMarker);

    // 15. select_camera
    TimelineOperation opCam = makeOp(QStringLiteral("op-cam-1"), QStringLiteral("select_camera"));
    opCam.clipId = QStringLiteral("clip-intro-part2");
    opCam.frameRange.timelineStart = 100;
    opCam.params.insert(QStringLiteral("camera_id"), QStringLiteral("cam-b-closeup"));
    batch.operations.append(opCam);

    // 16. export_sequence
    TimelineOperation opExport = makeOp(QStringLiteral("op-exp-1"), QStringLiteral("export_sequence"));
    opExport.params.insert(QStringLiteral("output_path"), QStringLiteral("/exports/ep01_master.mp4"));
    opExport.params.insert(QStringLiteral("preset"), QStringLiteral("youtube_4k_h264"));
    batch.operations.append(opExport);

    // Verify JSON round-trip of the batch before executing.
    const QJsonObject batchJson = TimelineCommandEngine::batchToJson(batch);
    TimelineCommandBatch parsedBatch;
    QString parseErr;
    QVERIFY2(TimelineCommandEngine::batchFromJson(batchJson, &parsedBatch, &parseErr), qPrintable(parseErr));

    const TimelineCommandResponse resp = engine.executeBatch(parsedBatch);
    QVERIFY2(resp.status == CommandStatus::Ok, qPrintable(resp.errorMessage));
    QCOMPARE(resp.previousRevision, 0);
    QCOMPARE(resp.currentRevision, 1);
    QCOMPARE(engine.currentRevision(), 1);
    QCOMPARE(resp.results.size(), 16);
    for (const TimelineOperationResult &r : resp.results) {
        QVERIFY2(r.applied, qPrintable(r.message));
    }

    // Verify resulting timeline state.
    QVERIFY(backend->hasSequence(QStringLiteral("seq-main")));
    QVERIFY(backend->hasMedia(QStringLiteral("media-cam-a")));
    QVERIFY(backend->hasTrack(QStringLiteral("V1")));
    QVERIFY(backend->hasTrack(QStringLiteral("A1")));
    QVERIFY(backend->hasClip(QStringLiteral("clip-intro")));
    QVERIFY(backend->hasClip(QStringLiteral("clip-intro-part2")));

    const auto leftClip = backend->clip(QStringLiteral("clip-intro"));
    const auto rightClip = backend->clip(QStringLiteral("clip-intro-part2"));
    QVERIFY(leftClip.has_value());
    QVERIFY(rightClip.has_value());
    QCOMPARE(leftClip->frameRange.inFrame, 25);
    QCOMPARE(leftClip->frameRange.outFrame, 125);
    QCOMPARE(leftClip->frameRange.timelineStart, 0);
    QCOMPARE(leftClip->duration(), 100);
    QCOMPARE(rightClip->frameRange.inFrame, 125);
    QCOMPARE(rightClip->frameRange.outFrame, 225);
    QCOMPARE(rightClip->frameRange.timelineStart, 100);
    QCOMPARE(rightClip->duration(), 100);
    QCOMPARE(rightClip->cameraId, QStringLiteral("cam-b-closeup"));

    QCOMPARE(leftClip->effects.size(), 1);
    QCOMPARE(leftClip->effects.first().instanceId, QStringLiteral("fx-grade-1"));
    QCOMPARE(leftClip->effects.first().parameters.value(QStringLiteral("G")).toDouble(), 0.52);
    QVERIFY(leftClip->effects.first().keyframes.contains(QStringLiteral("R")));
    QCOMPARE(leftClip->effects.first().keyframes.value(QStringLiteral("R")).size(), 1);
    QCOMPARE(leftClip->effects.first().keyframes.value(QStringLiteral("R")).first().frame, 50);
    QCOMPARE(leftClip->audioGainDb, -3.5);

    const auto audioTrack = backend->track(QStringLiteral("A1"));
    QVERIFY(audioTrack.has_value());
    QCOMPARE(audioTrack->effects.size(), 1);
    QVERIFY(audioTrack->effects.first().isAudioFilter);

    QCOMPARE(backend->captions().size(), 1);
    QCOMPARE(backend->captions().first().text, QStringLiteral("Welcome to Bang On Pro"));
    QCOMPARE(backend->markers().size(), 1);
    QCOMPARE(backend->markers().first().label, QStringLiteral("Beat Drop Cut"));
    QCOMPARE(backend->cameraSelections().size(), 1);
    QCOMPARE(backend->exportRequests().size(), 1);
    QCOMPARE(engine.activityLogJson().size(), 1);
}

void TestTimelineCommandEngine::testRevisionIncrementAndConflictDetection()
{
    auto backend = std::make_shared<InMemoryTimelineBackend>();
    TimelineCommandEngine engine(backend);
    QSignalSpy revSpy(&engine, &TimelineCommandEngine::revisionChanged);

    TimelineCommandBatch b1 = makeBaseBatch(QStringLiteral("req-rev-1"), 0);
    TimelineOperation op1 = makeOp(QStringLiteral("op-1"), QStringLiteral("create_track"));
    op1.trackId = QStringLiteral("V1");
    b1.operations.append(op1);

    const TimelineCommandResponse r1 = engine.executeBatch(b1);
    QCOMPARE(r1.status, CommandStatus::Ok);
    QCOMPARE(r1.previousRevision, 0);
    QCOMPARE(r1.currentRevision, 1);
    QCOMPARE(engine.currentRevision(), 1);
    QCOMPARE(revSpy.count(), 1);

    // Stale expected_revision = 0 must fail with RevisionConflict and NOT mutate state.
    TimelineCommandBatch stale = makeBaseBatch(QStringLiteral("req-rev-stale"), 0);
    TimelineOperation opStale = makeOp(QStringLiteral("op-stale"), QStringLiteral("create_track"));
    opStale.trackId = QStringLiteral("V2");
    stale.operations.append(opStale);

    const TimelineCommandResponse rStale = engine.executeBatch(stale);
    QCOMPARE(rStale.status, CommandStatus::RevisionConflict);
    QCOMPARE(rStale.previousRevision, 1);
    QCOMPARE(rStale.currentRevision, 1);
    QCOMPARE(engine.currentRevision(), 1);
    QVERIFY(!backend->hasTrack(QStringLiteral("V2")));

    // Matching expected_revision = 1 succeeds and increments to 2.
    stale.requestId = QStringLiteral("req-rev-2");
    stale.expectedRevision = 1;
    const TimelineCommandResponse r2 = engine.executeBatch(stale);
    QCOMPARE(r2.status, CommandStatus::Ok);
    QCOMPARE(r2.previousRevision, 1);
    QCOMPARE(r2.currentRevision, 2);
    QVERIFY(backend->hasTrack(QStringLiteral("V2")));
}

void TestTimelineCommandEngine::testAtomicRollbackMidBatch()
{
    auto backend = std::make_shared<InMemoryTimelineBackend>();
    TimelineCommandEngine engine(backend);

    // Seed revision 1 with a bin asset so we verify rollback restores non-empty state accurately.
    TimelineCommandBatch seed = makeBaseBatch(QStringLiteral("req-seed"), 0);
    TimelineOperation imp = makeOp(QStringLiteral("op-imp"), QStringLiteral("import_media"));
    imp.mediaRef = QStringLiteral("media-seed");
    imp.params.insert(QStringLiteral("path"), QStringLiteral("/media/seed.mp4"));
    imp.params.insert(QStringLiteral("duration_frames"), 500);
    seed.operations.append(imp);
    QCOMPARE(engine.executeBatch(seed).status, CommandStatus::Ok);
    QCOMPARE(engine.currentRevision(), 1);

    const QJsonObject stateBefore = backend->exportStateJson();

    // Batch of 3 operations: op #1 and op #2 succeed, op #3 fails mid-flight (nonexistent clip).
    TimelineCommandBatch failing = makeBaseBatch(QStringLiteral("req-atomic-fail"), 1);

    TimelineOperation op1 = makeOp(QStringLiteral("op-1"), QStringLiteral("create_track"));
    op1.trackId = QStringLiteral("V-temp");
    failing.operations.append(op1);

    TimelineOperation op2 = makeOp(QStringLiteral("op-2"), QStringLiteral("place_clip"));
    op2.trackId = QStringLiteral("V-temp");
    op2.mediaRef = QStringLiteral("media-seed");
    op2.clipId = QStringLiteral("clip-temp");
    op2.frameRange = {0, 120, 0, 25, 1};
    failing.operations.append(op2);

    TimelineOperation op3 = makeOp(QStringLiteral("op-3"), QStringLiteral("trim_clip"));
    op3.clipId = QStringLiteral("clip-does-not-exist");
    op3.frameRange = {10, 90, 0, 25, 1};
    failing.operations.append(op3);

    QSignalSpy rbSpy(&engine, &TimelineCommandEngine::transactionRolledBack);
    const TimelineCommandResponse resp = engine.executeBatch(failing);

    QCOMPARE(resp.status, CommandStatus::RolledBack);
    QCOMPARE(resp.previousRevision, 1);
    QCOMPARE(resp.currentRevision, 1);
    QCOMPARE(engine.currentRevision(), 1);
    QCOMPARE(rbSpy.count(), 1);
    QCOMPARE(resp.results.size(), 3);
    QVERIFY(!resp.results[0].applied);
    QVERIFY(resp.results[0].metadata.value(QStringLiteral("rolled_back")).toBool());
    QVERIFY(!resp.results[1].applied);
    QVERIFY(resp.results[1].metadata.value(QStringLiteral("rolled_back")).toBool());
    QVERIFY(!resp.results[2].applied);

    // State must be byte-for-byte identical to pre-transaction snapshot.
    QVERIFY(!backend->hasTrack(QStringLiteral("V-temp")));
    QVERIFY(!backend->hasClip(QStringLiteral("clip-temp")));
    QCOMPARE(backend->exportStateJson(), stateBefore);
}

void TestTimelineCommandEngine::testApprovalWorkflowAcceptAndReject()
{
    auto backend = std::make_shared<InMemoryTimelineBackend>();
    TimelineCommandEngine engine(backend);

    QSignalSpy stagedSpy(&engine, &TimelineCommandEngine::proposalStaged);
    QSignalSpy resolvedSpy(&engine, &TimelineCommandEngine::proposalResolved);

    // Proposal 1: staged -> approved -> applied.
    TimelineCommandBatch prop1 = makeBaseBatch(QStringLiteral("req-prop-approve"), 0, true, false);
    TimelineOperation op1 = makeOp(QStringLiteral("op-track"), QStringLiteral("create_track"));
    op1.trackId = QStringLiteral("V-ai");
    prop1.operations.append(op1);

    const TimelineCommandResponse stageResp1 = engine.executeBatch(prop1);
    QCOMPARE(stageResp1.status, CommandStatus::PendingApproval);
    QCOMPARE(engine.currentRevision(), 0);
    QVERIFY(!backend->hasTrack(QStringLiteral("V-ai")));
    QCOMPARE(engine.pendingProposals().size(), 1);
    QCOMPARE(stagedSpy.count(), 1);
    QVERIFY(engine.pendingProposals().first().proposedDiff.value(QStringLiteral("preview_valid")).toBool());

    const TimelineCommandResponse approveResp = engine.approveProposal(QStringLiteral("req-prop-approve"));
    QCOMPARE(approveResp.status, CommandStatus::Ok);
    QCOMPARE(approveResp.previousRevision, 0);
    QCOMPARE(approveResp.currentRevision, 1);
    QVERIFY(backend->hasTrack(QStringLiteral("V-ai")));
    QVERIFY(engine.pendingProposals().isEmpty());
    QCOMPARE(resolvedSpy.count(), 1);

    // Proposal 2: staged -> rejected -> discarded.
    TimelineCommandBatch prop2 = makeBaseBatch(QStringLiteral("req-prop-reject"), 1, true, false);
    TimelineOperation op2 = makeOp(QStringLiteral("op-track-2"), QStringLiteral("create_track"));
    op2.trackId = QStringLiteral("V-rejected");
    prop2.operations.append(op2);

    const TimelineCommandResponse stageResp2 = engine.executeBatch(prop2);
    QCOMPARE(stageResp2.status, CommandStatus::PendingApproval);
    QCOMPARE(engine.pendingProposals().size(), 1);

    QVERIFY(engine.rejectProposal(QStringLiteral("req-prop-reject"), QStringLiteral("Director prefers manual layout")));
    QVERIFY(!engine.rejectProposal(QStringLiteral("req-prop-reject")));
    QVERIFY(engine.pendingProposals().isEmpty());
    QCOMPARE(engine.currentRevision(), 1);
    QVERIFY(!backend->hasTrack(QStringLiteral("V-rejected")));
}

void TestTimelineCommandEngine::testUndoRedoRoundTrip()
{
    auto backend = std::make_shared<InMemoryTimelineBackend>();
    TimelineCommandEngine engine(backend);

    const QJsonObject snap0 = backend->exportStateJson();

    // Commit 1: import + create track
    TimelineCommandBatch b1 = makeBaseBatch(QStringLiteral("req-ur-1"), 0);
    TimelineOperation opImp = makeOp(QStringLiteral("op-imp"), QStringLiteral("import_media"));
    opImp.mediaRef = QStringLiteral("m1");
    opImp.params.insert(QStringLiteral("path"), QStringLiteral("/media/a.mov"));
    opImp.params.insert(QStringLiteral("duration_frames"), 400);
    TimelineOperation opTrk = makeOp(QStringLiteral("op-trk"), QStringLiteral("create_track"));
    opTrk.trackId = QStringLiteral("V1");
    b1.operations = {opImp, opTrk};
    QCOMPARE(engine.executeBatch(b1).status, CommandStatus::Ok);
    const QJsonObject snap1 = backend->exportStateJson();

    // Commit 2: place clip
    TimelineCommandBatch b2 = makeBaseBatch(QStringLiteral("req-ur-2"), 1);
    TimelineOperation opClip = makeOp(QStringLiteral("op-clip"), QStringLiteral("place_clip"));
    opClip.mediaRef = QStringLiteral("m1");
    opClip.trackId = QStringLiteral("V1");
    opClip.clipId = QStringLiteral("c1");
    opClip.frameRange = {0, 100, 0, 25, 1};
    b2.operations = {opClip};
    QCOMPARE(engine.executeBatch(b2).status, CommandStatus::Ok);
    const QJsonObject snap2 = backend->exportStateJson();
    QCOMPARE(engine.currentRevision(), 2);

    // Undo #1 -> restores snap1
    const TimelineCommandResponse u1 = engine.undoLastTransaction();
    QCOMPARE(u1.status, CommandStatus::Ok);
    QCOMPARE(u1.previousRevision, 2);
    QCOMPARE(u1.currentRevision, 3);
    QVERIFY(!backend->hasClip(QStringLiteral("c1")));
    QVERIFY(backend->hasTrack(QStringLiteral("V1")));
    QCOMPARE(backend->exportStateJson(), snap1);
    QVERIFY(engine.canRedo());

    // Undo #2 -> restores snap0
    const TimelineCommandResponse u2 = engine.undoLastTransaction();
    QCOMPARE(u2.status, CommandStatus::Ok);
    QCOMPARE(u2.currentRevision, 4);
    QVERIFY(!backend->hasTrack(QStringLiteral("V1")));
    QCOMPARE(backend->exportStateJson(), snap0);
    QVERIFY(!engine.canUndo());

    // Redo #1 -> restores snap1
    const TimelineCommandResponse r1 = engine.redoLastTransaction();
    QCOMPARE(r1.status, CommandStatus::Ok);
    QCOMPARE(r1.currentRevision, 5);
    QCOMPARE(backend->exportStateJson(), snap1);

    // Redo #2 -> restores snap2
    const TimelineCommandResponse r2 = engine.redoLastTransaction();
    QCOMPARE(r2.status, CommandStatus::Ok);
    QCOMPARE(r2.currentRevision, 6);
    QVERIFY(backend->hasClip(QStringLiteral("c1")));
    QCOMPARE(backend->exportStateJson(), snap2);
    QVERIFY(!engine.canRedo());
}

void TestTimelineCommandEngine::testDryRunMode()
{
    auto backend = std::make_shared<InMemoryTimelineBackend>();
    TimelineCommandEngine engine(backend);
    const QJsonObject initialSnap = backend->exportStateJson();

    TimelineCommandBatch dryBatch = makeBaseBatch(QStringLiteral("req-dry-1"), 0, true, true);
    TimelineOperation opImp = makeOp(QStringLiteral("op-imp"), QStringLiteral("import_media"));
    opImp.mediaRef = QStringLiteral("m-dry");
    opImp.params.insert(QStringLiteral("path"), QStringLiteral("/media/dry.mov"));
    opImp.params.insert(QStringLiteral("duration_frames"), 200);
    TimelineOperation opTrk = makeOp(QStringLiteral("op-trk"), QStringLiteral("create_track"));
    opTrk.trackId = QStringLiteral("V-dry");
    TimelineOperation opClip = makeOp(QStringLiteral("op-clip"), QStringLiteral("place_clip"));
    opClip.mediaRef = QStringLiteral("m-dry");
    opClip.trackId = QStringLiteral("V-dry");
    opClip.clipId = QStringLiteral("c-dry");
    opClip.frameRange = {0, 150, 0, 25, 1};
    dryBatch.operations = {opImp, opTrk, opClip};

    const TimelineCommandResponse resp = engine.executeBatch(dryBatch);
    QCOMPARE(resp.status, CommandStatus::Ok);
    QCOMPARE(resp.previousRevision, 0);
    QCOMPARE(resp.currentRevision, 0);
    QCOMPARE(engine.currentRevision(), 0);
    QCOMPARE(resp.results.size(), 3);
    for (const TimelineOperationResult &r : resp.results) {
        QVERIFY(r.applied);
        QVERIFY(r.metadata.value(QStringLiteral("dry_run")).toBool());
    }
    QVERIFY(!backend->hasMedia(QStringLiteral("m-dry")));
    QVERIFY(!backend->hasTrack(QStringLiteral("V-dry")));
    QVERIFY(!backend->hasClip(QStringLiteral("c-dry")));
    QCOMPARE(backend->exportStateJson(), initialSnap);
}

void TestTimelineCommandEngine::testLocalServerIpcRoundTrip()
{
    auto backend = std::make_shared<InMemoryTimelineBackend>();
    TimelineCommandEngine engine(backend);
    TimelineCommandServer server(&engine);

    const QString sockPath = QDir(QDir::tempPath()).filePath(QStringLiteral("bangonpro-test-%1.sock").arg(QCoreApplication::applicationPid()));
    QString startErr;
    QVERIFY2(server.start(sockPath, &startErr), qPrintable(startErr));
    QVERIFY(server.isListening());
    QCOMPARE(server.socketPath(), sockPath);

#ifdef Q_OS_UNIX
    const QFileDevice::Permissions perms = QFileInfo(sockPath).permissions();
    QVERIFY((perms & QFileDevice::ReadGroup) == 0);
    QVERIFY((perms & QFileDevice::WriteGroup) == 0);
    QVERIFY((perms & QFileDevice::ReadOther) == 0);
    QVERIFY((perms & QFileDevice::WriteOther) == 0);
#endif

    QLocalSocket client;
    client.connectToServer(sockPath);
    QVERIFY(client.waitForConnected(2000));

    auto sendAndReceive = [&client](const QByteArray &payload) -> QJsonObject {
        client.write(payload);
        client.write("\n", 1);
        client.flush();

        QByteArray received;
        QElapsedTimer timer;
        timer.start();
        while (!received.contains('\n') && timer.elapsed() < 3000) {
            QCoreApplication::processEvents(QEventLoop::AllEvents, 20);
            if (client.bytesAvailable() > 0) {
                received.append(client.readAll());
            } else {
                client.waitForReadyRead(20);
                received.append(client.readAll());
            }
        }
        const int nl = received.indexOf('\n');
        const QByteArray line = (nl >= 0) ? received.left(nl) : received;
        return QJsonDocument::fromJson(line).object();
    };

    // 1. Send valid Timeline Command API v1 JSON payload over local socket.
    TimelineCommandBatch batch = makeBaseBatch(QStringLiteral("req-ipc-1"), 0, false, false);
    TimelineOperation opTrk = makeOp(QStringLiteral("op-ipc-trk"), QStringLiteral("create_track"));
    opTrk.trackId = QStringLiteral("V-ipc");
    batch.operations.append(opTrk);

    const QByteArray reqBytes = QJsonDocument(TimelineCommandEngine::batchToJson(batch)).toJson(QJsonDocument::Compact);
    const QJsonObject resp1 = sendAndReceive(reqBytes);
    QCOMPARE(resp1.value(QStringLiteral("status")).toString(), QStringLiteral("ok"));
    QCOMPARE(resp1.value(QStringLiteral("request_id")).toString(), QStringLiteral("req-ipc-1"));
    QCOMPARE(resp1.value(QStringLiteral("current_revision")).toInt(), 1);
    QVERIFY(backend->hasTrack(QStringLiteral("V-ipc")));

    // 2. Send stale revision over IPC -> revision_conflict.
    const QJsonObject respConflict = sendAndReceive(reqBytes);
    QCOMPARE(respConflict.value(QStringLiteral("status")).toString(), QStringLiteral("revision_conflict"));
    QCOMPARE(respConflict.value(QStringLiteral("current_revision")).toInt(), 1);

    // 3. Send malformed JSON -> validation_error without dropping server.
    const QJsonObject respMalformed = sendAndReceive(QByteArray("{not-json"));
    QCOMPARE(respMalformed.value(QStringLiteral("status")).toString(), QStringLiteral("validation_error"));

    // 4. Send lightweight RPC inspection query.
    const QJsonObject respRev = sendAndReceive(QByteArray("{\"rpc_method\":\"get_revision\",\"request_id\":\"rpc-1\"}"));
    QCOMPARE(respRev.value(QStringLiteral("status")).toString(), QStringLiteral("ok"));
    QCOMPARE(respRev.value(QStringLiteral("current_revision")).toInt(), 1);

    client.disconnectFromServer();
    server.stop();
    QVERIFY(!server.isListening());
    QVERIFY(!QFileInfo::exists(sockPath));
}

void TestTimelineCommandEngine::testValidationRejections()
{
    auto backend = std::make_shared<InMemoryTimelineBackend>();
    TimelineCommandEngine engine(backend);

    // 1. Malformed JSON
    {
        const QByteArray out = engine.executeJson(QByteArray("{{{broken"));
        const QJsonObject obj = QJsonDocument::fromJson(out).object();
        QCOMPARE(obj.value(QStringLiteral("status")).toString(), QStringLiteral("validation_error"));
    }

    // 2. Unknown operation type
    {
        TimelineCommandBatch b = makeBaseBatch(QStringLiteral("req-bad-op"), 0);
        b.operations.append(makeOp(QStringLiteral("op-1"), QStringLiteral("run_shell_command")));
        const TimelineCommandResponse r = engine.executeBatch(b);
        QCOMPARE(r.status, CommandStatus::ValidationError);
    }

    // 3. Invalid frame range (out_frame < in_frame)
    {
        TimelineCommandBatch b = makeBaseBatch(QStringLiteral("req-bad-range"), 0);
        TimelineOperation op = makeOp(QStringLiteral("op-1"), QStringLiteral("place_clip"));
        op.trackId = QStringLiteral("V1");
        op.mediaRef = QStringLiteral("m1");
        op.frameRange = {150, 50, 0, 25, 1};
        b.operations.append(op);
        const TimelineCommandResponse r = engine.executeBatch(b);
        QCOMPARE(r.status, CommandStatus::ValidationError);
    }

    // 4. Wrong API version
    {
        TimelineCommandBatch b = makeBaseBatch(QStringLiteral("req-bad-ver"), 0);
        b.apiVersion = QStringLiteral("2.0");
        b.operations.append(makeOp(QStringLiteral("op-1"), QStringLiteral("create_track")));
        const TimelineCommandResponse r = engine.executeBatch(b);
        QCOMPARE(r.status, CommandStatus::ValidationError);
    }

    // 5. Missing actor / empty operations / duplicate op_id / invalid confidence
    {
        TimelineCommandBatch b = makeBaseBatch(QStringLiteral("req-empty-ops"), 0);
        QCOMPARE(engine.executeBatch(b).status, CommandStatus::ValidationError);

        TimelineCommandBatch dup = makeBaseBatch(QStringLiteral("req-dup-op"), 0);
        dup.operations.append(makeOp(QStringLiteral("dup-id"), QStringLiteral("create_track")));
        dup.operations.append(makeOp(QStringLiteral("dup-id"), QStringLiteral("create_track")));
        QCOMPARE(engine.executeBatch(dup).status, CommandStatus::ValidationError);

        TimelineCommandBatch badConf = makeBaseBatch(QStringLiteral("req-bad-conf"), 0);
        TimelineOperation op = makeOp(QStringLiteral("op-1"), QStringLiteral("create_track"));
        op.confidence = 1.75;
        badConf.operations.append(op);
        QCOMPARE(engine.executeBatch(badConf).status, CommandStatus::ValidationError);
    }

    // 6. Missing required JSON field (expected_revision)
    {
        const QByteArray missingRev = QByteArray("{\"api_version\":\"1.0\",\"request_id\":\"r1\",\"project_id\":\"p1\",\"sequence_id\":\"s1\","
                                                 "\"actor\":{\"agent_id\":\"a\",\"role\":\"r\"},\"operations\":[{\"op_id\":\"1\",\"type\":\"create_track\"}]}");
        const QJsonObject obj = QJsonDocument::fromJson(engine.executeJson(missingRev)).object();
        QCOMPARE(obj.value(QStringLiteral("status")).toString(), QStringLiteral("validation_error"));
    }

    QCOMPARE(engine.currentRevision(), 0);
    QVERIFY(backend->tracks().isEmpty());
}

void TestTimelineCommandEngine::testConcurrentSerializedWrites()
{
    auto backend = std::make_shared<InMemoryTimelineBackend>();
    TimelineCommandEngine engine(backend);

    constexpr int kWorkers = 8;
    std::atomic<int> okCount{0};
    std::atomic<int> conflictCount{0};

    std::vector<std::unique_ptr<QThread>> threads;
    threads.reserve(kWorkers);

    for (int i = 0; i < kWorkers; ++i) {
        threads.emplace_back(QThread::create([&engine, &okCount, &conflictCount, i]() {
            TimelineCommandBatch batch = makeBaseBatch(QStringLiteral("req-race-%1").arg(i), 0, false, false);
            TimelineOperation op = makeOp(QStringLiteral("op-race-%1").arg(i), QStringLiteral("create_track"));
            op.trackId = QStringLiteral("V-race-%1").arg(i);
            batch.operations.append(op);

            const TimelineCommandResponse resp = engine.executeBatch(batch);
            if (resp.status == CommandStatus::Ok) {
                okCount.fetch_add(1);
            } else if (resp.status == CommandStatus::RevisionConflict) {
                conflictCount.fetch_add(1);
            }
        }));
    }

    for (auto &t : threads) {
        t->start();
    }
    for (auto &t : threads) {
        t->wait();
    }

    QCOMPARE(okCount.load(), 1);
    QCOMPARE(conflictCount.load(), kWorkers - 1);
    QCOMPARE(engine.currentRevision(), 1);
    QCOMPARE(backend->tracks().size(), 1);
}

void TestTimelineCommandEngine::testMoveSyncDeleteAndJsonHistory()
{
    auto backend = std::make_shared<InMemoryTimelineBackend>();
    TimelineCommandEngine engine(backend);

    // Setup two tracks and two clips, then sync, move, and delete one.
    TimelineCommandBatch setup = makeBaseBatch(QStringLiteral("req-msd-1"), 0);
    TimelineOperation imp = makeOp(QStringLiteral("op-imp"), QStringLiteral("import_media"));
    imp.mediaRef = QStringLiteral("m-sync");
    imp.params.insert(QStringLiteral("path"), QStringLiteral("/media/sync.mov"));
    imp.params.insert(QStringLiteral("duration_frames"), 600);

    TimelineOperation t1 = makeOp(QStringLiteral("op-t1"), QStringLiteral("create_track"));
    t1.trackId = QStringLiteral("V1");
    TimelineOperation t2 = makeOp(QStringLiteral("op-t2"), QStringLiteral("create_track"));
    t2.trackId = QStringLiteral("V2");

    TimelineOperation p1 = makeOp(QStringLiteral("op-p1"), QStringLiteral("place_clip"));
    p1.mediaRef = QStringLiteral("m-sync");
    p1.trackId = QStringLiteral("V1");
    p1.clipId = QStringLiteral("c1");
    p1.frameRange = {0, 120, 50, 25, 1};

    TimelineOperation p2 = makeOp(QStringLiteral("op-p2"), QStringLiteral("place_clip"));
    p2.mediaRef = QStringLiteral("m-sync");
    p2.trackId = QStringLiteral("V2");
    p2.clipId = QStringLiteral("c2");
    p2.frameRange = {0, 120, 10, 25, 1};

    TimelineOperation syncOp = makeOp(QStringLiteral("op-sync"), QStringLiteral("sync_clips"));
    QJsonArray ids;
    ids.append(QStringLiteral("c1"));
    ids.append(QStringLiteral("c2"));
    syncOp.params.insert(QStringLiteral("clip_ids"), ids);
    syncOp.params.insert(QStringLiteral("reference_clip_id"), QStringLiteral("c1"));
    QJsonObject offsets;
    offsets.insert(QStringLiteral("c1"), 0);
    offsets.insert(QStringLiteral("c2"), 15);
    syncOp.params.insert(QStringLiteral("offsets"), offsets);

    TimelineOperation moveOp = makeOp(QStringLiteral("op-move"), QStringLiteral("move_clip"));
    moveOp.clipId = QStringLiteral("c2");
    moveOp.trackId = QStringLiteral("V1");
    moveOp.frameRange.timelineStart = 200;

    setup.operations = {imp, t1, t2, p1, p2, syncOp, moveOp};
    const TimelineCommandResponse r1 = engine.executeBatch(setup);
    QVERIFY2(r1.status == CommandStatus::Ok, qPrintable(r1.errorMessage));
    QCOMPARE(backend->syncGroups().size(), 1);
    QCOMPARE(backend->clip(QStringLiteral("c2"))->trackId, QStringLiteral("V1"));
    QCOMPARE(backend->clip(QStringLiteral("c2"))->frameRange.timelineStart, 200);

    // Delete clip c2 in revision 1 -> 2.
    TimelineCommandBatch delBatch = makeBaseBatch(QStringLiteral("req-msd-2"), 1);
    TimelineOperation delOp = makeOp(QStringLiteral("op-del"), QStringLiteral("delete_clip"));
    delOp.clipId = QStringLiteral("c2");
    delBatch.operations.append(delOp);
    QCOMPARE(engine.executeBatch(delBatch).status, CommandStatus::Ok);
    QVERIFY(!backend->hasClip(QStringLiteral("c2")));

    // Execute JSON "undo" command -> restores c2.
    TimelineCommandBatch undoBatch = makeBaseBatch(QStringLiteral("req-json-undo"), 2);
    undoBatch.operations.append(makeOp(QStringLiteral("op-undo"), QStringLiteral("undo")));
    const QByteArray undoJson = QJsonDocument(TimelineCommandEngine::batchToJson(undoBatch)).toJson(QJsonDocument::Compact);
    const QJsonObject undoResp = QJsonDocument::fromJson(engine.executeJson(undoJson)).object();
    QCOMPARE(undoResp.value(QStringLiteral("status")).toString(), QStringLiteral("ok"));
    QCOMPARE(undoResp.value(QStringLiteral("current_revision")).toInt(), 3);
    QVERIFY(backend->hasClip(QStringLiteral("c2")));

    // Execute JSON "redo" command -> removes c2 again.
    TimelineCommandBatch redoBatch = makeBaseBatch(QStringLiteral("req-json-redo"), 3);
    redoBatch.operations.append(makeOp(QStringLiteral("op-redo"), QStringLiteral("redo")));
    const QByteArray redoJson = QJsonDocument(TimelineCommandEngine::batchToJson(redoBatch)).toJson(QJsonDocument::Compact);
    const QJsonObject redoResp = QJsonDocument::fromJson(engine.executeJson(redoJson)).object();
    QCOMPARE(redoResp.value(QStringLiteral("status")).toString(), QStringLiteral("ok"));
    QCOMPARE(redoResp.value(QStringLiteral("current_revision")).toInt(), 4);
    QVERIFY(!backend->hasClip(QStringLiteral("c2")));
}

void TestTimelineCommandEngine::testProposalConflictAndRebase()
{
    auto backend = std::make_shared<InMemoryTimelineBackend>();
    TimelineCommandEngine engine(backend);

    // Stage a proposal targeting revision 0.
    TimelineCommandBatch staged = makeBaseBatch(QStringLiteral("req-staged-stale"), 0, true, false);
    TimelineOperation opStaged = makeOp(QStringLiteral("op-s1"), QStringLiteral("create_track"));
    opStaged.trackId = QStringLiteral("V-staged");
    staged.operations.append(opStaged);
    QCOMPARE(engine.executeBatch(staged).status, CommandStatus::PendingApproval);

    // Advance revision to 1 via an immediate edit.
    TimelineCommandBatch immediate = makeBaseBatch(QStringLiteral("req-imm"), 0, false, false);
    TimelineOperation opImm = makeOp(QStringLiteral("op-i1"), QStringLiteral("create_track"));
    opImm.trackId = QStringLiteral("V-imm");
    immediate.operations.append(opImm);
    QCOMPARE(engine.executeBatch(immediate).status, CommandStatus::Ok);
    QCOMPARE(engine.currentRevision(), 1);

    // Approving without rebase must detect stale revision conflict and keep proposal pending.
    const TimelineCommandResponse conflictResp = engine.approveProposal(QStringLiteral("req-staged-stale"), false);
    QCOMPARE(conflictResp.status, CommandStatus::RevisionConflict);
    QCOMPARE(engine.pendingProposals().size(), 1);
    QVERIFY(!backend->hasTrack(QStringLiteral("V-staged")));

    // Approving with rebase applies cleanly at revision 1 -> 2.
    const TimelineCommandResponse rebaseResp = engine.approveProposal(QStringLiteral("req-staged-stale"), true);
    QCOMPARE(rebaseResp.status, CommandStatus::Ok);
    QCOMPARE(rebaseResp.previousRevision, 1);
    QCOMPARE(rebaseResp.currentRevision, 2);
    QVERIFY(backend->hasTrack(QStringLiteral("V-staged")));
    QVERIFY(engine.pendingProposals().isEmpty());
}

void TestTimelineCommandEngine::testServerOversizedPayloadAndPipelining()
{
    auto backend = std::make_shared<InMemoryTimelineBackend>();
    TimelineCommandEngine engine(backend);
    TimelineCommandServer server(&engine);

    const QString defaultPath = TimelineCommandServer::defaultSocketPath(QStringLiteral("bangonpro-unit-check"));
    QVERIFY(defaultPath.endsWith(QStringLiteral(".sock")));

    const QString sockPath = QDir(QDir::tempPath()).filePath(QStringLiteral("bangonpro-limit-%1.sock").arg(QCoreApplication::applicationPid()));
    QString err;
    QVERIFY2(server.start(sockPath, &err), qPrintable(err));

    // 1. Pipelined two NDJSON requests in a single write.
    {
        QLocalSocket client;
        client.connectToServer(sockPath);
        QVERIFY(client.waitForConnected(2000));

        const QByteArray pipelined = QByteArray("{\"rpc_method\":\"ping\",\"request_id\":\"p1\"}\n"
                                                "{\"rpc_method\":\"get_state\",\"request_id\":\"p2\"}\n");
        client.write(pipelined);
        client.flush();

        QByteArray received;
        QElapsedTimer timer;
        timer.start();
        while (received.count('\n') < 2 && timer.elapsed() < 3000) {
            QCoreApplication::processEvents(QEventLoop::AllEvents, 20);
            if (client.bytesAvailable() > 0) {
                received.append(client.readAll());
            } else {
                client.waitForReadyRead(20);
                received.append(client.readAll());
            }
        }

        const QList<QByteArray> lines = received.split('\n');
        QVERIFY(lines.size() >= 2);
        const QJsonObject r1 = QJsonDocument::fromJson(lines.at(0)).object();
        const QJsonObject r2 = QJsonDocument::fromJson(lines.at(1)).object();
        QCOMPARE(r1.value(QStringLiteral("request_id")).toString(), QStringLiteral("p1"));
        QCOMPARE(r1.value(QStringLiteral("pong")).toBool(), true);
        QCOMPARE(r2.value(QStringLiteral("request_id")).toString(), QStringLiteral("p2"));
        QCOMPARE(r2.value(QStringLiteral("status")).toString(), QStringLiteral("ok"));
        client.disconnectFromServer();
    }

    // 2. Oversized payload (> 16 MiB) must be rejected with validation_error without crashing server.
    {
        QSignalSpy protoErrSpy(&server, &TimelineCommandServer::protocolError);
        QLocalSocket abusiveClient;
        QByteArray received;
        QObject::connect(&abusiveClient, &QLocalSocket::readyRead, &abusiveClient, [&abusiveClient, &received]() { received.append(abusiveClient.readAll()); });

        abusiveClient.connectToServer(sockPath);
        QVERIFY(abusiveClient.waitForConnected(2000));

        const QByteArray chunk(1024 * 1024, 'A');
        for (int i = 0; i < 17; ++i) {
            if (abusiveClient.state() != QLocalSocket::ConnectedState || received.contains('\n')) {
                break;
            }
            abusiveClient.write(chunk);
            abusiveClient.flush();
            QCoreApplication::processEvents(QEventLoop::AllEvents, 20);
        }

        QElapsedTimer timer;
        timer.start();
        while (!received.contains('\n') && timer.elapsed() < 3000) {
            QCoreApplication::processEvents(QEventLoop::AllEvents, 20);
            if (!abusiveClient.isOpen() && received.isEmpty()) {
                break;
            }
        }
        const QJsonObject errObj = QJsonDocument::fromJson(received.trimmed()).object();
        QCOMPARE(errObj.value(QStringLiteral("status")).toString(), QStringLiteral("validation_error"));
        QVERIFY(protoErrSpy.count() >= 1);
    }

    // 3. Server remains healthy for subsequent normal clients.
    {
        QLocalSocket healthyClient;
        healthyClient.connectToServer(sockPath);
        QVERIFY(healthyClient.waitForConnected(2000));
        healthyClient.write(QByteArray("{\"rpc_method\":\"ping\",\"request_id\":\"after-abuse\"}\n"));
        healthyClient.flush();

        QByteArray received;
        QElapsedTimer timer;
        timer.start();
        while (!received.contains('\n') && timer.elapsed() < 3000) {
            QCoreApplication::processEvents(QEventLoop::AllEvents, 20);
            if (healthyClient.bytesAvailable() > 0) {
                received.append(healthyClient.readAll());
            } else {
                healthyClient.waitForReadyRead(20);
                received.append(healthyClient.readAll());
            }
        }
        const QJsonObject okObj = QJsonDocument::fromJson(received.trimmed()).object();
        QCOMPARE(okObj.value(QStringLiteral("request_id")).toString(), QStringLiteral("after-abuse"));
        QCOMPARE(okObj.value(QStringLiteral("pong")).toBool(), true);
        healthyClient.disconnectFromServer();
    }

    server.stop();
}

QTEST_MAIN(TestTimelineCommandEngine)
#include "test_timeline_command_engine.moc"
