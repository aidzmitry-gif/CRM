"""Immutable originals and consistent source capture for sales documents.

A single SELECT observes all mutable database inputs at one statement snapshot.
Document transitions serialize on the deal row (SQLite takes its writer lock).
No service call is made while re-opening an issued original.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from html.parser import HTMLParser

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from core.domain.models import Counterparty, Sku
from modules.sales.models import (
    CompanyBranding,
    ContractTemplate,
    Deal,
    DealDocument,
    DealItem,
    PriceQuote,
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

    quote_id = (select(PriceQuote.id).where(
        PriceQuote.sku_code == Sku.code, PriceQuote.counterparty == Deal.counterparty,
    ).order_by(PriceQuote.created_at.desc(), PriceQuote.id.desc()).limit(1)
        .correlate(Deal, Sku).scalar_subquery())
    buyer_id = (select(Counterparty.id).where(
        Counterparty.name == Deal.counterparty, Counterparty.is_active.is_(True),
    ).order_by(Counterparty.id).limit(1).correlate(Deal).scalar_subquery())
    rows = (await session.execute(
        select(Deal, DealItem, Sku, PriceQuote, Counterparty, CompanyBranding, ContractTemplate)
        .select_from(Deal)
        .outerjoin(DealItem, DealItem.deal_id == Deal.id)
        .outerjoin(Sku, Sku.id == DealItem.sku_id)
        .outerjoin(PriceQuote, PriceQuote.id == quote_id)
        .outerjoin(Counterparty, Counterparty.id == buyer_id)
        .outerjoin(CompanyBranding, CompanyBranding.id == 1)
        .outerjoin(ContractTemplate, ContractTemplate.id == doc.template_id)
        .where(Deal.id == doc.deal_id).order_by(DealItem.id)
        .execution_options(populate_existing=True)
    )).all()
    if not rows:
        raise HTTPException(404, 'Сделка не найдена')
    deal, _, _, _, cp, branding, template = rows[0]
    seller = r._seller_with_facsimile(core, branding)
    buyer = {'name': deal.counterparty, 'unp': cp.unp or '' if cp else ''}
    if cp:
        buyer.update(cp.requisites or {})
    buyer.update((doc.terms_json or {}).get('buyer') or {})
    lines = []
    for _, item, sku, quote, *_ in rows:
        if item is None:
            continue
        if item.qty <= 0:
            raise HTTPException(422, 'Количество в выпущенном документе должно быть положительным')
        line = {'item_id': item.id, 'sku_id': item.sku_id, 'sku_code': sku.code if sku else None,
                'name': sku.title if sku else f'позиция #{item.sku_id}',
                'unit': sku.unit if sku else 'шт', 'qty': str(item.qty), 'currency': 'BYN',
                'discount': None, 'quote_id': quote.id if quote else None}
        if doc.kind == 'invoice':
            if quote is None or quote.price < 0:
                raise HTTPException(422, 'Для выпуска счёта нужна неотрицательная цена каждой позиции')
            net, tax, gross = r._invoice_line(item.qty, quote.price)
            line.update(price=str(quote.price), net=str(net), tax=str(tax), total=str(gross),
                        vat_rate=str(r._INVOICE_VAT_RATE))
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
        'deal': {'number': deal.number, 'title': deal.title, 'counterparty': deal.counterparty},
        'seller': seller, 'buyer': buyer, 'items': lines,
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
    original(doc)
    if doc.supersedes_id:
        old = await session.get(DealDocument, doc.supersedes_id)
        if old.superseded_by_id:
            raise HTTPException(409, 'У документа уже есть выпущенная замена')
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
