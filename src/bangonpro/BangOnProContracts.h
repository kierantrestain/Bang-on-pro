/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include <QJsonArray>
#include <QJsonObject>
#include <QString>
#include <QVector>
#include <cstdint>

namespace BangOnPro {

inline constexpr const char *kAppDisplayName = "Bang On Pro";
inline constexpr const char *kAppVersion = "1.7.0";
inline constexpr const char *kTimelineApiVersion = "1.0";
inline constexpr const char *kPremiereReportSchemaVersion = "1.0";

enum class CommandStatus { Ok = 0, ValidationError = 1, RevisionConflict = 2, ExecutionFailed = 3, RolledBack = 4, PendingApproval = 5 };

enum class PresetCompatibilityStatus { ExactValidated = 0, Approximated = 1, Unsupported = 2, MissingDependency = 3 };

struct FrameRange
{
    int inFrame = 0;
    int outFrame = 0;
    int timelineStart = 0;
    int fpsNum = 25;
    int fpsDen = 1;
};

struct TimelineOperation
{
    QString opId;
    QString type;
    QString mediaRef;
    QString clipId;
    QString trackId;
    FrameRange frameRange;
    QJsonObject params;
    double confidence = 1.0;
    QString rationale;
};

struct TimelineCommandBatch
{
    QString apiVersion = QString::fromLatin1(kTimelineApiVersion);
    QString requestId;
    QString projectId;
    QString sequenceId;
    qint64 expectedRevision = 0;
    QString agentId;
    QString agentRole;
    bool requiresApproval = true;
    QString transactionLabel = QStringLiteral("AI Edit Batch");
    bool dryRun = false;
    QVector<TimelineOperation> operations;
};

struct TimelineOperationResult
{
    QString opId;
    bool applied = false;
    QString createdId;
    QString message;
    QJsonObject metadata;
};

struct TimelineCommandResponse
{
    QString apiVersion = QString::fromLatin1(kTimelineApiVersion);
    QString requestId;
    CommandStatus status = CommandStatus::Ok;
    qint64 previousRevision = 0;
    qint64 currentRevision = 0;
    QString errorMessage;
    QVector<TimelineOperationResult> results;
};

struct PremierePresetRecord
{
    QString presetId;
    QString name;
    QString pack;
    QString attribution;
    QStringList folderPath;
    QString sourceFile;
    QString presetType;
    PresetCompatibilityStatus status = PresetCompatibilityStatus::Unsupported;
    QStringList notes;
    QJsonArray effectChain;
    QJsonArray missingDependencies;
    QJsonArray mltFilters;
    QString kdenliveEffectXml;
    bool originalXmlPreserved = true;
};

inline QString compatibilityStatusToString(PresetCompatibilityStatus st)
{
    switch (st) {
    case PresetCompatibilityStatus::ExactValidated:
        return QStringLiteral("exact_validated");
    case PresetCompatibilityStatus::Approximated:
        return QStringLiteral("approximated");
    case PresetCompatibilityStatus::Unsupported:
        return QStringLiteral("unsupported");
    case PresetCompatibilityStatus::MissingDependency:
        return QStringLiteral("missing_dependency");
    }
    return QStringLiteral("unsupported");
}

inline PresetCompatibilityStatus compatibilityStatusFromString(const QString &s)
{
    if (s == QStringLiteral("exact_validated")) {
        return PresetCompatibilityStatus::ExactValidated;
    }
    if (s == QStringLiteral("approximated")) {
        return PresetCompatibilityStatus::Approximated;
    }
    if (s == QStringLiteral("missing_dependency")) {
        return PresetCompatibilityStatus::MissingDependency;
    }
    return PresetCompatibilityStatus::Unsupported;
}

inline QString commandStatusToString(CommandStatus st)
{
    switch (st) {
    case CommandStatus::Ok:
        return QStringLiteral("ok");
    case CommandStatus::ValidationError:
        return QStringLiteral("validation_error");
    case CommandStatus::RevisionConflict:
        return QStringLiteral("revision_conflict");
    case CommandStatus::ExecutionFailed:
        return QStringLiteral("execution_failed");
    case CommandStatus::RolledBack:
        return QStringLiteral("rolled_back");
    case CommandStatus::PendingApproval:
        return QStringLiteral("pending_approval");
    }
    return QStringLiteral("execution_failed");
}

} // namespace BangOnPro
