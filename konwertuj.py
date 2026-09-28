#!/usr/bin/env python3
"""Konwersja plików źródłowych (wyciąg bankowy, lista dokumentów) do formatu kanonicznego pokrycie.py.

  python3 konwertuj.py podglad PLIK [--kodowanie K] [--separator S] [--pomin N] [--bez-cudzyslowow]
      kolumny pliku: numer, nagłówek, liczba wypełnionych, przykładowe wartości — do ułożenia mapy
  python3 konwertuj.py csv PLIK --mapa MAPA.json -o WYJSCIE.csv
      CSV/TSV -> dokumenty.csv albo transakcje.csv według mapy kolumn ("typ" w mapie)
  python3 konwertuj.py mt940 PLIK -o transakcje.csv [--kodowanie K]
      wyciąg MT940 (pola :25: :60F: :61: :86:) -> transakcje.csv

Po konwersji wypisuje statystyki (liczba wierszy, zakres dat, sumy, rachunki, kategorie) — porównaj je ze źródłem.
"""
import argparse
import csv
import io
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pokrycie import POLA_DOKUMENTU, POLA_TRANSAKCJI, cyfry, kwota


def odczytaj(sciezka, kodowanie=None):
    dane = Path(sciezka).read_bytes()
    for kod in [kodowanie] if kodowanie else ['utf-8-sig', 'cp1250']:
        try:
            return dane.decode(kod), kod
        except UnicodeDecodeError:
            pass
    sys.exit(f'{sciezka}: nie rozpoznano kodowania — podaj --kodowanie (np. cp1250, iso-8859-2, utf-8)')


def wiersze_csv(sciezka, kodowanie=None, separator=None, pomin=0, bez_cudzyslowow=False):
    tekst, kod = odczytaj(sciezka, kodowanie)
    linie = tekst.splitlines(keepends=True)[pomin:]
    if not linie:
        sys.exit(f'{sciezka}: pusty plik (albo za duże --pomin)')
    sep = {'tab': '\t', '\\t': '\t'}.get(separator, separator) or max(';\t,|', key=linie[0].count)
    wiersze = list(csv.reader(io.StringIO(''.join(linie)), delimiter=sep,
                              quoting=csv.QUOTE_NONE if bez_cudzyslowow else csv.QUOTE_MINIMAL))
    return wiersze[0], [r for r in wiersze[1:] if any(c.strip() for c in r)], kod, sep


def zapisz(sciezka, pola, wiersze):
    with open(sciezka, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=pola, delimiter=';', extrasaction='ignore')
        w.writeheader()
        w.writerows(wiersze)


def statystyki(typ, wiersze, pominiete):
    print(f'zapisano: {len(wiersze)}, pominięto: {len(pominiete)}')
    for p in pominiete[:5]:
        print('  pominięty', p)
    if not wiersze:
        return
    daty = sorted(w['data'] for w in wiersze)
    print(f'daty: {daty[0]} – {daty[-1]}')
    if typ == 'transakcje':
        kw = [(Decimal(w['kwota']), w) for w in wiersze]
        print(f"wydatki: {sum(k for k, _ in kw if k < 0)}  wpływy: {sum(k for k, _ in kw if k > 0)}  "
              f"waluty: {dict(Counter(w['waluta'] for w in wiersze))}")
        print('rachunki własne:', dict(Counter(w['rachunek'] for w in wiersze)))
        kat = defaultdict(lambda: [0, Decimal(0)])
        for k, w in kw:
            kat[(w['kategoria'], 'wydatek' if k < 0 else 'wpływ')][0] += 1
            kat[(w['kategoria'], 'wydatek' if k < 0 else 'wpływ')][1] += k
        print('kategorie (do reguł pomin / bez_dopasowania / gotowka w config.json):')
        for (k, kier), (n, s) in sorted(kat.items(), key=lambda x: -x[1][0]):
            print(f'  {n:5}  {s:>12}  {kier:7}  {k[:60]}')
    else:
        sumy = defaultdict(Decimal)
        for w in wiersze:
            sumy[w['waluta'] or 'PLN'] += Decimal(w['brutto'] or 0)
        print('brutto wg walut:', {k: str(v) for k, v in sumy.items()},
              f"| bez kwoty: {sum(not w['brutto'] for w in wiersze)}",
              f"| formy płatności: {dict(Counter(w['forma'] for w in wiersze))}")


