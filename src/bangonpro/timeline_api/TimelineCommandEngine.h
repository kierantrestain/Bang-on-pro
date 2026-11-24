/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include "../BangOnProContracts.h"

#include <QJsonArray>
#include <QJsonObject>
#include <QJsonValue>
#include <QMap>
#include <QMutex>
#include <QObject>
#include <QRecursiveMutex>
#include <QSet>
#include <QString>
#include <QStringList>
#include <QVector>
#include <QtGlobal>

#include <memory>
#include <optional>

namespace BangOnPro {

#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
using RecursiveMutexLocker = QMutexLocker<QRecursiveMutex>;
#else
using RecursiveMutexLocker = QMutexLocker;
#endif

struct TimelineStateFingerprint
{
    QString projectId;
    QString sequenceId;
    qint64 engineRevision = 0;
    qint64 backendMutationCounter = 0;
    QStringList trackIds;
    QStringList clipIds;
    QMap<QString, QString> trackSignatures;
    QMap<QString, QString> clipSignatures;
    QString digest;
    bool valid = false;

    QJsonObject toJson() const;
};

enum class ProposalFreshnessStatus {
    Fresh,
    StaleRevision,
    ManualEditDrift,
    ContextMismatch,
    EntityConflict,
    NotFound,
};

QString proposalFreshnessStatusToString(ProposalFreshnessStatus status);

struct ProposalFreshnessReport
{
    QString requestId;
    ProposalFreshnessStatus status = ProposalFreshnessStatus::NotFound;
    bool fresh = false;
    bool canRebase = false;
    bool requiresRegenerate = false;
    qint64 baseRevision = 0;
    qint64 currentRevision = 0;
    QString originalProjectId;
    QString currentProjectId;
    QString originalSequenceId;
    QString currentSequenceId;
    QStringList addedTrackIds;
    QStringList removedTrackIds;
    QStringList addedClipIds;
    QStringList removedClipIds;
    QStringList modifiedClipIds;
    QStringList missingReferencedTracks;
    QStringList missingReferencedClips;
    QStringList modifiedReferencedClips;
    QString summary;
    QString recommendedAction = QStringLiteral("regenerate");

    QJsonObject toJson() const;
};

struct OperationDependencyIssue
{
    QString opId;
    QString opType;
    QString requiredOpId;
    QString entityKind;
    QString entityId;
    QString message;
};

struct BatchDependencyAnalysis
{
    bool valid = true;
    QStringList missingPrerequisiteOpIds;
    QStringList unknownSelectedOpIds;
    QVector<OperationDependencyIssue> issues;
    QString errorMessage;

    QJsonObject toJson() const;
};

/**
 * @brief Abstract timeline backend adapter for Bang On Pro's Versioned Timeline Command API.
 *
 * Implementations translate validated Timeline Command API v1 operations into either:
 * - Deterministic in-memory state mutations (InMemoryTimelineBackend) for unit tests / simulation, or
 * - Native Kdenlive TimelineItemModel + MLT + DocUndoStack transactions (KdenliveTimelineBridge).
 *
 * Implementations MUST NEVER use simulated GUI clicks or shell commands.
 */
class ITimelineBackend
{
public:
    virtual ~ITimelineBackend() = default;

    /** @brief Human-readable backend identifier (e.g. "in_memory", "kdenlive_core"). */
    virtual QString backendName() const = 0;

    /** @brief Begins an atomic transaction and prepares rollback state. */
    virtual bool beginTransaction(const QString &label, QString *errorOut = nullptr) = 0;

    /** @brief Executes a single validated timeline operation within the active transaction. */
    virtual TimelineOperationResult executeOperation(const TimelineOperation &op, const QString &sequenceId) = 0;

    /** @brief Commits the active transaction (e.g. pushing a single atomic command to DocUndoStack). */
    virtual bool commitTransaction(const QString &label, QString *errorOut = nullptr) = 0;

    /** @brief Rolls back all mutations applied since beginTransaction(). */
    virtual bool rollbackTransaction(QString *errorOut = nullptr) = 0;

    /** @brief Returns true if beginTransaction -> executeOperation -> rollbackTransaction is side-effect free. */
    virtual bool supportsTransactionalDryRun() const { return true; }

