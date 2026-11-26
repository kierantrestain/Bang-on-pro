/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "BangOnProAutotest.h"
#include "BangOnProIntegration.h"
#include "ui/AgentControlCentre.h"

#include "bin/bin.h"
#include "bin/model/markerlistmodel.hpp"
#include "bin/projectclip.h"
#include "bin/projectitemmodel.h"
#include "core.h"
#include "doc/docundostack.hpp"
#include "doc/kdenlivedoc.h"
#include "project/projectmanager.h"
#include "timeline2/model/timelineitemmodel.hpp"

#include <QAbstractButton>
#include <QApplication>
#include <QCheckBox>
#include <QComboBox>
#include <QDateTime>
#include <QDialog>
#include <QDir>
#include <QDockWidget>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QLineEdit>
#include <QMainWindow>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QProcess>
#include <QTimer>
#include <QUrl>

namespace BangOnPro {

namespace {
QWidget *findObject(QMainWindow *w, const QString &name)
{
    if (!w) {
        return nullptr;
    }
    for (QWidget *top : QApplication::topLevelWidgets()) {
        if (top->objectName() == name) {
            return top;
        }
        if (QWidget *c = top->findChild<QWidget *>(name)) {
            return c;
        }
    }
    return nullptr;
}

AgentControlCentre *panel(QMainWindow *w)
{
    return w ? w->findChild<AgentControlCentre *>() : nullptr;
}
} // namespace

void BangOnProAutotest::startIfRequested(QMainWindow *window, BangOnProIntegration *integration)
{
    const QString script = qEnvironmentVariable("BANGONPRO_AUTOTEST");
    if (!script.isEmpty() && QFileInfo::exists(script)) {
        new BangOnProAutotest(window, integration, script, integration);
    }
}

BangOnProAutotest::BangOnProAutotest(QMainWindow *window, BangOnProIntegration *integration, const QString &scriptPath, QObject *parent)
    : QObject(parent)
    , m_window(window)
    , m_integration(integration)
    , m_scriptPath(scriptPath)
{
    QFile f(scriptPath);
    if (f.open(QIODevice::ReadOnly)) {
        m_steps = QJsonDocument::fromJson(f.readAll()).object().value(QStringLiteral("steps")).toArray();
    }
    m_timer = new QTimer(this);
    m_timer->setSingleShot(true);
    connect(m_timer, &QTimer::timeout, this, &BangOnProAutotest::next);
    // Modal dialogs (first-launch System check, the native New Sequence dialog…) are accepted like an editor
    // pressing the default button, so a script never blocks on one.
    m_dialogTimer = new QTimer(this);
    m_dialogTimer->setInterval(400);
    connect(m_dialogTimer, &QTimer::timeout, this, [this]() {
        if (auto *dlg = qobject_cast<QDialog *>(QApplication::activeModalWidget())) {
            m_log.append(QJsonObject{{QStringLiteral("dialog_accepted"), dlg->windowTitle()},
                                     {QStringLiteral("class"), QString::fromLatin1(dlg->metaObject()->className())}});
            if (auto *box = qobject_cast<QMessageBox *>(dlg)) {
                if (QAbstractButton *def = box->defaultButton()) {
                    def->click();
                    return;
                }
            }
            dlg->accept();
        }
    });
    m_dialogTimer->start();
    m_timer->start(qEnvironmentVariableIntValue("BANGONPRO_AUTOTEST_DELAY_MS") > 0 ? qEnvironmentVariableIntValue("BANGONPRO_AUTOTEST_DELAY_MS") : 4000);
}

void BangOnProAutotest::next()
{
    if (!m_waiting.isEmpty()) {
        if (pollWait()) {
            m_log.append(QJsonObject{{QStringLiteral("step"), m_index}, {QStringLiteral("ok"), true}, {QStringLiteral("waited"), m_waiting}});
            m_waiting = QJsonObject();
        } else if (QDateTime::currentMSecsSinceEpoch() > m_waitDeadline) {
            m_log.append(QJsonObject{{QStringLiteral("step"), m_index},
                                     {QStringLiteral("ok"), false},
                                     {QStringLiteral("error"), QStringLiteral("timed out")},
                                     {QStringLiteral("waited"), m_waiting},
                                     {QStringLiteral("state"), snapshot()}});
            m_failed = true;
            m_waiting = QJsonObject();
            if (!m_steps.at(m_index).toObject().value(QStringLiteral("optional")).toBool()) {
                finish(false, QStringLiteral("wait timed out at step %1").arg(m_index));
                return;
            }
        } else {
            m_timer->start(250);
            return;
        }
    }
    ++m_index;
    if (m_index >= m_steps.size()) {
        finish(!m_failed, QStringLiteral("script complete"));
        return;
    }
    const QJsonObject step = m_steps.at(m_index).toObject();
    QString err;
    const bool ok = runStep(step, &err);
    if (!m_waiting.isEmpty()) {
        m_timer->start(250);
        return;
    }
    m_log.append(QJsonObject{{QStringLiteral("step"), m_index},
                             {QStringLiteral("do"), step.value(QStringLiteral("do"))},
                             {QStringLiteral("ok"), ok},
                             {QStringLiteral("error"), err}});
    if (!ok) {
        m_failed = true;
        if (!step.value(QStringLiteral("optional")).toBool()) {
            finish(false, err);
            return;
        }
    }
    m_timer->start(step.value(QStringLiteral("after_ms")).toInt(300));
}

bool BangOnProAutotest::runStep(const QJsonObject &step, QString *error)
{
    const QString op = step.value(QStringLiteral("do")).toString();
    const QString object = step.value(QStringLiteral("object")).toString();
    auto need = [&](QWidget *w) {
        if (!w) {
            *error = QStringLiteral("no widget '%1'").arg(object);
        }
        return w;
    };
    if (op == QLatin1String("click")) {
        auto *b = qobject_cast<QAbstractButton *>(need(findObject(m_window, object)));
        if (!b) {
            return false;
        }
        if (!b->isEnabled()) {
            *error = QStringLiteral("'%1' is disabled").arg(object);
            return false;
        }
        b->click(); // the real button: its clicked() signal runs the production slot
        return true;
    }
    if (op == QLatin1String("set_text")) {
        QWidget *w = need(findObject(m_window, object));
        if (auto *pe = qobject_cast<QPlainTextEdit *>(w)) {
            pe->setPlainText(step.value(QStringLiteral("text")).toString());
        } else if (auto *le = qobject_cast<QLineEdit *>(w)) {
            le->setText(step.value(QStringLiteral("text")).toString());
        } else {
            return false;
        }
        return true;
    }
    if (op == QLatin1String("set_check")) {
        auto *c = qobject_cast<QAbstractButton *>(need(findObject(m_window, object)));
        if (!c) {
            return false;
        }
        c->setChecked(step.value(QStringLiteral("checked")).toBool());
        return true;
    }
    if (op == QLatin1String("select")) {
        auto *c = qobject_cast<QComboBox *>(need(findObject(m_window, object)));
        if (!c) {
            return false;
        }
        const int idx = c->findText(step.value(QStringLiteral("text")).toString(), Qt::MatchContains);
        if (idx < 0) {
            *error = QStringLiteral("no item '%1'").arg(step.value(QStringLiteral("text")).toString());
            return false;
        }
        c->setCurrentIndex(idx);
        Q_EMIT c->activated(idx);
        return true;
    }
    if (op == QLatin1String("raise_dock")) {
        for (QDockWidget *d : m_window->findChildren<QDockWidget *>()) {
            if (d->windowTitle() == step.value(QStringLiteral("title")).toString()) {
                d->show();
                d->raise();
                return true;
            }
        }
        *error = QStringLiteral("no dock '%1'").arg(step.value(QStringLiteral("title")).toString());
        return false;
    }
    if (op == QLatin1String("import")) {
        QList<QUrl> urls;
        for (const QJsonValue &v : step.value(QStringLiteral("paths")).toArray()) {
            urls << QUrl::fromLocalFile(v.toString());
        }
        pCore->bin()->droppedUrls(urls); // what dragging files from Finder into the Project Bin does
        return true;
    }
    if (op == QLatin1String("select_bin_all")) {
        pCore->bin()->selectAll();
        return true;
    }
    if (op == QLatin1String("open_project")) {
        pCore->projectManager()->openFile(QUrl::fromLocalFile(step.value(QStringLiteral("path")).toString()));
        return true;
    }
    if (op == QLatin1String("save_project_as")) {
        return pCore->projectManager()->saveFileAs(step.value(QStringLiteral("path")).toString(), true);
    }
    if (op == QLatin1String("wait_ms")) {
        m_waiting = QJsonObject{{QStringLiteral("until_ms"), QDateTime::currentMSecsSinceEpoch() + step.value(QStringLiteral("ms")).toInt(1000)}};
        m_waitDeadline = QDateTime::currentMSecsSinceEpoch() + step.value(QStringLiteral("ms")).toInt(1000) + 1000;
        return true;
    }
    if (op == QLatin1String("wait_job") || op == QLatin1String("wait_object")) {
        m_waiting = step;
        m_waitDeadline = QDateTime::currentMSecsSinceEpoch() + step.value(QStringLiteral("timeout_ms")).toInt(180000);
        return true;
    }
    if (op == QLatin1String("screenshot")) {
        QWidget *target = object.isEmpty() ? static_cast<QWidget *>(m_window) : need(findObject(m_window, object));
        if (!target) {
            return false;
        }
        const QString path = step.value(QStringLiteral("path")).toString();
        QDir().mkpath(QFileInfo(path).absolutePath());
        return target->grab().save(path);
    }
    if (op == QLatin1String("resize_window")) {
        m_window->showNormal();
        m_window->resize(step.value(QStringLiteral("w")).toInt(1470), step.value(QStringLiteral("h")).toInt(956));
        return true;
    }
    if (op == QLatin1String("record")) {
        m_records.insert(step.value(QStringLiteral("as")).toString(QString::number(m_index)), snapshot());
        return true;
    }
    if (op == QLatin1String("expect")) {
        const QJsonObject s = snapshot();
        const QString key = step.value(QStringLiteral("key")).toString();
        const QJsonValue actual = s.value(key);
        bool ok = true;
        if (step.contains(QStringLiteral("equals"))) {
            ok = actual == step.value(QStringLiteral("equals"));
        }
        if (step.contains(QStringLiteral("min"))) {
            ok = ok && actual.toDouble() >= step.value(QStringLiteral("min")).toDouble();
        }
        if (step.contains(QStringLiteral("contains"))) {
            ok = ok && QString::fromUtf8(QJsonDocument(QJsonObject{{QStringLiteral("v"), actual}}).toJson(QJsonDocument::Compact))
                           .contains(step.value(QStringLiteral("contains")).toString());
        }
        if (!ok) {
            *error = QStringLiteral("expected %1 %2, got %3")
                         .arg(key, QString::fromUtf8(QJsonDocument(step).toJson(QJsonDocument::Compact)),
                              QString::fromUtf8(QJsonDocument(QJsonObject{{QStringLiteral("v"), actual}}).toJson(QJsonDocument::Compact)));
        }
        return ok;
    }
    if (op == QLatin1String("undo")) {
        if (pCore->undoStack() && pCore->undoStack()->canUndo()) {
            pCore->undoStack()->undo();
            return true;
        }
        *error = QStringLiteral("nothing to undo");
        return false;
    }
    if (op == QLatin1String("kill_department")) {
        // Simulates a crash of the production process (SIGKILL: nothing can be recorded by the worker itself).
        if (QProcess *p = m_integration->departmentProcess()) {
            p->kill();
            return true;
        }
        *error = QStringLiteral("no department process running");
        return false;
    }
    if (op == QLatin1String("accept_dialogs")) {
        step.value(QStringLiteral("on")).toBool(true) ? m_dialogTimer->start() : m_dialogTimer->stop();
        return true;
    }
    if (op == QLatin1String("quit")) {
        finish(!m_failed, QStringLiteral("quit requested"));
        return true;
    }
    *error = QStringLiteral("unknown step '%1'").arg(op);
    return false;
}

bool BangOnProAutotest::pollWait()
{
    if (m_waiting.contains(QStringLiteral("until_ms"))) {
        return QDateTime::currentMSecsSinceEpoch() >= qint64(m_waiting.value(QStringLiteral("until_ms")).toDouble());
    }
    if (m_waiting.value(QStringLiteral("do")).toString() == QLatin1String("wait_object")) {
        QWidget *w = findObject(m_window, m_waiting.value(QStringLiteral("object")).toString());
        return w && w->isVisible() && (!m_waiting.value(QStringLiteral("enabled")).toBool() || w->isEnabled());
    }
    AgentControlCentre *acc = panel(m_window);
    if (!acc || acc->activeJobDir().isEmpty() || m_integration->departmentProcess()) {
        return false;
    }
    const QString status = acc->jobStatus();
    const QJsonArray wanted = m_waiting.value(QStringLiteral("status")).toArray();
    if (wanted.isEmpty()) {
        return status != QLatin1String("running") && status != QLatin1String("planned");
    }
    for (const QJsonValue &v : wanted) {
        if (status == v.toString()) {
            return true;
        }
    }
    return false;
}

QJsonObject BangOnProAutotest::snapshot() const
{
    QJsonObject s;
    if (AgentControlCentre *acc = panel(m_window)) {
        s.insert(QStringLiteral("job_dir"), acc->activeJobDir());
        s.insert(QStringLiteral("job_status"), acc->jobStatus());
        s.insert(QStringLiteral("stages"), QJsonArray::fromStringList(acc->stageStates()));
        s.insert(QStringLiteral("status_text"), acc->statusText());
        s.insert(QStringLiteral("reasoning_text"), acc->reasoningText());
        s.insert(QStringLiteral("problem_text"), acc->problemText());
        s.insert(QStringLiteral("completion_visible"), acc->completionVisible());
        s.insert(QStringLiteral("review_items"), acc->reviewItemCount());
        s.insert(QStringLiteral("project_context"), acc->projectContextText());
    }
    s.insert(QStringLiteral("department_running"), m_integration->departmentProcess() != nullptr);
    if (pCore && pCore->currentDoc() && pCore->projectItemModel()) {
        const QUuid current = pCore->currentTimelineId();
        QJsonArray seqs;
        const QMap<QUuid, QString> all = pCore->projectItemModel()->getAllSequenceClips();
        for (auto it = all.constBegin(); it != all.constEnd(); ++it) {
            QJsonObject q;
            const auto clip = pCore->projectItemModel()->getClipByBinID(it.value());
            q.insert(QStringLiteral("name"), clip ? clip->clipName() : QString());
            q.insert(QStringLiteral("current"), it.key() == current);
            if (const auto tl = pCore->currentDoc()->getTimeline(it.key(), true)) {
                int clips = 0;
                int titles = 0;
                for (bool audio : {false, true}) {
                    for (int tid : tl->getTracksIds(audio)) {
                        for (int item : tl->getItemsInRange(tid, 0, -1, false)) {
                            ++clips;
                            const auto c = pCore->projectItemModel()->getClipByBinID(tl->getClipBinId(item));
                            titles += (c && c->clipType() == ClipType::Text) ? 1 : 0;
                        }
                    }
                }
                q.insert(QStringLiteral("timeline_items"), clips);
                q.insert(QStringLiteral("title_clips"), titles);
                q.insert(QStringLiteral("video_tracks"), tl->getTracksIds(false).size());
                q.insert(QStringLiteral("audio_tracks"), tl->getTracksIds(true).size());
                q.insert(QStringLiteral("duration_frames"), tl->duration());
                q.insert(QStringLiteral("guides"), tl->getGuideModel()->getAllMarkers().size());
            }
            seqs.append(q);
        }
        s.insert(QStringLiteral("sequences"), seqs);
        s.insert(QStringLiteral("sequence_count"), seqs.size());
        s.insert(QStringLiteral("bin_clips"), int(pCore->projectItemModel()->getAllClipIds().size()));
        s.insert(QStringLiteral("project_url"), pCore->currentDoc()->url().toLocalFile());
        s.insert(QStringLiteral("can_undo"), pCore->undoStack() && pCore->undoStack()->canUndo());
    }
    if (m_window) {
        s.insert(QStringLiteral("window_w"), m_window->width());
        s.insert(QStringLiteral("window_h"), m_window->height());
    }
    return s;
}

void BangOnProAutotest::finish(bool ok, const QString &why)
{
    m_timer->stop();
    m_dialogTimer->stop();
    QJsonObject result{{QStringLiteral("ok"), ok},
                       {QStringLiteral("why"), why},
                       {QStringLiteral("log"), m_log},
                       {QStringLiteral("records"), m_records},
                       {QStringLiteral("final"), snapshot()},
                       {QStringLiteral("finished_at"), QDateTime::currentDateTimeUtc().toString(Qt::ISODate)}};
    QFile out(m_scriptPath + QStringLiteral(".result.json"));
    if (out.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        out.write(QJsonDocument(result).toJson());
    }
    if (pCore && pCore->currentDoc()) {
        pCore->currentDoc()->setModified(false); // never block quitting on a "save changes?" prompt
    }
    QTimer::singleShot(200, qApp, [ok]() { QCoreApplication::exit(ok ? 0 : 1); });
}

} // namespace BangOnPro
