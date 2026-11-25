/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "ShowProfileEditorDialog.h"

#include <QCheckBox>
#include <QComboBox>
#include <QDoubleSpinBox>
#include <QFile>
#include <QFileDialog>
#include <QFormLayout>
#include <QGridLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QLabel>
#include <QLineEdit>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QRegularExpression>
#include <QSet>
#include <QSpinBox>
#include <QTabWidget>
#include <QTableWidget>
#include <QTableWidgetItem>
#include <QVBoxLayout>

namespace BangOnPro {

namespace {

constexpr int kCamColId = 0;
constexpr int kCamColRole = 1;
constexpr int kCamColSpeaker = 2;
constexpr int kCamColPriority = 3;

const QStringList &validCameraRoles()
{
    static const QStringList kRoles = {
        QStringLiteral("wide"),     QStringLiteral("host"),     QStringLiteral("guest"), QStringLiteral("panel"),
        QStringLiteral("reaction"), QStringLiteral("overhead"), QStringLiteral("desk"),  QStringLiteral("broll"),
    };
    return kRoles;
}

} // namespace

ShowProfileEditorDialog::ShowProfileEditorDialog(QWidget *parent)
    : QDialog(parent)
{
    setObjectName(QStringLiteral("bangOnProShowProfileEditor"));
    setWindowTitle(tr("Bang On Pro — Custom Production Show Profile Editor (v1)"));
    resize(740, 580);
    buildUi();
    populateDefaultProfile();
    updateValidationState();
}

void ShowProfileEditorDialog::buildUi()
{
    auto *root = new QVBoxLayout(this);
    root->setContentsMargins(12, 12, 12, 12);
    root->setSpacing(10);

    auto *tabs = new QTabWidget(this);
    tabs->setObjectName(QStringLiteral("showProfileEditorTabs"));

    // --- Tab 1: General & Pacing ---
    auto *generalPage = new QWidget(tabs);
    auto *generalVBox = new QVBoxLayout(generalPage);
    generalVBox->setSpacing(10);

    auto *identityGroup = new QGroupBox(tr("Profile Identity && Sequence Spec"), generalPage);
    auto *idForm = new QFormLayout(identityGroup);
    idForm->setSpacing(6);

    m_profileIdEdit = new QLineEdit(identityGroup);
    m_profileIdEdit->setObjectName(QStringLiteral("profileIdEdit"));
    m_profileIdEdit->setPlaceholderText(QStringLiteral("e.g. studio_podcast_4cam"));

    m_displayNameEdit = new QLineEdit(identityGroup);
    m_displayNameEdit->setObjectName(QStringLiteral("profileDisplayNameEdit"));
    m_displayNameEdit->setPlaceholderText(tr("e.g. Studio Podcast 4-Camera"));

    m_formatTypeCombo = new QComboBox(identityGroup);
    m_formatTypeCombo->setObjectName(QStringLiteral("profileFormatTypeCombo"));
    m_formatTypeCombo->addItem(tr("Podcast"), QStringLiteral("podcast"));
    m_formatTypeCombo->addItem(tr("Gameshow"), QStringLiteral("gameshow"));
    m_formatTypeCombo->addItem(tr("Sports Discussion"), QStringLiteral("sports_discussion"));
    m_formatTypeCombo->addItem(tr("Daily News"), QStringLiteral("daily_news"));
    m_formatTypeCombo->addItem(tr("Interview"), QStringLiteral("interview"));
    m_formatTypeCombo->addItem(tr("Social"), QStringLiteral("social"));
    m_formatTypeCombo->addItem(tr("Custom"), QStringLiteral("custom"));

    m_descriptionEdit = new QPlainTextEdit(identityGroup);
    m_descriptionEdit->setObjectName(QStringLiteral("profileDescriptionEdit"));
    m_descriptionEdit->setFixedHeight(52);
    m_descriptionEdit->setPlaceholderText(tr("Editorial house-style notes for directors and AI agents…"));

    auto *videoRow = new QHBoxLayout();
    m_widthSpin = new QSpinBox(identityGroup);
    m_widthSpin->setRange(320, 7680);
    m_widthSpin->setValue(1920);
    m_heightSpin = new QSpinBox(identityGroup);
    m_heightSpin->setRange(240, 4320);
    m_heightSpin->setValue(1080);
    m_fpsNumSpin = new QSpinBox(identityGroup);
    m_fpsNumSpin->setRange(1, 240000);
    m_fpsNumSpin->setValue(25);
    m_fpsDenSpin = new QSpinBox(identityGroup);
    m_fpsDenSpin->setRange(1, 1001);
    m_fpsDenSpin->setValue(1);

    videoRow->addWidget(new QLabel(tr("W:"), identityGroup));
    videoRow->addWidget(m_widthSpin);
    videoRow->addWidget(new QLabel(tr("H:"), identityGroup));
    videoRow->addWidget(m_heightSpin);
    videoRow->addWidget(new QLabel(tr("FPS:"), identityGroup));
    videoRow->addWidget(m_fpsNumSpin);
    videoRow->addWidget(new QLabel(QStringLiteral("/"), identityGroup));
    videoRow->addWidget(m_fpsDenSpin);

    idForm->addRow(tr("Profile ID*:"), m_profileIdEdit);
    idForm->addRow(tr("Display Name*:"), m_displayNameEdit);
    idForm->addRow(tr("Format Type:"), m_formatTypeCombo);
    idForm->addRow(tr("Description:"), m_descriptionEdit);
    idForm->addRow(tr("Video Spec:"), videoRow);
    generalVBox->addWidget(identityGroup);

    auto *pacingGroup = new QGroupBox(tr("Pacing, Silence, Interruption && Review Rules"), generalPage);
    auto *pacingGrid = new QGridLayout(pacingGroup);
    pacingGrid->setSpacing(6);

    m_minShotSpin = new QDoubleSpinBox(pacingGroup);
    m_minShotSpin->setObjectName(QStringLiteral("minShotSecSpin"));
    m_minShotSpin->setRange(0.4, 60.0);
    m_minShotSpin->setSingleStep(0.2);
    m_minShotSpin->setSuffix(QStringLiteral(" s"));

    m_maxShotSpin = new QDoubleSpinBox(pacingGroup);
    m_maxShotSpin->setObjectName(QStringLiteral("maxShotSecSpin"));
    m_maxShotSpin->setRange(1.0, 120.0);
    m_maxShotSpin->setSingleStep(0.5);
    m_maxShotSpin->setSuffix(QStringLiteral(" s"));

    m_reactionIntervalSpin = new QDoubleSpinBox(pacingGroup);
    m_reactionIntervalSpin->setRange(2.0, 60.0);
    m_reactionIntervalSpin->setSingleStep(0.5);
    m_reactionIntervalSpin->setSuffix(QStringLiteral(" s"));

    m_silenceDbSpin = new QDoubleSpinBox(pacingGroup);
    m_silenceDbSpin->setObjectName(QStringLiteral("silenceThresholdDbSpin"));
    m_silenceDbSpin->setRange(-80.0, -5.0);
    m_silenceDbSpin->setSingleStep(1.0);
    m_silenceDbSpin->setSuffix(QStringLiteral(" dB"));

    m_minSilenceSpin = new QDoubleSpinBox(pacingGroup);
    m_minSilenceSpin->setObjectName(QStringLiteral("minSilenceSecSpin"));
    m_minSilenceSpin->setRange(0.1, 10.0);
    m_minSilenceSpin->setSingleStep(0.1);
    m_minSilenceSpin->setSuffix(QStringLiteral(" s"));

    m_interruptionGapSpin = new QDoubleSpinBox(pacingGroup);
    m_interruptionGapSpin->setObjectName(QStringLiteral("interruptionGapSecSpin"));
    m_interruptionGapSpin->setRange(0.05, 5.0);
    m_interruptionGapSpin->setSingleStep(0.05);
    m_interruptionGapSpin->setSuffix(QStringLiteral(" s"));

    m_keepPaddingSpin = new QDoubleSpinBox(pacingGroup);
    m_keepPaddingSpin->setRange(0.0, 2.0);
    m_keepPaddingSpin->setSingleStep(0.05);
    m_keepPaddingSpin->setSuffix(QStringLiteral(" s"));

    m_reviewThresholdSpin = new QDoubleSpinBox(pacingGroup);
    m_reviewThresholdSpin->setObjectName(QStringLiteral("reviewConfidenceThresholdSpin"));
    m_reviewThresholdSpin->setRange(0.0, 1.0);
    m_reviewThresholdSpin->setSingleStep(0.02);
    m_reviewThresholdSpin->setDecimals(2);

    m_trimSetupCheck = new QCheckBox(tr("Trim leading pre-dialogue setup delay"), pacingGroup);

    pacingGrid->addWidget(new QLabel(tr("Min Shot Duration:"), pacingGroup), 0, 0);
    pacingGrid->addWidget(m_minShotSpin, 0, 1);
    pacingGrid->addWidget(new QLabel(tr("Max Shot Duration:"), pacingGroup), 0, 2);
    pacingGrid->addWidget(m_maxShotSpin, 0, 3);

    pacingGrid->addWidget(new QLabel(tr("Silence Threshold:"), pacingGroup), 1, 0);
    pacingGrid->addWidget(m_silenceDbSpin, 1, 1);
    pacingGrid->addWidget(new QLabel(tr("Min Silence Duration:"), pacingGroup), 1, 2);
    pacingGrid->addWidget(m_minSilenceSpin, 1, 3);

    pacingGrid->addWidget(new QLabel(tr("Interruption Gap:"), pacingGroup), 2, 0);
    pacingGrid->addWidget(m_interruptionGapSpin, 2, 1);
    pacingGrid->addWidget(new QLabel(tr("Reaction Cut Interval:"), pacingGroup), 2, 2);
    pacingGrid->addWidget(m_reactionIntervalSpin, 2, 3);

    pacingGrid->addWidget(new QLabel(tr("Keep Padding:"), pacingGroup), 3, 0);
    pacingGrid->addWidget(m_keepPaddingSpin, 3, 1);
    pacingGrid->addWidget(new QLabel(tr("Review Conf Threshold:"), pacingGroup), 3, 2);
    pacingGrid->addWidget(m_reviewThresholdSpin, 3, 3);

    pacingGrid->addWidget(m_trimSetupCheck, 4, 0, 1, 4);
    generalVBox->addWidget(pacingGroup);

    tabs->addTab(generalPage, tr("General && Pacing"));

    // --- Tab 2: Cameras & Speaker Mapping ---
    auto *camerasPage = new QWidget(tabs);
    auto *camVBox = new QVBoxLayout(camerasPage);
    camVBox->setSpacing(8);

    auto *camNote = new QLabel(tr("Map each camera angle to its editorial role and speaker label (leave speaker blank for wide/b-roll angles)."), camerasPage);
    camNote->setWordWrap(true);
    camNote->setObjectName(QStringLiteral("bangOnProMutedLabel"));
    camVBox->addWidget(camNote);

    m_cameraTable = new QTableWidget(camerasPage);
    m_cameraTable->setObjectName(QStringLiteral("cameraMappingTable"));
    m_cameraTable->setColumnCount(4);
    m_cameraTable->setHorizontalHeaderLabels({tr("Camera ID"), tr("Role"), tr("Speaker Label"), tr("Priority")});
    m_cameraTable->horizontalHeader()->setSectionResizeMode(kCamColId, QHeaderView::Stretch);
    m_cameraTable->horizontalHeader()->setSectionResizeMode(kCamColRole, QHeaderView::ResizeToContents);
    m_cameraTable->horizontalHeader()->setSectionResizeMode(kCamColSpeaker, QHeaderView::Stretch);
    m_cameraTable->horizontalHeader()->setSectionResizeMode(kCamColPriority, QHeaderView::ResizeToContents);
    m_cameraTable->setSelectionBehavior(QAbstractItemView::SelectRows);
    camVBox->addWidget(m_cameraTable, 1);

    auto *camBtnRow = new QHBoxLayout();
    m_addCameraButton = new QPushButton(tr("+ Add Camera"), camerasPage);
    m_addCameraButton->setObjectName(QStringLiteral("addCameraRowButton"));
    m_removeCameraButton = new QPushButton(tr("Remove Selected"), camerasPage);
    m_removeCameraButton->setObjectName(QStringLiteral("removeCameraRowButton"));
    camBtnRow->addWidget(m_addCameraButton);
    camBtnRow->addWidget(m_removeCameraButton);
    camBtnRow->addStretch(1);
    camVBox->addLayout(camBtnRow);

    tabs->addTab(camerasPage, tr("Cameras && Speakers"));

    // --- Tab 3: Audio & Graphics ---
    auto *agPage = new QWidget(tabs);
    auto *agVBox = new QVBoxLayout(agPage);
    agVBox->setSpacing(10);

    auto *audioGroup = new QGroupBox(tr("Audio Loudness && Restoration Rules"), agPage);
    auto *audioForm = new QFormLayout(audioGroup);

    m_targetLufsSpin = new QDoubleSpinBox(audioGroup);
    m_targetLufsSpin->setObjectName(QStringLiteral("targetLufsSpin"));
    m_targetLufsSpin->setRange(-36.0, -6.0);
    m_targetLufsSpin->setSingleStep(0.5);
    m_targetLufsSpin->setSuffix(QStringLiteral(" LUFS"));

    m_truePeakSpin = new QDoubleSpinBox(audioGroup);
    m_truePeakSpin->setRange(-6.0, 0.0);
    m_truePeakSpin->setSingleStep(0.1);
    m_truePeakSpin->setSuffix(QStringLiteral(" dBTP"));

    m_musicDuckSpin = new QDoubleSpinBox(audioGroup);
    m_musicDuckSpin->setRange(-40.0, 0.0);
    m_musicDuckSpin->setSingleStep(1.0);
    m_musicDuckSpin->setSuffix(QStringLiteral(" dB"));

    m_denoiseCheck = new QCheckBox(tr("Enable dialogue noise reduction"), audioGroup);
    m_compressorCheck = new QCheckBox(tr("Enable broadcast dialogue dynamics compressor"), audioGroup);

    audioForm->addRow(tr("Target Loudness:"), m_targetLufsSpin);
    audioForm->addRow(tr("True Peak Limit:"), m_truePeakSpin);
    audioForm->addRow(tr("Music Ducking:"), m_musicDuckSpin);
    audioForm->addRow(QString(), m_denoiseCheck);
    audioForm->addRow(QString(), m_compressorCheck);
    agVBox->addWidget(audioGroup);

    auto *gfxGroup = new QGroupBox(tr("Captions && Lower-Third Graphics Rules"), agPage);
    auto *gfxForm = new QFormLayout(gfxGroup);

    m_captionsCheck = new QCheckBox(tr("Generate synchronized captions (.srt/.ass)"), gfxGroup);
    m_lowerThirdsCheck = new QCheckBox(tr("Insert speaker lower-third graphics on first appearance"), gfxGroup);

    m_lowerThirdDurationSpin = new QDoubleSpinBox(gfxGroup);
    m_lowerThirdDurationSpin->setRange(1.0, 15.0);
    m_lowerThirdDurationSpin->setSingleStep(0.5);
    m_lowerThirdDurationSpin->setSuffix(QStringLiteral(" s"));

    m_accentHexEdit = new QLineEdit(gfxGroup);
    m_accentHexEdit->setObjectName(QStringLiteral("profileAccentHexEdit"));
    m_accentHexEdit->setPlaceholderText(QStringLiteral("#FF5A1F"));

    gfxForm->addRow(QString(), m_captionsCheck);
    gfxForm->addRow(QString(), m_lowerThirdsCheck);
    gfxForm->addRow(tr("Lower-Third Duration:"), m_lowerThirdDurationSpin);
    gfxForm->addRow(tr("Brand Accent Hex:"), m_accentHexEdit);
    agVBox->addWidget(gfxGroup);
    agVBox->addStretch(1);

    tabs->addTab(agPage, tr("Audio && Graphics"));

    root->addWidget(tabs, 1);

    // Live validation banner
    m_validationLabel = new QLabel(this);
    m_validationLabel->setObjectName(QStringLiteral("profileValidationLabel"));
    m_validationLabel->setWordWrap(true);
    root->addWidget(m_validationLabel);

    // Footer buttons
    auto *footer = new QHBoxLayout();
    m_loadButton = new QPushButton(tr("Load JSON…"), this);
    m_saveJsonButton = new QPushButton(tr("Save JSON…"), this);
    m_saveJsonButton->setObjectName(QStringLiteral("saveProfileJsonButton"));

    m_cancelButton = new QPushButton(tr("Cancel"), this);
    m_useProfileButton = new QPushButton(tr("Apply Profile"), this);
    m_useProfileButton->setObjectName(QStringLiteral("applyProfileDialogButton"));
    m_useProfileButton->setProperty("bangOnProPrimary", true);
    m_useProfileButton->setDefault(true);

    footer->addWidget(m_loadButton);
    footer->addWidget(m_saveJsonButton);
    footer->addStretch(1);
    footer->addWidget(m_cancelButton);
    footer->addWidget(m_useProfileButton);
    root->addLayout(footer);

    // Wire validation triggers
    connect(m_profileIdEdit, &QLineEdit::textChanged, this, &ShowProfileEditorDialog::updateValidationState);
    connect(m_displayNameEdit, &QLineEdit::textChanged, this, &ShowProfileEditorDialog::updateValidationState);
    connect(m_accentHexEdit, &QLineEdit::textChanged, this, &ShowProfileEditorDialog::updateValidationState);
    connect(m_minShotSpin, QOverload<double>::of(&QDoubleSpinBox::valueChanged), this, &ShowProfileEditorDialog::updateValidationState);
    connect(m_maxShotSpin, QOverload<double>::of(&QDoubleSpinBox::valueChanged), this, &ShowProfileEditorDialog::updateValidationState);
    connect(m_cameraTable, &QTableWidget::cellChanged, this, &ShowProfileEditorDialog::updateValidationState);

    connect(m_addCameraButton, &QPushButton::clicked, this, &ShowProfileEditorDialog::handleAddCameraClicked);
    connect(m_removeCameraButton, &QPushButton::clicked, this, &ShowProfileEditorDialog::handleRemoveCameraClicked);
    connect(m_loadButton, &QPushButton::clicked, this, &ShowProfileEditorDialog::handleLoadJsonClicked);
    connect(m_saveJsonButton, &QPushButton::clicked, this, &ShowProfileEditorDialog::handleSaveJsonClicked);
    connect(m_useProfileButton, &QPushButton::clicked, this, &ShowProfileEditorDialog::handleUseProfileClicked);
    connect(m_cancelButton, &QPushButton::clicked, this, &QDialog::reject);
}

void ShowProfileEditorDialog::populateDefaultProfile()
{
    m_profileIdEdit->setText(QStringLiteral("custom_studio_show"));
    m_displayNameEdit->setText(tr("Custom Studio Multicam"));
    m_formatTypeCombo->setCurrentIndex(m_formatTypeCombo->findData(QStringLiteral("custom")));
    m_descriptionEdit->setPlainText(tr("Custom multi-camera editorial ruleset for Bang On Pro."));

    m_widthSpin->setValue(1920);
    m_heightSpin->setValue(1080);
    m_fpsNumSpin->setValue(25);
    m_fpsDenSpin->setValue(1);

    m_minShotSpin->setValue(2.0);
    m_maxShotSpin->setValue(12.0);
    m_reactionIntervalSpin->setValue(6.0);
    m_silenceDbSpin->setValue(-36.0);
    m_minSilenceSpin->setValue(0.8);
    m_interruptionGapSpin->setValue(0.35);
    m_keepPaddingSpin->setValue(0.2);
    m_trimSetupCheck->setChecked(true);
    m_reviewThresholdSpin->setValue(0.72);

    clearCameraRows();
    addCameraRow(QStringLiteral("cam_wide"), QStringLiteral("wide"), QString(), 1);
    addCameraRow(QStringLiteral("cam_host"), QStringLiteral("host"), QStringLiteral("Host"), 2);
    addCameraRow(QStringLiteral("cam_guest"), QStringLiteral("guest"), QStringLiteral("Guest"), 2);

    m_targetLufsSpin->setValue(-16.0);
    m_truePeakSpin->setValue(-1.5);
    m_musicDuckSpin->setValue(-18.0);
    m_denoiseCheck->setChecked(true);
    m_compressorCheck->setChecked(true);

    m_captionsCheck->setChecked(true);
    m_lowerThirdsCheck->setChecked(true);
    m_lowerThirdDurationSpin->setValue(4.5);
    m_accentHexEdit->setText(QStringLiteral("#FF5A1F"));

    m_exportPresets = defaultExportPresets();
}

QJsonArray ShowProfileEditorDialog::defaultExportPresets() const
{
    QJsonObject master;
    master.insert(QStringLiteral("name"), QStringLiteral("master_1080p_h264"));
    master.insert(QStringLiteral("container"), QStringLiteral("mp4"));
    master.insert(QStringLiteral("video_codec"), QStringLiteral("libx264"));
    master.insert(QStringLiteral("audio_codec"), QStringLiteral("aac"));
    master.insert(QStringLiteral("video_bitrate"), QStringLiteral("12M"));
    master.insert(QStringLiteral("audio_bitrate"), QStringLiteral("192k"));

    QJsonArray arr;
    arr.append(master);
    return arr;
}

bool ShowProfileEditorDialog::loadProfileFile(const QString &filePath, QString *errorMessage)
{
    QFile file(filePath);
    if (!file.open(QIODevice::ReadOnly)) {
        if (errorMessage != nullptr) {
            *errorMessage = tr("Unable to open profile file: %1").arg(filePath);
        }
        return false;
    }

    QJsonParseError pe{};
    const QJsonDocument doc = QJsonDocument::fromJson(file.readAll(), &pe);
    if (pe.error != QJsonParseError::NoError || !doc.isObject()) {
        if (errorMessage != nullptr) {
            *errorMessage = tr("Invalid Show Profile JSON: %1").arg(pe.errorString());
        }
        return false;
    }

    const bool ok = loadProfileJson(doc.object(), errorMessage);
    if (ok) {
        m_savedFilePath = filePath;
    }
    return ok;
}

bool ShowProfileEditorDialog::loadProfileJson(const QJsonObject &profileObj, QString *errorMessage)
{
    const QString schemaVer = profileObj.value(QStringLiteral("schema_version")).toString();
    if (!schemaVer.isEmpty() && schemaVer != QStringLiteral("1.0")) {
        if (errorMessage != nullptr) {
            *errorMessage = tr("Unsupported schema_version '%1' (expected '1.0').").arg(schemaVer);
        }
        return false;
    }

    m_profileIdEdit->setText(profileObj.value(QStringLiteral("profile_id")).toString());
    m_displayNameEdit->setText(profileObj.value(QStringLiteral("display_name")).toString());
    m_descriptionEdit->setPlainText(profileObj.value(QStringLiteral("description")).toString());

    const QString fmt = profileObj.value(QStringLiteral("format_type")).toString(QStringLiteral("custom"));
    const int fmtIdx = m_formatTypeCombo->findData(fmt);
    m_formatTypeCombo->setCurrentIndex(fmtIdx >= 0 ? fmtIdx : m_formatTypeCombo->findData(QStringLiteral("custom")));

    const QJsonObject vs = profileObj.value(QStringLiteral("video_spec")).toObject();
    if (!vs.isEmpty()) {
        m_widthSpin->setValue(vs.value(QStringLiteral("width")).toInt(1920));
        m_heightSpin->setValue(vs.value(QStringLiteral("height")).toInt(1080));
        m_fpsNumSpin->setValue(vs.value(QStringLiteral("fps_num")).toInt(25));
        m_fpsDenSpin->setValue(vs.value(QStringLiteral("fps_den")).toInt(1));
        m_colorSpace = vs.value(QStringLiteral("color_space")).toString(QStringLiteral("bt709"));
    }

    const QJsonArray cams = profileObj.value(QStringLiteral("cameras")).toArray();
    if (!cams.isEmpty()) {
        clearCameraRows();
        for (const QJsonValue &cv : cams) {
            const QJsonObject co = cv.toObject();
            addCameraRow(co.value(QStringLiteral("camera_id")).toString(), co.value(QStringLiteral("role")).toString(QStringLiteral("wide")),
                         co.value(QStringLiteral("speaker_label")).toString(), co.value(QStringLiteral("priority")).toInt(1));
        }
    }

    const QJsonObject er = profileObj.value(QStringLiteral("editing_rules")).toObject();
    if (!er.isEmpty()) {
        m_minShotSpin->setValue(er.value(QStringLiteral("min_shot_sec")).toDouble(2.0));
        m_maxShotSpin->setValue(er.value(QStringLiteral("max_shot_sec")).toDouble(12.0));
        m_reactionIntervalSpin->setValue(er.value(QStringLiteral("reaction_cut_interval_sec")).toDouble(6.0));
        m_silenceDbSpin->setValue(er.value(QStringLiteral("silence_threshold_db")).toDouble(-36.0));
        m_minSilenceSpin->setValue(er.value(QStringLiteral("min_silence_sec")).toDouble(0.8));
        m_interruptionGapSpin->setValue(er.value(QStringLiteral("interruption_gap_sec")).toDouble(0.35));
        m_keepPaddingSpin->setValue(er.value(QStringLiteral("keep_padding_sec")).toDouble(0.2));
        m_trimSetupCheck->setChecked(er.value(QStringLiteral("trim_setup_delay")).toBool(true));
        m_reviewThresholdSpin->setValue(er.value(QStringLiteral("review_confidence_threshold")).toDouble(0.72));
    }

    const QJsonObject ar = profileObj.value(QStringLiteral("audio_rules")).toObject();
    if (!ar.isEmpty()) {
        m_targetLufsSpin->setValue(ar.value(QStringLiteral("target_lufs")).toDouble(-16.0));
        m_truePeakSpin->setValue(ar.value(QStringLiteral("true_peak_dbtp")).toDouble(-1.5));
        m_denoiseCheck->setChecked(ar.value(QStringLiteral("enable_denoise")).toBool(true));
        m_compressorCheck->setChecked(ar.value(QStringLiteral("enable_compressor")).toBool(true));
        m_musicDuckSpin->setValue(ar.value(QStringLiteral("music_duck_db")).toDouble(-18.0));
    }

    const QJsonObject gr = profileObj.value(QStringLiteral("graphics_rules")).toObject();
    if (!gr.isEmpty()) {
        m_captionsCheck->setChecked(gr.value(QStringLiteral("enable_captions")).toBool(true));
        m_lowerThirdsCheck->setChecked(gr.value(QStringLiteral("enable_lower_thirds")).toBool(true));
        m_lowerThirdDurationSpin->setValue(gr.value(QStringLiteral("lower_third_duration_sec")).toDouble(4.5));
        m_accentHexEdit->setText(gr.value(QStringLiteral("brand_accent_hex")).toString(QStringLiteral("#FF5A1F")));
    }

    const QJsonArray ep = profileObj.value(QStringLiteral("export_presets")).toArray();
    m_exportPresets = ep.isEmpty() ? defaultExportPresets() : ep;

    updateValidationState();
    return true;
}

QJsonArray ShowProfileEditorDialog::buildCamerasArray() const
{
    QJsonArray cameras;
    for (int row = 0; row < m_cameraTable->rowCount(); ++row) {
        const QTableWidgetItem *idItem = m_cameraTable->item(row, kCamColId);
        const QString camId = idItem ? idItem->text().trimmed() : QString();
        if (camId.isEmpty()) {
            continue;
        }
        auto *roleCombo = qobject_cast<QComboBox *>(m_cameraTable->cellWidget(row, kCamColRole));
        const QString role = roleCombo ? roleCombo->currentText() : QStringLiteral("wide");

        const QTableWidgetItem *spkItem = m_cameraTable->item(row, kCamColSpeaker);
        const QString speaker = spkItem ? spkItem->text().trimmed() : QString();

        auto *prioSpin = qobject_cast<QSpinBox *>(m_cameraTable->cellWidget(row, kCamColPriority));
        const int priority = prioSpin ? prioSpin->value() : 1;

        QJsonObject camObj;
        camObj.insert(QStringLiteral("camera_id"), camId);
        camObj.insert(QStringLiteral("role"), role);
        if (speaker.isEmpty()) {
            camObj.insert(QStringLiteral("speaker_label"), QJsonValue::Null);
        } else {
            camObj.insert(QStringLiteral("speaker_label"), speaker);
        }
        camObj.insert(QStringLiteral("priority"), priority);
        cameras.append(camObj);
    }
    return cameras;
}

QJsonObject ShowProfileEditorDialog::buildProfileJson() const
{
    QJsonObject root;
    root.insert(QStringLiteral("schema_version"), QStringLiteral("1.0"));
    root.insert(QStringLiteral("profile_id"), m_profileIdEdit->text().trimmed());
    root.insert(QStringLiteral("display_name"), m_displayNameEdit->text().trimmed());
    const QString desc = m_descriptionEdit->toPlainText().trimmed();
    if (!desc.isEmpty()) {
        root.insert(QStringLiteral("description"), desc);
    }
    root.insert(QStringLiteral("format_type"), m_formatTypeCombo->currentData().toString());

    QJsonObject videoSpec;
    videoSpec.insert(QStringLiteral("width"), m_widthSpin->value());
    videoSpec.insert(QStringLiteral("height"), m_heightSpin->value());
    videoSpec.insert(QStringLiteral("fps_num"), m_fpsNumSpin->value());
    videoSpec.insert(QStringLiteral("fps_den"), m_fpsDenSpin->value());
    videoSpec.insert(QStringLiteral("color_space"), m_colorSpace);
    root.insert(QStringLiteral("video_spec"), videoSpec);

    root.insert(QStringLiteral("cameras"), buildCamerasArray());

    QJsonObject editingRules;
    editingRules.insert(QStringLiteral("min_shot_sec"), m_minShotSpin->value());
    editingRules.insert(QStringLiteral("max_shot_sec"), m_maxShotSpin->value());
    editingRules.insert(QStringLiteral("reaction_cut_interval_sec"), m_reactionIntervalSpin->value());
    editingRules.insert(QStringLiteral("silence_threshold_db"), m_silenceDbSpin->value());
    editingRules.insert(QStringLiteral("min_silence_sec"), m_minSilenceSpin->value());
    editingRules.insert(QStringLiteral("interruption_gap_sec"), m_interruptionGapSpin->value());
    editingRules.insert(QStringLiteral("keep_padding_sec"), m_keepPaddingSpin->value());
    editingRules.insert(QStringLiteral("trim_setup_delay"), m_trimSetupCheck->isChecked());
    editingRules.insert(QStringLiteral("review_confidence_threshold"), m_reviewThresholdSpin->value());
    root.insert(QStringLiteral("editing_rules"), editingRules);

    QJsonObject audioRules;
    audioRules.insert(QStringLiteral("target_lufs"), m_targetLufsSpin->value());
    audioRules.insert(QStringLiteral("true_peak_dbtp"), m_truePeakSpin->value());
    audioRules.insert(QStringLiteral("enable_denoise"), m_denoiseCheck->isChecked());
    audioRules.insert(QStringLiteral("enable_compressor"), m_compressorCheck->isChecked());
    audioRules.insert(QStringLiteral("music_duck_db"), m_musicDuckSpin->value());
    root.insert(QStringLiteral("audio_rules"), audioRules);

    QJsonObject graphicsRules;
    graphicsRules.insert(QStringLiteral("enable_captions"), m_captionsCheck->isChecked());
    graphicsRules.insert(QStringLiteral("enable_lower_thirds"), m_lowerThirdsCheck->isChecked());
    graphicsRules.insert(QStringLiteral("lower_third_duration_sec"), m_lowerThirdDurationSpin->value());
    graphicsRules.insert(QStringLiteral("brand_accent_hex"), m_accentHexEdit->text().trimmed());
    root.insert(QStringLiteral("graphics_rules"), graphicsRules);

    root.insert(QStringLiteral("export_presets"), m_exportPresets.isEmpty() ? defaultExportPresets() : m_exportPresets);
    return root;
}

QStringList ShowProfileEditorDialog::validateProfile() const
{
    QStringList errors;
    const QString profileId = m_profileIdEdit->text().trimmed();
    if (profileId.isEmpty()) {
        errors.append(tr("Profile ID is required."));
    } else {
        static const QRegularExpression kIdRx(QStringLiteral("^[A-Za-z0-9_-]+$"));
        if (!kIdRx.match(profileId).hasMatch()) {
            errors.append(tr("Profile ID may only contain letters, digits, '_' and '-'."));
        }
    }

    if (m_displayNameEdit->text().trimmed().isEmpty()) {
        errors.append(tr("Display Name is required."));
    }

    if (m_maxShotSpin->value() < m_minShotSpin->value()) {
        errors.append(tr("Max Shot Duration must be greater than or equal to Min Shot Duration."));
    }

    QSet<QString> seenCamIds;
    int validCamCount = 0;
    for (int row = 0; row < m_cameraTable->rowCount(); ++row) {
        const QTableWidgetItem *idItem = m_cameraTable->item(row, kCamColId);
        const QString cid = idItem ? idItem->text().trimmed() : QString();
        if (cid.isEmpty()) {
            errors.append(tr("Camera row #%1 has an empty Camera ID.").arg(row + 1));
            continue;
        }
        if (seenCamIds.contains(cid)) {
            errors.append(tr("Duplicate Camera ID '%1'.").arg(cid));
        } else {
            seenCamIds.insert(cid);
            ++validCamCount;
        }
    }
    if (validCamCount == 0) {
        errors.append(tr("At least one camera definition is required."));
    }

    static const QRegularExpression kHexRx(QStringLiteral("^#[0-9A-Fa-f]{6}$"));
    if (!kHexRx.match(m_accentHexEdit->text().trimmed()).hasMatch()) {
        errors.append(tr("Brand Accent Hex must use #RRGGBB format (e.g. #FF5A1F)."));
    }

    return errors;
}

bool ShowProfileEditorDialog::isValid() const
{
    return validateProfile().isEmpty();
}

bool ShowProfileEditorDialog::saveProfileToFile(const QString &filePath, QString *errorMessage) const
{
    const QStringList errs = validateProfile();
    if (!errs.isEmpty()) {
        if (errorMessage != nullptr) {
            *errorMessage = errs.join(QStringLiteral(" "));
        }
        return false;
    }

    QFile file(filePath);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        if (errorMessage != nullptr) {
            *errorMessage = tr("Cannot write Show Profile JSON to %1").arg(filePath);
        }
        return false;
    }