def podglad(a):
    naglowek, wiersze, kod, sep = wiersze_csv(a.plik, a.kodowanie, a.separator, a.pomin, a.bez_cudzyslowow)
    print(f'kodowanie: {kod}, separator: {sep!r}, wierszy danych: {len(wiersze)}, kolumn: {len(naglowek)}')
    dlugosci = Counter(len(r) for r in wiersze)
    if len(dlugosci) > 1:
        print('UWAGA: różna liczba kolumn w wierszach:', dict(dlugosci), '— sprawdź --pomin / --bez-cudzyslowow')
    powtorzone = [n for n, c in Counter(naglowek).items() if c > 1 and n.strip()]
    if powtorzone:
        print('UWAGA: powtórzone nagłówki — w mapie użyj numerów kolumn:', powtorzone)
    for i, n in enumerate(naglowek):
        wartosci = [r[i].strip() for r in wiersze if i < len(r) and r[i].strip()]
        if wartosci:
            przyklady = ' | '.join(w[:40] for w in list(dict.fromkeys(wartosci))[:3])
            print(f'{i:3}  {n.strip()[:35]:35} {len(wartosci):6}  {przyklady}')


def konwertuj_csv(a):
    mapa = json.loads(Path(a.mapa).read_text(encoding='utf-8'))
    typ = mapa['typ']
    pola = {'transakcje': POLA_TRANSAKCJI, 'dokumenty': POLA_DOKUMENTU}[typ]
    pomin = mapa.get('pomin_wiersze', 0)
    naglowek, wiersze, _, _ = wiersze_csv(a.plik, mapa.get('kodowanie'), mapa.get('separator'), pomin,
                                          mapa.get('bez_cudzyslowow', False))

    def indeks(spec):
        if isinstance(spec, int):
            return spec
        if spec not in naglowek:
            sys.exit(f'{a.mapa}: brak kolumny {spec!r} w nagłówku pliku')
        return naglowek.index(spec)

    idx = {p: [indeks(s) for s in (spec if isinstance(spec, list) else [spec])] for p, spec in mapa['kolumny'].items()}
    nieznane = set(idx) - set(pola) - {'wydatek', 'wplyw'}
    if nieznane:
        sys.exit(f'{a.mapa}: nieznane pola {sorted(nieznane)}; dozwolone: {pola} (+ wydatek/wplyw dla transakcji)')
    fmt = mapa.get('format_daty', '%Y-%m-%d')

    def wartosc(r, pole):  # lista kolumn -> pierwsza niepusta
        return next((r[i].strip() for i in idx.get(pole, []) if i < len(r) and r[i].strip()), '')

    def data(s):  # pierwszy człon — ignoruje godzinę ('01.09.2026 20:16:14')
        return datetime.strptime(s.split()[0], fmt).date().isoformat() if s else ''

    wynik, pominiete = [], []
    for nr, r in enumerate(wiersze, 2 + pomin):
        try:
            w = {p: wartosc(r, p) for p in pola}
            for p, v in (mapa.get('stale') or {}).items():
                w[p] = w.get(p) or v
            w['data'] = data(w['data'])
            if not w['data']:
                raise ValueError('brak daty')
            surowe = ' '.join(wartosc(r, p) for p in ('kwota', 'wydatek', 'wplyw', 'brutto'))
            if typ == 'transakcje':
                k = kwota(wartosc(r, 'kwota'))
                if k is None and (wartosc(r, 'wydatek') or wartosc(r, 'wplyw')):
                    k = abs(kwota(wartosc(r, 'wplyw')) or 0) - abs(kwota(wartosc(r, 'wydatek')) or 0)
                if k is None:
                    raise ValueError('brak kwoty')
                w['kwota'] = str(k)
            else:
                w['termin'] = data(w['termin'])
                for p in ('brutto', 'netto', 'vat'):
                    k = kwota(w[p])
                    w[p] = '' if k is None else str(k)
            if not w['waluta']:  # np. '-62,10 PLN'
                m = re.search(r'\b[A-Z]{3}\b', surowe)
                w['waluta'] = m.group() if m else ''
            wynik.append(w)
        except (ValueError, ArithmeticError) as e:
            pominiete.append(f'wiersz {nr}: {e}')
    zapisz(a.o, pola, wynik)
    statystyki(typ, wynik, pominiete)