    /** @brief Returns true if backend can capture and restore full serialized state snapshots. */
    virtual bool supportsSnapshots() const { return false; }

    /** @brief Captures a lossless JSON snapshot of the current timeline state (when supported). */
    virtual QJsonObject captureStateSnapshot() const { return {}; }

    /** @brief Restores timeline state from a previously captured JSON snapshot (when supported). */
    virtual bool restoreStateSnapshot(const QJsonObject &snapshot, QString *errorOut = nullptr)
    {
        Q_UNUSED(snapshot);
        if (errorOut != nullptr) {
            *errorOut = QStringLiteral("Backend does not support state snapshot restoration");
        }
        return false;
    }

    /** @brief Returns active project identifier known to the backend, or empty if unmanaged. */
    virtual QString currentProjectId() const { return {}; }

    /** @brief Returns active sequence identifier known to the backend, or empty if unmanaged. */
    virtual QString currentSequenceId() const { return {}; }

    /** @brief Captures a strictly read-only structural state fingerprint for stale-proposal detection. */
    virtual TimelineStateFingerprint captureStateFingerprint(const QString &sequenceId = QString()) const
    {
        Q_UNUSED(sequenceId);
        return {};
    }

    /** @brief Captures a strictly read-only InMemoryTimelineBackend-compatible state snapshot for isolated preview simulation. */
    virtual QJsonObject captureReadOnlyStateJson(const QString &sequenceId = QString()) const
    {
        Q_UNUSED(sequenceId);
        return supportsSnapshots() ? captureStateSnapshot() : QJsonObject{};
    }

    /** @brief Native backend undo support (used when delegating to Kdenlive DocUndoStack). */
    virtual bool canNativeUndo() const { return false; }
    virtual bool canNativeRedo() const { return false; }

    virtual bool nativeUndo(QString *errorOut = nullptr)
    {
        if (errorOut != nullptr) {
            *errorOut = QStringLiteral("Backend does not support native undo");
        }
        return false;
    }

    virtual bool nativeRedo(QString *errorOut = nullptr)
    {
        if (errorOut != nullptr) {
            *errorOut = QStringLiteral("Backend does not support native redo");
        }
        return false;
    }
};

struct MediaAsset
{
    QString mediaRef;
    QString sourcePath;
    QString name;
    QString mediaType = QStringLiteral("av");
    int durationFrames = 0;
    int fpsNum = 25;
    int fpsDen = 1;
    QJsonObject metadata;
};

struct KeyframeRecord
{
    int frame = 0;
    QJsonValue value;
    QString interpolation = QStringLiteral("linear");
};

struct EffectInstance
{
    QString instanceId;
    QString effectId;
    bool isAudioFilter = false;
    bool enabled = true;
    QJsonObject parameters;
    QMap<QString, QVector<KeyframeRecord>> keyframes;
};

struct AudioLevelPoint
{
    int frame = 0;
    double gainDb = 0.0;
};

struct TimelineClip
{
    QString clipId;
    QString sequenceId;
    QString trackId;
    QString mediaRef;
    QString name;
    FrameRange frameRange;
    QString cameraId;
    double speedPercent = 100.0;
    bool pitchCompensate = true;
    double audioGainDb = 0.0;
    QVector<AudioLevelPoint> audioAutomation;
    QVector<EffectInstance> effects;
    QMap<QString, QVector<KeyframeRecord>> intrinsicKeyframes;
    QJsonObject metadata;

    int duration() const { return qMax(0, frameRange.outFrame - frameRange.inFrame); }
};

struct TimelineMask
{
    QString maskId;
    QString sequenceId;
    QString clipId;
    QString trackId;
    QString maskType = QStringLiteral("spline");
    QString mltService = QStringLiteral("rotoscoping");
    QString splinesJson;
    QString mattePath;
    double feather = 0.0;
    double expand = 0.0;
    bool invert = false;
    QString operation = QStringLiteral("write_on_clear");
    QJsonArray keyframes;
    QString effectInstanceId;
};

struct TimelineTransition
{
    QString transitionInstanceId;
    QString transitionId;
    QString sequenceId;
    QString aTrackId;
    QString bTrackId;
    int inFrame = 0;
    int outFrame = 0;
    QJsonObject properties;