    const QJsonDocument doc(buildProfileJson());
    const QByteArray bytes = doc.toJson(QJsonDocument::Indented);
    if (file.write(bytes) != bytes.size()) {
        if (errorMessage != nullptr) {
            *errorMessage = tr("Failed writing complete JSON payload to %1").arg(filePath);
        }
        return false;
    }
    return true;
}

QString ShowProfileEditorDialog::savedFilePath() const
{
    return m_savedFilePath;
}

void ShowProfileEditorDialog::setProfileId(const QString &profileId)
{
    m_profileIdEdit->setText(profileId);
}

void ShowProfileEditorDialog::setDisplayName(const QString &displayName)
{
    m_displayNameEdit->setText(displayName);
}

void ShowProfileEditorDialog::setDescription(const QString &description)
{
    m_descriptionEdit->setPlainText(description);
}

void ShowProfileEditorDialog::setFormatType(const QString &formatType)
{
    const int idx = m_formatTypeCombo->findData(formatType);
    if (idx >= 0) {
        m_formatTypeCombo->setCurrentIndex(idx);
    }
}

void ShowProfileEditorDialog::setPacingRules(double minShotSec, double maxShotSec, double reactionIntervalSec)
{
    m_minShotSpin->setValue(minShotSec);
    m_maxShotSpin->setValue(maxShotSec);
    m_reactionIntervalSpin->setValue(reactionIntervalSec);
}

