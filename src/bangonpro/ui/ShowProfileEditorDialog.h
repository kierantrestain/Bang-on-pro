/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#pragma once

#include <QDialog>
#include <QJsonArray>
#include <QJsonObject>
#include <QString>
#include <QStringList>

class QCheckBox;
class QComboBox;
class QDoubleSpinBox;
class QLabel;
class QLineEdit;
class QPlainTextEdit;
class QPushButton;
class QSpinBox;
class QTableWidget;

namespace BangOnPro {

/**
 * @brief Visual studio editor for creating and modifying Show Profile v1 JSON files.
 *
 * Covers identity, pacing, silence/interruption detection, camera-to-speaker
 * mapping, audio loudness/restoration, captions/lower-thirds, and review
 * confidence thresholds with live schema validation and JSON file save.
 */
class ShowProfileEditorDialog : public QDialog
{
    Q_OBJECT

public:
    explicit ShowProfileEditorDialog(QWidget *parent = nullptr);
    ~ShowProfileEditorDialog() override = default;

    /** @brief Populate all editor controls from a Show Profile v1 JSON object. */
    bool loadProfileJson(const QJsonObject &profileObj, QString *errorMessage = nullptr);

    /** @brief Load a Show Profile v1 JSON file from disk into the editor. */
    bool loadProfileFile(const QString &filePath, QString *errorMessage = nullptr);

    /** @brief Build a validated Show Profile v1 JSON object from current UI values. */
    QJsonObject buildProfileJson() const;

    /** @brief Run live schema validation; returns empty list when valid. */
    QStringList validateProfile() const;

    /** @brief True when validateProfile() has zero errors. */
    bool isValid() const;

    /** @brief Write the validated profile JSON to @p filePath. */
    bool saveProfileToFile(const QString &filePath, QString *errorMessage = nullptr) const;

    /** @brief Last file path loaded or saved by this dialog. */
    QString savedFilePath() const;

    // Programmatic setters for scripting and automated tests
    void setProfileId(const QString &profileId);
    void setDisplayName(const QString &displayName);
    void setDescription(const QString &description);
    void setFormatType(const QString &formatType);
    void setPacingRules(double minShotSec, double maxShotSec, double reactionIntervalSec = 6.0);
    void setSilenceAndInterruptionRules(double silenceThresholdDb, double minSilenceSec, double interruptionGapSec);
    void setReviewConfidenceThreshold(double threshold);
    void setAudioRules(double targetLufs, bool enableDenoise, bool enableCompressor, double musicDuckDb = -18.0);
    void setGraphicsRules(bool enableCaptions, bool enableLowerThirds, double lowerThirdDurationSec = 4.5,
                          const QString &brandAccentHex = QStringLiteral("#FF5A1F"));
    void clearCameraRows();
    void addCameraRow(const QString &cameraId, const QString &role, const QString &speakerLabel, int priority = 1);

Q_SIGNALS:
    void profileSaved(const QString &filePath, const QJsonObject &profileJson);

private Q_SLOTS:
    void handleAddCameraClicked();
    void handleRemoveCameraClicked();
    void handleLoadJsonClicked();
    void handleSaveJsonClicked();
    void handleUseProfileClicked();
    void updateValidationState();

private:
    void buildUi();
    void populateDefaultProfile();
    QJsonArray buildCamerasArray() const;
    QJsonArray defaultExportPresets() const;

    // General & Video Spec
    QLineEdit *m_profileIdEdit = nullptr;
    QLineEdit *m_displayNameEdit = nullptr;
    QComboBox *m_formatTypeCombo = nullptr;
    QPlainTextEdit *m_descriptionEdit = nullptr;
    QSpinBox *m_widthSpin = nullptr;
    QSpinBox *m_heightSpin = nullptr;
    QSpinBox *m_fpsNumSpin = nullptr;
    QSpinBox *m_fpsDenSpin = nullptr;

    // Pacing, Silence & Review
    QDoubleSpinBox *m_minShotSpin = nullptr;
    QDoubleSpinBox *m_maxShotSpin = nullptr;
    QDoubleSpinBox *m_reactionIntervalSpin = nullptr;
    QDoubleSpinBox *m_silenceDbSpin = nullptr;
    QDoubleSpinBox *m_minSilenceSpin = nullptr;
    QDoubleSpinBox *m_interruptionGapSpin = nullptr;
    QDoubleSpinBox *m_keepPaddingSpin = nullptr;
    QCheckBox *m_trimSetupCheck = nullptr;
    QDoubleSpinBox *m_reviewThresholdSpin = nullptr;

    // Cameras & Speaker Mapping
    QTableWidget *m_cameraTable = nullptr;
    QPushButton *m_addCameraButton = nullptr;
    QPushButton *m_removeCameraButton = nullptr;

    // Audio Rules
    QDoubleSpinBox *m_targetLufsSpin = nullptr;
    QDoubleSpinBox *m_truePeakSpin = nullptr;
    QCheckBox *m_denoiseCheck = nullptr;
    QCheckBox *m_compressorCheck = nullptr;
    QDoubleSpinBox *m_musicDuckSpin = nullptr;

    // Graphics & Captions Rules
    QCheckBox *m_captionsCheck = nullptr;
    QCheckBox *m_lowerThirdsCheck = nullptr;
    QDoubleSpinBox *m_lowerThirdDurationSpin = nullptr;
    QLineEdit *m_accentHexEdit = nullptr;

    // Validation & Footer
    QLabel *m_validationLabel = nullptr;
    QPushButton *m_loadButton = nullptr;
    QPushButton *m_saveJsonButton = nullptr;
    QPushButton *m_useProfileButton = nullptr;
    QPushButton *m_cancelButton = nullptr;

    QJsonArray m_exportPresets;
    QString m_colorSpace = QStringLiteral("bt709");
    QString m_savedFilePath;
};

} // namespace BangOnPro
