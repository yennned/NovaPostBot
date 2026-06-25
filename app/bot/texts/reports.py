"""uk-тексты отчётов/аналитики (Фаза 6). parse_mode=HTML."""

from __future__ import annotations

import html
from datetime import datetime
from decimal import Decimal

from app.services.reports import (
    FinancialReport,
    ManagerSupportStat,
    PeriodReport,
)
from app.utils.timefmt import fmt_dt

_PERIOD_LABELS = {"today": "сьогодні", "week": "тиждень", "month": "місяць"}


def _period_label(report: PeriodReport) -> str:
    """Заголовочная метка периода: пресет или конкретная дата (при выборе дня)."""
    if report.period in _PERIOD_LABELS:
        return _PERIOD_LABELS[report.period]
    return report.start.strftime("%d.%m.%Y")


def _esc(value: str) -> str:
    return html.escape(value)


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _local(value: datetime | None) -> str:
    return fmt_dt(value) if value is not None else "—"


def period_report_text(report: PeriodReport) -> str:
    lines = [
        f"📊 <b>Звіт</b> · {_period_label(report)}",
        f"Відправлено: {report.shipped} · Повернення: {report.returns} · Втрати: {report.losses}",
        f"<b>Чисті продажі: {report.net}</b>",
    ]
    if not report.clients:
        lines.append("Даних за період немає.")
        return "\n".join(lines)
    lines.append("")
    lines.append("<b>По клієнтах:</b>")
    for c in report.clients:
        lines.append(
            f"• {_esc(c.client_name)}: відпр {c.shipped} / повер {c.returns} / втр {c.losses} "
            f"(чисті {c.net})"
        )
    return "\n".join(lines)


def financial_text(fin: FinancialReport) -> str:
    lines = [
        "💰 <b>Фінанси</b>",
        f"Відправлено ТТН: {fin.dispatched_count}",
        f"Сума комісії: {_money(fin.fee_total)} грн",
        f"Безкоштовних (промах SLA): {fin.free_count}",
    ]
    if fin.late:
        lines.append("")
        lines.append("<b>Опоздавшие ТТН (SLA &gt; 30 хв):</b>")
        for item in fin.late:
            ttn = _esc(item.ttn_number or "—")
            when = _local(item.dispatched_at)
            lines.append(f"• <code>{ttn}</code> — {_esc(item.client_name)} · {when}")
    else:
        lines.append("Прострочених ТТН немає.")
    return "\n".join(lines)


def manager_support_text(stats: list[ManagerSupportStat]) -> str:
    lines = ["🧑‍💼 <b>Підтримка по менеджерах</b>"]
    if not stats:
        lines.append("Менеджерів немає.")
        return "\n".join(lines)
    for s in stats:
        lines.append(
            f"• {_esc(s.name)}: відкритих {s.open_count} · закрито за період {s.closed_count}"
        )
    return "\n".join(lines)


def analytics_text(
    report: PeriodReport, fin: FinancialReport, stats: list[ManagerSupportStat]
) -> str:
    return "\n\n".join(
        [period_report_text(report), financial_text(fin), manager_support_text(stats)]
    )