void ShowProfileEditorDialog::setSilenceAndInterruptionRules(double silenceThresholdDb, double minSilenceSec, double interruptionGapSec)
{
    m_silenceDbSpin->setValue(silenceThresholdDb);
    m_minSilenceSpin->setValue(minSilenceSec);
    m_interruptionGapSpin->setValue(interruptionGapSec);
}

void ShowProfileEditorDialog::setReviewConfidenceThreshold(double threshold)
{
    m_reviewThresholdSpin->setValue(threshold);
}

void ShowProfileEditorDialog::setAudioRules(double targetLufs, bool enableDenoise, bool enableCompressor, double musicDuckDb)
{
    m_targetLufsSpin->setValue(targetLufs);
    m_denoiseCheck->setChecked(enableDenoise);
    m_compressorCheck->setChecked(enableCompressor);
    m_musicDuckSpin->setValue(musicDuckDb);
}

void ShowProfileEditorDialog::setGraphicsRules(bool enableCaptions, bool enableLowerThirds, double lowerThirdDurationSec, const QString &brandAccentHex)
{
    m_captionsCheck->setChecked(enableCaptions);
    m_lowerThirdsCheck->setChecked(enableLowerThirds);
    m_lowerThirdDurationSpin->setValue(lowerThirdDurationSec);
    m_accentHexEdit->setText(brandAccentHex);
}

