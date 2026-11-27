/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include "TimelineCommandEngine.h"

#include <QByteArray>
#include <QHash>
#include <QLocalServer>
#include <QLocalSocket>
#include <QObject>
#include <QString>
#include <QtGlobal>

class QTimer;

namespace BangOnPro {

/**
 * @brief Local Unix-domain / QLocalServer newline-delimited JSON-RPC bridge for TimelineCommandEngine.
 *
 * Allows out-of-process Python AI agents to submit validated Timeline Command API v1 JSON payloads
 * to a running Bang On Pro instance over a user-private socket without any shell execution.
 *
 * Security & framing guarantees:
 * - User-private socket permissions (QLocalServer::UserAccessOption + 0600 on Unix)
 * - Strict maximum payload size of 16 MiB per newline-delimited JSON frame
 * - Zero shell execution; all requests are validated by TimelineCommandEngine
 */
class TimelineCommandServer : public QObject
{
    Q_OBJECT

public:
    static constexpr qint64 kMaxPayloadBytes = 16 * 1024 * 1024; // 16 MiB
    static constexpr int kMaxConcurrentClients = 8;
    static constexpr int kClientIdleTimeoutMs = 60 * 1000; // 60 seconds

    explicit TimelineCommandServer(TimelineCommandEngine *engine, QObject *parent = nullptr);
    ~TimelineCommandServer() override;

    /** @brief Computes a user-private default socket path inside QStandardPaths::RuntimeLocation. */
    static QString defaultSocketPath(const QString &instanceName = QStringLiteral("bangonpro-timeline-v1"));

    /** @brief Starts listening on the given socket path (or defaultSocketPath() if empty). */
    bool start(const QString &socketPath = QString(), QString *errorMessageOut = nullptr);

    /** @brief Stops listening, disconnects all active clients, and removes the socket endpoint. */
    void stop();

    bool isListening() const;
    QString socketPath() const;
    int activeClientCount() const;

Q_SIGNALS:
    void clientConnected();
    void clientDisconnected();
    void requestProcessed(const QString &requestId, CommandStatus status);
    void protocolError(const QString &message);

private Q_SLOTS:
    void handleNewConnection();
    void handleClientReadyRead();
    void handleClientDisconnected();
    void handleClientError(QLocalSocket::LocalSocketError socketError);

private:
    QByteArray dispatchLine(const QByteArray &line);
    QByteArray handleRpcControlRequest(const QJsonObject &obj);
    void startClientIdleTimer(QLocalSocket *client);
    void resetClientIdleTimer(QLocalSocket *client);
    void removeClientIdleTimer(QLocalSocket *client);
    static QByteArray makeErrorEnvelope(const QString &requestId, CommandStatus status, qint64 revision, const QString &message);

    TimelineCommandEngine *m_engine = nullptr;
    QLocalServer *m_server = nullptr;
    QString m_socketPath;
    QHash<QLocalSocket *, QByteArray> m_clientBuffers;
    QHash<QLocalSocket *, QTimer *> m_clientIdleTimers;
    QSet<QLocalSocket *> m_rejectedClients;
};

} // namespace BangOnPro
