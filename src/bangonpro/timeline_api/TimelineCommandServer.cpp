/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "TimelineCommandServer.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QStandardPaths>
#include <QTimer>

#include <utility>

namespace BangOnPro {

namespace {
constexpr qint64 kReadChunkBytes = 64 * 1024;
} // namespace

TimelineCommandServer::TimelineCommandServer(TimelineCommandEngine *engine, QObject *parent)
    : QObject(parent)
    , m_engine(engine)
    , m_server(new QLocalServer(this))
{
    connect(m_server, &QLocalServer::newConnection, this, &TimelineCommandServer::handleNewConnection);
}

TimelineCommandServer::~TimelineCommandServer()
{
    stop();
}

QString TimelineCommandServer::defaultSocketPath(const QString &instanceName)
{
    QString baseDir = QStandardPaths::writableLocation(QStandardPaths::RuntimeLocation);
    if (baseDir.isEmpty()) {
        baseDir = QStandardPaths::writableLocation(QStandardPaths::TempLocation);
    }
    const QString safeName = instanceName.trimmed().isEmpty() ? QStringLiteral("bangonpro-timeline-v1") : instanceName.trimmed();
    QDir dir(baseDir);
    if (!dir.exists()) {
        dir.mkpath(QStringLiteral("."));
    }
    return dir.filePath(safeName + QStringLiteral(".sock"));
}

bool TimelineCommandServer::start(const QString &socketPath, QString *errorMessageOut)
{
    auto fail = [errorMessageOut](const QString &msg) {
        if (errorMessageOut != nullptr) {
            *errorMessageOut = msg;
        }
        return false;
    };

    if (m_engine == nullptr) {
        return fail(QStringLiteral("TimelineCommandServer requires a non-null TimelineCommandEngine"));
    }

    if (m_server->isListening()) {
        stop();
    }

    const QString resolvedPath = socketPath.trimmed().isEmpty() ? defaultSocketPath() : socketPath.trimmed();
    const QFileInfo fi(resolvedPath);
    if (!fi.dir().exists()) {
        if (!fi.dir().mkpath(QStringLiteral("."))) {
            return fail(QStringLiteral("Failed to create socket parent directory '%1'").arg(fi.dir().absolutePath()));
        }
    }

    // Restrict socket access to the current OS user only.
    m_server->setSocketOptions(QLocalServer::UserAccessOption);

    if (!m_server->listen(resolvedPath)) {
        // Recover cleanly from stale socket files left behind by a crashed prior instance,
        // while refusing to hijack a socket actively served by a live process.
        if (m_server->serverError() == QAbstractSocket::AddressInUseError) {
            QLocalSocket probe;
            probe.connectToServer(resolvedPath);
            if (probe.waitForConnected(150)) {
                probe.disconnectFromServer();
                return fail(QStringLiteral("Socket '%1' is already actively in use").arg(resolvedPath));
            }
            QLocalServer::removeServer(resolvedPath);
            if (!m_server->listen(resolvedPath)) {
                return fail(QStringLiteral("Failed to listen on '%1': %2").arg(resolvedPath, m_server->errorString()));
            }
        } else {
            return fail(QStringLiteral("Failed to listen on '%1': %2").arg(resolvedPath, m_server->errorString()));
        }
    }

#ifdef Q_OS_UNIX
    QFile::setPermissions(resolvedPath, QFileDevice::ReadOwner | QFileDevice::WriteOwner);
#endif

    m_socketPath = resolvedPath;
    return true;
}

void TimelineCommandServer::stop()
{
    const QList<QLocalSocket *> clients = m_clientBuffers.keys();
    for (QLocalSocket *client : clients) {
        if (client != nullptr) {
            client->disconnect(this);
            client->disconnectFromServer();
            client->deleteLater();
        }
    }
    m_clientBuffers.clear();

    for (QLocalSocket *client : std::as_const(m_rejectedClients)) {
        if (client != nullptr) {
            client->disconnect(this);
            client->disconnectFromServer();
            client->deleteLater();
        }
    }
    m_rejectedClients.clear();

    if (m_server != nullptr && m_server->isListening()) {
        m_server->close();
    }
    if (!m_socketPath.isEmpty()) {
        QLocalServer::removeServer(m_socketPath);
        m_socketPath.clear();
    }
}

bool TimelineCommandServer::isListening() const
{
    return m_server != nullptr && m_server->isListening();
}

QString TimelineCommandServer::socketPath() const
{
    return m_socketPath;
}

int TimelineCommandServer::activeClientCount() const
{
    return m_clientBuffers.size();
}

void TimelineCommandServer::handleNewConnection()
{
    while (m_server->hasPendingConnections()) {
        QLocalSocket *client = m_server->nextPendingConnection();
        if (client == nullptr) {
            continue;
        }
        m_clientBuffers.insert(client, QByteArray());
        connect(client, &QLocalSocket::readyRead, this, &TimelineCommandServer::handleClientReadyRead);
        connect(client, &QLocalSocket::disconnected, this, &TimelineCommandServer::handleClientDisconnected);
#if QT_VERSION >= QT_VERSION_CHECK(5, 15, 0)
        connect(client, &QLocalSocket::errorOccurred, this, &TimelineCommandServer::handleClientError);
#else
        connect(client, qOverload<QLocalSocket::LocalSocketError>(&QLocalSocket::error), this, &TimelineCommandServer::handleClientError);
#endif
        Q_EMIT clientConnected();
    }
}

void TimelineCommandServer::handleClientReadyRead()
{
    auto *client = qobject_cast<QLocalSocket *>(sender());
    if (client == nullptr) {
        return;
    }
    if (m_rejectedClients.contains(client)) {
        // Discard any remaining in-flight bytes from an already-rejected abusive client.
        client->readAll();
        return;
    }
    if (!m_clientBuffers.contains(client)) {
        return;
    }

    auto rejectOversized = [this, client]() {
        const QString errMsg = QStringLiteral("Payload exceeds maximum allowed size of %1 bytes").arg(kMaxPayloadBytes);
        const QByteArray errPayload = makeErrorEnvelope(QString(), CommandStatus::ValidationError, m_engine ? m_engine->currentRevision() : 0, errMsg);
        m_clientBuffers.remove(client);
        m_rejectedClients.insert(client);
        client->readAll();
        client->write(errPayload);
        client->write("\n", 1);
        client->flush();
        Q_EMIT protocolError(errMsg);
        QTimer::singleShot(150, client, &QLocalSocket::disconnectFromServer);
    };

    while (client->bytesAvailable() > 0) {
        if (!m_clientBuffers.contains(client)) {
            return;
        }
        QByteArray &buffer = m_clientBuffers[client];
        const qint64 toRead = qMin(client->bytesAvailable(), kReadChunkBytes);
        buffer.append(client->read(toRead));

        // Process all complete newline-delimited frames currently in buffer.
        while (true) {
            const auto newlineIdx = buffer.indexOf('\n');
            if (newlineIdx < 0) {
                if (buffer.size() > kMaxPayloadBytes) {
                    rejectOversized();
                    return;
                }
                break;
            }

            if (newlineIdx > kMaxPayloadBytes) {
                rejectOversized();
                return;
            }

            QByteArray line = buffer.left(newlineIdx);
            buffer.remove(0, newlineIdx + 1);
            if (line.endsWith('\r')) {
                line.chop(1);
            }
            if (line.trimmed().isEmpty()) {
                continue;
            }

            const QByteArray reply = dispatchLine(line);
            client->write(reply);
            client->write("\n", 1);
            client->flush();
        }
    }
}

void TimelineCommandServer::handleClientDisconnected()
{
    auto *client = qobject_cast<QLocalSocket *>(sender());
    if (client == nullptr) {
        return;
    }
    m_clientBuffers.remove(client);
    m_rejectedClients.remove(client);
    client->deleteLater();
    Q_EMIT clientDisconnected();
}

void TimelineCommandServer::handleClientError(QLocalSocket::LocalSocketError socketError)
{
    if (socketError == QLocalSocket::PeerClosedError) {
        return;
    }
    auto *client = qobject_cast<QLocalSocket *>(sender());
    const QString err = client != nullptr ? client->errorString() : QStringLiteral("Unknown local socket error");
    Q_EMIT protocolError(err);
}

QByteArray TimelineCommandServer::dispatchLine(const QByteArray &line)
{
    if (m_engine == nullptr) {
        return makeErrorEnvelope(QString(), CommandStatus::ExecutionFailed, 0, QStringLiteral("Engine unavailable"));
    }

    // Check if the caller sent a lightweight control RPC envelope ("rpc_method").
    QJsonParseError parseErr;
    const QJsonDocument doc = QJsonDocument::fromJson(line, &parseErr);
    if (parseErr.error == QJsonParseError::NoError && doc.isObject()) {
        const QJsonObject rootObj = doc.object();
        if (rootObj.contains(QStringLiteral("rpc_method")) && rootObj.value(QStringLiteral("rpc_method")).isString()) {
            return handleRpcControlRequest(rootObj);
        }
    }

    // Standard Timeline Command API v1 batch execution path.
    const QByteArray responseBytes = m_engine->executeJson(line);

    QJsonParseError respErr;
    const QJsonDocument respDoc = QJsonDocument::fromJson(responseBytes, &respErr);
    if (respErr.error == QJsonParseError::NoError && respDoc.isObject()) {
        TimelineCommandResponse parsed;
        if (TimelineCommandEngine::responseFromJson(respDoc.object(), &parsed)) {
            Q_EMIT requestProcessed(parsed.requestId, parsed.status);
        }
    }

    return responseBytes;
}

QByteArray TimelineCommandServer::handleRpcControlRequest(const QJsonObject &obj)
{
    const QString method = obj.value(QStringLiteral("rpc_method")).toString().trimmed();
    const QString reqId = obj.value(QStringLiteral("request_id")).toString();

    if (method == QStringLiteral("ping")) {
        QJsonObject out;
        out.insert(QStringLiteral("api_version"), QString::fromLatin1(kTimelineApiVersion));
        out.insert(QStringLiteral("request_id"), reqId);
        out.insert(QStringLiteral("status"), commandStatusToString(CommandStatus::Ok));
        out.insert(QStringLiteral("current_revision"), m_engine->currentRevision());
        out.insert(QStringLiteral("pong"), true);
        return QJsonDocument(out).toJson(QJsonDocument::Compact);
    }
    if (method == QStringLiteral("get_revision")) {
        QJsonObject out;
        out.insert(QStringLiteral("api_version"), QString::fromLatin1(kTimelineApiVersion));
        out.insert(QStringLiteral("request_id"), reqId);
        out.insert(QStringLiteral("status"), commandStatusToString(CommandStatus::Ok));
        out.insert(QStringLiteral("current_revision"), m_engine->currentRevision());
        return QJsonDocument(out).toJson(QJsonDocument::Compact);
    }
    if (method == QStringLiteral("get_state")) {
        QJsonObject out = m_engine->stateSnapshotJson();
        out.insert(QStringLiteral("request_id"), reqId);
        out.insert(QStringLiteral("status"), commandStatusToString(CommandStatus::Ok));
        return QJsonDocument(out).toJson(QJsonDocument::Compact);
    }
    if (method == QStringLiteral("get_activity_log")) {
        QJsonObject out;
        out.insert(QStringLiteral("api_version"), QString::fromLatin1(kTimelineApiVersion));
        out.insert(QStringLiteral("request_id"), reqId);
        out.insert(QStringLiteral("status"), commandStatusToString(CommandStatus::Ok));
        out.insert(QStringLiteral("current_revision"), m_engine->currentRevision());
        out.insert(QStringLiteral("activity_log"), m_engine->activityLogJson());
        return QJsonDocument(out).toJson(QJsonDocument::Compact);
    }
    if (method == QStringLiteral("get_pending_proposals")) {
        QJsonObject out;
        out.insert(QStringLiteral("api_version"), QString::fromLatin1(kTimelineApiVersion));
        out.insert(QStringLiteral("request_id"), reqId);
        out.insert(QStringLiteral("status"), commandStatusToString(CommandStatus::Ok));
        out.insert(QStringLiteral("current_revision"), m_engine->currentRevision());
        out.insert(QStringLiteral("pending_proposals"), m_engine->pendingProposalsJson());
        return QJsonDocument(out).toJson(QJsonDocument::Compact);
    }
    if (method == QStringLiteral("approve_proposal")) {
        const QString proposalId = obj.value(QStringLiteral("proposal_request_id")).toString(reqId);
        const bool rebase = obj.value(QStringLiteral("rebase_to_current_revision")).toBool(false);
        const TimelineCommandResponse resp = m_engine->approveProposal(proposalId, rebase);
        Q_EMIT requestProcessed(resp.requestId, resp.status);
        return QJsonDocument(TimelineCommandEngine::responseToJson(resp)).toJson(QJsonDocument::Compact);
    }
    if (method == QStringLiteral("reject_proposal")) {
        const QString proposalId = obj.value(QStringLiteral("proposal_request_id")).toString(reqId);
        const QString reason = obj.value(QStringLiteral("reason")).toString();
        const bool removed = m_engine->rejectProposal(proposalId, reason);
        if (!removed) {
            return makeErrorEnvelope(proposalId, CommandStatus::ValidationError, m_engine->currentRevision(),
                                     QStringLiteral("Pending proposal '%1' not found").arg(proposalId));
        }
        QJsonObject out;
        out.insert(QStringLiteral("api_version"), QString::fromLatin1(kTimelineApiVersion));
        out.insert(QStringLiteral("request_id"), proposalId);
        out.insert(QStringLiteral("status"), commandStatusToString(CommandStatus::Ok));
        out.insert(QStringLiteral("current_revision"), m_engine->currentRevision());
        out.insert(QStringLiteral("rejected"), true);
        return QJsonDocument(out).toJson(QJsonDocument::Compact);
    }
    if (method == QStringLiteral("undo")) {
        const QString actor = obj.value(QStringLiteral("actor_id")).toString(QStringLiteral("rpc-client"));
        const TimelineCommandResponse resp = m_engine->undoLastTransaction(actor);
        Q_EMIT requestProcessed(resp.requestId, resp.status);
        return QJsonDocument(TimelineCommandEngine::responseToJson(resp)).toJson(QJsonDocument::Compact);
    }
    if (method == QStringLiteral("redo")) {
        const QString actor = obj.value(QStringLiteral("actor_id")).toString(QStringLiteral("rpc-client"));
        const TimelineCommandResponse resp = m_engine->redoLastTransaction(actor);
        Q_EMIT requestProcessed(resp.requestId, resp.status);
        return QJsonDocument(TimelineCommandEngine::responseToJson(resp)).toJson(QJsonDocument::Compact);
    }

    return makeErrorEnvelope(reqId, CommandStatus::ValidationError, m_engine->currentRevision(), QStringLiteral("Unknown rpc_method '%1'").arg(method));
}

QByteArray TimelineCommandServer::makeErrorEnvelope(const QString &requestId, CommandStatus status, qint64 revision, const QString &message)
{
    TimelineCommandResponse resp;
    resp.apiVersion = QString::fromLatin1(kTimelineApiVersion);
    resp.requestId = requestId;
    resp.status = status;
    resp.previousRevision = revision;
    resp.currentRevision = revision;
    resp.errorMessage = message;
    return QJsonDocument(TimelineCommandEngine::responseToJson(resp)).toJson(QJsonDocument::Compact);
}

} // namespace BangOnPro