void ShowProfileEditorDialog::clearCameraRows()
{
    m_cameraTable->setRowCount(0);
    updateValidationState();
}

void ShowProfileEditorDialog::addCameraRow(const QString &cameraId, const QString &role, const QString &speakerLabel, int priority)
{
    const int row = m_cameraTable->rowCount();
    m_cameraTable->insertRow(row);

    m_cameraTable->setItem(row, kCamColId, new QTableWidgetItem(cameraId));

    auto *roleCombo = new QComboBox(m_cameraTable);
    roleCombo->addItems(validCameraRoles());
    const int rIdx = roleCombo->findText(role);
    roleCombo->setCurrentIndex(rIdx >= 0 ? rIdx : 0);
    m_cameraTable->setCellWidget(row, kCamColRole, roleCombo);

    m_cameraTable->setItem(row, kCamColSpeaker, new QTableWidgetItem(speakerLabel));

    auto *prioSpin = new QSpinBox(m_cameraTable);
    prioSpin->setRange(1, 9);
    prioSpin->setValue(qBound(1, priority, 9));
    m_cameraTable->setCellWidget(row, kCamColPriority, prioSpin);

    updateValidationState();
}

void ShowProfileEditorDialog::handleAddCameraClicked()
{
    const int nextNum = m_cameraTable->rowCount() + 1;
    addCameraRow(QStringLiteral("cam_%1").arg(nextNum), QStringLiteral("guest"), QStringLiteral("Speaker %1").arg(nextNum), 2);
}

