/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "BangOnProTheme.h"

#include <QAbstractScrollArea>
#include <QApplication>
#include <QFrame>
#include <QGuiApplication>
#include <QHash>
#include <QPainter>
#include <QPixmap>
#include <QScreen>
#include <QScrollArea>
#include <QStyle>
#include <QStyleFactory>
#include <QWidget>

namespace BangOnPro {

namespace {

QColor tok(const char *hex)
{
    return QColor(QString::fromLatin1(hex));
}

// Token substitution: {{name}} placeholders keep the stylesheet readable and single-sourced.
QString expandTokens(QString qss)
{
    using T = StudioPaletteTokens;
    using M = StudioMetrics;
    const QList<QPair<QString, QString>> tokens = {
        {QStringLiteral("chrome"), QString::fromLatin1(T::kWindowChrome)},
        {QStringLiteral("well"), QString::fromLatin1(T::kSunkenWell)},
        {QStringLiteral("panel"), QString::fromLatin1(T::kPanelSurface)},
        {QStringLiteral("raised"), QString::fromLatin1(T::kElevatedSurface)},
        {QStringLiteral("hover"), QString::fromLatin1(T::kElevatedHover)},
        {QStringLiteral("selected"), QString::fromLatin1(T::kSelectedRow)},
        {QStringLiteral("hairline"), QString::fromLatin1(T::kBorderHairline)},
        {QStringLiteral("outline"), QString::fromLatin1(T::kBorderActive)},
        {QStringLiteral("text"), QString::fromLatin1(T::kTextPrimary)},
        {QStringLiteral("text2"), QString::fromLatin1(T::kTextSecondary)},
        {QStringLiteral("muted"), QString::fromLatin1(T::kTextMuted)},
        {QStringLiteral("disabled"), QString::fromLatin1(T::kTextDisabled)},
        {QStringLiteral("accent"), QString::fromLatin1(T::kAccentPrimary)},
        {QStringLiteral("accentHover"), QString::fromLatin1(T::kAccentPrimaryHover)},
        {QStringLiteral("accentPressed"), QString::fromLatin1(T::kAccentPressed)},
        {QStringLiteral("accentDim"), QString::fromLatin1(T::kAccentSelection)},
        {QStringLiteral("onAccent"), QString::fromLatin1(T::kTextOnAccent)},
        {QStringLiteral("success"), QString::fromLatin1(T::kStatusSuccess)},
        {QStringLiteral("warning"), QString::fromLatin1(T::kStatusWarning)},
        {QStringLiteral("danger"), QString::fromLatin1(T::kStatusDanger)},
        {QStringLiteral("rc"), QString::number(M::kRadiusControl)},
        {QStringLiteral("rg"), QString::number(M::kRadiusGroup)},
        {QStringLiteral("fMeta"), QString::number(M::kFontMeta)},
        {QStringLiteral("fBody"), QString::number(M::kFontBody)},
        {QStringLiteral("fTitle"), QString::number(M::kFontTitle)},
        {QStringLiteral("mono"), QStringLiteral("\"SF Mono\", \"Menlo\", \"JetBrains Mono\", \"DejaVu Sans Mono\", monospace")},
    };
    for (const auto &kv : tokens) {
        qss.replace(QStringLiteral("{{") + kv.first + QStringLiteral("}}"), kv.second);
    }
    return qss;
}

// Native Qt controls: quiet graphite chrome, hairlines, 4 px grid, two weights.
const char *kBaseQss = R"QSS(
QMainWindow, QDialog { background-color: {{chrome}}; color: {{text}}; }
QWidget { selection-background-color: {{selected}}; selection-color: {{text}}; }
QMainWindow::separator { background-color: {{chrome}}; width: 4px; height: 4px; }
QMainWindow::separator:hover { background-color: {{outline}}; }
QSplitter::handle { background-color: {{chrome}}; }
QSplitter::handle:horizontal { width: 4px; }
QSplitter::handle:vertical { height: 4px; }
QSplitter::handle:hover { background-color: {{outline}}; }
QToolTip { background-color: {{raised}}; color: {{text}}; border: 1px solid {{outline}}; border-radius: {{rc}}px; padding: 4px 8px; font-size: {{fBody}}px; }

QMenuBar { background-color: {{chrome}}; color: {{text2}}; border-bottom: 1px solid {{hairline}}; padding: 0px 4px; font-size: {{fBody}}px; }
QMenuBar::item { background: transparent; padding: 4px 8px; border-radius: {{rc}}px; }
QMenuBar::item:selected, QMenuBar::item:pressed { background-color: {{hover}}; color: {{text}}; }
QMenu { background-color: {{raised}}; color: {{text}}; border: 1px solid {{outline}}; border-radius: {{rg}}px; padding: 4px; font-size: {{fBody}}px; }
QMenu::item { padding: 4px 24px 4px 12px; border-radius: {{rc}}px; }
QMenu::item:selected { background-color: {{hover}}; color: {{text}}; }
QMenu::item:disabled { color: {{disabled}}; }
QMenu::separator { height: 1px; background: {{hairline}}; margin: 4px 8px; }
QToolBar { background-color: {{panel}}; border: none; spacing: 4px; padding: 2px 4px; }
QToolBar::separator { background-color: {{hairline}}; width: 1px; height: 1px; margin: 4px 4px; }
QStatusBar { background-color: {{chrome}}; color: {{text2}}; border-top: 1px solid {{hairline}}; font-size: {{fMeta}}px; }
QStatusBar::item { border: none; }

QDockWidget { color: {{text2}}; font-size: {{fBody}}px; }
QDockWidget::title { background-color: {{chrome}}; color: {{text2}}; padding: 4px 8px; border-bottom: 1px solid {{hairline}}; }
QDockWidget::close-button, QDockWidget::float-button { background: transparent; border: none; border-radius: {{rc}}px; padding: 2px; }
QDockWidget::close-button:hover, QDockWidget::float-button:hover { background-color: {{hover}}; }

QTabWidget::pane { border: none; border-top: 1px solid {{hairline}}; background-color: {{panel}}; }
QTabBar { background-color: {{chrome}}; font-size: {{fBody}}px; }
QTabBar::tab { background: transparent; color: {{muted}}; padding: 6px 12px; border: none; border-bottom: 2px solid transparent; }
QTabBar::tab:hover { color: {{text}}; }
QTabBar::tab:selected { color: {{text}}; border-bottom: 2px solid {{accent}}; }
QTabBar::tab:!selected { margin-top: 0px; }
QTabBar::close-button:hover { background-color: {{hover}}; border-radius: {{rc}}px; }

QPushButton { background-color: {{raised}}; color: {{text}}; border: 1px solid {{outline}}; border-radius: {{rc}}px; padding: 4px 12px; min-height: 18px; font-size: {{fBody}}px; font-weight: 400; }
QPushButton:hover { background-color: {{hover}}; }
QPushButton:pressed { background-color: {{panel}}; }
QPushButton:checked { background-color: {{selected}}; border-color: {{outline}}; }
QPushButton:focus { border-color: {{accentDim}}; }
QPushButton:disabled { background-color: {{panel}}; color: {{disabled}}; border-color: {{hairline}}; }
QPushButton:flat { background: transparent; border: none; }
QPushButton::menu-indicator { subcontrol-origin: padding; subcontrol-position: right center; right: 4px; }
QToolButton { background: transparent; color: {{text2}}; border: 1px solid transparent; border-radius: {{rc}}px; padding: 2px; }
QToolButton:hover { background-color: {{hover}}; color: {{text}}; }
QToolButton:pressed { background-color: {{panel}}; }
QToolButton:checked { background-color: {{selected}}; color: {{text}}; }
QToolButton:focus { border-color: {{accentDim}}; }
QToolButton:disabled { color: {{disabled}}; }
QToolButton[popupMode="1"] { padding-right: 12px; }
QToolButton::menu-button { border: none; width: 12px; }

QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox, QKeySequenceEdit { background-color: {{well}}; color: {{text}}; border: 1px solid {{outline}}; border-radius: {{rc}}px; padding: 4px 8px; font-size: {{fBody}}px; selection-background-color: {{accentDim}}; selection-color: #FFFFFF; }
QPlainTextEdit, QTextEdit { padding: 4px; }
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QAbstractSpinBox:focus, QKeySequenceEdit:focus { border-color: {{accentDim}}; }
QLineEdit:disabled, QAbstractSpinBox:disabled { color: {{disabled}}; border-color: {{hairline}}; }
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button { background: transparent; border: none; width: 14px; }
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover { background-color: {{hover}}; }
QComboBox { background-color: {{raised}}; color: {{text}}; border: 1px solid {{outline}}; border-radius: {{rc}}px; padding: 4px 24px 4px 8px; min-height: 18px; font-size: {{fBody}}px; }
QComboBox:hover { background-color: {{hover}}; }
QComboBox:focus { border-color: {{accentDim}}; }
QComboBox:disabled { color: {{disabled}}; border-color: {{hairline}}; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView { background-color: {{raised}}; border: 1px solid {{outline}}; selection-background-color: {{hover}}; selection-color: {{text}}; outline: 0; padding: 4px; }

QAbstractItemView { background-color: {{well}}; alternate-background-color: #141416; color: {{text}}; border: none; outline: 0; font-size: {{fBody}}px; selection-background-color: {{selected}}; selection-color: {{text}}; }
QAbstractItemView::item { padding: 2px 4px; }
QAbstractItemView::item:hover { background-color: #1D1D20; }
QAbstractItemView::item:selected { background-color: {{selected}}; color: {{text}}; }
QAbstractItemView::item:selected:active { border-left: 2px solid {{accent}}; }
QHeaderView { background-color: {{panel}}; }
QHeaderView::section { background-color: {{panel}}; color: {{muted}}; border: none; border-bottom: 1px solid {{hairline}}; border-right: 1px solid {{hairline}}; padding: 4px 8px; font-size: {{fMeta}}px; font-weight: 500; }
QTableCornerButton::section { background-color: {{panel}}; border: none; }

QGroupBox { background: transparent; border: none; border-top: 1px solid {{hairline}}; margin-top: 20px; padding-top: 8px; font-size: {{fBody}}px; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 0px; top: 4px; padding: 0px; color: {{text2}}; font-weight: 500; }
QCheckBox, QRadioButton { color: {{text}}; spacing: 8px; font-size: {{fBody}}px; }
QCheckBox:disabled, QRadioButton:disabled { color: {{disabled}}; }
QCheckBox::indicator, QRadioButton::indicator { width: 14px; height: 14px; background-color: {{well}}; border: 1px solid {{outline}}; }
QCheckBox::indicator { border-radius: 3px; }
QRadioButton::indicator { border-radius: 7px; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: {{text2}}; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked { background-color: {{accent}}; border-color: {{accent}}; }
QCheckBox::indicator:indeterminate { background-color: {{accentDim}}; border-color: {{accentDim}}; }

QSlider::groove:horizontal { height: 4px; background: {{well}}; border: 1px solid {{hairline}}; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #5A5A60; border-radius: 2px; }
QSlider::handle:horizontal { width: 12px; height: 12px; margin: -5px 0px; background: {{text}}; border-radius: 6px; }
QSlider::handle:horizontal:hover { background: #FFFFFF; }
QSlider::groove:vertical { width: 4px; background: {{well}}; border: 1px solid {{hairline}}; border-radius: 2px; }
QSlider::add-page:vertical { background: #5A5A60; border-radius: 2px; }
QSlider::handle:vertical { width: 12px; height: 12px; margin: 0px -5px; background: {{text}}; border-radius: 6px; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 0px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0px; }
QScrollBar::handle:vertical { background: #3A3A3F; min-height: 24px; border-radius: 4px; margin: 2px; }
QScrollBar::handle:horizontal { background: #3A3A3F; min-width: 24px; border-radius: 4px; margin: 2px; }
QScrollBar::handle:hover { background: #4A4A50; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; border: none; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

QProgressBar { background-color: {{well}}; border: 1px solid {{hairline}}; border-radius: {{rc}}px; color: {{text2}}; text-align: center; font-size: {{fMeta}}px; min-height: 12px; }
QProgressBar::chunk { background-color: {{accent}}; border-radius: 3px; }
)QSS";

// Reusable Bang On Pro component layer: roles, tones, tally, banners, workspace bar, AI dock.
const char *kComponentQss = R"QSS(
QPushButton[bopRole="primary"], QPushButton[bangOnProPrimary="true"] { background-color: {{accent}}; border-color: {{accent}}; color: {{onAccent}}; font-weight: 500; }
QPushButton[bopRole="primary"]:hover, QPushButton[bangOnProPrimary="true"]:hover { background-color: {{accentHover}}; border-color: {{accentHover}}; }
QPushButton[bopRole="primary"]:pressed, QPushButton[bangOnProPrimary="true"]:pressed { background-color: {{accentPressed}}; }
QPushButton[bopRole="primary"]:disabled, QPushButton[bangOnProPrimary="true"]:disabled { background-color: {{raised}}; border-color: {{hairline}}; color: {{disabled}}; }
QPushButton[bopRole="accept"], QPushButton[bangOnProAccept="true"] { background-color: #1B3428; border-color: #2E5A44; color: #9BE3BF; }
QPushButton[bopRole="accept"]:hover, QPushButton[bangOnProAccept="true"]:hover { background-color: #21402F; }
QPushButton[bopRole="danger"], QPushButton[bangOnProDanger="true"] { background-color: {{raised}}; border-color: #5A2A2D; color: #F0A3A6; }
QPushButton[bopRole="danger"]:hover, QPushButton[bangOnProDanger="true"]:hover { background-color: #3A2022; }
QPushButton[bangOnProWarning="true"] { background-color: {{raised}}; border-color: #5A4A24; color: #E8C27A; }
QPushButton[bopRole="accept"]:disabled, QPushButton[bangOnProAccept="true"]:disabled, QPushButton[bopRole="danger"]:disabled, QPushButton[bangOnProDanger="true"]:disabled, QPushButton[bangOnProWarning="true"]:disabled { background-color: {{panel}}; border-color: {{hairline}}; color: {{disabled}}; }
QPushButton[bopRole="quiet"], QPushButton[bangOnProChip="true"] { background: transparent; border: 1px solid {{hairline}}; color: {{text2}}; padding: 4px 8px; text-align: left; }
QPushButton[bopRole="quiet"]:hover, QPushButton[bangOnProChip="true"]:hover { background-color: {{hover}}; color: {{text}}; }

QLabel[bopText="primary"] { color: {{text}}; }
QLabel[bopText="secondary"] { color: {{text2}}; }
QLabel[bopText="muted"] { color: {{muted}}; font-size: {{fMeta}}px; }
QLabel[bopText="meta"] { color: {{muted}}; font-size: {{fMeta}}px; font-family: {{mono}}; }
QLabel[bopText="section"] { color: {{text2}}; font-size: {{fBody}}px; font-weight: 500; }
QLabel[bopText="title"] { color: {{text}}; font-size: {{fTitle}}px; font-weight: 500; }
QLabel[bopTone="success"] { color: {{success}}; }
QLabel[bopTone="warning"] { color: {{warning}}; }
QLabel[bopTone="danger"] { color: {{danger}}; }
QLabel[bopTone="accent"] { color: {{accent}}; }
QCheckBox[bopTone="warning"] { color: {{warning}}; }

QFrame[bopBanner="warning"], QFrame#clarificationBanner { background-color: {{raised}}; border: none; border-left: 2px solid {{warning}}; border-radius: 0px; }
QFrame[bopBanner="danger"], QFrame#staleProposalBanner { background-color: {{raised}}; border: none; border-left: 2px solid {{danger}}; border-radius: 0px; }
QFrame[bopBanner="info"] { background-color: {{raised}}; border: none; border-left: 2px solid {{outline}}; border-radius: 0px; }

QWidget#bangOnProWorkspaceSwitcher { background-color: {{chrome}}; border-bottom: 1px solid {{hairline}}; }
QLabel#bangOnProBadge { color: {{text}}; font-size: {{fTitle}}px; font-weight: 500; padding: 0px 0px 0px 8px; border: none; border-left: 3px solid {{accent}}; background: transparent; }
QPushButton[bangOnProWorkspacePill="true"] { background: transparent; border: none; border-bottom: 2px solid transparent; border-radius: 0px; color: {{muted}}; padding: 8px 8px 6px 8px; font-size: {{fBody}}px; }
QPushButton[bangOnProWorkspacePill="true"]:hover { color: {{text}}; background: transparent; }
QPushButton[bangOnProWorkspacePill="true"]:checked { color: {{text}}; background: transparent; border-bottom: 2px solid {{accent}}; }
QPushButton[bangOnProWorkspacePill="true"]:focus { color: {{text}}; }
QPushButton#workspaceAiPanelToggle, QToolButton#workspaceSaveLayoutButton { background: transparent; border: 1px solid {{hairline}}; color: {{text2}}; padding: 2px 8px; }
QPushButton#workspaceAiPanelToggle:checked { color: {{text}}; border-color: {{outline}}; background-color: {{selected}}; }

QWidget#bangOnProAiAssistant, QWidget#bangOnProTranscriptEditor, QWidget#bangon_pro_agent_control_centre, QWidget#bangonpro_agent_control_centre { background-color: {{panel}}; color: {{text}}; }
QScrollArea#bangOnProDockScroll, QScrollArea#bangOnProDockScroll > QWidget > QWidget { background-color: {{panel}}; border: none; }
QFrame#bangOnProHeader { background: transparent; border: none; border-bottom: 1px solid {{hairline}}; border-radius: 0px; }
QLabel#bangOnProTitle { color: {{text}}; font-size: {{fTitle}}px; font-weight: 500; }
QLabel#bangOnProStatusBadge { background: transparent; color: {{text2}}; font-size: {{fMeta}}px; padding: 0px; }
QLabel#bangOnProStatusBadge[statusState="proposal"] { color: {{warning}}; }
QLabel#bangOnProStatusBadge[statusState="ok"] { color: {{success}}; }
QLabel#bangOnProStatusBadge[statusState="busy"] { color: {{accent}}; }
QLabel#bangOnProStatusBadge[statusState="error"] { color: {{danger}}; }
QLabel#bangOnProSubtitle, QLabel#bangOnProMutedLabel, QLabel#taskStatusLabel { color: {{muted}}; font-size: {{fMeta}}px; }
QLabel#bangOnProRevision { color: {{muted}}; font-size: {{fMeta}}px; font-family: {{mono}}; }
QLabel#bangOnProSectionLabel { color: {{text2}}; font-size: {{fBody}}px; font-weight: 500; }
QLabel#proposalSummaryLabel { color: {{text2}}; font-size: {{fMeta}}px; }
QPlainTextEdit#agentActivityLog { font-family: {{mono}}; font-size: {{fMeta}}px; color: {{text2}}; }

QProgressBar#acc_progress { min-height: 4px; max-height: 4px; border: none; border-radius: 2px; background-color: {{raised}}; }
QProgressBar#acc_progress::chunk { border-radius: 2px; background-color: {{accent}}; }
QTreeWidget#acc_agents::item { padding: 4px 4px; }
QListWidget#acc_review::item { padding: 4px 4px; color: {{text2}}; }
QPlainTextEdit#acc_summary { font-family: {{mono}}; font-size: {{fMeta}}px; color: {{text2}}; }
)QSS";

} // namespace

QPalette createBangOnProPalette()
{
    using T = StudioPaletteTokens;
    QPalette pal;
    pal.setColor(QPalette::Window, tok(T::kPanelSurface));
    pal.setColor(QPalette::WindowText, tok(T::kTextPrimary));
    pal.setColor(QPalette::Base, tok(T::kSunkenWell));
    pal.setColor(QPalette::AlternateBase, QColor(0x14, 0x14, 0x16));
    pal.setColor(QPalette::ToolTipBase, tok(T::kElevatedSurface));
    pal.setColor(QPalette::ToolTipText, tok(T::kTextPrimary));
    pal.setColor(QPalette::Text, tok(T::kTextPrimary));
    pal.setColor(QPalette::Button, tok(T::kElevatedSurface));
    pal.setColor(QPalette::ButtonText, tok(T::kTextPrimary));
    pal.setColor(QPalette::BrightText, QColor(255, 255, 255));
    pal.setColor(QPalette::Link, tok(T::kAccentPrimaryHover));
    pal.setColor(QPalette::LinkVisited, tok(T::kAccentPrimary));
    // Timeline/monitor selection keeps the accent family so selected clips stay unmistakable.
    pal.setColor(QPalette::Highlight, tok(T::kAccentSelection));
    pal.setColor(QPalette::HighlightedText, QColor(255, 255, 255));
    pal.setColor(QPalette::Light, tok(T::kBorderActive));
    pal.setColor(QPalette::Midlight, tok(T::kElevatedHover));
    pal.setColor(QPalette::Mid, tok(T::kBorderHairline));
    pal.setColor(QPalette::Dark, tok(T::kWindowChrome));
    pal.setColor(QPalette::Shadow, QColor(4, 4, 5));
#if QT_VERSION >= QT_VERSION_CHECK(5, 12, 0)
    pal.setColor(QPalette::PlaceholderText, tok(T::kTextMuted));
#endif
    pal.setColor(QPalette::Disabled, QPalette::WindowText, tok(T::kTextDisabled));
    pal.setColor(QPalette::Disabled, QPalette::Text, tok(T::kTextDisabled));
    pal.setColor(QPalette::Disabled, QPalette::ButtonText, tok(T::kTextDisabled));
    pal.setColor(QPalette::Disabled, QPalette::Button, tok(T::kPanelSurface));
    pal.setColor(QPalette::Disabled, QPalette::Highlight, tok(T::kBorderActive));
    pal.setColor(QPalette::Disabled, QPalette::HighlightedText, tok(T::kTextSecondary));
    pal.setColor(QPalette::Inactive, QPalette::Highlight, QColor(0x7A, 0x34, 0x1A));
    pal.setColor(QPalette::Inactive, QPalette::HighlightedText, tok(T::kTextPrimary));
    return pal;
}

QString componentStyleSheet()
{
    return expandTokens(QString::fromUtf8(kComponentQss));
}

QString globalApplicationStyleSheet()
{
    return expandTokens(QString::fromUtf8(kBaseQss)) + componentStyleSheet();
}

bool applyBangOnProTheme(QApplication *app)
{
    if (app == nullptr) {
        return false;
    }
    if (QStyle *fusion = QStyleFactory::create(QStringLiteral("Fusion"))) {
        app->setStyle(fusion);
    }
    app->setPalette(createBangOnProPalette());
    app->setStyleSheet(globalApplicationStyleSheet());
    return true;
}

namespace {
void repolish(QWidget *w)
{
    if (w != nullptr && w->style() != nullptr) {
        w->style()->unpolish(w);
        w->style()->polish(w);
        w->update();
    }
}
} // namespace

void setRole(QWidget *widget, const char *role)
{
    if (widget != nullptr) {
        widget->setProperty("bopRole", QString::fromLatin1(role));
        repolish(widget);
    }
}

void setTextRole(QWidget *widget, const char *role)
{
    if (widget != nullptr) {
        widget->setProperty("bopText", QString::fromLatin1(role));
        repolish(widget);
    }
}

void setTone(QWidget *widget, const char *tone)
{
    if (widget != nullptr) {
        widget->setProperty("bopTone", QString::fromLatin1(tone));
        repolish(widget);
    }
}

QColor tallyColor(TallyState state)
{
    using T = StudioPaletteTokens;
    switch (state) {
    case TallyState::Running:
        return tok(T::kAccentPrimary);
    case TallyState::Done:
        return tok(T::kStatusSuccess);
    case TallyState::Review:
        return tok(T::kStatusWarning);
    case TallyState::Failed:
        return tok(T::kStatusDanger);
    case TallyState::Skipped:
        return tok(T::kTextDisabled);
    case TallyState::Idle:
    default:
        return tok(T::kBorderActive);
    }
}

QIcon tallyIcon(TallyState state)
{
    static QHash<int, QIcon> cache;
    const int key = static_cast<int>(state);
    if (!cache.contains(key)) {
        // 12 px box with an 8 px rounded square, like a broadcast tally lamp.
        QPixmap pm(12, 12);
        pm.fill(Qt::transparent);
        QPainter p(&pm);
        p.setRenderHint(QPainter::Antialiasing, true);
        p.setPen(Qt::NoPen);
        p.setBrush(tallyColor(state));
        p.drawRoundedRect(QRectF(2, 2, 8, 8), 2, 2);
        p.end();
        cache.insert(key, QIcon(pm));
    }
    return cache.value(key);
}

QWidget *makeScrollableDockContent(QWidget *content, QWidget *parent)
{
    auto *scroll = new QScrollArea(parent);
    scroll->setObjectName(QStringLiteral("bangOnProDockScroll"));
    scroll->setFrameShape(QFrame::NoFrame);
    scroll->setWidgetResizable(true);
    scroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAsNeeded);
    scroll->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
    scroll->setSizeAdjustPolicy(QAbstractScrollArea::AdjustIgnored);
    scroll->setMinimumSize(160, 120);
    if (content != nullptr) {
        scroll->setWidget(content);
    }
    return scroll;
}

bool fitWindowToScreen(QWidget *window)
{
    if (window == nullptr || window->isFullScreen()) {
        return false;
    }
    QScreen *screen = window->screen() != nullptr ? window->screen() : QGuiApplication::primaryScreen();
    if (screen == nullptr) {
        return false;
    }
    const QRect avail = screen->availableGeometry();
    const QRect frame = window->frameGeometry();
    const int extraW = frame.width() - window->width();
    const int extraH = frame.height() - window->height();
    const int maxW = avail.width() - extraW;
    const int maxH = avail.height() - extraH;
    if (window->width() <= maxW && window->height() <= maxH && avail.contains(frame.topLeft())) {
        return false;
    }
    const int w = qMax(qMin(window->width(), maxW), qMin(window->minimumSizeHint().width(), maxW));
    const int h = qMax(qMin(window->height(), maxH), qMin(window->minimumSizeHint().height(), maxH));
    window->resize(w, h);
    window->move(avail.topLeft());
    return true;
}

} // namespace BangOnPro