    int duration() const { return qMax(0, outFrame - inFrame); }
};

struct TimelineLowerThird
{
    QString lowerThirdId;
    QString sequenceId;
    QString trackId;
    QString clipId;
    FrameRange frameRange;
    QString speakerName;
    QString subtitle;
    QString templateName = QStringLiteral("modern_clean");
    QString position = QStringLiteral("bottom_left");
    QString titleXml;
    QJsonObject style;
};

struct TimelineTrack
{
    QString trackId;
    QString sequenceId;
    QString name;
    QString kind = QStringLiteral("video");
    int index = 0;
    bool locked = false;
    bool muted = false;
    double audioGainDb = 0.0;
    QString cameraId;
    QVector<EffectInstance> effects;
    QMap<QString, QVector<KeyframeRecord>> intrinsicKeyframes;
};

struct TimelineCaption
{
    QString captionId;
    QString sequenceId;
    QString trackId;
    FrameRange frameRange;
    QString text;
    QString speaker;
    QString language = QStringLiteral("en");
    QJsonObject style;
};

struct TimelineMarker
{
    QString markerId;
    QString sequenceId;
    QString clipId;
    int frame = 0;
    QString label;
    QString category = QStringLiteral("editorial");
    QString color = QStringLiteral("#3daee9");
};

struct CameraSelection
{
    QString selectionId;
    QString sequenceId;
    QString trackId;
    QString clipId;
    QString cameraId;
    int timelineFrame = 0;
    QJsonObject metadata;
};

struct SyncGroup
{
    QString syncGroupId;
    QString sequenceId;
    QString method = QStringLiteral("audio_waveform");
    QString referenceClipId;
    QMap<QString, int> clipOffsets;
};

struct SequenceRecord
{
    QString sequenceId;
    QString name;
    int fpsNum = 25;
    int fpsDen = 1;
    int width = 1920;
    int height = 1080;
    double masterGainDb = 0.0;
    QVector<EffectInstance> masterEffects;
    QMap<QString, QVector<KeyframeRecord>> masterKeyframes;
};

struct ExportRequestRecord
{
    QString exportId;
    QString sequenceId;
    QString outputPath;
    QString preset;
    FrameRange frameRange;
    QJsonObject params;
    QString status = QStringLiteral("queued");
};

/**
 * @brief Full deterministic non-destructive in-memory timeline backend.
 *
 * Models sequences, bin media, tracks, clips, effects, keyframes, audio levels/filters,
 * captions, markers, multicam camera selections, sync groups, export requests, and
 * lossless snapshot-based transactional rollback.
 */
class InMemoryTimelineBackend : public ITimelineBackend
{
public:
    InMemoryTimelineBackend();
    ~InMemoryTimelineBackend() override = default;

    QString backendName() const override;
    bool beginTransaction(const QString &label, QString *errorOut = nullptr) override;
    TimelineOperationResult executeOperation(const TimelineOperation &op, const QString &sequenceId) override;
    bool commitTransaction(const QString &label, QString *errorOut = nullptr) override;
    bool rollbackTransaction(QString *errorOut = nullptr) override;

    bool supportsTransactionalDryRun() const override { return true; }
    bool supportsSnapshots() const override { return true; }
    QJsonObject captureStateSnapshot() const override;
    bool restoreStateSnapshot(const QJsonObject &snapshot, QString *errorOut = nullptr) override;

    void setActiveContext(const QString &projectId, const QString &sequenceId);
    QString currentProjectId() const override;
    QString currentSequenceId() const override;
    TimelineStateFingerprint captureStateFingerprint(const QString &sequenceId = QString()) const override;
    QJsonObject captureReadOnlyStateJson(const QString &sequenceId = QString()) const override;
    qint64 mutationCounter() const { return m_mutationCounter; }

    // Read-only state inspection helpers for UI previews, diffs, and unit tests.
    QString activeSequenceId() const;
    bool hasSequence(const QString &sequenceId) const;
    bool hasMedia(const QString &mediaRef) const;
    bool hasTrack(const QString &trackId) const;
    bool hasClip(const QString &clipId) const;
    bool hasCaption(const QString &captionId) const;
    bool hasMask(const QString &maskId) const;
    bool hasTransition(const QString &transitionInstanceId) const;
    bool hasLowerThird(const QString &lowerThirdId) const;

