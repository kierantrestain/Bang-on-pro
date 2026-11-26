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
class QListWidget;
class QPlainTextEdit;
class QPushButton;

namespace BangOnPro {

/**
 * @brief Reference-video style learning dialog (V1.6).
 *
 * Allows an editor to import one or multiple reference videos, run local
 * analysis via the production department CLI (cached by content hash),
 * preview measured editing/colour/audio/caption/graphics observations,
 * adjust parameters, approve and save a reusable Show Profile, or select
 * and remove previously saved styles.
 */
class ReferenceStyleDialog : public QDialog
{
    Q_OBJECT

public:
    explicit ReferenceStyleDialog(QWidget *parent = nullptr);
    ~ReferenceStyleDialog() override = default;

    /** @brief Populate the list of saved custom/reference styles. */
    void setSavedStyles(const QJsonArray &styles);

    /** @brief Populate the preview & editable fields from `style analyze` JSON output. */
    void setAnalysisResult(const QJsonObject &result);

    /** @brief Report an error or status message inside the dialog. */
    void setStatusMessage(const QString &message, bool isError = false);

    /** @brief Set busy state while analysis or save runs asynchronously. */
    void setBusy(bool busy, const QString &message = QString());

    /** @brief Programmatically add reference video paths (for tests and drag/drop). */
    void addReferenceVideos(const QStringList &paths);

    /** @brief Current reference video paths queued for analysis. */
    QStringList referenceVideos() const;

    /** @brief Current style display name entered by the editor. */
    QString styleName() const;

    /** @brief Build the edited Show Profile JSON combining analysed profile + user tweaks. */
    QJsonObject buildEditedProfile() const;

Q_SIGNALS:
    /** @brief Emitted when the editor clicks Analyse reference videos. */
    void analyzeRequested(const QStringList &videoPaths, const QString &styleName);

    /** @brief Emitted when the editor clicks Approve & Save Style. */
    void saveProfileRequested(const QJsonObject &profileJson);

    /** @brief Emitted when the editor chooses an existing saved style to activate. */
    void selectSavedStyleRequested(const QString &profileId, const QString &displayName);

    /** @brief Emitted when the editor deletes an existing saved style. */
    void deleteSavedStyleRequested(const QString &profileId);

private Q_SLOTS:
    void handleAddVideos();
    void handleRemoveVideo();
    void handleAnalyzeClicked();
    void handleSaveClicked();
    void handleUseSavedClicked();
    void handleDeleteSavedClicked();
    void updateButtonStates();

private:
    void buildUi();

    QListWidget *m_videoList{nullptr};
    QPushButton *m_addVideosBtn{nullptr};
    QPushButton *m_removeVideoBtn{nullptr};
    QLineEdit *m_styleNameEdit{nullptr};
    QPushButton *m_analyzeBtn{nullptr};
    QLabel *m_statusLabel{nullptr};

    QPlainTextEdit *m_observationsView{nullptr};
    QDoubleSpinBox *m_minSilenceSpin{nullptr};
    QDoubleSpinBox *m_minShotSpin{nullptr};
    QDoubleSpinBox *m_maxShotSpin{nullptr};
    QDoubleSpinBox *m_targetLufsSpin{nullptr};
    QDoubleSpinBox *m_targetLumaSpin{nullptr};
    QCheckBox *m_matchColourCheck{nullptr};
    QLineEdit *m_accentHexEdit{nullptr};
    QComboBox *m_gfxPosCombo{nullptr};
    QComboBox *m_capAlignCombo{nullptr};
    QCheckBox *m_capUpperCheck{nullptr};
    QPushButton *m_saveBtn{nullptr};

    QListWidget *m_savedStylesList{nullptr};
    QPushButton *m_useSavedBtn{nullptr};
    QPushButton *m_deleteSavedBtn{nullptr};

    QJsonObject m_currentProfile;
    bool m_busy{false};
};

} // namespace BangOnPro
