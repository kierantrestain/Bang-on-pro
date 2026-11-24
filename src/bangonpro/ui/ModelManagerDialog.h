/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include <QDialog>
#include <QJsonObject>
#include <QPointer>
#include <QString>
#include <QStringList>

class QLabel;
class QProcess;
class QProgressBar;
class QPushButton;
class QTreeWidget;

namespace BangOnPro {

/**
 * @brief Studio dialog for inspecting hardware acceleration and managing local
 * Whisper-family ASR and SAM-family segmentation models under BANGONPRO_MODEL_DIR.
 *
 * Explicitly distinguishes backend package installation, on-disk model weights,
 * and active neural inference vs DSP/OpenCV fallback status.
 */
class ModelManagerDialog : public QDialog
{
    Q_OBJECT

public:
    explicit ModelManagerDialog(const QString &pythonExecutable, const QString &pythonPackageRoot, QWidget *parent = nullptr);
    ~ModelManagerDialog() override;

    /** @brief Populate hardware banner and model table from a ModelManager storage report JSON. */
    bool loadStatusJson(const QJsonObject &reportObj);

    /** @brief Number of catalog models currently displayed in the table. */
    int modelCount() const;

    /** @brief Currently selected catalog model_id, or empty string. */
    QString selectedModelId() const;

    /** @brief Programmatically select a model row by catalog model_id. */
    bool selectModelById(const QString &modelId);

    /** @brief Hardware summary banner text (for inspection/testing). */
    QString hardwareSummaryText() const;

    /** @brief Access underlying tree widget (for inspection/testing). */
    QTreeWidget *modelTreeWidget() const { return m_modelTree; }

    /** @brief Override model storage directory passed to CLI (--model-dir). */
    void setModelDirOverride(const QString &dirPath) { m_modelDirOverride = dirPath; }

public Q_SLOTS:
    void refreshStatus();

Q_SIGNALS:
    void modelStatusUpdated(const QJsonObject &reportObj);
    void modelOperationCompleted(const QString &action, const QString &modelId, bool success, const QString &message);

private Q_SLOTS:
    void handleSelectionChanged();
    void handleInstallClicked();
    void handleImportLocalClicked();
    void handleRemoveClicked();
    void handleCancelClicked();

private:
    void buildUi();
    void setBusy(bool busy, const QString &statusText = QString());
    QProcess *spawnCli(const QStringList &args);
    void runInstallModel(const QString &modelId, const QString &localSourcePath);
    void updateButtonStates();

    QString m_pythonExe;
    QString m_pythonRoot;
    QString m_modelDirOverride;
    QJsonObject m_lastReport;

    QLabel *m_hardwareLabel = nullptr;
    QLabel *m_storageLabel = nullptr;
    QTreeWidget *m_modelTree = nullptr;
    QProgressBar *m_progressBar = nullptr;
    QLabel *m_statusLabel = nullptr;

    QPushButton *m_installBtn = nullptr;
    QPushButton *m_importLocalBtn = nullptr;
    QPushButton *m_removeBtn = nullptr;
    QPushButton *m_cancelBtn = nullptr;
    QPushButton *m_refreshBtn = nullptr;
    QPushButton *m_closeBtn = nullptr;

    QPointer<QProcess> m_activeProcess;
    bool m_busy = false;
};

} // namespace BangOnPro