    std::optional<SequenceRecord> sequence(const QString &sequenceId) const;
    std::optional<MediaAsset> media(const QString &mediaRef) const;
    std::optional<TimelineTrack> track(const QString &trackId) const;
    std::optional<TimelineClip> clip(const QString &clipId) const;
    std::optional<TimelineCaption> caption(const QString &captionId) const;
    std::optional<TimelineMask> mask(const QString &maskId) const;
    std::optional<TimelineTransition> transition(const QString &transitionInstanceId) const;
    std::optional<TimelineLowerThird> lowerThird(const QString &lowerThirdId) const;

    QVector<SequenceRecord> sequences() const;
    QVector<MediaAsset> mediaAssets() const;
    QVector<TimelineTrack> tracks() const;
    QVector<TimelineClip> clips() const;
    QVector<TimelineCaption> captions() const;
    QVector<TimelineMarker> markers() const;
    QVector<TimelineMask> masks() const;
    QVector<TimelineTransition> transitions() const;
    QVector<TimelineLowerThird> lowerThirds() const;
    QVector<CameraSelection> cameraSelections() const;
    QVector<SyncGroup> syncGroups() const;
    QVector<ExportRequestRecord> exportRequests() const;

    QJsonObject exportStateJson() const { return captureStateSnapshot(); }

private:
    struct State
    {
        QString activeSequenceId;
        QMap<QString, SequenceRecord> sequences;
        QMap<QString, MediaAsset> mediaBin;
        QMap<QString, TimelineTrack> tracks;
        QMap<QString, TimelineClip> clips;
        QMap<QString, TimelineCaption> captions;
        QMap<QString, TimelineMarker> markers;
        QMap<QString, TimelineMask> masks;
        QMap<QString, TimelineTransition> transitions;
        QMap<QString, TimelineLowerThird> lowerThirds;
        QMap<QString, CameraSelection> cameraSelections;
        QMap<QString, SyncGroup> syncGroups;
        QMap<QString, ExportRequestRecord> exportRequests;

        qint64 nextMediaId = 1;
        qint64 nextSequenceId = 1;
        qint64 nextTrackId = 1;
        qint64 nextClipId = 1;
        qint64 nextEffectId = 1;
        qint64 nextCaptionId = 1;
        qint64 nextMarkerId = 1;
        qint64 nextMaskId = 1;
        qint64 nextTransitionId = 1;
        qint64 nextLowerThirdId = 1;
        qint64 nextCameraSelectionId = 1;
        qint64 nextSyncGroupId = 1;
        qint64 nextExportId = 1;
    };

    void ensureSequenceExists(const QString &sequenceId);
    TimelineOperationResult handleImportMedia(const TimelineOperation &op);
    TimelineOperationResult handleSyncClips(const TimelineOperation &op, const QString &sequenceId);
    TimelineOperationResult handleCreateSequence(const TimelineOperation &op, const QString &sequenceId);
    TimelineOperationResult handleCreateTrack(const TimelineOperation &op, const QString &sequenceId);
    TimelineOperationResult handlePlaceClip(const TimelineOperation &op, const QString &sequenceId);
    TimelineOperationResult handleMoveClip(const TimelineOperation &op);
    TimelineOperationResult handleTrimClip(const TimelineOperation &op);
    TimelineOperationResult handleSplitClip(const TimelineOperation &op);
    TimelineOperationResult handleDeleteClip(const TimelineOperation &op);
    TimelineOperationResult handleSelectCamera(const TimelineOperation &op, const QString &sequenceId);
    TimelineOperationResult handleAddEffect(const TimelineOperation &op, const QString &sequenceId, bool asAudioFilter);
    TimelineOperationResult handleSetEffectParam(const TimelineOperation &op, const QString &sequenceId);
    TimelineOperationResult handleSetKeyframe(const TimelineOperation &op, const QString &sequenceId);
    TimelineOperationResult handleSetAudioLevel(const TimelineOperation &op, const QString &sequenceId);
    TimelineOperationResult handleAddCaption(const TimelineOperation &op, const QString &sequenceId);
    TimelineOperationResult handleUpdateCaption(const TimelineOperation &op, const QString &sequenceId);
    TimelineOperationResult handleDeleteCaption(const TimelineOperation &op);
    TimelineOperationResult handleAddMarker(const TimelineOperation &op, const QString &sequenceId);
    TimelineOperationResult handleApplyMask(const TimelineOperation &op, const QString &sequenceId);
    TimelineOperationResult handleAddTransition(const TimelineOperation &op, const QString &sequenceId);
    TimelineOperationResult handleAddLowerThird(const TimelineOperation &op, const QString &sequenceId);
    TimelineOperationResult handleSetClipSpeed(const TimelineOperation &op);
    TimelineOperationResult handleExportSequence(const TimelineOperation &op, const QString &sequenceId);

