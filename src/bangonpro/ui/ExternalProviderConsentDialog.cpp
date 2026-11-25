/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "ExternalProviderConsentDialog.h"
#include "BangOnProTheme.h"

#include <QCheckBox>
#include <QComboBox>
#include <QDateTime>
#include <QFormLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QLabel>
#include <QLineEdit>
#include <QPushButton>
#include <QRadioButton>
#include <QSettings>
#include <QUrl>
#include <QVBoxLayout>

namespace BangOnPro {

ExternalProviderConsentDialog::ExternalProviderConsentDialog(QWidget *parent, const QString &settingsOrg, const QString &settingsApp)
    : QDialog(parent)
    , m_settingsOrg(settingsOrg)
    , m_settingsApp(settingsApp)
{
    qRegisterMetaType<BangOnPro::ExternalProviderSettings>("BangOnPro::ExternalProviderSettings");
    setObjectName(QStringLiteral("bangOnProExternalProviderConsentDialog"));
    setWindowTitle(tr("Bang On Pro — AI Providers && Data Privacy Consent"));
    resize(620, 520);
    buildUi();
    setSettings(loadSettings(m_settingsOrg, m_settingsApp));
}

void ExternalProviderConsentDialog::buildUi()
{
    auto *root = new QVBoxLayout(this);
    root->setContentsMargins(12, 12, 12, 12);
    root->setSpacing(10);

    // 1. Mode Selection
    auto *modeGroup = new QGroupBox(tr("AI Processing Mode"), this);
    auto *modeLayout = new QVBoxLayout(modeGroup);
    modeLayout->setSpacing(6);

    m_localOnlyRadio = new QRadioButton(tr("Local Only — Free && Offline [Default, Recommended]"), modeGroup);
    m_localOnlyRadio->setObjectName(QStringLiteral("localOnlyModeRadio"));
    auto *localNote = new QLabel(tr("All sync, VAD, speaker diarisation, rough cutting, audio restoration, and captions run 100% locally on your "
                                    "machine. Zero media, audio, or transcript data leaves your computer."),
                                 modeGroup);
    localNote->setWordWrap(true);
    localNote->setObjectName(QStringLiteral("bangOnProMutedLabel"));
    localNote->setContentsMargins(20, 0, 0, 4);

    m_externalRadio = new QRadioButton(tr("External AI Provider (Optional Opt-In)"), modeGroup);
    m_externalRadio->setObjectName(QStringLiteral("externalProviderModeRadio"));
    auto *extNote = new QLabel(tr("Connect an optional cloud or remote LAN model endpoint for advanced script reasoning. Requires explicit "
                                  "privacy consent below."),
                               modeGroup);
    extNote->setWordWrap(true);
    extNote->setObjectName(QStringLiteral("bangOnProMutedLabel"));
    extNote->setContentsMargins(20, 0, 0, 0);

    modeLayout->addWidget(m_localOnlyRadio);
    modeLayout->addWidget(localNote);
    modeLayout->addWidget(m_externalRadio);
    modeLayout->addWidget(extNote);
    root->addWidget(modeGroup);

    // 2. External Endpoint Configuration
    m_externalConfigGroup = new QGroupBox(tr("External Provider Endpoint"), this);
    auto *cfgForm = new QFormLayout(m_externalConfigGroup);
    cfgForm->setSpacing(6);

    m_providerCombo = new QComboBox(m_externalConfigGroup);
    m_providerCombo->setObjectName(QStringLiteral("externalProviderPresetCombo"));
    m_providerCombo->addItem(tr("OpenAI-Compatible"), QStringLiteral("https://api.openai.com/v1"));
    m_providerCombo->addItem(tr("Anthropic"), QStringLiteral("https://api.anthropic.com/v1"));
    m_providerCombo->addItem(tr("Remote LAN / Self-Hosted (Ollama / vLLM)"), QStringLiteral("http://localhost:11434/v1"));
    m_providerCombo->addItem(tr("Custom HTTPS Endpoint"), QString());

    m_endpointEdit = new QLineEdit(m_externalConfigGroup);
    m_endpointEdit->setObjectName(QStringLiteral("externalEndpointUrlEdit"));
    m_endpointEdit->setPlaceholderText(QStringLiteral("https://api.example.com/v1"));

    m_modelEdit = new QLineEdit(m_externalConfigGroup);
    m_modelEdit->setObjectName(QStringLiteral("externalModelNameEdit"));
    m_modelEdit->setPlaceholderText(QStringLiteral("e.g. gpt-4o-mini, claude-3-5-sonnet, llama3.1"));

    auto *keyNote = new QLabel(tr("Security: API keys are never saved in plaintext settings. Export BANGONPRO_AI_API_KEY in your environment."),
                               m_externalConfigGroup);
    keyNote->setWordWrap(true);
    keyNote->setObjectName(QStringLiteral("bangOnProMutedLabel"));

    cfgForm->addRow(tr("Provider:"), m_providerCombo);
    cfgForm->addRow(tr("Endpoint URL*:"), m_endpointEdit);
    cfgForm->addRow(tr("Model ID*:"), m_modelEdit);
    cfgForm->addRow(QString(), keyNote);
    root->addWidget(m_externalConfigGroup);

    // 3. Explicit Privacy & Data Transmission Checklist
    m_privacyGroup = new QGroupBox(tr("External Data Transmission && Privacy Consent Checklist"), this);
    auto *privLayout = new QVBoxLayout(m_privacyGroup);
    privLayout->setSpacing(6);

    auto *privIntro = new QLabel(tr("Choose exactly which project data may be transmitted externally when running an external AI request. "
                                    "Full-resolution raw camera media files are NEVER uploaded."),
                                 m_privacyGroup);
    privIntro->setWordWrap(true);
    privIntro->setObjectName(QStringLiteral("bangOnProMutedLabel"));

    m_sendTranscriptCheck = new QCheckBox(tr("Transcript text && speaker labels (from local speech/diarisation engine)"), m_privacyGroup);
    m_sendTranscriptCheck->setObjectName(QStringLiteral("consentSendTranscriptCheck"));

    m_sendAudioCheck = new QCheckBox(tr("Compressed mono audio excerpts (only when requesting external ASR/transcription)"), m_privacyGroup);
    m_sendAudioCheck->setObjectName(QStringLiteral("consentSendAudioCheck"));

    m_sendFramesCheck = new QCheckBox(tr("Downscaled keyframe thumbnails (only when requesting external visual framing checks)"), m_privacyGroup);
    m_sendFramesCheck->setObjectName(QStringLiteral("consentSendFramesCheck"));

    m_sendMetadataCheck = new QCheckBox(tr("Sequence edit metadata (cut timecodes, markers, and active Show Profile rules)"), m_privacyGroup);
    m_sendMetadataCheck->setObjectName(QStringLiteral("consentSendMetadataCheck"));

    m_explicitConsentCheck =
        new QCheckBox(tr("I explicitly consent to sending the selected data above to the configured external AI endpoint."), m_privacyGroup);
    m_explicitConsentCheck->setObjectName(QStringLiteral("explicitConsentCheckBox"));
    setTone(m_explicitConsentCheck, "warning");

    privLayout->addWidget(privIntro);
    privLayout->addWidget(m_sendTranscriptCheck);
    privLayout->addWidget(m_sendAudioCheck);
    privLayout->addWidget(m_sendFramesCheck);
    privLayout->addWidget(m_sendMetadataCheck);
    privLayout->addSpacing(4);
    privLayout->addWidget(m_explicitConsentCheck);
    root->addWidget(m_privacyGroup);

    // Status / gate banner
    m_statusLabel = new QLabel(this);
    m_statusLabel->setObjectName(QStringLiteral("providerConsentStatusLabel"));
    m_statusLabel->setWordWrap(true);
    root->addWidget(m_statusLabel);

    // Footer buttons
    auto *footer = new QHBoxLayout();
    m_cancelButton = new QPushButton(tr("Cancel"), this);
    m_saveButton = new QPushButton(tr("Save AI provider settings"), this);
    m_saveButton->setObjectName(QStringLiteral("saveProviderSettingsButton"));
    m_saveButton->setProperty("bangOnProPrimary", true);
    m_saveButton->setDefault(true);

    footer->addStretch(1);
    footer->addWidget(m_cancelButton);
    footer->addWidget(m_saveButton);
    root->addLayout(footer);

    connect(m_localOnlyRadio, &QRadioButton::toggled, this, &ExternalProviderConsentDialog::handleModeChanged);
    connect(m_externalRadio, &QRadioButton::toggled, this, &ExternalProviderConsentDialog::handleModeChanged);
    connect(m_providerCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, &ExternalProviderConsentDialog::handleProviderPresetChanged);
    connect(m_endpointEdit, &QLineEdit::textChanged, this, &ExternalProviderConsentDialog::updateConsentGate);
    connect(m_modelEdit, &QLineEdit::textChanged, this, &ExternalProviderConsentDialog::updateConsentGate);
    connect(m_sendTranscriptCheck, &QCheckBox::toggled, this, &ExternalProviderConsentDialog::updateConsentGate);
    connect(m_sendAudioCheck, &QCheckBox::toggled, this, &ExternalProviderConsentDialog::updateConsentGate);
    connect(m_sendFramesCheck, &QCheckBox::toggled, this, &ExternalProviderConsentDialog::updateConsentGate);
    connect(m_sendMetadataCheck, &QCheckBox::toggled, this, &ExternalProviderConsentDialog::updateConsentGate);
    connect(m_explicitConsentCheck, &QCheckBox::toggled, this, &ExternalProviderConsentDialog::updateConsentGate);
    connect(m_saveButton, &QPushButton::clicked, this, &ExternalProviderConsentDialog::handleSaveClicked);
    connect(m_cancelButton, &QPushButton::clicked, this, &QDialog::reject);
}

ExternalProviderSettings ExternalProviderConsentDialog::loadSettings(const QString &settingsOrg, const QString &settingsApp)
{
    ExternalProviderSettings s;
    QSettings qs(settingsOrg, settingsApp);
    const QString modeStr = qs.value(QStringLiteral("mode"), QStringLiteral("local_only")).toString();
    s.mode = (modeStr == QStringLiteral("external_provider")) ? AiBackendMode::ExternalProvider : AiBackendMode::LocalOnly;
    s.providerName = qs.value(QStringLiteral("provider_name"), s.providerName).toString();
    s.endpointUrl = qs.value(QStringLiteral("endpoint_url"), s.endpointUrl).toString();
    s.modelName = qs.value(QStringLiteral("model_name"), s.modelName).toString();
    s.sendTranscriptText = qs.value(QStringLiteral("send_transcript_text"), true).toBool();
    s.sendAudioExcerpts = qs.value(QStringLiteral("send_audio_excerpts"), false).toBool();
    s.sendFrameSnapshots = qs.value(QStringLiteral("send_frame_snapshots"), false).toBool();
    s.sendProjectMetadata = qs.value(QStringLiteral("send_project_metadata"), true).toBool();
    s.explicitConsentGranted = qs.value(QStringLiteral("explicit_consent_granted"), false).toBool();
    s.consentTimestampUtc = qs.value(QStringLiteral("consent_timestamp_utc")).toString();

    // Safety invariant: never restore external mode without explicit consent
    if (s.mode == AiBackendMode::ExternalProvider && !s.explicitConsentGranted) {
        s.mode = AiBackendMode::LocalOnly;
    }
    return s;
}

void ExternalProviderConsentDialog::saveSettings(const ExternalProviderSettings &settings, const QString &settingsOrg, const QString &settingsApp)
{
    QSettings qs(settingsOrg, settingsApp);
    const bool allowExternal = settings.isExternalTransmissionAllowed();
    qs.setValue(QStringLiteral("mode"), allowExternal ? QStringLiteral("external_provider") : QStringLiteral("local_only"));
    qs.setValue(QStringLiteral("provider_name"), settings.providerName);
    qs.setValue(QStringLiteral("endpoint_url"), settings.endpointUrl.trimmed());
    qs.setValue(QStringLiteral("model_name"), settings.modelName.trimmed());
    qs.setValue(QStringLiteral("send_transcript_text"), settings.sendTranscriptText);
    qs.setValue(QStringLiteral("send_audio_excerpts"), settings.sendAudioExcerpts);
    qs.setValue(QStringLiteral("send_frame_snapshots"), settings.sendFrameSnapshots);
    qs.setValue(QStringLiteral("send_project_metadata"), settings.sendProjectMetadata);
    qs.setValue(QStringLiteral("explicit_consent_granted"), allowExternal && settings.explicitConsentGranted);
    qs.setValue(QStringLiteral("consent_timestamp_utc"), allowExternal ? settings.consentTimestampUtc : QString());
    qs.sync();
}

ExternalProviderSettings ExternalProviderConsentDialog::currentSettings() const
{
    ExternalProviderSettings s;
    s.mode = m_externalRadio->isChecked() ? AiBackendMode::ExternalProvider : AiBackendMode::LocalOnly;
    s.providerName = m_providerCombo->currentText();
    s.endpointUrl = m_endpointEdit->text().trimmed();
    s.modelName = m_modelEdit->text().trimmed();
    s.sendTranscriptText = m_sendTranscriptCheck->isChecked();
    s.sendAudioExcerpts = m_sendAudioCheck->isChecked();
    s.sendFrameSnapshots = m_sendFramesCheck->isChecked();
    s.sendProjectMetadata = m_sendMetadataCheck->isChecked();
    s.explicitConsentGranted = m_explicitConsentCheck->isChecked();
    if (s.mode == AiBackendMode::ExternalProvider && s.explicitConsentGranted) {
        s.consentTimestampUtc =
            m_existingConsentTimestamp.isEmpty() ? QDateTime::currentDateTimeUtc().toString(Qt::ISODate) : m_existingConsentTimestamp;
    }
    return s;
}

void ExternalProviderConsentDialog::setSettings(const ExternalProviderSettings &settings)
{
    m_existingConsentTimestamp = settings.consentTimestampUtc;
    m_localOnlyRadio->setChecked(settings.mode == AiBackendMode::LocalOnly);
    m_externalRadio->setChecked(settings.mode == AiBackendMode::ExternalProvider);

    const int pIdx = m_providerCombo->findText(settings.providerName);
    m_providerCombo->blockSignals(true);
    if (pIdx >= 0) {
        m_providerCombo->setCurrentIndex(pIdx);
    }
    m_providerCombo->blockSignals(false);

    m_endpointEdit->setText(settings.endpointUrl);
    m_modelEdit->setText(settings.modelName);
    m_sendTranscriptCheck->setChecked(settings.sendTranscriptText);
    m_sendAudioCheck->setChecked(settings.sendAudioExcerpts);
    m_sendFramesCheck->setChecked(settings.sendFrameSnapshots);
    m_sendMetadataCheck->setChecked(settings.sendProjectMetadata);
    m_explicitConsentCheck->setChecked(settings.explicitConsentGranted);

    handleModeChanged();
}

bool ExternalProviderConsentDialog::canAcceptConfiguration() const
{
    return validationError().isEmpty();
}

QString ExternalProviderConsentDialog::validationError() const
{
    if (m_localOnlyRadio->isChecked()) {
        return {};
    }

    const QString urlStr = m_endpointEdit->text().trimmed();
    if (urlStr.isEmpty()) {
        return tr("External endpoint URL is required.");
    }
    const QUrl parsed(urlStr);
    const QString scheme = parsed.scheme().toLower();
    if (!parsed.isValid() || (scheme != QStringLiteral("https") && scheme != QStringLiteral("http")) || parsed.host().isEmpty()) {
        return tr("Endpoint URL must be a valid http:// or https:// address.");
    }

    if (m_modelEdit->text().trimmed().isEmpty()) {
        return tr("Target model ID is required for external provider mode.");
    }

    const bool anyDataSelected = m_sendTranscriptCheck->isChecked() || m_sendAudioCheck->isChecked() || m_sendFramesCheck->isChecked() ||
                                 m_sendMetadataCheck->isChecked();
    if (!anyDataSelected) {
        return tr("Select at least one permitted data scope or switch to Local Only mode.");
    }

    if (!m_explicitConsentCheck->isChecked()) {
        return tr("Explicit opt-in consent checkbox is required before enabling external AI transmission.");
    }

    return {};
}

void ExternalProviderConsentDialog::setBackendMode(AiBackendMode mode)
{
    if (mode == AiBackendMode::ExternalProvider) {
        m_externalRadio->setChecked(true);
    } else {
        m_localOnlyRadio->setChecked(true);
    }
}

void ExternalProviderConsentDialog::setEndpointUrl(const QString &url)
{
    m_endpointEdit->setText(url);
}

void ExternalProviderConsentDialog::setModelName(const QString &model)
{
    m_modelEdit->setText(model);
}

void ExternalProviderConsentDialog::setExplicitConsentGranted(bool granted)
{
    m_explicitConsentCheck->setChecked(granted);
}

void ExternalProviderConsentDialog::handleModeChanged()
{
    const bool isExternal = m_externalRadio->isChecked();
    m_externalConfigGroup->setEnabled(isExternal);
    m_privacyGroup->setEnabled(isExternal);
    updateConsentGate();
}

void ExternalProviderConsentDialog::handleProviderPresetChanged(int index)
{
    const QString defaultUrl = m_providerCombo->itemData(index).toString();
    if (!defaultUrl.isEmpty()) {
        m_endpointEdit->setText(defaultUrl);
    }
}

void ExternalProviderConsentDialog::updateConsentGate()
{
    const QString err = validationError();
    const bool ok = err.isEmpty();

    if (m_localOnlyRadio->isChecked()) {
        m_statusLabel->setText(tr("✓ Local Only mode active — 100% offline, free, and private."));
        setTone(m_statusLabel, "success");
    } else if (ok) {
        m_statusLabel->setText(tr("✓ Explicit consent recorded — external requests permitted for selected scopes only."));
        setTone(m_statusLabel, "warning");
    } else {
        m_statusLabel->setText(tr("⚠ External transmission blocked: %1").arg(err));
        setTone(m_statusLabel, "danger");
    }

    m_saveButton->setEnabled(ok);
}

void ExternalProviderConsentDialog::handleSaveClicked()
{
    if (!canAcceptConfiguration()) {
        return;
    }
    const ExternalProviderSettings s = currentSettings();
    saveSettings(s, m_settingsOrg, m_settingsApp);
    Q_EMIT settingsSaved(s);
    accept();
}

} // namespace BangOnPro
