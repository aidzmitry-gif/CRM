"""Сумма прописью (BYN) — рубли словами + копейки цифрами.

Порт логики JS ``intWords``/``triplet``/``plural``/``moneyWords`` из
``sales-invoice-template.html`` на Python (§ печатная форма счёта, kind=invoice).
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_ONES = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_ONES_F = ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_TEENS = [
    "десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
    "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать",
]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUND = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]

_RUB = ("рубль", "рубля", "рублей")
_KOP = ("копейка", "копейки", "копеек")


def _plural(n: int, forms: tuple[str, str, str]) -> str:
    a, b = abs(n) % 100, abs(n) % 10
    if 10 < a < 20:
        return forms[2]
    if 1 < b < 5:
        return forms[1]
    if b == 1:
        return forms[0]
    return forms[2]


def _triplet(num: int, female: bool) -> str:
    words = []
    h, t, o = num // 100, (num % 100) // 10, num % 10
    if h:
        words.append(_HUND[h])
    if t > 1:
        words.append(_TENS[t])
        if o:
            words.append((_ONES_F if female else _ONES)[o])
    elif t == 1:
        words.append(_TEENS[o])
    elif o:
        words.append((_ONES_F if female else _ONES)[o])
    return " ".join(words)


def _int_words(n: int) -> str:
    if n == 0:
        return "ноль"
    parts: list[str] = []
    bil, n = divmod(n, 10**9)
    mil, n = divmod(n, 10**6)
    th, n = divmod(n, 1000)
    if bil:
        parts.append(_triplet(bil, False))
        parts.append(_plural(bil, ("миллиард", "миллиарда", "миллиардов")))
    if mil:
        parts.append(_triplet(mil, False))
        parts.append(_plural(mil, ("миллион", "миллиона", "миллионов")))
    if th:
        parts.append(_triplet(th, True))
        parts.append(_plural(th, ("тысяча", "тысячи", "тысяч")))
    if n:
        parts.append(_triplet(n, False))
    return " ".join(parts)


def money_words(amount: Decimal) -> str:
    """«Сто восемь рублей 25 копеек» — рубли словами, копейки числом (BYN).

    Одно округление до копеек с переносом в рубли (иначе возможно «...100 копеек»).
    """
    neg = amount < 0
    cents = int((abs(amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    rub, kop = divmod(cents, 100)
    words = _int_words(rub)
    words = words[0].upper() + words[1:]
    return f"{'минус ' if neg else ''}{words} {_plural(rub, _RUB)} {kop:02d} {_plural(kop, _KOP)}"