    static QJsonObject serializeState(const State &state);
    static bool deserializeState(const QJsonObject &obj, State *stateOut, QString *errorOut);

    State m_state;
    QString m_projectId;
    qint64 m_mutationCounter = 0;
    bool m_inTransaction = false;
    State m_transactionBackup;
};

struct PendingProposal
{
    TimelineCommandBatch batch;
    QString projectId;
    QString sequenceId;
    qint64 baseRevision = 0;
    qint64 stagedAtRevision = 0;
    QString stagedAtUtc;
    TimelineStateFingerprint baseFingerprint;
    QJsonObject proposedDiff;
    QVector<TimelineOperationResult> previewResults;
};

struct ActivityLogEntry
{
    qint64 entryId = 0;
    QString timestampUtc;
    QString requestId;
    QString projectId;
    QString sequenceId;
    QString agentId;
    QString agentRole;
    QString transactionLabel;
    CommandStatus status = CommandStatus::Ok;
    qint64 previousRevision = 0;
    qint64 currentRevision = 0;
    int operationCount = 0;
    bool dryRun = false;
    QString message;
    QJsonArray operationSummaries;
};

/**
 * @brief Single authorized transactional writer for Bang On Pro's Versioned AI Timeline Command API v1.
 *
 * Enforces contract validation, optimistic concurrency via revision numbers, atomic batch rollback,
 * user approval staging, undo/redo history, and audit logging. Protected by a QRecursiveMutex so
 * stateless parsing/validation can proceed concurrently while state writes are strictly serialized.
 */
class TimelineCommandEngine : public QObject
{
    Q_OBJECT

public:
    explicit TimelineCommandEngine(std::shared_ptr<ITimelineBackend> backend = nullptr, QObject *parent = nullptr);
    ~TimelineCommandEngine() override = default;

    std::shared_ptr<ITimelineBackend> backend() const;
    qint64 currentRevision() const;

    // Active project/sequence context tracking & external/manual edit notification.
    void setActiveContext(const QString &projectId, const QString &sequenceId);
    QString activeProjectId() const;
    QString activeSequenceId() const;
    void notifyExternalEdit(const QString &reason = QString());

    bool canUndo() const;
    bool canRedo() const;
    int undoDepth() const;
    int redoDepth() const;

    // Stateless contract validation & JSON serialization helpers (safe to invoke concurrently).
    static bool isSupportedOperationType(const QString &type);
    static QStringList supportedOperationTypes();
    static bool validateBatch(const TimelineCommandBatch &batch, QString *errorMessageOut = nullptr);
    static bool batchFromJson(const QJsonObject &obj, TimelineCommandBatch *batchOut, QString *errorMessageOut = nullptr);
    static QJsonObject batchToJson(const TimelineCommandBatch &batch);
    static QJsonObject responseToJson(const TimelineCommandResponse &response);
    static bool responseFromJson(const QJsonObject &obj, TimelineCommandResponse *responseOut, QString *errorMessageOut = nullptr);

    // Core transactional entry points.
    TimelineCommandResponse executeBatch(const TimelineCommandBatch &batch);
    QByteArray executeJson(const QByteArray &requestJson);

