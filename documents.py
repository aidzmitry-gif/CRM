"""Immutable originals and consistent source capture for sales documents.

A single SELECT observes all mutable database inputs at one statement snapshot.
Document transitions serialize on the deal row (SQLite takes its writer lock).
No service call is made while re-opening an issued original.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from html.parser import HTMLParser

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from core.domain.models import Counterparty, CounterpartyBranch, Sku
from modules.sales.models import (
    CompanyBranding,
    ContractTemplate,
    CrmClient,
    Deal,
    DealDocument,
    DealItem,
)


def digest(value: object) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


async def lock_deal(session, deal_id: int) -> None:
    # An actual UPDATE also serializes SQLite and refreshes read-committed PG callers.
    await session.execute(update(Deal).where(Deal.id == deal_id).values(id=Deal.id))


async def locked_document(session, doc_id: int, access):
    from modules.sales.access import visible_deal_or_404
    doc = await session.get(DealDocument, doc_id)
    if doc is None:
        raise HTTPException(404, 'Документ не найден')
    await visible_deal_or_404(session, doc.deal_id, access)
    await lock_deal(session, doc.deal_id)
    await session.refresh(doc)
    return doc


async def retry_document(session, key, request):
    if key is None:
        return None
    doc = (await session.execute(select(DealDocument).where(DealDocument.request_key == key))).scalar_one_or_none()
    if doc is not None and doc.request_hash != digest(request):
        raise HTTPException(409, 'Ключ запроса уже использован с другим содержимым')
    return doc


def validate_css(css):
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    if "\\" in css or re.search(r'@import|url\s*\(|expression\s*\(', css, re.I):
        raise HTTPException(422, 'CSS оригинала не должен загружать внешние ресурсы')


class StaticOriginal(HTMLParser):
    in_style = False

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'iframe', 'object', 'embed', 'link', 'base', 'meta', 'svg', 'math',
                   'audio', 'video', 'source', 'form', 'input', 'button', 'textarea', 'select'}:
            if tag == 'meta' and all(k.lower() != 'http-equiv' for k, _ in attrs):
                return
            raise HTTPException(422, 'Оригинал должен быть автономным: уберите активные и внешние ресурсы шаблона')
        self.in_style = self.in_style or tag == 'style'
        for key, value in attrs:
            if key.lower().startswith('on') or key.lower() == 'srcdoc':
                raise HTTPException(422, 'Активное содержимое шаблона нельзя выпускать')
            if key.lower() == 'style':
                validate_css(value or '')
            if key.lower() in {'src', 'srcset', 'href', 'action', 'poster', 'background'}:
                if not value or value.startswith('#'):
                    continue
                if key.lower() == 'src' and re.fullmatch(r'data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=\s]+', value):
                    continue
                raise HTTPException(422, 'Внешние ресурсы шаблона нужно встроить в оригинал')

    def handle_data(self, data):
        if self.in_style:
            validate_css(data)

    def handle_endtag(self, tag):
        if tag == 'style':
            self.in_style = False


def validate_static(html):
    StaticOriginal().feed(html)


async def capture(session, core, doc):
    """Save the agreed candidate; called only on issue or submission, never GET."""
    from modules.sales import routes as r

    rows = (await session.execute(
        select(Deal, DealItem, Sku, Counterparty, CompanyBranding, ContractTemplate, CounterpartyBranch, CrmClient)
        .select_from(Deal)
        .outerjoin(CrmClient, CrmClient.id == Deal.crm_client_id)
        .outerjoin(DealItem, DealItem.deal_id == Deal.id)
        .outerjoin(Sku, Sku.id == DealItem.sku_id)
        .outerjoin(Counterparty, Counterparty.id == Deal.counterparty_id)
        .outerjoin(CounterpartyBranch, CounterpartyBranch.id == Deal.branch_id)
        .outerjoin(CompanyBranding, CompanyBranding.id == 1)
        .outerjoin(ContractTemplate, ContractTemplate.id == doc.template_id)
        .where(Deal.id == doc.deal_id).order_by(DealItem.id)
        .execution_options(populate_existing=True)
    )).all()
    if not rows:
        raise HTTPException(404, 'Сделка не найдена')
    deal, _, _, cp, branding, template, branch, client = rows[0]
    if cp is None:
        raise HTTPException(409, 'Выберите контрагента по ID перед выпуском нового документа')
    if not cp.is_active or cp.merged_into_id is not None:
        raise HTTPException(409, 'Сторона сделки недоступна; выберите актуального контрагента')
    if deal.branch_id is not None and (branch is None or branch.legal_entity_id != cp.id or not branch.is_active):
        raise HTTPException(409, 'Филиал недоступен или не принадлежит стороне сделки')
    if deal.crm_client_id is not None and (client is None or client.counterparty_id != cp.id):
        raise HTTPException(409, 'CRM-клиент не связан с выбранным контрагентом документа')
    seller = r._seller_with_facsimile(core, branding)
    buyer = dict(cp.requisites or {})
    buyer.update((doc.terms_json or {}).get('buyer') or {})
    buyer.update(name=cp.legal_name or cp.name, unp=cp.unp or '')
    party = {
        'legal_entity_id': cp.id,
        'legal_entity_revision': cp.revision,
        'display_name': cp.display_name or cp.name,
        'legal_name': cp.legal_name,
        'legal_entity_unp': cp.unp,
        'branch_id': branch.id if branch else None,
        'branch_revision': branch.revision if branch else None,
        'branch_legal_entity_id': branch.legal_entity_id if branch else None,
        'branch_name': branch.name if branch else None,
        'branch_address': branch.address if branch else None,
        'branch_tax_mode': branch.tax_mode if branch else None,
        'portal_branch_code': branch.portal_branch_code if branch else None,
        'identity_status': 'selected',
    }
    lines = []
    for _, item, sku, *_ in rows:
        if item is None:
            continue
        if item.qty <= 0:
            raise HTTPException(422, 'Количество в выпущенном документе должно быть положительным')
        line = {'item_id': item.id, 'sku_id': item.sku_id, 'sku_code': sku.code if sku else None,
                'name': sku.title if sku else f'позиция #{item.sku_id}',
                'unit': sku.unit if sku else 'шт', 'qty': str(item.qty), 'currency': 'BYN',
                'discount': None, 'quote_id': None}
        if doc.kind == 'invoice':
            if sku is None:
                raise HTTPException(422, 'Номенклатура позиции счёта не найдена')
            if item.unit_price is None or not item.unit_price.is_finite() or item.unit_price < 0:
                raise HTTPException(422, 'Для выпуска счёта подтвердите неотрицательную цену каждой позиции')
            net, tax, gross = r._invoice_line(item.qty, item.unit_price)
            line.update(price=str(item.unit_price), net=str(net), tax=str(tax), total=str(gross),
                        vat_rate=str(r._INVOICE_VAT_RATE), basis='confirmed_deal_item_price')
        else:
            # Existing contract model is an unpriced specification with one agreed total.
            line.update(price=None, net=None, tax=None, total=None, vat_rate=None)
        lines.append(line)
    if doc.kind == 'invoice':
        if not lines:
            # Historical no-item workflow has an explicitly agreed GROSS deal amount.
            total = Decimal(deal.amount).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            net = (total / (1 + r._INVOICE_VAT_RATE / 100)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            lines = [dict(item_id=None, sku_id=None, sku_code=None, name=deal.title,
                          unit='усл.', qty='1', currency='BYN', discount=None, quote_id=None,
                          price=str(net), net=str(net), tax=str(total-net), total=str(total),
                          vat_rate=str(r._INVOICE_VAT_RATE), basis='agreed_gross_amount')]
        doc.amount = sum((Decimal(line['total']) for line in lines), Decimal('0'))
    else:
        doc.amount = deal.amount
    if not doc.amount.is_finite() or doc.amount < 0:
        raise HTTPException(422, 'Некорректная сумма документа')
    source = {
        'schema_version': 1, 'document_id': doc.id, 'version': doc.version,
        'number': doc.number, 'kind': doc.kind, 'deal_id': deal.id,
        'reserve_mode': doc.reserve_mode,
        'deal': {'number': deal.number, 'title': deal.title, 'counterparty': deal.counterparty},
        'seller': seller, 'buyer': buyer, 'party': party, 'items': lines,
        'currency': 'BYN', 'amount': str(doc.amount),
        'amount_basis': 'line_gross_total' if doc.kind == 'invoice' else 'agreed_unpriced_specification',
        'template': {'id': template.id, 'code': template.code, 'body': template.body} if template else None,
        'payment_terms': doc.payment_terms, 'delivery_terms': doc.delivery_terms,
        'terms': doc.terms_json, 'valid_until': doc.valid_until.isoformat() if doc.valid_until else None,
        'valid_days': int(r.os.getenv('AIOS_INVOICE_VALID_DAYS', '5')),
        'created_at': doc.created_at.isoformat(),
    }
    items = '; '.join(f"{line['name']} — {line['qty']} {line['unit']}" for line in lines)
    if doc.kind == 'invoice':
        original = r._render_invoice(doc, deal, seller, buyer, lines)
    elif template:
        context = {'number': doc.number, 'items': items, 'total': f'{doc.amount:.2f} BYN',
                   'payment_terms': doc.payment_terms or '', 'delivery_terms': doc.delivery_terms or '',
                   'valid_until': source['valid_until'] or '', 'deal': deal.number}
        context.update({f'seller.{k}': v for k, v in seller.items()})
        context.update({f'buyer.{k}': str(v) for k, v in buyer.items()})
        original = r._render_contract(template.body, context, r._contract_facsimile_block(seller))
    else:
        original = r._contract_cover_html(doc, seller, buyer, items)
    validate_static(original)
    doc.snapshot_json, doc.original_html = source, original
    doc.content_sha256 = digest(original)


def original(doc, *, issued_only=False):
    if not doc.original_html or not doc.snapshot_json:
        raise HTTPException(409, 'Первоначальный оригинал не сохранён. Историческую версию восстановить нельзя; подготовьте новую явно.')
    if issued_only and not doc.issued_at:
        raise HTTPException(409, 'Документ ещё не выпущен')
    if digest(doc.original_html) != doc.content_sha256 or Decimal(doc.snapshot_json['amount']) != doc.amount:
        raise HTTPException(409, 'Контроль целостности оригинала не пройден')
    return doc.original_html


async def mark_issued(session, core, doc, actor):
    if doc.kind == 'invoice':
        from modules.sales.invoice_issuance import is_erp_invoice
        if await is_erp_invoice(session, doc) or doc.supersedes_id:
            raise HTTPException(409, 'ERP invoice/replacement cannot use the legacy issue/release path')
    original(doc)
    if doc.supersedes_id:
        old = await session.get(DealDocument, doc.supersedes_id)
        if old.superseded_by_id:
            raise HTTPException(409, 'У документа уже есть выпущенная замена')
        from modules.sales.invoice_settlements import receipt_query

        if old.kind == 'invoice' and (old.status == 'paid' or await session.scalar(receipt_query(old.id))):
            raise HTTPException(409, 'Оплаченный счёт нельзя заменить со снятием резерва без подтверждённого возврата или отдельного исправительного документа')
        # Preserve old paid/status and original; a new row has its own payment identity.
        old.superseded_by_id = doc.id
        if old.reserve_status == 'reserved':
            if core.services.stock is None:
                raise HTTPException(503, 'Для замены нужен складской шлюз: старый резерв не снят')
            items = (old.snapshot_json or {}).get('items')
            if items is None:
                raise HTTPException(409, 'Состав старого резерва неизвестен; требуется сверка старого документа')
            await core.services.stock.release(session, [
                {'sku_code': i['sku_code'], 'qty': Decimal(i['qty'])} for i in items if i.get('sku_code')
            ])
            old.reserve_status = 'released'
        core.event_bus.emit(session, 'sales.document.superseded', {
            'document_id': old.id, 'replacement_document_id': doc.id,
            'kind': old.kind, 'number': old.number, 'deal_id': old.deal_id,
            'amount': str(old.amount), 'replacement_amount': str(doc.amount),
            'content_sha256': old.content_sha256,
            'payment_transfer': False, 'entity_ref': f'deal:{doc.deal_id}',
        })
    doc.issued_at = datetime.now(timezone.utc).replace(tzinfo=None)
    doc.issued_by = actor


async def flush_document(session):
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "Ключ или версия документа уже используются; повторите исходный запрос") from None


def render_erp_invoice(number, prepared):
    """Static local original from confirmed exact facts, with no runtime defaults."""
    def esc(value):
        return html.escape(str(value if value is not None else ''), quote=True)

    def present(value):
        return value is not None and str(value).strip() != ''

    def group(label, values):
        text = ', '.join(esc(value) for value in values if present(value))
        return f'<dt>{esc(label)}</dt><dd>{text}</dd>' if text else ''

    labels = {
        'address': 'Адрес', 'legal_address': 'Юридический адрес',
        'postal_address': 'Почтовый адрес', 'bank': 'Банк', 'bank_name': 'Банк',
        'bik': 'БИК', 'account': 'Расчётный счёт', 'iban': 'IBAN',
        'phone': 'Телефон', 'email': 'Email', 'director': 'Руководитель',
    }

    def requisites(value, path=()):
        if isinstance(value, dict):
            return ''.join(requisites(item, (*path, labels.get(str(key), str(key))))
                           for key, item in sorted(value.items(), key=lambda pair: str(pair[0])))
        if isinstance(value, list):
            return ''.join(requisites(item, (*path, str(index)))
                           for index, item in enumerate(value, 1))
        return group(' / '.join(path), [value]) if present(value) else ''

    seller, buyer = prepared['seller'], prepared['buyer']
    logo = seller.get('logo_data_url') or ''
    if logo and (not isinstance(logo, str) or not logo.startswith(('data:image/png;base64,', 'data:image/jpeg;base64,'))):
        raise ValueError('Invoice logo must be a confirmed local PNG/JPEG')
    logo_html = (f'<div class="logo"><img src="{esc(logo)}" alt="{esc(seller["name"])}" '
                 'style="max-width:300px;max-height:100px;object-fit:contain"></div>') if logo else ''
    party_fields = ''.join((
        group('Продавец', [seller['name'], f"УНП {seller['unp']}", seller['address']]),
        group('Банк / счёт', [seller['bank'], seller['bik'], seller['account']]),
        group('Контакты / руководитель', [seller.get('phone'), seller.get('email'), seller['director']]),
        group('Покупатель', [buyer['name'], f"УНП {buyer['unp']}"]),
        requisites(buyer['requisites']),
    ))
    terms = ''.join(f'<p>{esc(label)}: {esc(prepared[key])}</p>' for label, key in (
        ('Условия оплаты', 'payment_terms'), ('Условия поставки', 'delivery_terms'))
        if present(prepared[key]))
    rows = ''.join('<tr>' + ''.join(f'<td>{esc(line[key])}</td>' for key in (
        'line_no', 'sku_code', 'name', 'qty', 'unit', 'price', 'vat_rate', 'net', 'tax', 'total'))
        + '</tr>' for line in prepared['lines'])
    return f'''<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">
<title>Счёт {esc(number)}</title><style>
@page{{size:A4 landscape;margin:14mm}}
body{{font-family:Arial,sans-serif;font-size:9pt;color:#172033;margin:0 auto;max-width:269mm}}
h1{{font-size:16pt}}table{{width:100%;table-layout:fixed;border-collapse:collapse}}
th,td{{border:1px solid #9ca3af;padding:5px;text-align:left;overflow-wrap:anywhere}}
thead{{display:table-header-group}}tr{{break-inside:avoid}}dl{{line-height:1.4}}
dd{{margin:0 0 6px 16px;overflow-wrap:anywhere}}dt{{font-weight:bold}}
</style></head><body>{logo_html}<h1>Счёт {esc(number)} от {esc(prepared['document_date'])}</h1><dl>{party_fields}</dl>
<table><colgroup>{''.join(f'<col style="width:{width}%">' for width in (3, 11, 25, 7, 4, 10, 6, 11, 11, 12))}</colgroup>
<thead><tr><th>№</th><th>Код</th><th>Товар</th><th>Количество</th><th>Ед.</th><th>Цена нетто</th>
<th>НДС %</th><th>Нетто</th><th>НДС</th><th>Всего</th></tr></thead><tbody>{rows}</tbody></table>
<p>Всего: {esc(prepared['amount'])} {esc(prepared['currency'])}</p>
<p>{'Под заказ — товар не зарезервирован.' if prepared.get('reserve_mode') == 'on_order' else 'Поставка со склада.'}</p>
<p>Действителен до {esc(prepared['valid_until'])}.</p>
{terms}
<p>Основание цен и ставок: {esc(prepared['pricing_evidence'])}.</p></body></html>'''


def capture_erp_invoice(doc, prepared, actor):
    if doc.original_html or doc.snapshot_json or doc.issued_at:
        raise HTTPException(409, 'An existing original cannot be captured again')
    original_html = render_erp_invoice(doc.number, prepared)
    validate_static(original_html)
    doc.amount = Decimal(prepared['amount'])
    doc.valid_until = datetime.fromisoformat(prepared['valid_until']).date()
    doc.snapshot_json = {**prepared, 'schema_version': 1, 'issuance_mode': 'erp_issuance_v1',
                         'document_id': doc.id, 'version': doc.version, 'number': doc.number,
                         'kind': 'invoice', 'items': prepared['lines'],
                         'created_at': doc.created_at.isoformat()}
    doc.original_html = original_html
    doc.content_sha256 = digest(original_html)
    doc.issued_at = datetime.now(timezone.utc).replace(tzinfo=None)
    doc.issued_by = actor
