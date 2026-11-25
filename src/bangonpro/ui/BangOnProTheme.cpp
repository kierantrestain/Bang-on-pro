/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "BangOnProTheme.h"

#include <QApplication>
#include <QStyle>
#include <QStyleFactory>

namespace BangOnPro {

QPalette createBangOnProPalette()
{
    QPalette pal;

    const QColor windowChrome(QString::fromLatin1(StudioPaletteTokens::kWindowChrome));
    const QColor panelSurface(QString::fromLatin1(StudioPaletteTokens::kPanelSurface));
    const QColor elevated(QString::fromLatin1(StudioPaletteTokens::kElevatedSurface));
    const QColor sunkenWell(QString::fromLatin1(StudioPaletteTokens::kSunkenWell));
    const QColor border(QString::fromLatin1(StudioPaletteTokens::kBorderHairline));
    const QColor textPrimary(QString::fromLatin1(StudioPaletteTokens::kTextPrimary));
    const QColor textSecondary(QString::fromLatin1(StudioPaletteTokens::kTextSecondary));
    const QColor textDisabled(QString::fromLatin1(StudioPaletteTokens::kTextDisabled));
    const QColor accentPrimary(QString::fromLatin1(StudioPaletteTokens::kAccentPrimary));
    const QColor accentSelection(QString::fromLatin1(StudioPaletteTokens::kAccentSelection));

    pal.setColor(QPalette::Window, panelSurface);
    pal.setColor(QPalette::WindowText, textPrimary);
    pal.setColor(QPalette::Base, sunkenWell);
    pal.setColor(QPalette::AlternateBase, panelSurface);
    pal.setColor(QPalette::ToolTipBase, elevated);
    pal.setColor(QPalette::ToolTipText, textPrimary);
    pal.setColor(QPalette::Text, textPrimary);
    pal.setColor(QPalette::Button, elevated);
    pal.setColor(QPalette::ButtonText, textPrimary);
    pal.setColor(QPalette::BrightText, QColor(255, 255, 255));
    pal.setColor(QPalette::Link, accentSelection);
    pal.setColor(QPalette::LinkVisited, accentPrimary);
    pal.setColor(QPalette::Highlight, accentSelection);
    pal.setColor(QPalette::HighlightedText, QColor(255, 255, 255));
    pal.setColor(QPalette::Light, border.lighter(115));
    pal.setColor(QPalette::Midlight, elevated);
    pal.setColor(QPalette::Mid, border);
    pal.setColor(QPalette::Dark, windowChrome);
    pal.setColor(QPalette::Shadow, QColor(4, 5, 8));

#if QT_VERSION >= QT_VERSION_CHECK(5, 12, 0)
    pal.setColor(QPalette::PlaceholderText, textSecondary);
#endif

    // Disabled color group
    pal.setColor(QPalette::Disabled, QPalette::WindowText, textDisabled);
    pal.setColor(QPalette::Disabled, QPalette::Text, textDisabled);
    pal.setColor(QPalette::Disabled, QPalette::ButtonText, textDisabled);
    pal.setColor(QPalette::Disabled, QPalette::Button, panelSurface);
    pal.setColor(QPalette::Disabled, QPalette::Highlight, border);
    pal.setColor(QPalette::Disabled, QPalette::HighlightedText, textSecondary);

    // Inactive selection stays muted yet legible
    pal.setColor(QPalette::Inactive, QPalette::Highlight, QColor(36, 74, 130));
    pal.setColor(QPalette::Inactive, QPalette::HighlightedText, textPrimary);

    return pal;
}

QString globalApplicationStyleSheet()
{
    const QString chrome = QString::fromLatin1(StudioPaletteTokens::kWindowChrome);
    const QString panel = QString::fromLatin1(StudioPaletteTokens::kPanelSurface);
    const QString elevated = QString::fromLatin1(StudioPaletteTokens::kElevatedSurface);
    const QString hover = QString::fromLatin1(StudioPaletteTokens::kElevatedHover);
    const QString well = QString::fromLatin1(StudioPaletteTokens::kSunkenWell);
    const QString hairline = QString::fromLatin1(StudioPaletteTokens::kBorderHairline);
    const QString activeBorder = QString::fromLatin1(StudioPaletteTokens::kBorderActive);
    const QString text = QString::fromLatin1(StudioPaletteTokens::kTextPrimary);
    const QString textMuted = QString::fromLatin1(StudioPaletteTokens::kTextSecondary);
    const QString textDisabled = QString::fromLatin1(StudioPaletteTokens::kTextDisabled);
    const QString accent = QString::fromLatin1(StudioPaletteTokens::kAccentPrimary);
    const QString accentHover = QString::fromLatin1(StudioPaletteTokens::kAccentPrimaryHover);
    const QString selection = QString::fromLatin1(StudioPaletteTokens::kAccentSelection);
    const QString green = QString::fromLatin1(StudioPaletteTokens::kStatusSuccess);
    const QString amber = QString::fromLatin1(StudioPaletteTokens::kStatusWarning);
    const QString red = QString::fromLatin1(StudioPaletteTokens::kStatusDanger);

    QString qss;
    qss.reserve(12288);

    // 1. Main window, dialogs, tooltips, and split handles
    qss += QStringLiteral(
               "QMainWindow, QDialog {"
               "  background-color: %1;"
               "  color: %2;"
               "}"
               "QMainWindow::separator {"
               "  background-color: %1;"
               "  width: 3px;"
               "  height: 3px;"
               "}"
               "QMainWindow::separator:hover {"
               "  background-color: %3;"
               "}"
               "QSplitter::handle {"
               "  background-color: %1;"
               "}"
               "QSplitter::handle:horizontal { width: 3px; }"
               "QSplitter::handle:vertical { height: 3px; }"
               "QSplitter::handle:hover {"
               "  background-color: %3;"
               "}"
               "QToolTip {"
               "  background-color: %4;"
               "  color: %2;"
               "  border: 1px solid %5;"
               "  border-radius: 3px;"
               "  padding: 4px 7px;"
               "  font-size: 11px;"
               "}")
               .arg(chrome, text, accent, elevated, activeBorder);

    // 2. Menu bar, context menus, toolbars, and status bar
    qss += QStringLiteral(
               "QMenuBar {"
               "  background-color: %1;"
               "  color: %2;"
               "  border-bottom: 1px solid %3;"
               "  spacing: 2px;"
               "  padding: 1px 4px;"
               "}"
               "QMenuBar::item {"
               "  background: transparent;"
               "  padding: 4px 8px;"
               "  border-radius: 3px;"
               "}"
               "QMenuBar::item:selected, QMenuBar::item:pressed {"
               "  background-color: %4;"
               "  color: #FFFFFF;"
               "}"
               "QMenu {"
               "  background-color: %5;"
               "  color: %2;"
               "  border: 1px solid %3;"
               "  padding: 4px 2px;"
               "}"
               "QMenu::item {"
               "  padding: 5px 24px 5px 14px;"
               "  border-radius: 3px;"
               "  margin: 1px 3px;"
               "}"
               "QMenu::item:selected {"
               "  background-color: %6;"
               "  color: #FFFFFF;"
               "}"
               "QMenu::item:disabled {"
               "  color: %7;"
               "}"
               "QMenu::separator {"
               "  height: 1px;"
               "  background-color: %3;"
               "  margin: 4px 8px;"
               "}"
               "QToolBar {"
               "  background-color: %4;"
               "  border: 0px;"
               "  border-bottom: 1px solid %3;"
               "  spacing: 4px;"
               "  padding: 3px 6px;"
               "}"
               "QToolBar::separator {"
               "  background-color: %3;"
               "  width: 1px;"
               "  height: 16px;"
               "  margin: 2px 4px;"
               "}"
               "QStatusBar {"
               "  background-color: %1;"
               "  color: %8;"
               "  border-top: 1px solid %3;"
               "  font-size: 11px;"
               "}"
               "QStatusBar::item {"
               "  border: none;"
               "}")
               .arg(chrome, text, hairline, elevated, panel, selection, textDisabled, textMuted);

    // 3. Studio Dock Widgets & DaVinci/Premiere Crisp Tab Bars
    qss += QStringLiteral(
               "QDockWidget {"
               "  background-color: %1;"
               "  color: %2;"
               "  titlebar-close-icon: none;"
               "  titlebar-normal-icon: none;"
               "}"
               "QDockWidget::title {"
               "  background-color: %3;"
               "  color: %4;"
               "  text-align: left;"
               "  padding: 5px 8px;"
               "  border-bottom: 1px solid %5;"
               "  font-weight: 600;"
               "  font-size: 11px;"
               "}"
               "QDockWidget::close-button, QDockWidget::float-button {"
               "  background-color: transparent;"
               "  border: 1px solid transparent;"
               "  border-radius: 3px;"
               "  padding: 2px;"
               "}"
               "QDockWidget::close-button:hover, QDockWidget::float-button:hover {"
               "  background-color: %6;"
               "  border-color: %5;"
               "}"
               "QTabWidget::pane {"
               "  background-color: %1;"
               "  border: 1px solid %5;"
               "  top: -1px;"
               "}"
               "QTabBar {"
               "  background-color: %7;"
               "  qproperty-drawBase: 0;"
               "}"
               "QTabBar::tab {"
               "  background-color: %7;"
               "  color: %4;"
               "  border: 0px;"
               "  border-right: 1px solid %5;"
               "  border-bottom: 2px solid transparent;"
               "  padding: 6px 12px;"
               "  font-size: 11px;"
               "  font-weight: 600;"
               "}"
               "QTabBar::tab:hover {"
               "  background-color: %3;"
               "  color: %2;"
               "}"
               "QTabBar::tab:selected {"
               "  background-color: %1;"
               "  color: #FFFFFF;"
               "  border-bottom: 2px solid %8;"
               "}")
               .arg(panel, text, elevated, textMuted, hairline, hover, chrome, accent);

    // 4. Views (Media Bin, Effects Browser, Keyframe Tables, Diff Trees, Inspector)
    qss += QStringLiteral(
               "QTreeView, QTreeWidget, QListView, QListWidget, QTableView, QTableWidget {"
               "  background-color: %1;"
               "  alternate-background-color: %2;"
               "  color: %3;"
               "  border: 1px solid %4;"
               "  border-radius: 4px;"
               "  gridline-color: %4;"
               "  selection-background-color: %5;"
               "  selection-color: #FFFFFF;"
               "  outline: 0;"
               "}"
               "QTreeView::item, QListView::item, QTableView::item {"
               "  padding: 3px 5px;"
               "}"
               "QTreeView::item:hover, QListView::item:hover, QTableView::item:hover {"
               "  background-color: %6;"
               "}"
               "QTreeView::item:selected, QListView::item:selected, QTableView::item:selected {"
               "  background-color: %5;"
               "  color: #FFFFFF;"
               "}"
               "QHeaderView::section {"
               "  background-color: %6;"
               "  color: %7;"
               "  border: 0px;"
               "  border-right: 1px solid %4;"
               "  border-bottom: 1px solid %4;"
               "  padding: 4px 7px;"
               "  font-size: 11px;"
               "  font-weight: 600;"
               "}"
               "QTableCornerButton::section {"
               "  background-color: %6;"
               "  border: 0px;"
               "  border-right: 1px solid %4;"
               "  border-bottom: 1px solid %4;"
               "}")
               .arg(well, panel, text, hairline, selection, elevated, textMuted);

    // 5. Minimal thin dark studio scrollbars
    qss += QStringLiteral(
               "QScrollBar:vertical {"
               "  background-color: %1;"
               "  width: 9px;"
               "  margin: 0px;"
               "}"
               "QScrollBar::handle:vertical {"
               "  background-color: %2;"
               "  min-height: 24px;"
               "  border-radius: 4px;"
               "  margin: 2px;"
               "}"
               "QScrollBar::handle:vertical:hover { background-color: %3; }"
               "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
               "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }"
               "QScrollBar:horizontal {"
               "  background-color: %1;"
               "  height: 9px;"
               "  margin: 0px;"
               "}"
               "QScrollBar::handle:horizontal {"
               "  background-color: %2;"
               "  min-width: 24px;"
               "  border-radius: 4px;"
               "  margin: 2px;"
               "}"
               "QScrollBar::handle:horizontal:hover { background-color: %3; }"
               "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }"
               "QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }")
               .arg(chrome, hairline, activeBorder);

    // 6. Inputs, SpinBoxes, ComboBoxes, Sliders, GroupBoxes, CheckBoxes, RadioButtons
    qss += QStringLiteral(
               "QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QKeySequenceEdit {"
               "  background-color: %1;"
               "  color: %2;"
               "  border: 1px solid %3;"
               "  border-radius: 4px;"
               "  padding: 4px 7px;"
               "  selection-background-color: %4;"
               "  selection-color: #FFFFFF;"
               "}"
               "QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QKeySequenceEdit:focus {"
               "  border-color: %4;"
               "}"
               "QLineEdit:disabled, QPlainTextEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {"
               "  background-color: %5;"
               "  color: %6;"
               "  border-color: %3;"
               "}"
               "QComboBox::drop-down {"
               "  subcontrol-origin: padding;"
               "  subcontrol-position: top right;"
               "  width: 20px;"
               "  border-left: 1px solid %3;"
               "  background-color: %7;"
               "  border-top-right-radius: 3px;"
               "  border-bottom-right-radius: 3px;"
               "}"
               "QComboBox QAbstractItemView {"
               "  background-color: %5;"
               "  color: %2;"
               "  border: 1px solid %8;"
               "  selection-background-color: %4;"
               "  selection-color: #FFFFFF;"
               "}"
               "QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {"
               "  background-color: %7;"
               "  border-left: 1px solid %3;"
               "  width: 16px;"
               "}"
               "QSpinBox::up-button:hover, QSpinBox::down-button:hover, QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {"
               "  background-color: %9;"
               "}")
               .arg(well, text, hairline, selection, panel, textDisabled, elevated, activeBorder, hover);

    qss += QStringLiteral(
               "QSlider::groove:horizontal {"
               "  background-color: %1;"
               "  border: 1px solid %2;"
               "  height: 4px;"
               "  border-radius: 2px;"
               "}"
               "QSlider::sub-page:horizontal {"
               "  background-color: %3;"
               "  border-radius: 2px;"
               "}"
               "QSlider::handle:horizontal {"
               "  background-color: #E8ECF2;"
               "  border: 1px solid %4;"
               "  width: 12px;"
               "  height: 12px;"
               "  margin: -5px 0px;"
               "  border-radius: 6px;"
               "}"
               "QSlider::handle:horizontal:hover {"
               "  background-color: #FFFFFF;"
               "  border-color: %3;"
               "}"
               "QGroupBox {"
               "  background-color: %5;"
               "  border: 1px solid %2;"
               "  border-radius: 5px;"
               "  margin-top: 10px;"
               "  padding: 10px 8px 8px 8px;"
               "  font-weight: 600;"
               "  color: %6;"
               "}"
               "QGroupBox::title {"
               "  subcontrol-origin: margin;"
               "  subcontrol-position: top left;"
               "  left: 10px;"
               "  padding: 0px 4px;"
               "  color: %7;"
               "}"
               "QCheckBox, QRadioButton {"
               "  color: %6;"
               "  spacing: 6px;"
               "}"
               "QCheckBox::indicator, QRadioButton::indicator {"
               "  width: 14px;"
               "  height: 14px;"
               "  background-color: %1;"
               "  border: 1px solid %4;"
               "}"
               "QCheckBox::indicator { border-radius: 3px; }"
               "QRadioButton::indicator { border-radius: 7px; }"
               "QCheckBox::indicator:checked {"
               "  background-color: %3;"
               "  border-color: %3;"
               "}"
               "QRadioButton::indicator:checked {"
               "  background-color: %8;"
               "  border: 3px solid %1;"
               "}")
               .arg(well, hairline, accent, activeBorder, panel, text, textMuted, selection);

    // 7. Buttons, ToolButtons, ProgressBars, and Studio Role Properties
    qss += QStringLiteral(
               "QPushButton, QToolButton {"
               "  background-color: %1;"
               "  color: %2;"
               "  border: 1px solid %3;"
               "  border-radius: 4px;"
               "  padding: 4px 8px;"
               "  font-size: 11px;"
               "  font-weight: 500;"
               "}"
               "QPushButton:hover, QToolButton:hover {"
               "  background-color: %4;"
               "  border-color: %5;"
               "  color: #FFFFFF;"
               "}"
               "QPushButton:focus, QToolButton:focus { border-color: %7; }"
               "QPushButton:pressed, QToolButton:pressed {"
               "  background-color: %6;"
               "}"
               "QPushButton:checked, QToolButton:checked {"
               "  background-color: #1D3557;"
               "  border-color: %7;"
               "  color: #FFFFFF;"
               "}"
               "QPushButton:disabled, QToolButton:disabled {"
               "  background-color: %8;"
               "  color: %9;"
               "  border-color: %3;"
               "}")
               .arg(elevated, text, hairline, hover, activeBorder, well, selection, panel, textDisabled);

    qss += QStringLiteral(
               "QPushButton[bangOnProPrimary=\"true\"] {"
               "  background-color: %1;"
               "  border-color: %1;"
               "  color: #161616;"
               "  font-weight: 600;"
               "}"
               "QPushButton[bangOnProPrimary=\"true\"]:hover {"
               "  background-color: %2;"
               "  border-color: %2;"
               "}"
               "QPushButton[bangOnProAccept=\"true\"] {"
               "  background-color: %3;"
               "  border-color: %3;"
               "  color: #161616;"
               "  font-weight: 600;"
               "}"
               "QPushButton[bangOnProAccept=\"true\"]:hover {"
               "  background-color: #28BA7C;"
               "  border-color: #28BA7C;"
               "}"
               "QPushButton[bangOnProDanger=\"true\"]:hover {"
               "  background-color: %4;"
               "  border-color: %4;"
               "  color: #FFFFFF;"
               "}"
               "QPushButton[bangOnProWarning=\"true\"] {"
               "  background-color: #4A3506;"
               "  border-color: %5;"
               "  color: #FFD57A;"
               "}"
               "QProgressBar {"
               "  background-color: %6;"
               "  border: 1px solid %7;"
               "  border-radius: 3px;"
               "  text-align: center;"
               "  color: %8;"
               "  font-size: 10px;"
               "}"
               "QProgressBar::chunk {"
               "  background-color: #A43D17;"
               "  border-radius: 2px;"
               "}")
               .arg(accent, accentHover, green, red, amber, well, hairline, text);

    return qss;
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

} // namespace BangOnPro