    // Approval workflow for AI-proposed timeline edits.
    QVector<PendingProposal> pendingProposals() const;
    QJsonArray pendingProposalsJson() const;
    ProposalFreshnessReport validateProposalFreshness(const QString &requestId) const;
    QJsonObject validateProposalFreshnessJson(const QString &requestId) const;
    static BatchDependencyAnalysis analyzeSelectionDependencies(const TimelineCommandBatch &batch, const QSet<QString> &selectedOpIds,
                                                                const QJsonObject &currentStateJson = QJsonObject(),
                                                                const QVector<TimelineOperationResult> &stagedPreviewResults = {});
    BatchDependencyAnalysis analyzeProposalSelectionDependencies(const QString &requestId, const QSet<QString> &selectedOpIds) const;
    QJsonObject previewPendingProposal(const QString &requestId, const QSet<QString> *selectedOpIds = nullptr) const;
    TimelineCommandResponse approveBatch(const QString &requestId, bool rebaseToCurrentRevision = false);
    TimelineCommandResponse approveProposal(const QString &requestId, bool rebaseToCurrentRevision = false);
    TimelineCommandResponse approveBatchWithSelection(const QString &requestId, const QSet<QString> &acceptedOpIds,
                                                      const QString &approvedBy = QStringLiteral("user"), bool rebaseToCurrentRevision = false);
    bool rejectProposal(const QString &requestId, const QString &reason = QString());

    // Revision-tracked undo / redo.
    TimelineCommandResponse undoLastTransaction(const QString &actorId = QStringLiteral("user"));
    TimelineCommandResponse redoLastTransaction(const QString &actorId = QStringLiteral("user"));

    // Activity history & state inspection.
    QVector<ActivityLogEntry> activityLog() const;
    QJsonArray activityLogJson() const;
    QJsonObject stateSnapshotJson() const;

Q_SIGNALS:
    void revisionChanged(qint64 newRevision);
    void transactionCommitted(const QString &requestId, qint64 newRevision, const QString &label);
    void transactionRolledBack(const QString &requestId, const QString &reason);
    void proposalStaged(const QString &requestId, const QString &label);
    void proposalResolved(const QString &requestId, bool approved);
    void activityLogUpdated();

private:
    struct UndoRecord
    {
        QString requestId;
        QString projectId;
        QString sequenceId;
        QString agentId;
        QString agentRole;
        QString transactionLabel;
        bool usesSnapshot = false;
        QJsonObject beforeSnapshot;
        QJsonObject afterSnapshot;
    };

    QString resolveCurrentProjectIdLocked() const;
    QString resolveCurrentSequenceIdLocked() const;
    TimelineStateFingerprint captureStateFingerprintLocked(const QString &sequenceId) const;
    ProposalFreshnessReport validateProposalFreshnessLocked(const PendingProposal &proposal, const QSet<QString> *selectedOpIds = nullptr) const;
    QJsonObject simulateBatchOnIsolatedCloneLocked(const TimelineCommandBatch &batch, const QSet<QString> *selectedOpIds,
                                                   QVector<TimelineOperationResult> *previewResultsOut) const;
    TimelineCommandResponse executeBatchLocked(const TimelineCommandBatch &batch, bool bypassApprovalGate);
    QJsonObject computeProposalDiffLocked(const TimelineCommandBatch &batch, QVector<TimelineOperationResult> *previewResultsOut) const;
    void appendActivityLogLocked(const TimelineCommandBatch &batch, const TimelineCommandResponse &response, const QString &overrideMessage = QString());
    void appendActivityLogSimpleLocked(const QString &requestId, const QString &projectId, const QString &sequenceId, const QString &agentId,
                                       const QString &agentRole, const QString &label, CommandStatus status, qint64 prevRev, qint64 currRev, int opCount,
                                       bool dryRun, const QString &message);

    static QString currentUtcTimestamp();
    static QJsonObject serializeProposal(const PendingProposal &proposal);
    static QJsonObject serializeActivityEntry(const ActivityLogEntry &entry);

    std::shared_ptr<ITimelineBackend> m_backend;
    mutable QRecursiveMutex m_writeMutex;
    qint64 m_revision = 0;
    qint64 m_nextLogEntryId = 1;
    QString m_activeProjectId;
    QString m_activeSequenceId;
    bool m_hasExplicitContext = false;
    QVector<PendingProposal> m_pendingProposals;
    QVector<UndoRecord> m_undoStack;
    QVector<UndoRecord> m_redoStack;
    QVector<ActivityLogEntry> m_activityLog;
};

} // namespace BangOnPro
