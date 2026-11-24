/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include <QDialog>
#include <QMetaType>
#include <QString>

class QCheckBox;
class QComboBox;
class QGroupBox;
class QLabel;
class QLineEdit;
class QPushButton;
class QRadioButton;

namespace BangOnPro {

/**
 * @brief AI execution backend mode. LocalOnly is always the free offline default.
 */
enum class AiBackendMode {
    LocalOnly = 0,
    ExternalProvider = 1
};

/**
 * @brief Persisted AI backend and explicit privacy consent preferences.
 *
 * API credentials are never persisted in QSettings; external endpoints read
 * credentials exclusively from environment variables or OS keychain integration.
 */
struct ExternalProviderSettings
{
    AiBackendMode mode = AiBackendMode::LocalOnly;
    QString providerName = QStringLiteral("OpenAI-Compatible");
    QString endpointUrl = QStringLiteral("https://api.openai.com/v1");
    QString modelName = QStringLiteral("gpt-4o-mini");
    bool sendTranscriptText = true;
    bool sendAudioExcerpts = false;
    bool sendFrameSnapshots = false;
    bool sendProjectMetadata = true;
    bool explicitConsentGranted = false;
    QString consentTimestampUtc;

    /** @brief True only when ExternalProvider is selected AND explicit consent was granted. */
    bool isExternalTransmissionAllowed() const
    {
        return mode == AiBackendMode::ExternalProvider && explicitConsentGranted && !endpointUrl.trimmed().isEmpty() && !modelName.trimmed().isEmpty();
    }
};

/**
 * @brief Studio privacy & AI provider configuration dialog.
 *
 * Defaults to Local Only (Free & Offline). Enabling an optional external AI
 * provider requires configuring an endpoint + model, reviewing the explicit
 * data transmission checklist, and checking the mandatory opt-in consent box.
 */
class ExternalProviderConsentDialog : public QDialog
{
    Q_OBJECT

public:
    explicit ExternalProviderConsentDialog(QWidget *parent = nullptr, const QString &settingsOrg = QStringLiteral("BangOnPro"),
                                           const QString &settingsApp = QStringLiteral("AiProviders"));
    ~ExternalProviderConsentDialog() override = default;

    /** @brief Load persisted provider & consent preferences from QSettings. */
    static ExternalProviderSettings loadSettings(const QString &settingsOrg = QStringLiteral("BangOnPro"),
                                                 const QString &settingsApp = QStringLiteral("AiProviders"));

    /** @brief Persist provider & consent preferences to QSettings. */
    static void saveSettings(const ExternalProviderSettings &settings, const QString &settingsOrg = QStringLiteral("BangOnPro"),
                             const QString &settingsApp = QStringLiteral("AiProviders"));

    /** @brief Build settings struct reflecting current UI state. */
    ExternalProviderSettings currentSettings() const;

    /** @brief Populate dialog controls from @p settings. */
    void setSettings(const ExternalProviderSettings &settings);

    /** @brief True if the current UI configuration satisfies all safety & consent gates. */
    bool canAcceptConfiguration() const;

    /** @brief Human-readable validation / consent error (empty when valid). */
    QString validationError() const;

    // Programmatic setters for scripting and automated tests
    void setBackendMode(AiBackendMode mode);
    void setEndpointUrl(const QString &url);
    void setModelName(const QString &model);
    void setExplicitConsentGranted(bool granted);

Q_SIGNALS:
    void settingsSaved(const BangOnPro::ExternalProviderSettings &settings);

private Q_SLOTS:
    void handleModeChanged();
    void handleProviderPresetChanged(int index);
    void handleSaveClicked();
    void updateConsentGate();

private:
    void buildUi();

    QString m_settingsOrg;
    QString m_settingsApp;
    QString m_existingConsentTimestamp;

    QRadioButton *m_localOnlyRadio = nullptr;
    QRadioButton *m_externalRadio = nullptr;

    QGroupBox *m_externalConfigGroup = nullptr;
    QComboBox *m_providerCombo = nullptr;
    QLineEdit *m_endpointEdit = nullptr;
    QLineEdit *m_modelEdit = nullptr;

    QGroupBox *m_privacyGroup = nullptr;
    QCheckBox *m_sendTranscriptCheck = nullptr;
    QCheckBox *m_sendAudioCheck = nullptr;
    QCheckBox *m_sendFramesCheck = nullptr;
    QCheckBox *m_sendMetadataCheck = nullptr;
    QCheckBox *m_explicitConsentCheck = nullptr;

    QLabel *m_statusLabel = nullptr;
    QPushButton *m_saveButton = nullptr;
    QPushButton *m_cancelButton = nullptr;
};

} // namespace BangOnPro

Q_DECLARE_METATYPE(BangOnPro::ExternalProviderSettings)
