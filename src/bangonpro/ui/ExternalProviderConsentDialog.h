/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include <QDialog>
#include <QHash>
#include <QJsonObject>
#include <QStringList>

#include <functional>

class QCheckBox;
class QLabel;
class QProcess;
class QPushButton;
class QRadioButton;
class QSpinBox;
class QVBoxLayout;

namespace BangOnPro {

enum class AiBackendMode { LocalOnly, ExternalProvider };

struct ExternalProviderSettings
{
    AiBackendMode mode = AiBackendMode::LocalOnly;
    bool sendTranscriptText = true;
    bool sendProjectMetadata = true;
    bool explicitConsentGranted = false;

    bool isExternalTransmissionAllowed() const { return mode == AiBackendMode::ExternalProvider && explicitConsentGranted; }
};

class ExternalProviderConsentDialog : public QDialog
{
    Q_OBJECT

public:
    using StartPython = std::function<QProcess *(const QStringList &)>;
    explicit ExternalProviderConsentDialog(QWidget *parent = nullptr, StartPython startPython = {}, const QString &settingsOrg = QStringLiteral("BangOnPro"),
                                           const QString &settingsApp = QStringLiteral("AiProviders"));

    ExternalProviderSettings currentSettings() const;
    bool canAcceptConfiguration() const;
    void setBackendMode(AiBackendMode mode);
    void setExplicitConsentGranted(bool granted);

Q_SIGNALS:
    void settingsSaved(bool connected);

private:
    struct ProviderRow
    {
        QWidget *widget = nullptr;
        QLabel *status = nullptr;
        QPushButton *connect = nullptr;
        QPushButton *check = nullptr;
        QPushButton *test = nullptr;
        QPushButton *disconnect = nullptr;
        QPushButton *install = nullptr;
        QPushButton *up = nullptr;
        QPushButton *down = nullptr;
    };

    void run(const QStringList &args, bool reloadSettings = false, bool closeOnSuccess = false);
    void showStatus(const QJsonObject &status, bool reloadSettings);
    void moveProvider(const QString &id, int delta);
    void syncControls();
    void save();

    StartPython m_startPython;
    QString m_settingsOrg;
    QString m_settingsApp;
    QStringList m_priority;
    QHash<QString, ProviderRow> m_rows;
    QJsonObject m_status;
    QVBoxLayout *m_providers = nullptr;
    QRadioButton *m_local = nullptr;
    QRadioButton *m_connected = nullptr;
    QCheckBox *m_consent = nullptr;
    QCheckBox *m_transcript = nullptr;
    QCheckBox *m_metadata = nullptr;
    QCheckBox *m_failover = nullptr;
    QSpinBox *m_taskLimit = nullptr;
    QSpinBox *m_dayLimit = nullptr;
    QLabel *m_routing = nullptr;
    QLabel *m_feedback = nullptr;
    QPushButton *m_save = nullptr;
    QPushButton *m_refresh = nullptr;
    bool m_busy = false;
    bool m_loaded = false;
};

} // namespace BangOnPro
