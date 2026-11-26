/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include <QJsonArray>
#include <QJsonObject>
#include <QObject>
#include <QPointer>

class QMainWindow;
class QTimer;

namespace BangOnPro {

class BangOnProIntegration;

/**
 * @brief Editor acceptance-test runner for the real application (V1.6).
 *
 * Inactive unless BANGONPRO_AUTOTEST names a JSON script. It drives the running app the way
 * an editor does: it clicks the actual buttons by object name (so every signal, slot and
 * subprocess is the production code path), waits for real job states, records what the
 * project contains (sequences, clips, guides) and saves genuine screenshots with QWidget::grab.
 * Results go to <script>.result.json; the app exits with status 0 when every check passed.
 *
 * Steps: {"do": "click"|"set_text"|"set_check"|"select"|"raise_dock"|"import"|"select_bin_all"|"open_project"|
 *         "save_project_as"|"wait_ms"|"wait_job"|"wait_object"|"screenshot"|"resize_window"|"record"|"expect"|
 *         "undo"|"kill_department"|"accept_dialogs"|"quit", ...}
 */
class BangOnProAutotest : public QObject
{
    Q_OBJECT
public:
    BangOnProAutotest(QMainWindow *window, BangOnProIntegration *integration, const QString &scriptPath, QObject *parent = nullptr);
    static void startIfRequested(QMainWindow *window, BangOnProIntegration *integration);

private:
    void next();
    bool runStep(const QJsonObject &step, QString *error);
    bool pollWait();
    QJsonObject snapshot() const;
    void finish(bool ok, const QString &why);

    QPointer<QMainWindow> m_window;
    BangOnProIntegration *m_integration;
    QString m_scriptPath;
    QJsonArray m_steps;
    int m_index{-1};
    QJsonObject m_waiting;
    qint64 m_waitDeadline{0};
    QTimer *m_timer{nullptr};
    QTimer *m_dialogTimer{nullptr};
    QJsonArray m_log;
    QJsonObject m_records;
    bool m_failed{false};
};

} // namespace BangOnPro
