/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include "BangOnProContracts.h"

#include <QJsonArray>
#include <QObject>
#include <QPointer>
#include <QString>
#include <QStringList>
#include <memory>

class QDockWidget;
class QMainWindow;
class QProcess;
class EffectListWidget;

namespace BangOnPro {

class AiAssistantDock;
class PremierePresetBrowser;
class TimelineCommandEngine;
class TimelineCommandServer;

/**
 * @brief Wires Bang On Pro modules into the Kdenlive MainWindow.
 *
 * - Timeline Command API v1 engine backed by the live Kdenlive timeline (single authorised writer)
 * - Local IPC server so out-of-process agents submit validated JSON batches (no simulated input)
 * - AI Assistant dock: proposal → review → accept/reject/undo, executed through the engine
 * - Premiere preset browser: import archive via the Python compatibility engine, install mapped
 *   presets as native Kdenlive custom effects and apply them to the selected timeline clip
 *
 * The Python AI/preset tooling runs as a separate process (QProcess, argument vector, no shell).
 */
class BangOnProIntegration : public QObject
{
    Q_OBJECT

public:
    BangOnProIntegration(QMainWindow *mainWindow, EffectListWidget *effectList, QObject *parent = nullptr);
    ~BangOnProIntegration() override;

    QDockWidget *aiDock() const { return m_aiDock; }
    QDockWidget *presetDock() const { return m_presetDock; }
    QString ipcSocketPath() const;

    /** Directory containing the `bangonpro` Python package (env BANGONPRO_PYTHONPATH overrides). */
    static QString locatePythonPackageRoot();
    static QString pythonExecutable();
    static QString dataDir();

private:
    void setupEngine();
    void setupAiDock();
    void setupPresetBrowser();
    void loadPresetReportIfPresent();

    void startProposal(const QString &instruction, const QString &profileId, const QString &customProfilePath);
    void handleProposalFinished(int exitCode);
    void approveBatch(const TimelineCommandBatch &batch);
    void rejectBatch(const QString &requestId);
    void undoLast();
    void cancelTask();

    void importPresetArchive(const QString &zipPath);
    void applyPreset(const QString &presetId, const QJsonArray &mltFilters);

    QProcess *startPython(const QStringList &args);

    QMainWindow *m_mainWindow;
    QPointer<EffectListWidget> m_effectList;
    std::unique_ptr<TimelineCommandEngine> m_engine;
    std::unique_ptr<TimelineCommandServer> m_server;
    AiAssistantDock *m_aiWidget{nullptr};
    QDockWidget *m_aiDock{nullptr};
    PremierePresetBrowser *m_presetWidget{nullptr};
    QDockWidget *m_presetDock{nullptr};
    QPointer<QProcess> m_aiProcess;
    QString m_aiOutputDir;
    QStringList m_lastInputs;
    QString m_lastProfile;
};

} // namespace BangOnPro
