/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "ReferenceStyleDialog.h"
#include "BangOnProTheme.h"

#include <QCheckBox>
#include <QComboBox>
#include <QDoubleSpinBox>
#include <QFileDialog>
#include <QFileInfo>
#include <QFormLayout>
#include <QGridLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QRegularExpression>
#include <QVBoxLayout>

namespace BangOnPro {

namespace {
constexpr int kProfileIdRole = Qt::UserRole + 1;
constexpr int kDisplayNameRole = Qt::UserRole + 2;

QString slugifyId(const QString &name)
{
    QString s = name.trimmed().toLower();
    s.replace(QRegularExpression(QStringLiteral("[^a-z0-9]+")), QStringLiteral("_"));
    while (s.startsWith(QLatin1Char('_'))) {
        s.remove(0, 1);
    }
    while (s.endsWith(QLatin1Char('_'))) {
        s.chop(1);
    }
    if (s.isEmpty()) {
        return QStringLiteral("reference_style");
    }
    return s.left(48);
}
} // namespace

ReferenceStyleDialog::ReferenceStyleDialog(QWidget *parent)
    : QDialog(parent)
{
    setObjectName(QStringLiteral("bangonpro_reference_style_dialog"));
    setWindowTitle(tr("Reference Video Editing Styles"));
    resize(780, 620);
    setMinimumSize(640, 500);
    buildUi();
    updateButtonStates();
}

void ReferenceStyleDialog::buildUi()
{
    using M = StudioMetrics;
    auto *root = new QVBoxLayout(this);
    root->setContentsMargins(M::kSpace4, M::kSpace4, M::kSpace4, M::kSpace4);
    root->setSpacing(M::kSpace3);

    auto *intro = new QLabel(tr("Import one or more reference videos to learn pacing, cuts, colour, audio loudness, captions and graphics placement. "
                                "Everything is analysed locally and saved as a reusable style you can edit or remove."),
                             this);
    intro->setWordWrap(true);
    setTextRole(intro, "secondary");
    root->addWidget(intro);

    // 1. Reference video picker + analyse trigger
    auto *srcGroup = new QGroupBox(tr("1. Reference Videos"), this);
    auto *srcLay = new QVBoxLayout(srcGroup);
    srcLay->setSpacing(M::kSpace2);

    auto *srcRow = new QHBoxLayout;
    m_videoList = new QListWidget(srcGroup);
    m_videoList->setObjectName(QStringLiteral("ref_video_list"));
    m_videoList->setMaximumHeight(86);
    m_videoList->setSelectionMode(QAbstractItemView::ExtendedSelection);

    auto *srcBtns = new QVBoxLayout;
    m_addVideosBtn = new QPushButton(tr("Add videos…"), srcGroup);
    m_addVideosBtn->setObjectName(QStringLiteral("ref_add_videos"));
    m_removeVideoBtn = new QPushButton(tr("Remove"), srcGroup);
    m_removeVideoBtn->setObjectName(QStringLiteral("ref_remove_video"));
    srcBtns->addWidget(m_addVideosBtn);
    srcBtns->addWidget(m_removeVideoBtn);
    srcBtns->addStretch(1);
    srcRow->addWidget(m_videoList, 1);
    srcRow->addLayout(srcBtns);
    srcLay->addLayout(srcRow);

    auto *actionRow = new QHBoxLayout;
    actionRow->setSpacing(M::kSpace2);
    auto *nameLbl = new QLabel(tr("Style name:"), srcGroup);
    m_styleNameEdit = new QLineEdit(srcGroup);
    m_styleNameEdit->setObjectName(QStringLiteral("ref_style_name"));
    m_styleNameEdit->setPlaceholderText(tr("e.g. Fast Tech Review, Calm Documentary"));
    m_analyzeBtn = new QPushButton(tr("Analyse reference videos"), srcGroup);
    m_analyzeBtn->setObjectName(QStringLiteral("ref_analyse_btn"));
    setRole(m_analyzeBtn, "primary");
    actionRow->addWidget(nameLbl);
    actionRow->addWidget(m_styleNameEdit, 1);
    actionRow->addWidget(m_analyzeBtn);
    srcLay->addLayout(actionRow);

    m_statusLabel = new QLabel(tr("Add one or more reference videos, then click Analyse."), srcGroup);
    m_statusLabel->setObjectName(QStringLiteral("ref_status_label"));
    m_statusLabel->setWordWrap(true);
    setTextRole(m_statusLabel, "muted");
    srcLay->addWidget(m_statusLabel);
    root->addWidget(srcGroup);

    // 2. Preview & editable profile parameters
    auto *midRow = new QHBoxLayout;
    midRow->setSpacing(M::kSpace3);

    auto *previewGroup = new QGroupBox(tr("2. Observed Style && Adjustments"), this);
    auto *previewLay = new QVBoxLayout(previewGroup);
    previewLay->setSpacing(M::kSpace2);

    m_observationsView = new QPlainTextEdit(previewGroup);
    m_observationsView->setObjectName(QStringLiteral("ref_observations_view"));
    m_observationsView->setReadOnly(true);
    m_observationsView->setMaximumHeight(120);
    m_observationsView->setPlaceholderText(tr("Measured pacing, cut frequency, colour look, loudness, captions and graphics will appear here."));
    previewLay->addWidget(m_observationsView);

    auto *grid = new QGridLayout;
    grid->setHorizontalSpacing(M::kSpace3);
    grid->setVerticalSpacing(M::kSpace2);

    m_minSilenceSpin = new QDoubleSpinBox(previewGroup);
    m_minSilenceSpin->setObjectName(QStringLiteral("ref_min_silence"));
    m_minSilenceSpin->setRange(0.20, 3.00);
    m_minSilenceSpin->setSingleStep(0.05);
    m_minSilenceSpin->setSuffix(QStringLiteral(" s"));
    m_minSilenceSpin->setValue(0.45);

    m_minShotSpin = new QDoubleSpinBox(previewGroup);
    m_minShotSpin->setObjectName(QStringLiteral("ref_min_shot"));
    m_minShotSpin->setRange(0.50, 10.0);
    m_minShotSpin->setSingleStep(0.25);
    m_minShotSpin->setSuffix(QStringLiteral(" s"));
    m_minShotSpin->setValue(1.5);

    m_maxShotSpin = new QDoubleSpinBox(previewGroup);
    m_maxShotSpin->setObjectName(QStringLiteral("ref_max_shot"));
    m_maxShotSpin->setRange(2.0, 60.0);
    m_maxShotSpin->setSingleStep(1.0);
    m_maxShotSpin->setSuffix(QStringLiteral(" s"));
    m_maxShotSpin->setValue(12.0);

    m_targetLufsSpin = new QDoubleSpinBox(previewGroup);
    m_targetLufsSpin->setObjectName(QStringLiteral("ref_target_lufs"));
    m_targetLufsSpin->setRange(-26.0, -8.0);
    m_targetLufsSpin->setSingleStep(0.5);
    m_targetLufsSpin->setSuffix(QStringLiteral(" LUFS"));
    m_targetLufsSpin->setValue(-14.0);

    m_targetLumaSpin = new QDoubleSpinBox(previewGroup);
    m_targetLumaSpin->setObjectName(QStringLiteral("ref_target_luma"));
    m_targetLumaSpin->setRange(0.15, 0.85);
    m_targetLumaSpin->setSingleStep(0.05);
    m_targetLumaSpin->setValue(0.45);

    m_matchColourCheck = new QCheckBox(tr("Apply reference colour look"), previewGroup);
    m_matchColourCheck->setObjectName(QStringLiteral("ref_match_colour"));
    m_matchColourCheck->setChecked(true);

    m_accentHexEdit = new QLineEdit(QStringLiteral("#FF5A1F"), previewGroup);
    m_accentHexEdit->setObjectName(QStringLiteral("ref_accent_hex"));
    m_accentHexEdit->setMaxLength(7);

    m_gfxPosCombo = new QComboBox(previewGroup);
    m_gfxPosCombo->setObjectName(QStringLiteral("ref_gfx_position"));
    m_gfxPosCombo->addItem(tr("Bottom left"), QStringLiteral("bottom_left"));
    m_gfxPosCombo->addItem(tr("Bottom center"), QStringLiteral("bottom_center"));
    m_gfxPosCombo->addItem(tr("Bottom right"), QStringLiteral("bottom_right"));
    m_gfxPosCombo->addItem(tr("Top left"), QStringLiteral("top_left"));
    m_gfxPosCombo->addItem(tr("Top right"), QStringLiteral("top_right"));

    m_capAlignCombo = new QComboBox(previewGroup);
    m_capAlignCombo->setObjectName(QStringLiteral("ref_cap_alignment"));
    m_capAlignCombo->addItem(tr("Bottom center"), QStringLiteral("bottom_center"));
    m_capAlignCombo->addItem(tr("Bottom left"), QStringLiteral("bottom_left"));
    m_capAlignCombo->addItem(tr("Top center"), QStringLiteral("top_center"));

    m_capUpperCheck = new QCheckBox(tr("Uppercase captions"), previewGroup);
    m_capUpperCheck->setObjectName(QStringLiteral("ref_cap_uppercase"));

    grid->addWidget(new QLabel(tr("Pause trim:"), previewGroup), 0, 0);
    grid->addWidget(m_minSilenceSpin, 0, 1);
    grid->addWidget(new QLabel(tr("Loudness:"), previewGroup), 0, 2);
    grid->addWidget(m_targetLufsSpin, 0, 3);

    grid->addWidget(new QLabel(tr("Min shot:"), previewGroup), 1, 0);
    grid->addWidget(m_minShotSpin, 1, 1);
    grid->addWidget(new QLabel(tr("Max shot:"), previewGroup), 1, 2);
    grid->addWidget(m_maxShotSpin, 1, 3);

    grid->addWidget(new QLabel(tr("Target brightness:"), previewGroup), 2, 0);
    grid->addWidget(m_targetLumaSpin, 2, 1);
    grid->addWidget(m_matchColourCheck, 2, 2, 1, 2);

    grid->addWidget(new QLabel(tr("Accent colour:"), previewGroup), 3, 0);
    grid->addWidget(m_accentHexEdit, 3, 1);
    grid->addWidget(new QLabel(tr("Graphics zone:"), previewGroup), 3, 2);
    grid->addWidget(m_gfxPosCombo, 3, 3);

    grid->addWidget(new QLabel(tr("Captions:"), previewGroup), 4, 0);
    grid->addWidget(m_capAlignCombo, 4, 1);
    grid->addWidget(m_capUpperCheck, 4, 2, 1, 2);

    previewLay->addLayout(grid);

    m_saveBtn = new QPushButton(tr("Approve && Save Style"), previewGroup);
    m_saveBtn->setObjectName(QStringLiteral("ref_save_btn"));
    setRole(m_saveBtn, "primary");
    previewLay->addWidget(m_saveBtn);

    // 3. Saved styles management
    auto *savedGroup = new QGroupBox(tr("Saved Styles"), this);
    savedGroup->setMaximumWidth(240);
    auto *savedLay = new QVBoxLayout(savedGroup);
    savedLay->setSpacing(M::kSpace2);

    m_savedStylesList = new QListWidget(savedGroup);
    m_savedStylesList->setObjectName(QStringLiteral("ref_saved_list"));
    savedLay->addWidget(m_savedStylesList, 1);

    m_useSavedBtn = new QPushButton(tr("Use selected style"), savedGroup);
    m_useSavedBtn->setObjectName(QStringLiteral("ref_use_btn"));
    m_deleteSavedBtn = new QPushButton(tr("Delete style"), savedGroup);
    m_deleteSavedBtn->setObjectName(QStringLiteral("ref_delete_btn"));
    setRole(m_deleteSavedBtn, "danger");
    savedLay->addWidget(m_useSavedBtn);
    savedLay->addWidget(m_deleteSavedBtn);

    midRow->addWidget(previewGroup, 1);
    midRow->addWidget(savedGroup);
    root->addLayout(midRow, 1);

    auto *footer = new QHBoxLayout;
    footer->addStretch(1);
    auto *closeBtn = new QPushButton(tr("Close"), this);
    closeBtn->setObjectName(QStringLiteral("ref_close_btn"));
    connect(closeBtn, &QPushButton::clicked, this, &QDialog::reject);
    footer->addWidget(closeBtn);
    root->addLayout(footer);

    connect(m_addVideosBtn, &QPushButton::clicked, this, &ReferenceStyleDialog::handleAddVideos);
    connect(m_removeVideoBtn, &QPushButton::clicked, this, &ReferenceStyleDialog::handleRemoveVideo);
    connect(m_analyzeBtn, &QPushButton::clicked, this, &ReferenceStyleDialog::handleAnalyzeClicked);
    connect(m_saveBtn, &QPushButton::clicked, this, &ReferenceStyleDialog::handleSaveClicked);
    connect(m_useSavedBtn, &QPushButton::clicked, this, &ReferenceStyleDialog::handleUseSavedClicked);
    connect(m_deleteSavedBtn, &QPushButton::clicked, this, &ReferenceStyleDialog::handleDeleteSavedClicked);
    connect(m_videoList, &QListWidget::itemSelectionChanged, this, &ReferenceStyleDialog::updateButtonStates);
    connect(m_savedStylesList, &QListWidget::itemSelectionChanged, this, &ReferenceStyleDialog::updateButtonStates);
    connect(m_styleNameEdit, &QLineEdit::textChanged, this, &ReferenceStyleDialog::updateButtonStates);
}

void ReferenceStyleDialog::setSavedStyles(const QJsonArray &styles)
{
    m_savedStylesList->clear();
    for (const QJsonValue &v : styles) {
        const QJsonObject o = v.toObject();
        const QString pid = o.value(QStringLiteral("profile_id")).toString();
        const QString name = o.value(QStringLiteral("display_name")).toString(pid);
        if (pid.isEmpty()) {
            continue;
        }
        QString label = name;
        if (o.value(QStringLiteral("learned_from_reference")).toBool()) {
            label += tr(" (Ref)");
        }
        auto *item = new QListWidgetItem(label, m_savedStylesList);
        item->setData(kProfileIdRole, pid);
        item->setData(kDisplayNameRole, name);
        item->setToolTip(o.value(QStringLiteral("description")).toString());
    }
    updateButtonStates();
}

void ReferenceStyleDialog::setAnalysisResult(const QJsonObject &result)
{
    m_currentProfile = result.value(QStringLiteral("profile")).toObject();
    const QJsonObject obs = result.value(QStringLiteral("observations")).toObject();
    const QJsonObject pacing = obs.value(QStringLiteral("pacing")).toObject();
    const QJsonObject audio = obs.value(QStringLiteral("audio")).toObject();
    const QJsonObject colour = obs.value(QStringLiteral("colour")).toObject();
    const QJsonObject captions = obs.value(QStringLiteral("captions")).toObject();
    const QJsonObject graphics = obs.value(QStringLiteral("graphics")).toObject();
    const QJsonObject broll = obs.value(QStringLiteral("broll")).toObject();
    const QJsonObject opening = obs.value(QStringLiteral("opening")).toObject();
    const QJsonObject reasoning = result.value(QStringLiteral("reasoning")).toObject();

    const QString name = result.value(QStringLiteral("display_name")).toString();
    if (!name.isEmpty() && m_styleNameEdit->text().trimmed().isEmpty()) {
        m_styleNameEdit->setText(name);
    }

    QStringList lines;
    lines << tr("Pacing: %1 (%2 cuts/min · median shot %3 s)")
                 .arg(pacing.value(QStringLiteral("pacing_tier")).toString(QStringLiteral("balanced")))
                 .arg(pacing.value(QStringLiteral("cuts_per_minute")).toDouble(), 0, 'f', 1)
                 .arg(pacing.value(QStringLiteral("median_shot_sec")).toDouble(), 0, 'f', 1);
    lines << tr("Opening: first shot %1 s · %2")
                 .arg(opening.value(QStringLiteral("first_shot_sec")).toDouble(), 0, 'f', 1)
                 .arg(opening.value(QStringLiteral("hook_opening")).toBool() ? tr("fast hook") : tr("establishing intro"));
    lines << tr("Colour: %1 (luma %2 · saturation %3)")
                 .arg(colour.value(QStringLiteral("treatment")).toString().replace(QLatin1Char('_'), QLatin1Char(' ')))
                 .arg(colour.value(QStringLiteral("median_luma")).toDouble(), 0, 'f', 2)
                 .arg(colour.value(QStringLiteral("mean_saturation")).toDouble(), 0, 'f', 2);
    lines << tr("Audio: %1 LUFS · true peak %2 dBTP · median pause %3 s")
                 .arg(audio.value(QStringLiteral("integrated_lufs")).toDouble(), 0, 'f', 1)
                 .arg(audio.value(QStringLiteral("true_peak_dbtp")).toDouble(), 0, 'f', 1)
                 .arg(audio.value(QStringLiteral("median_pause_sec")).toDouble(), 0, 'f', 2);
    lines << tr("Captions & Graphics: captions %1 (%2) · graphics %3 · accent %4 · B-roll share %5%")
                 .arg(captions.value(QStringLiteral("detected")).toBool() ? tr("detected") : tr("optional"))
                 .arg(captions.value(QStringLiteral("position")).toString())
                 .arg(graphics.value(QStringLiteral("preferred_position")).toString())
                 .arg(graphics.value(QStringLiteral("accent_hex")).toString())
                 .arg(qRound(broll.value(QStringLiteral("cutaway_share")).toDouble() * 100.0));
    lines << (reasoning.value(QStringLiteral("used")).toBool()
                  ? tr("Reasoning: semantic summary via %1").arg(reasoning.value(QStringLiteral("provider")).toString())
                  : tr("Analysis: 100% local media inspection (%1)").arg(reasoning.value(QStringLiteral("reason")).toString(tr("no subscription needed"))));
    m_observationsView->setPlainText(lines.join(QLatin1Char('\n')));

    // Populate editable controls from the generated profile
    const QJsonObject er = m_currentProfile.value(QStringLiteral("editing_rules")).toObject();
    const QJsonObject ar = m_currentProfile.value(QStringLiteral("audio_rules")).toObject();
    const QJsonObject cr = m_currentProfile.value(QStringLiteral("colour_rules")).toObject();
    const QJsonObject gr = m_currentProfile.value(QStringLiteral("graphics_rules")).toObject();
    const QJsonObject gt = m_currentProfile.value(QStringLiteral("graphics_templates")).toObject();
    const QJsonObject cs = m_currentProfile.value(QStringLiteral("caption_style")).toObject();

    m_minSilenceSpin->setValue(er.value(QStringLiteral("min_silence_sec")).toDouble(0.45));
    m_minShotSpin->setValue(er.value(QStringLiteral("min_shot_duration_sec")).toDouble(1.5));
    m_maxShotSpin->setValue(er.value(QStringLiteral("max_shot_duration_sec")).toDouble(12.0));
    m_targetLufsSpin->setValue(ar.value(QStringLiteral("target_lufs")).toDouble(-14.0));
    m_targetLumaSpin->setValue(cr.value(QStringLiteral("target_luma")).toDouble(0.45));
    m_matchColourCheck->setChecked(cr.value(QStringLiteral("match_reference_look")).toBool(true));
    m_accentHexEdit->setText(gr.value(QStringLiteral("brand_accent_hex")).toString(QStringLiteral("#FF5A1F")));

    const int gfxIdx = m_gfxPosCombo->findData(gt.value(QStringLiteral("lower_third_position")).toString());
    if (gfxIdx >= 0) {
        m_gfxPosCombo->setCurrentIndex(gfxIdx);
    }
    const int capIdx = m_capAlignCombo->findData(cs.value(QStringLiteral("alignment")).toString());
    if (capIdx >= 0) {
        m_capAlignCombo->setCurrentIndex(capIdx);
    }
    m_capUpperCheck->setChecked(cs.value(QStringLiteral("uppercase")).toBool(false));

    setStatusMessage(tr("Reference analysis complete. Review or tweak settings, then click Approve & Save Style."));
    updateButtonStates();
}

void ReferenceStyleDialog::setStatusMessage(const QString &message, bool isError)
{
    m_statusLabel->setText(message);
    setTextRole(m_statusLabel, isError ? "error" : "meta");
}

void ReferenceStyleDialog::setBusy(bool busy, const QString &message)
{
    m_busy = busy;
    if (!message.isEmpty()) {
        setStatusMessage(message, false);
    }
    updateButtonStates();
}

void ReferenceStyleDialog::addReferenceVideos(const QStringList &paths)
{
    QStringList existing = referenceVideos();
    for (const QString &raw : paths) {
        const QString p = raw.trimmed();
        if (p.isEmpty() || existing.contains(p)) {
            continue;
        }
        auto *item = new QListWidgetItem(QFileInfo(p).fileName(), m_videoList);
        item->setData(Qt::UserRole, p);
        item->setToolTip(p);
        existing.append(p);
    }
    if (m_styleNameEdit->text().trimmed().isEmpty() && !existing.isEmpty()) {
        m_styleNameEdit->setText(QFileInfo(existing.first()).completeBaseName().replace(QLatin1Char('_'), QLatin1Char(' ')));
    }
    updateButtonStates();
}

QStringList ReferenceStyleDialog::referenceVideos() const
{
    QStringList out;
    for (int i = 0; i < m_videoList->count(); ++i) {
        out << m_videoList->item(i)->data(Qt::UserRole).toString();
    }
    return out;
}

QString ReferenceStyleDialog::styleName() const
{
    return m_styleNameEdit->text().trimmed();
}

QJsonObject ReferenceStyleDialog::buildEditedProfile() const
{
    QJsonObject prof = m_currentProfile;
    if (prof.isEmpty()) {
        return prof;
    }
    const QString name = styleName().isEmpty() ? prof.value(QStringLiteral("display_name")).toString(QStringLiteral("Reference Style")) : styleName();
    prof.insert(QStringLiteral("display_name"), name);
    prof.insert(QStringLiteral("profile_id"), slugifyId(name));

    const double minShot = m_minShotSpin->value();
    const double maxShot = qMax(minShot + 1.0, m_maxShotSpin->value());
    const double minSil = m_minSilenceSpin->value();
    const double lufs = m_targetLufsSpin->value();
    const QString accent = m_accentHexEdit->text().trimmed().startsWith(QLatin1Char('#')) ? m_accentHexEdit->text().trimmed() : QStringLiteral("#FF5A1F");

    prof.insert(QStringLiteral("min_shot_duration_sec"), minShot);
    prof.insert(QStringLiteral("max_shot_duration_sec"), maxShot);
    prof.insert(QStringLiteral("loudness_target_lufs"), lufs);

    QJsonObject er = prof.value(QStringLiteral("editing_rules")).toObject();
    er.insert(QStringLiteral("min_silence_sec"), minSil);
    er.insert(QStringLiteral("natural_pause_sec"), qMax(0.18, minSil * 0.72));
    er.insert(QStringLiteral("turn_pause_sec"), qMax(0.22, minSil * 0.84));
    er.insert(QStringLiteral("min_shot_sec"), minShot);
    er.insert(QStringLiteral("max_shot_sec"), maxShot);
    er.insert(QStringLiteral("min_shot_duration_sec"), minShot);
    er.insert(QStringLiteral("max_shot_duration_sec"), maxShot);
    prof.insert(QStringLiteral("editing_rules"), er);

    QJsonObject ar = prof.value(QStringLiteral("audio_rules")).toObject();
    ar.insert(QStringLiteral("target_lufs"), lufs);
    ar.insert(QStringLiteral("loudness_target_lufs"), lufs);
    prof.insert(QStringLiteral("audio_rules"), ar);

    if (prof.contains(QStringLiteral("delivery_spec"))) {
        QJsonObject ds = prof.value(QStringLiteral("delivery_spec")).toObject();
        ds.insert(QStringLiteral("target_lufs"), lufs);
        prof.insert(QStringLiteral("delivery_spec"), ds);
    }

    QJsonObject cr = prof.value(QStringLiteral("colour_rules")).toObject();
    cr.insert(QStringLiteral("target_luma"), m_targetLumaSpin->value());
    cr.insert(QStringLiteral("match_reference_look"), m_matchColourCheck->isChecked());
    prof.insert(QStringLiteral("colour_rules"), cr);

    QJsonObject gr = prof.value(QStringLiteral("graphics_rules")).toObject();
    gr.insert(QStringLiteral("brand_accent_hex"), accent);
    prof.insert(QStringLiteral("graphics_rules"), gr);

    QJsonObject gt = prof.value(QStringLiteral("graphics_templates")).toObject();
    gt.insert(QStringLiteral("lower_third_position"), m_gfxPosCombo->currentData().toString());
    gt.insert(QStringLiteral("accent_hex"), accent);
    prof.insert(QStringLiteral("graphics_templates"), gt);

    QJsonObject cs = prof.value(QStringLiteral("caption_style")).toObject();
    cs.insert(QStringLiteral("alignment"), m_capAlignCombo->currentData().toString());
    cs.insert(QStringLiteral("uppercase"), m_capUpperCheck->isChecked());
    cs.insert(QStringLiteral("highlight_color_hex"), accent);
    prof.insert(QStringLiteral("caption_style"), cs);

    return prof;
}

void ReferenceStyleDialog::handleAddVideos()
{
    const QStringList files =
        QFileDialog::getOpenFileNames(this, tr("Choose Reference Videos"), QString(), tr("Video Files (*.mp4 *.mov *.mkv *.webm *.m4v *.avi);;All Files (*)"));
    if (!files.isEmpty()) {
        addReferenceVideos(files);
    }
}

void ReferenceStyleDialog::handleRemoveVideo()
{
    for (QListWidgetItem *item : m_videoList->selectedItems()) {
        delete item;
    }
    updateButtonStates();
}

void ReferenceStyleDialog::handleAnalyzeClicked()
{
    const QStringList vids = referenceVideos();
    if (vids.isEmpty()) {
        return;
    }
    Q_EMIT analyzeRequested(vids, styleName());
}

void ReferenceStyleDialog::handleSaveClicked()
{
    const QJsonObject prof = buildEditedProfile();
    if (prof.isEmpty()) {
        return;
    }
    Q_EMIT saveProfileRequested(prof);
}

void ReferenceStyleDialog::handleUseSavedClicked()
{
    if (QListWidgetItem *item = m_savedStylesList->currentItem()) {
        Q_EMIT selectSavedStyleRequested(item->data(kProfileIdRole).toString(), item->data(kDisplayNameRole).toString());
        accept();
    }
}

void ReferenceStyleDialog::handleDeleteSavedClicked()
{
    if (QListWidgetItem *item = m_savedStylesList->currentItem()) {
        Q_EMIT deleteSavedStyleRequested(item->data(kProfileIdRole).toString());
    }
}

void ReferenceStyleDialog::updateButtonStates()
{
    m_addVideosBtn->setEnabled(!m_busy);
    m_removeVideoBtn->setEnabled(!m_busy && !m_videoList->selectedItems().isEmpty());
    m_analyzeBtn->setEnabled(!m_busy && m_videoList->count() > 0);
    m_saveBtn->setEnabled(!m_busy && !m_currentProfile.isEmpty() && !styleName().isEmpty());
    const bool hasSavedSel = m_savedStylesList->currentItem() != nullptr;
    m_useSavedBtn->setEnabled(!m_busy && hasSavedSel);
    m_deleteSavedBtn->setEnabled(!m_busy && hasSavedSel);
}

} // namespace BangOnPro
