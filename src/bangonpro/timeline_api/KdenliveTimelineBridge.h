/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include "TimelineCommandEngine.h"

#ifdef BANGONPRO_WITH_KDENLIVE_CORE

#include "undohelper.hpp"

#include <QMap>
#include <QString>

#include <memory>

class TimelineItemModel;

namespace BangOnPro {

/**
 * @brief Native Kdenlive 24.08 core backend adapter for TimelineCommandEngine.
 *
 * Translates validated Timeline Command API v1 operations directly into Kdenlive
 * Core::self(), ProjectItemModel, TimelineItemModel, TimelineFunctions, EffectStackModel,
 * SubtitleModel, MarkerListModel, and DocUndoStack transactions.
 *
 * Never uses simulated mouse clicks, keyboard shortcuts, or GUI hacks.
 */
class KdenliveTimelineBridge : public ITimelineBackend
{
public:
    explicit KdenliveTimelineBridge(std::shared_ptr<TimelineItemModel> explicitTimeline = nullptr);
    ~KdenliveTimelineBridge() override = default;

    QString backendName() const override;
    bool beginTransaction(const QString &label, QString *errorOut = nullptr) override;
    TimelineOperationResult executeOperation(const TimelineOperation &op, const QString &sequenceId) override;
    bool commitTransaction(const QString &label, QString *errorOut = nullptr) override;
    bool rollbackTransaction(QString *errorOut = nullptr) override;

    // Live MLT / Kdenlive timelines avoid mutating active producers during proposal staging previews.
    bool supportsTransactionalDryRun() const override { return false; }
    bool supportsSnapshots() const override { return false; }

    QString currentProjectId() const override;
    QString currentSequenceId() const override;
    TimelineStateFingerprint captureStateFingerprint(const QString &sequenceId = QString()) const override;
    QJsonObject captureReadOnlyStateJson(const QString &sequenceId = QString()) const override;

    bool canNativeUndo() const override;
    bool canNativeRedo() const override;
    bool nativeUndo(QString *errorOut = nullptr) override;
    bool nativeRedo(QString *errorOut = nullptr) override;

    void setTimelineOverride(std::shared_ptr<TimelineItemModel> timeline);

private:
    std::shared_ptr<TimelineItemModel> resolveTimeline(const QString &sequenceId) const;
    bool resolveTrackId(const std::shared_ptr<TimelineItemModel> &timeline, const QString &rawTrackId, int *trackIdOut) const;
    bool resolveClipId(const std::shared_ptr<TimelineItemModel> &timeline, const QString &rawClipId, int *clipIdOut) const;
    QString resolveBinId(const QString &rawMediaRef) const;

    TimelineOperationResult handleImportMedia(const TimelineOperation &op);
    TimelineOperationResult handleCreateSequence(const TimelineOperation &op);
    TimelineOperationResult handleCreateTrack(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handlePlaceClip(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleMoveClip(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleTrimClip(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleSplitClip(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleDeleteClip(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleSyncClips(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleSelectCamera(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleAddEffect(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op, bool asAudioFilter);
    TimelineOperationResult handleSetEffectParam(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleSetKeyframe(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleSetAudioLevel(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleAddCaption(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleUpdateCaption(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleDeleteCaption(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleAddLowerThird(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleApplyMask(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleAddTransition(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleSetClipSpeed(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleAddMarker(const std::shared_ptr<TimelineItemModel> &timeline, const TimelineOperation &op);
    TimelineOperationResult handleExportSequence(const TimelineOperation &op, const QString &sequenceId);

    void composeUndoRedo(Fun localUndo, Fun localRedo);

    std::shared_ptr<TimelineItemModel> m_explicitTimeline;
    bool m_inTransaction = false;
    Fun m_txUndo;
    Fun m_txRedo;

    QMap<QString, QString> m_mediaAliases;
    QMap<QString, int> m_trackAliases;
    QMap<QString, int> m_clipAliases;

    QMap<QString, QString> m_mediaAliasesBackup;
    QMap<QString, int> m_trackAliasesBackup;
    QMap<QString, int> m_clipAliasesBackup;
};

} // namespace BangOnPro

#endif // BANGONPRO_WITH_KDENLIVE_CORE