def szczegoly_86(s):
    """Pole :86: — podpola ~NN / ^NN / <NN / ?NN (różne banki); bez podpól cały tekst idzie do tytułu."""
    s = re.sub(r'\s*\n\s*', '', s)
    pola = defaultdict(str)
    for nr, v in re.findall(r'[~^<?](\d\d)([^~^<?]*)', s):
        pola[nr] += v
    if not pola:
        return {'tytul': s.strip()}
    z = lambda *nr: ''.join(pola[n] for n in nr).strip()
    return {'tytul': z('20', '21', '22', '23', '24', '25', '26', '60', '61', '62', '63'),
            'kontrahent': z('32', '33') or z('27', '28', '29'),
            'rachunek_kontrahenta': z('38') or z('31'),
            'kategoria': z('00') or s[:3]}


def z_mt940(tekst):
    tx, rachunek, waluta, poprzedni, pominiete = [], '', 'PLN', '', []
    for tag, v in re.findall(r'^:(\d\d[A-Z]?):(.*?)(?=^:\d\d[A-Z]?:|^-\}?\s*$|\Z)', tekst, flags=re.M | re.S):
        v = v.strip()
        if tag == '25':
            rachunek = v.lstrip('/')
        elif tag in ('60F', '60M'):
            waluta = v[7:10]
        elif tag == '61':
            m = re.match(r'(\d{6})(\d{4})?(R?[CD])[A-Z]?(\d+,\d*)', v)
            if not m:
                pominiete.append(f':61:{v[:40]}')
                poprzedni = ''
                continue
            k = Decimal(m[4].replace(',', '.'))
            tx.append({'data': datetime.strptime(m[1], '%y%m%d').date().isoformat(),
                       'kwota': str(-k if m[3] in ('D', 'RC') else k), 'waluta': waluta, 'rachunek': rachunek,
                       'kontrahent': '', 'rachunek_kontrahenta': '', 'tytul': '', 'kategoria': ''})
        elif tag == '86' and poprzedni == '61':  # :86: po :62F: to informacja do wyciągu, nie do transakcji
            tx[-1].update(szczegoly_86(v))
        poprzedni = tag
    return tx, pominiete


def konwertuj_mt940(a):
    tekst, _ = odczytaj(a.plik, a.kodowanie)
    tx, pominiete = z_mt940(tekst)
    zapisz(a.o, POLA_TRANSAKCJI, tx)
    statystyki('transakcje', tx, pominiete)
    if tx and not any(cyfry(t['rachunek_kontrahenta']) or t['kontrahent'] for t in tx):
        print('UWAGA: pole :86: bez rozpoznanych podpól — cały opis trafił do "tytul"; sprawdź plik wynikowy')


if __name__ == '__main__':
    for strumien in (sys.stdout, sys.stderr):  # Windows: wyjście przez potok ma kodowanie cp125x -> błąd na '≠', 'ą'
        strumien.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('podglad')
    s.add_argument('plik')
    s.add_argument('--kodowanie')
    s.add_argument('--separator')
    s.add_argument('--pomin', type=int, default=0, help='wiersze przed nagłówkiem')
    s.add_argument('--bez-cudzyslowow', action='store_true', help='cudzysłów to zwykły znak (np. eksport TSV)')
    s.set_defaults(f=podglad)
    s = sub.add_parser('csv')
    s.add_argument('plik')
    s.add_argument('--mapa', required=True)
    s.add_argument('-o', required=True)
    s.set_defaults(f=konwertuj_csv)
    s = sub.add_parser('mt940')
    s.add_argument('plik')
    s.add_argument('-o', required=True)
    s.add_argument('--kodowanie')
    s.set_defaults(f=konwertuj_mt940)
    a = p.parse_args()
    a.f(a)