void ShowProfileEditorDialog::handleRemoveCameraClicked()
{
    const int row = m_cameraTable->currentRow();
    if (row >= 0) {
        m_cameraTable->removeRow(row);
        updateValidationState();
    }
}

void ShowProfileEditorDialog::handleLoadJsonClicked()
{
    const QString path = QFileDialog::getOpenFileName(this, tr("Load Show Profile JSON"), QString(), tr("Show Profile JSON (*.json);;All Files (*)"));
    if (!path.isEmpty()) {
        loadProfileFile(path);
    }
}

void ShowProfileEditorDialog::handleSaveJsonClicked()
{
    if (!isValid()) {
        return;
    }
    const QString suggested = m_savedFilePath.isEmpty() ? (m_profileIdEdit->text().trimmed() + QStringLiteral(".json")) : m_savedFilePath;
    const QString path = QFileDialog::getSaveFileName(this, tr("Save Show Profile JSON"), suggested, tr("Show Profile JSON (*.json);;All Files (*)"));
    if (path.isEmpty()) {
        return;
    }
    QString err;
    if (saveProfileToFile(path, &err)) {
        m_savedFilePath = path;
        Q_EMIT profileSaved(path, buildProfileJson());
    }
}

void ShowProfileEditorDialog::handleUseProfileClicked()
{
    if (!isValid()) {
        return;
    }
    Q_EMIT profileSaved(m_savedFilePath, buildProfileJson());
    accept();
}

void ShowProfileEditorDialog::updateValidationState()
{
    const QStringList errs = validateProfile();
    const bool valid = errs.isEmpty();
    if (valid) {
        m_validationLabel->setText(tr("✓ Show Profile v1 schema valid — ready to save or apply."));
        m_validationLabel->setStyleSheet(QStringLiteral("color: #22A06B; font-size: 11px; font-weight: 600;"));
    } else {
        m_validationLabel->setText(tr("⚠ Validation: %1").arg(errs.join(QStringLiteral(" • "))));
        m_validationLabel->setStyleSheet(QStringLiteral("color: #E04F5F; font-size: 11px; font-weight: 600;"));
    }
    m_saveJsonButton->setEnabled(valid);
    m_useProfileButton->setEnabled(valid);
}

} // namespace BangOnPro
