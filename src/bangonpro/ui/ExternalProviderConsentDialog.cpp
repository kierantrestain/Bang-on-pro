/*
    SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
    SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
*/

#include "ExternalProviderConsentDialog.h"
#include "BangOnProTheme.h"

#include <QCheckBox>
#include <QDateTime>
#include <QDesktopServices>
#include <QFormLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLabel>
#include <QLocale>

#include <QProcess>
#include <QPushButton>
#include <QRadioButton>
#include <QScrollArea>
#include <QSettings>
#include <QSpinBox>
#include <QUrl>
#include <QVBoxLayout>
#include <utility>

namespace BangOnPro {
namespace {

QString displayTime(const QJsonValue &value)
{
    const QDateTime time = QDateTime::fromString(value.toString(), Qt::ISODate);
    return time.isValid() ? QLocale().toString(time.toLocalTime(), QLocale::ShortFormat) : QString();
}

QLabel *statusLabel(QWidget *parent)
{
    auto *label = new QLabel(parent);
    label->setTextFormat(Qt::PlainText);
    label->setWordWrap(true);
    label->setObjectName(QStringLiteral("bangOnProMutedLabel"));
    return label;
}

} // namespace

ExternalProviderConsentDialog::ExternalProviderConsentDialog(QWidget *parent, StartPython startPython, const QString &settingsOrg, const QString &settingsApp)
    : QDialog(parent)
    , m_startPython(std::move(startPython))
    , m_settingsOrg(settingsOrg)
    , m_settingsApp(settingsApp)
    , m_priority{QStringLiteral("claude"), QStringLiteral("codex"), QStringLiteral("grok"), QStringLiteral("agy")}
{
    setObjectName(QStringLiteral("bangOnProExternalProviderConsentDialog"));
    setWindowTitle(tr("AI Providers & Subscriptions"));
    resize(760, 770);
    auto *root = new QVBoxLayout(this);
    auto *scroll = new QScrollArea(this);
    scroll->setWidgetResizable(true);
    scroll->setFrameShape(QFrame::NoFrame);
    auto *content = new QWidget(scroll);
    auto *layout = new QVBoxLayout(content);
    scroll->setWidget(content);
    root->addWidget(scroll);

    auto *privacy = new QGroupBox(tr("Processing and privacy"), content);
    auto *privacyLayout = new QVBoxLayout(privacy);
    m_local = new QRadioButton(tr("Local Only"), privacy);
    m_connected = new QRadioButton(tr("Connected Subscriptions"), privacy);
    m_local->setObjectName(QStringLiteral("localOnlyModeRadio"));
    m_connected->setObjectName(QStringLiteral("connectedSubscriptionsRadio"));
    privacyLayout->addWidget(m_local);
    privacyLayout->addWidget(m_connected);
    auto *privacyHelp = statusLabel(privacy);
    privacyHelp->setText(tr("Media processing stays on this computer. Connected reasoning sends editing instructions as text to an authorised subscription. "
                            "Your official client handles account sign-in separately."));
    privacyLayout->addWidget(privacyHelp);
    m_consent = new QCheckBox(tr("Allow external reasoning for my editing instructions"), privacy);
    m_consent->setObjectName(QStringLiteral("explicitConsentCheckBox"));
    setTone(m_consent, "warning");
    m_metadata = new QCheckBox(tr("Include project context such as cut times, camera labels and graphics"), privacy);
    m_transcript = new QCheckBox(tr("Include transcript excerpts with project context"), privacy);
    privacyLayout->addWidget(m_consent);
    privacyLayout->addWidget(m_metadata);
    privacyLayout->addWidget(m_transcript);
    layout->addWidget(privacy);

    auto *providers = new QGroupBox(tr("Subscription providers"), content);
    m_providers = new QVBoxLayout(providers);
    const QStringList titles{tr("Claude Code"), tr("OpenAI Codex"), tr("Grok Build"), tr("Google Antigravity")};
    const QStringList accounts{tr("Anthropic account · claude"), tr("ChatGPT account · codex"), tr("Eligible Grok account · grok"),
                               tr("Eligible Google account · agy")};
    for (int i = 0; i < m_priority.size(); ++i) {
        const QString id = m_priority.at(i);
        ProviderRow row;
        row.widget = new QWidget(providers);
        row.widget->setObjectName(QStringLiteral("subscriptionProvider_") + id);
        auto *section = new QVBoxLayout(row.widget);
        section->setContentsMargins(0, 3, 0, 3);
        auto *heading = new QHBoxLayout;
        auto *name = new QLabel(tr("%1 · %2").arg(titles.at(i), accounts.at(i)), row.widget);
        heading->addWidget(name, 1);
        row.up = new QPushButton(tr("↑"), row.widget);
        row.down = new QPushButton(tr("↓"), row.widget);
        row.up->setAccessibleName(tr("Move %1 up").arg(titles.at(i)));
        row.down->setAccessibleName(tr("Move %1 down").arg(titles.at(i)));
        heading->addWidget(row.up);
        heading->addWidget(row.down);
        section->addLayout(heading);
        row.status = statusLabel(row.widget);
        row.status->setText(tr("Checking installation…"));
        section->addWidget(row.status);
        auto *buttons = new QHBoxLayout;
        row.connect = new QPushButton(tr("Connect"), row.widget);
        row.check = new QPushButton(tr("Check sign-in"), row.widget);
        row.test = new QPushButton(tr("Test"), row.widget);
        row.disconnect = new QPushButton(tr("Disconnect"), row.widget);
        row.install = new QPushButton(tr("Install instructions"), row.widget);
        for (auto *button : {row.connect, row.check, row.test, row.disconnect, row.install}) {
            buttons->addWidget(button);
        }
        buttons->addStretch();
        section->addLayout(buttons);
        connect(row.up, &QPushButton::clicked, this, [this, id] { moveProvider(id, -1); });
        connect(row.down, &QPushButton::clicked, this, [this, id] { moveProvider(id, 1); });
        connect(row.connect, &QPushButton::clicked, this,
                [this, id] { run({QStringLiteral("--client"), id, QStringLiteral("--connect"), QStringLiteral("--check-auth")}); });
        connect(row.check, &QPushButton::clicked, this,
                [this, id] { run({QStringLiteral("--client"), id, QStringLiteral("--check-sign-in"), QStringLiteral("--check-auth")}); });
        connect(row.test, &QPushButton::clicked, this,
                [this, id] { run({QStringLiteral("--client"), id, QStringLiteral("--test"), QStringLiteral("--check-auth")}); });
        connect(row.disconnect, &QPushButton::clicked, this,
                [this, id] { run({QStringLiteral("--client"), id, QStringLiteral("--disconnect"), QStringLiteral("--check-auth")}); });
        connect(row.install, &QPushButton::clicked, this, [this, id] {
            const QUrl url(m_status.value(QStringLiteral("providers")).toObject().value(id).toObject().value(QStringLiteral("install_url")).toString());
            if (url.scheme() == QStringLiteral("https")) {
                QDesktopServices::openUrl(url);
            }
        });
        m_providers->addWidget(row.widget);
        m_rows.insert(id, row);
    }
    layout->addWidget(providers);

    auto *routing = new QGroupBox(tr("Routing and usage limits"), content);
    auto *routingLayout = new QFormLayout(routing);
    m_failover = new QCheckBox(tr("Switch automatically when the preferred provider is unavailable"), routing);
    m_failover->setChecked(true);
    m_taskLimit = new QSpinBox(routing);
    m_taskLimit->setRange(1, 1000000);
    m_taskLimit->setValue(3);
    m_dayLimit = new QSpinBox(routing);
    m_dayLimit->setRange(0, 1000000);
    m_dayLimit->setSpecialValueText(tr("Unlimited"));
    routingLayout->addRow(m_failover);
    routingLayout->addRow(tr("Maximum reasoning calls per task"), m_taskLimit);
    routingLayout->addRow(tr("Maximum reasoning calls per day"), m_dayLimit);
    m_routing = statusLabel(routing);
    routingLayout->addRow(m_routing);
    layout->addWidget(routing);

    m_feedback = statusLabel(this);
    root->addWidget(m_feedback);
    auto *footer = new QHBoxLayout;
    m_refresh = new QPushButton(tr("Refresh status"), this);
    auto *close = new QPushButton(tr("Close"), this);
    m_save = new QPushButton(tr("Save settings"), this);
    m_save->setObjectName(QStringLiteral("saveProviderSettingsButton"));
    m_save->setProperty("bangOnProPrimary", true);
    footer->addWidget(m_refresh);
    footer->addStretch();
    footer->addWidget(close);
    footer->addWidget(m_save);
    root->addLayout(footer);

    connect(m_local, &QRadioButton::toggled, this, &ExternalProviderConsentDialog::syncControls);
    connect(m_consent, &QCheckBox::toggled, this, &ExternalProviderConsentDialog::syncControls);
    connect(m_metadata, &QCheckBox::toggled, this, &ExternalProviderConsentDialog::syncControls);
    connect(m_refresh, &QPushButton::clicked, this, [this] { run({QStringLiteral("--check-auth")}, !m_loaded); });
    connect(m_save, &QPushButton::clicked, this, &ExternalProviderConsentDialog::save);
    connect(close, &QPushButton::clicked, this, &QDialog::reject);

    m_local->setChecked(true);
    run({QStringLiteral("--check-auth")}, true);
}

ExternalProviderSettings ExternalProviderConsentDialog::currentSettings() const
{
    return {m_connected->isChecked() ? AiBackendMode::ExternalProvider : AiBackendMode::LocalOnly, m_transcript->isChecked(), m_metadata->isChecked(),
            m_consent->isChecked()};
}

bool ExternalProviderConsentDialog::canAcceptConfiguration() const
{
    return m_local->isChecked() || m_consent->isChecked();
}

void ExternalProviderConsentDialog::setBackendMode(AiBackendMode mode)
{
    (mode == AiBackendMode::LocalOnly ? m_local : m_connected)->setChecked(true);
}

void ExternalProviderConsentDialog::setExplicitConsentGranted(bool granted)
{
    m_consent->setChecked(granted);
}

void ExternalProviderConsentDialog::syncControls()
{
    m_consent->setEnabled(m_connected->isChecked());
    m_metadata->setEnabled(m_connected->isChecked());
    m_transcript->setEnabled(m_connected->isChecked() && m_metadata->isChecked());
    m_refresh->setEnabled(!m_busy && bool(m_startPython));
    m_save->setEnabled(m_loaded && !m_busy && canAcceptConfiguration());
    const QJsonObject privacy = m_status.value(QStringLiteral("privacy")).toObject();
    const bool testingAllowed =
        privacy.value(QStringLiteral("mode")).toString() == QStringLiteral("connected_subscriptions") && privacy.value(QStringLiteral("consent")).toBool();
    const auto providers = m_status.value(QStringLiteral("providers")).toObject();
    for (int i = 0; i < m_priority.size(); ++i) {
        const QString &id = m_priority.at(i);
        const auto &row = m_rows[id];
        const auto state = providers.value(id).toObject();
        const bool ready = m_loaded && !m_busy;
        const bool installed = state.value(QStringLiteral("installed")).toBool();
        const bool authorised = state.value(QStringLiteral("authorised")).toBool();
        const bool account = state.value(QStringLiteral("auth")).toString() == QStringLiteral("account");
        row.connect->setEnabled(ready && installed && (!authorised || !account));
        row.check->setEnabled(ready && installed);
        row.test->setEnabled(ready && installed && authorised && testingAllowed && state.value(QStringLiteral("test_supported")).toBool());
        row.test->setToolTip(tr("Requires saved Connected Subscriptions consent and uses one reasoning call if eligible."));
        row.disconnect->setEnabled(ready && authorised);
        row.install->setVisible(!installed);
        row.install->setEnabled(ready);
        row.up->setEnabled(ready && i > 0);
        row.down->setEnabled(ready && i + 1 < m_priority.size());
    }
}

void ExternalProviderConsentDialog::run(const QStringList &args, bool reloadSettings, bool closeOnSuccess)
{
    if (!m_startPython || m_busy) {
        syncControls();
        return;
    }
    auto *process = m_startPython(QStringList{QStringLiteral("-m"), QStringLiteral("bangonpro.ai_engine.mcp"), QStringLiteral("providers")} + args);
    if (!process) {
        m_feedback->setText(tr("The bundled reasoning runtime could not be found."));
        return;
    }
    m_busy = true;
    m_feedback->setText(tr("Checking subscription settings…"));
    syncControls();
    connect(process, &QProcess::errorOccurred, this, [this, process](QProcess::ProcessError error) {
        if (error == QProcess::FailedToStart) {
            process->deleteLater();
            m_busy = false;
            m_feedback->setText(tr("The bundled reasoning runtime could not start."));
            syncControls();
        }
    });
    connect(process, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
            [this, process, reloadSettings, closeOnSuccess](int code, QProcess::ExitStatus status) {
                const auto result = QJsonDocument::fromJson(process->readAllStandardOutput()).object();
                process->deleteLater();
                m_busy = false;
                if (status != QProcess::NormalExit || code != 0 || !result.contains(QStringLiteral("providers"))) {
                    m_feedback->setText(
                        result.value(QStringLiteral("error")).toString(tr("The reasoning runtime could not read or save the subscription settings.")));
                    syncControls();
                    return;
                }
                showStatus(result, reloadSettings);
                if (closeOnSuccess) {
                    QSettings legacy(m_settingsOrg, m_settingsApp);
                    for (const QString &key : {QStringLiteral("provider_name"), QStringLiteral("endpoint_url"), QStringLiteral("model_name")}) {
                        legacy.remove(key);
                    }
                    const bool connected = currentSettings().isExternalTransmissionAllowed();
                    Q_EMIT settingsSaved(connected);
                    accept();
                }
            });
}

void ExternalProviderConsentDialog::showStatus(const QJsonObject &status, bool reloadSettings)
{
    m_loaded = true;
    m_status = status;
    const auto privacy = status.value(QStringLiteral("privacy")).toObject();
    const auto budget = status.value(QStringLiteral("budget")).toObject();
    if (reloadSettings) {
        if (privacy.isEmpty()) {
            // Legacy consent named an API endpoint. Retain the data preferences and ask for consent to the new flow.
            const QSettings legacy(m_settingsOrg, m_settingsApp);
            m_metadata->setChecked(legacy.value(QStringLiteral("send_project_metadata"), true).toBool());
            m_transcript->setChecked(legacy.value(QStringLiteral("send_transcript_text"), true).toBool());
        } else {
            m_metadata->setChecked(privacy.value(QStringLiteral("metadata")).toBool());
            m_transcript->setChecked(privacy.value(QStringLiteral("transcript")).toBool());
            setExplicitConsentGranted(privacy.value(QStringLiteral("consent")).toBool());
            setBackendMode(privacy.value(QStringLiteral("mode")).toString() == QStringLiteral("connected_subscriptions") ? AiBackendMode::ExternalProvider
                                                                                                                         : AiBackendMode::LocalOnly);
        }
        m_priority.clear();
        for (const QJsonValue &id : status.value(QStringLiteral("priority")).toArray()) {
            m_priority.append(id.toString());
        }
        for (const auto &id : m_priority) {
            m_providers->removeWidget(m_rows[id].widget);
            m_providers->addWidget(m_rows[id].widget);
        }
        m_failover->setChecked(status.value(QStringLiteral("auto_failover")).toBool());
        m_taskLimit->setValue(budget.value(QStringLiteral("max_calls_per_task")).toInt(3));
        m_dayLimit->setValue(budget.value(QStringLiteral("max_calls_per_day")).toInt());
    }

    const auto providers = status.value(QStringLiteral("providers")).toObject();
    QStringList available;
    QStringList unavailable;
    for (const auto &id : m_priority) {
        const auto provider = providers.value(id).toObject();
        const auto auth = provider.value(QStringLiteral("auth")).toString();
        const QString authText = auth == QStringLiteral("account")      ? tr("Account signed in")
                                 : auth == QStringLiteral("signed_out") ? tr("Signed out")
                                 : auth == QStringLiteral("other")      ? tr("Alternate sign-in detected")
                                                                        : tr("Sign-in unverified");
        auto &names = provider.value(QStringLiteral("available")).toBool() ? available : unavailable;
        names << provider.value(QStringLiteral("title")).toString();
        QStringList lines{
            QStringList{provider.value(QStringLiteral("installed")).toBool() ? tr("Installed") : tr("Missing"), authText,
                        provider.value(QStringLiteral("authorised")).toBool() ? tr("Authorised") : tr("Unauthorised")}
                .join(QStringLiteral(" · ")),
            provider.value(QStringLiteral("state")).toString(),
            tr("Subscription access: %1").arg(provider.value(QStringLiteral("subscription")).toString()),
        };
        const auto tested = displayTime(provider.value(QStringLiteral("last_test")));
        lines << tr("Last successful test: %1").arg(tested.isEmpty() ? tr("None") : tested);
        const auto cooldown = displayTime(provider.value(QStringLiteral("cooldown_until")));
        const auto reset = displayTime(provider.value(QStringLiteral("reported_reset")));
        if (!cooldown.isEmpty()) {
            lines << tr("Cooldown until %1").arg(cooldown);
        }
        if (!reset.isEmpty()) {
            lines << tr("Reported reset: %1").arg(reset);
        }
        m_rows[id].status->setText(lines.join(QStringLiteral("\n")));
    }
    const auto title = [&providers, this](const QString &provider) {
        return providers.value(provider).toObject().value(QStringLiteral("title")).toString(tr("None"));
    };
    const auto events = status.value(QStringLiteral("failover_events")).toArray();
    QString lastSwitch = tr("None");
    for (qsizetype i = events.size(); i > 0; --i) {
        const auto entry = events.at(i - 1).toObject();
        if (entry.value(QStringLiteral("to")).isString()) {
            lastSwitch = tr("%1 → %2 · %3")
                             .arg(title(entry.value(QStringLiteral("from")).toString()), title(entry.value(QStringLiteral("to")).toString()),
                                  displayTime(entry.value(QStringLiteral("at"))));
            break;
        }
    }
    m_routing->setText(tr("Active provider: %1\nAvailable: %2\nLimited or unavailable: %3\nLast failover: %4\nReasoning calls today: %5")
                           .arg(title(status.value(QStringLiteral("active")).toString()),
                                available.isEmpty() ? tr("None") : available.join(QStringLiteral(", ")),
                                unavailable.isEmpty() ? tr("None") : unavailable.join(QStringLiteral(", ")), lastSwitch,
                                QString::number(budget.value(QStringLiteral("used_today")).toInt())));
    const QJsonArray tests = status.value(QStringLiteral("self_test")).toArray();
    if (!tests.isEmpty()) {
        const auto test = tests.first().toObject();
        m_feedback->setText(test.value(QStringLiteral("ok")).toBool() ? tr("Connection test passed.") : test.value(QStringLiteral("error")).toString());
    } else {
        m_feedback->setText(status.value(QStringLiteral("message")).toString());
    }
    syncControls();
}

void ExternalProviderConsentDialog::moveProvider(const QString &id, int delta)
{
    const int index = m_priority.indexOf(id);
    const int destination = index + delta;
    if (index < 0 || destination < 0 || destination >= m_priority.size()) {
        return;
    }
    m_priority.move(index, destination);
    m_providers->removeWidget(m_rows[id].widget);
    m_providers->insertWidget(destination, m_rows[id].widget);
    syncControls();
}

void ExternalProviderConsentDialog::save()
{
    const auto settings = currentSettings();
    const bool connected = settings.isExternalTransmissionAllowed();
    const QJsonObject policy{{QStringLiteral("mode"), connected ? QStringLiteral("connected_subscriptions") : QStringLiteral("local_only")},
                             {QStringLiteral("consent"), connected},
                             {QStringLiteral("metadata"), settings.sendProjectMetadata},
                             {QStringLiteral("transcript"), settings.sendTranscriptText}};
    const int dayLimit = m_dayLimit->value();
    const QJsonObject config{{QStringLiteral("privacy"), policy},
                             {QStringLiteral("auto_failover"), m_failover->isChecked()},
                             {QStringLiteral("priority"), QJsonArray::fromStringList(m_priority)},
                             {QStringLiteral("max_calls_per_task"), m_taskLimit->value()},
                             {QStringLiteral("max_calls_per_day"), dayLimit > 0 ? QJsonValue(dayLimit) : QJsonValue(QJsonValue::Null)}};
    run({QStringLiteral("--configure"), QString::fromUtf8(QJsonDocument(config).toJson(QJsonDocument::Compact))}, false, true);
}

} // namespace BangOnPro
