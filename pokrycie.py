#!/usr/bin/env python3
"""Pokrycie kosztów: które faktury i paragony zapłacono z konta firmowego, a które nie.

Użycie:  python3 pokrycie.py config.json

Wejście — format kanoniczny (opis w README.md; konwersja z plików banku/księgowości: konwertuj.py):
  dokumenty.csv   numer;data;termin;brutto;netto;vat;waluta;forma;kontrahent;nip;rachunek;opis
  transakcje.csv  data;kwota;waluta;kontrahent;rachunek_kontrahenta;tytul;kategoria;rachunek
  korekty.csv     numer;nip;pole;wartosc;powod   (opcjonalnie — ręczne poprawki błędnych danych)

Wynik (katalog "wyniki" z config.json):
  raport_koszty.csv                 wszystkie dokumenty, dopasowane transakcje, uwagi
  raport_niepokryte.csv             dokumenty bez pokrycia, kwoty w PLN (średni kurs NBP)
  raport_wydatki_bez_dokumentu.csv  wydatki z konta bez dokumentu (w tym gotówka i opłaty)
  podsumowanie.md                   sumy, gotówka, pozycje do sprawdzenia, problemy z danymi
"""
import csv
import io
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

POLA_DOKUMENTU = ['numer', 'data', 'termin', 'brutto', 'netto', 'vat', 'waluta', 'forma', 'kontrahent', 'nip',
                  'rachunek', 'opis']
POLA_TRANSAKCJI = ['data', 'kwota', 'waluta', 'kontrahent', 'rachunek_kontrahenta', 'tytul', 'kategoria', 'rachunek']

REGULY = {  # nadpisywane przez "reguly" w config.json
    'dni_przed': 31,             # zapłata do 31 dni przed datą dokumentu (faktura wystawiona po zapłacie, np. hotel)
    'dni_po': 60,                # ... i do 60 dni po terminie płatności
    'dni_po_karta': 7,           # karta/gotówka/BLIK: zapłata najpóźniej tydzień po dacie dokumentu
    'dni_pewne': 7,              # dopasowanie samą kwotą przy większej różnicy dat -> 'do sprawdzenia'
    'tolerancja': '0.01',        # różnica groszowa — tylko przy zgodnym numerze dokumentu lub rachunku
    'tolerancja_walut': '0.05',  # dokument w walucie, płatność w PLN: ±5% kwoty po kursie NBP (spread banku)
    'kurs_dzien_przed': True,    # kurs NBP z ostatniego dnia roboczego PRZED datą dokumentu (jak dla kosztów w CIT)
}
FORMY = {'karta': 'karta', 'card': 'karta', 'płatność kartą': 'karta', 'przelew': 'przelew', 'transfer': 'przelew',
         'gotówka': 'gotowka', 'gotowka': 'gotowka', 'cash': 'gotowka', 'blik': 'blik'}
W_CHWILI_ZAKUPU = {'karta', 'gotowka', 'blik'}


# ---------- parsowanie ----------

def kwota(s):
    """'-1 234,56 PLN' / '1,234.56' / '12.5' -> Decimal; brak cyfr -> None."""
    s = re.sub(r'[^\d,.\-]', '', (s or '').replace('−', '-'))
    if not re.search(r'\d', s):
        return None
    if ',' in s and '.' in s:  # ostatni separator jest dziesiętny, pozostałe to tysiące
        s = s.replace(',' if s.rfind('.') > s.rfind(',') else '.', '')
    return Decimal(s.replace(',', '.'))


def cyfry(s):
    return re.sub(r'\D', '', s or '')[-26:]


def znormalizuj(s):
    return re.sub(r'[\s/_-]', '', s.upper())


def liczby(s):
    """'rachunek 05/2026' -> '/5/2026/' — porównanie numerów bez zer wiodących."""
    return '/' + '/'.join(str(int(x)) for x in re.findall(r'\d+', s)) + '/'


def numer_w_tytule(numer, tytul):
    nr = znormalizuj(numer)
    return (len(nr) >= 6 and nr in znormalizuj(tytul)) or (liczby(numer).count('/') >= 3 and liczby(numer) in liczby(tytul))


def pl(d):
    return '' if d is None else str(d).replace('.', ',')


def zl(d):
    return f'{d:,.2f}'.replace(',', ' ').replace('.', ',')


def data_iso(s, gdzie):
    try:
        return date.fromisoformat(s.strip())
    except ValueError:
        sys.exit(f'{gdzie}: zła data {s!r} (oczekiwano RRRR-MM-DD)')


def czytaj_csv(sciezka):
    tekst = Path(sciezka).read_text(encoding='utf-8-sig')
    naglowek = tekst.split('\n', 1)[0]
    return list(csv.DictReader(io.StringIO(tekst), delimiter=max(';\t,', key=naglowek.count)))


def zapisz_csv(sciezka, naglowek, wiersze):
    with open(sciezka, 'w', encoding='utf-8-sig', newline='') as f:  # BOM + ';' -> otwiera się w polskim Excelu
        w = csv.writer(f, delimiter=';')
        w.writerow(naglowek)
        w.writerows(wiersze)


# ---------- wczytywanie ----------

def wczytaj_dokumenty(sciezka, sciezka_korekty=None):
    dok = [{p: (r.get(p) or '').strip() for p in POLA_DOKUMENTU} for r in czytaj_csv(sciezka)]
    korekty = czytaj_csv(sciezka_korekty) if sciezka_korekty and Path(sciezka_korekty).exists() else []
    for kor in korekty:
        pole = kor['pole'].strip()
        trafione = [k for k in dok if k['numer'] == kor['numer'].strip()
                    and (not kor.get('nip') or cyfry(k['nip']) == cyfry(kor['nip']))]
        if len(trafione) != 1 or pole not in POLA_DOKUMENTU:
            print(f"UWAGA korekty: {kor['numer']}/{pole} pasuje do {len(trafione)} dokumentów — pominięta", file=sys.stderr)
            continue
        k = trafione[0]
        k['_korekta'] = f"{pole}: {k[pole]} -> {kor['wartosc']} ({kor.get('powod') or ''})"
        k[pole] = kor['wartosc'].strip()
    for i, k in enumerate(dok, 2):
        k.setdefault('_korekta', '')
        k['_kwota'] = kwota(k['brutto'])
        k['waluta'] = k['waluta'].upper() or 'PLN'
        k['_od'] = data_iso(k['data'], f'{sciezka}:{i}')
        k['_termin'] = data_iso(k['termin'], f'{sciezka}:{i}') if k['termin'] else None
        k['_do'] = max(k['_od'], k['_termin'] or k['_od'])
        k['_forma'] = FORMY.get(k['forma'].lower(), 'inna' if k['forma'] else '')
    return dok


def wczytaj_transakcje(sciezki, cfg):
    """Wydatki z rachunków własnych, sklasyfikowane: wydatek / gotowka / bez_dopasowania ('pomin' odrzucone).

    Zwraca (transakcje, (pierwsza data, ostatnia data) wyciągu)."""
    wlasne = {cyfry(r) for r in cfg.get('rachunki_wlasne') or []}
    wzorce = [(typ, re.compile('|'.join(cfg.get(typ) or ['(?!)']), re.I))
              for typ in ('pomin', 'bez_dopasowania', 'gotowka')]  # kolejność: pierwszy pasujący wygrywa
    tx, daty, inne = [], [], Counter()
    for sciezka in sciezki:
        for i, r in enumerate(czytaj_csv(sciezka), 2):
            r = {p: (r.get(p) or '').strip() for p in POLA_TRANSAKCJI}
            if wlasne and r['rachunek'] and cyfry(r['rachunek']) not in wlasne:
                inne[r['rachunek']] += 1
                continue
            d = data_iso(r['data'], f'{sciezka}:{i}')
            daty.append(d)
            k = kwota(r['kwota'])
            if k is None or k >= 0:
                continue  # wpływy nie pokrywają kosztów
            opis = ' | '.join((r['kategoria'], r['kontrahent'], r['tytul']))
            typ = next((typ for typ, w in wzorce if w.search(opis)), 'wydatek')
            if typ != 'pomin':
                tx.append({**r, 'data': d, 'kwota': -k, 'waluta': r['waluta'].upper() or 'PLN',
                           'rachunek_kontrahenta': cyfry(r['rachunek_kontrahenta']), 'typ': typ})
    if not daty:
        sys.exit(f'Brak transakcji z rachunków {sorted(cfg.get("rachunki_wlasne") or [])}; '
                 f'rachunki w plikach: {dict(inne)} — popraw rachunki_wlasne w config.json')
    return tx, (min(daty), max(daty))


# ---------- kursy NBP ----------

def kursy_nbp(waluta, od, do):
    """{data: (kurs, nr tabeli)} z tabeli A NBP. API daje max 367 dni na zapytanie, więc pobiera kawałkami."""
    kursy = {}
    while od <= do:
        koniec = min(do, od + timedelta(366))
        url = f'https://api.nbp.pl/api/exchangerates/rates/a/{waluta.lower()}/{od}/{koniec}/?format=json'
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                kursy.update({date.fromisoformat(x['effectiveDate']): (x['mid'], x['no'])
                              for x in json.load(r, parse_float=Decimal)['rates']})
        except urllib.error.HTTPError as e:
            if e.code != 404:  # 404 = brak notowań w zakresie (same dni wolne) albo waluty spoza tabeli A
                raise
        except urllib.error.URLError as e:
            sys.exit(f'Brak połączenia z api.nbp.pl ({e.reason}) — kurs {waluta} jest potrzebny do przeliczenia dokumentów')
        od = koniec + timedelta(1)
    return kursy


def kurs_dla(kursy, dzien):
    """(data kursu, kurs, nr tabeli) właściwe dla dokumentu z dnia `dzien`."""
    dni = [d for d in kursy if d < dzien or (d == dzien and not REGULY['kurs_dzien_przed'])]
    if not dni:
        sys.exit(f'Brak kursu NBP (tabela A) dla dokumentu z {dzien}')
    return (max(dni), *kursy[max(dni)])


def przelicz(dok):
    """Dokumenty w walutach obcych: _kurs = (data, kurs, tabela), _pln = kwota w PLN."""
    walutowe = [k for k in dok if k['waluta'] != 'PLN' and k['_kwota']]
    kursy = {w: kursy_nbp(w, min(k['_od'] for k in walutowe if k['waluta'] == w) - timedelta(14),
                          max(k['_od'] for k in walutowe if k['waluta'] == w))
             for w in {k['waluta'] for k in walutowe}}
    for k in dok:
        k['_kurs'], k['_pln'] = None, k['_kwota']
        if k['waluta'] != 'PLN' and k['_kwota']:
            k['_kurs'] = kurs_dla(kursy[k['waluta']], k['_od'])
            k['_pln'] = (k['_kwota'] * k['_kurs'][1]).quantize(Decimal('0.01'), ROUND_HALF_UP)


# ---------- dopasowanie ----------

def dopasuj(dok, tx):
    """{indeks dokumentu: (indeks transakcji, sposób)}. Każda transakcja pokrywa najwyżej jeden dokument.

    Kolejność pewności: numer dokumentu w tytule > rachunek sprzedawcy > kwota + najbliższa data > kurs NBP ±%."""
    tol, tol_walut = Decimal(str(REGULY['tolerancja'])), Decimal(str(REGULY['tolerancja_walut']))
    pary = []
    for i, k in enumerate(dok):
        if not k['_kwota'] or k['_kwota'] <= 0:
            continue
        rach = cyfry(k['rachunek'])
        od = k['_od'] - timedelta(REGULY['dni_przed'])
        do = k['_do'] + timedelta(REGULY['dni_po_karta'] if k['_forma'] in W_CHWILI_ZAKUPU else REGULY['dni_po'])
        for j, t in enumerate(tx):
            if t['typ'] != 'wydatek' or not od <= t['data'] <= do:
                continue
            walutowe = t['waluta'] != k['waluta']
            if not walutowe:
                cel, dopuszczalna = k['_kwota'], tol
            elif t['waluta'] == 'PLN' and k.get('_kurs'):
                cel = k['_pln']
                dopuszczalna = cel * tol_walut
            else:
                continue
            roznica = abs(t['kwota'] - cel)
            if roznica > dopuszczalna:
                continue
            po_numerze = numer_w_tytule(k['numer'], t['tytul'])
            po_rachunku = bool(rach) and rach == t['rachunek_kontrahenta']
            if roznica and not (po_numerze or po_rachunku or walutowe):
                continue
            sposob = ('numer w tytule' if po_numerze else 'rachunek' if po_rachunku
                      else 'kurs NBP ±%' if walutowe else 'kwota+data')
            wynik = (not po_numerze, not po_rachunku, walutowe, roznica / cel, abs((t['data'] - k['_od']).days))
            pary.append((wynik, i, j, sposob))
    wynik, uzyte = {}, set()
    for _, i, j, sposob in sorted(pary):
        if i not in wynik and j not in uzyte:
            wynik[i] = (j, sposob)
            uzyte.add(j)
    return wynik


def zsumuj(elementy, indeksy, klucz, pola):
    """Elementy o tym samym kluczu (min. 2) łączy w jeden z sumą `pola`; '_sklad' = indeksy składowych."""
    grupy = defaultdict(list)
    for i in indeksy:
        grupy[klucz(elementy[i])].append(i)
    return [{**elementy[s[0]], **{p: sum(elementy[i][p] for i in s) for p in pola}, '_sklad': s}
            for s in grupy.values() if len(s) > 1]


def pokryj(dok, tx):
    """{indeks dokumentu: ([indeksy transakcji], sposób)}."""
    wynik = {i: ([j], s) for i, (j, s) in dopasuj(dok, tx).items()}

    def wolne():
        uzyte = {j for js, _ in wynik.values() for j in js}
        return ([i for i in range(len(dok)) if i not in wynik and dok[i]['_kwota']],
                [j for j in range(len(tx)) if j not in uzyte and tx[j]['typ'] == 'wydatek'])

    # kilka płatności u jednego sprzedawcy jednego dnia za jeden dokument (np. bilet rozbity na kilka transakcji)
    wolne_d, wolne_t = wolne()
    grupy = zsumuj(tx, wolne_t, lambda t: (t['data'], t['kontrahent']), ['kwota'])
    for i, (g, s) in dopasuj([dok[i] for i in wolne_d], grupy).items():
        wynik[wolne_d[i]] = (grupy[g]['_sklad'], s + ', suma płatności')

    # jedna płatność za kilka dokumentów jednego sprzedawcy z jednego dnia (np. dwa bilety)
    wolne_d, wolne_t = wolne()
    grupy = zsumuj(dok, wolne_d, lambda k: (k['data'], cyfry(k['nip']) or k['kontrahent'], k['waluta']),
                   ['_kwota', '_pln'])
    for g, (j, s) in dopasuj(grupy, [tx[j] for j in wolne_t]).items():
        for i in grupy[g]['_sklad']:
            wynik[i] = ([wolne_t[j]], s + ', suma dokumentów')
    return wynik


def status(k, t, sposob):
    if t:
        dni = abs((t[0]['data'] - k['_od']).days)
        pewna = sposob.startswith(('numer', 'rachunek')) or (sposob.startswith('kwota+data') and dni <= REGULY['dni_pewne'])
        return 'pokryta' if pewna else 'do sprawdzenia'
    if k['_kwota'] is None:
        return 'brak kwoty'
    if k['_kwota'] <= 0:
        return 'kwota ≤ 0'
    return 'niepokryta' if k['waluta'] == 'PLN' else f"niepokryta ({k['waluta']})"


# ---------- kontrole danych ----------

def uwagi_danych(dok):
    """Problemy z danymi dokumentów (niezależne od wyciągu) -> k['_uwagi']."""
    klucz = lambda k: (znormalizuj(k['numer']), cyfry(k['nip']) or k['kontrahent'].lower())
    ile = Counter(klucz(k) for k in dok if k['numer'])
    for k in dok:
        u = []
        if not k['numer']:
            u.append('brak numeru')
        elif ile[klucz(k)] > 1:
            u.append('duplikat numeru')
        netto, vat = kwota(k['netto']), kwota(k['vat'])
        if None not in (netto, vat, k['_kwota']) and abs(netto + vat - k['_kwota']) > Decimal('0.01'):
            u.append(f"netto+VAT ({netto}+{vat}) ≠ brutto ({k['_kwota']}) — błąd odczytu?")
        if k['_termin'] and k['_termin'] < k['_od'] - timedelta(30):  # kilka dni wcześniej = zapłacono przed wystawieniem
            u.append('termin ponad 30 dni przed datą dokumentu')
        elif k['_termin'] and (k['_termin'] - k['_od']).days > 120:
            u.append('termin ponad 120 dni po dacie')
        k['_uwagi'] = u


def uwagi_zakresu(k, zakres):
    """Dla niepokrytych: czy zapłata mogła wypaść poza okresem wyciągu."""
    if not zakres:
        return []
    if k['_od'] < zakres[0]:
        return [f'dokument sprzed początku wyciągu ({zakres[0]})']
    if k['_do'] + timedelta(7) > zakres[1]:
        return [f'termin przy końcu wyciągu ({zakres[1]}) — zapłata mogła nastąpić później']
    return []


# ---------- raporty ----------

def podsumowanie(dok, tx, dop, zakres, sumy):
    uzyte = {j for js, _ in dop.values() for j in js}
    got = [t for t in tx if t['typ'] == 'gotowka']
    dok_got = [k for k in dok if k['_forma'] == 'gotowka' and k['_status'].startswith('niepokryta')]
    niepokryte = sum(v for (st, _), (_, v) in sumy.items() if st.startswith('niepokryta'))
    do_spr = sum(v for (st, _), (_, v) in sumy.items() if st == 'do sprawdzenia')
    L = ['# Pokrycie kosztów — podsumowanie', '',
         f"- Wyciąg: {zakres[0]} – {zakres[1]}" if zakres else '- Wyciąg: brak transakcji',
         f"- Dokumenty: {len(dok)}, daty {min(k['_od'] for k in dok)} – {max(k['_od'] for k in dok)}" if dok else '',
         f"- Transakcje wydatków: {len(tx)} (w tym gotówka {len(got)}, "
         f"opłaty i inne bez dopasowania {sum(t['typ'] == 'bez_dopasowania' for t in tx)})", '',
         '## Wynik', '', '| status | waluta | liczba | kwota PLN |', '|---|---|---:|---:|']
    L += [f'| {st} | {w} | {n} | {zl(v)} |' for (st, w), (n, v) in sorted(sumy.items())]
    L += ['', f'**Bez pokrycia: {zl(niepokryte)} zł**' + (f' (+ {zl(do_spr)} zł do sprawdzenia)' if do_spr else ''), '',
          '## Gotówka', '',
          f"Wypłaty gotówki z konta: **{zl(sum(t['kwota'] for t in got))} zł** ({len(got)}). "
          f"Niepokryte dokumenty z formą płatności „gotówka”: **{zl(sum(k['_pln'] or 0 for k in dok_got))} zł** "
          f"({len(dok_got)}). Gotówki nie przypisuje się do dokumentów — porównaj sumy.", '']
    spr = [(i, k) for i, k in enumerate(dok) if k['_status'] == 'do sprawdzenia']
    if spr:
        L += ['## Do sprawdzenia', '', '| numer | data | kontrahent | kwota | transakcja | sposób |', '|---|---|---|---:|---|---|']
        for i, k in spr:
            js, sposob = dop[i]
            t = [tx[j] for j in js]
            L.append(f"| {k['numer']} | {k['data']} | {k['kontrahent'][:40]} | {pl(k['_kwota'])} {k['waluta']} | "
                     f"{t[0]['data']} {pl(sum(x['kwota'] for x in t))} {t[0]['kontrahent'][:30]} | {sposob} |")
        L.append('')
    problemy = [k for k in dok if k['_uwagi']]
    if problemy:
        L += ['## Problemy z danymi', '', '| numer | data | kontrahent | brutto | uwagi |', '|---|---|---|---:|---|']
        L += [f"| {k['numer']} | {k['data']} | {k['kontrahent'][:40]} | {pl(k['_kwota'])} {k['waluta']} | "
              f"{'; '.join(k['_uwagi'])} |" for k in problemy]
        L.append('')
    granice = [k for k in dok if k['_zakres']]
    if granice:
        L += ['## Niepokryte na granicy wyciągu', '', 'Zapłata mogła wypaść poza okresem wyciągu — pobierz dłuższy wyciąg.', '']
        L += [f"- {k['numer']} ({k['data']}, {k['kontrahent'][:40]}, {pl(k['_kwota'])} {k['waluta']}): "
              f"{'; '.join(k['_zakres'])}" for k in granice]
        L.append('')
    bez = defaultdict(lambda: [0, Decimal(0)])
    for j, t in enumerate(tx):
        if j not in uzyte:
            bez[(t['typ'], t['kategoria'])][0] += 1
            bez[(t['typ'], t['kategoria'])][1] += t['kwota']
    L += ['## Wydatki bez dokumentu', '', '| typ | kategoria | liczba | kwota |', '|---|---|---:|---:|']
    L += [f'| {typ} | {kat} | {n} | {zl(v)} |' for (typ, kat), (n, v) in sorted(bez.items(), key=lambda x: -x[1][1])]
    L += ['', '## Założenia', '', '- ' + ', '.join(f'{k}={v}' for k, v in REGULY.items()),
          '- Kwoty walutowe przeliczone średnim kursem NBP (tabela A).', '']
    return '\n'.join(L)


def main(sciezka_config):
    cfg_plik = Path(sciezka_config)
    cfg = json.loads(cfg_plik.read_text(encoding='utf-8'))
    sciezka = lambda p: cfg_plik.parent / p  # ścieżki w config.json są względne wobec pliku config
    REGULY.update(cfg.get('reguly') or {})

    dok = wczytaj_dokumenty(sciezka(cfg['dokumenty']), cfg.get('korekty') and sciezka(cfg['korekty']))
    pliki = cfg['transakcje'] if isinstance(cfg['transakcje'], list) else [cfg['transakcje']]
    tx, zakres = wczytaj_transakcje([sciezka(p) for p in pliki], cfg)
    przelicz(dok)
    uwagi_danych(dok)
    dop = pokryj(dok, tx)

    raport, niepokryte, sumy = [], [], defaultdict(lambda: [0, Decimal(0)])
    for i, k in enumerate(dok):
        js, sposob = dop.get(i, ([], ''))
        t = [tx[j] for j in js]
        k['_status'] = st = status(k, t, sposob)
        k['_zakres'] = uwagi_zakresu(k, zakres) if st.startswith('niepokryta') else []
        sumy[(st, k['waluta'])][0] += 1
        sumy[(st, k['waluta'])][1] += k['_pln'] or 0
        uwagi = '; '.join(k['_uwagi'] + k['_zakres'])
        raport.append([st, k['numer'], k['data'], k['forma'], k['kontrahent'], k['nip'], pl(k['_kwota']), k['waluta'],
                       *([t[0]['data'], pl(sum(x['kwota'] for x in t)), t[0]['kategoria'], t[0]['kontrahent'],
                          t[0]['tytul'], sposob + (f' ({len(t)} transakcji)' if len(t) > 1 else '')] if t else [''] * 6),
                       uwagi, k['_korekta']])
        if st not in ('pokryta', 'kwota ≤ 0'):
            kurs = [pl(k['_kurs'][1]), k['_kurs'][2], k['_kurs'][0]] if k['_kurs'] else [''] * 3
            niepokryte.append([st, k['numer'], k['data'], k['forma'], k['kontrahent'], k['nip'], pl(k['_pln']),
                               pl(k['_kwota']), k['waluta'], *kurs, uwagi, k['_korekta']])

    wyniki = sciezka(cfg.get('wyniki', 'wyniki'))
    wyniki.mkdir(parents=True, exist_ok=True)
    zapisz_csv(wyniki / 'raport_koszty.csv',
               ['status', 'numer', 'data', 'forma', 'kontrahent', 'nip', 'brutto', 'waluta', 'tx_data', 'tx_kwota',
                'tx_kategoria', 'tx_kontrahent', 'tx_tytul', 'dopasowanie', 'uwagi', 'korekta'],
               sorted(raport, key=lambda w: (w[0], w[2])))
    zapisz_csv(wyniki / 'raport_niepokryte.csv',
               ['status', 'numer', 'data', 'forma', 'kontrahent', 'nip', 'kwota_pln', 'kwota_oryg', 'waluta_oryg',
                'kurs_nbp', 'tabela_nbp', 'data_kursu', 'uwagi', 'korekta'],
               sorted(niepokryte, key=lambda w: (w[0], w[2])))
    uzyte = {j for js, _ in dop.values() for j in js}
    zapisz_csv(wyniki / 'raport_wydatki_bez_dokumentu.csv',
               ['data', 'kwota', 'waluta', 'typ', 'kategoria', 'kontrahent', 'tytul'],
               [[t['data'], pl(t['kwota']), t['waluta'], t['typ'], t['kategoria'], t['kontrahent'], t['tytul']]
                for j, t in enumerate(tx) if j not in uzyte])
    tekst = podsumowanie(dok, tx, dop, zakres, sumy)
    (wyniki / 'podsumowanie.md').write_text(tekst, encoding='utf-8')
    print(tekst.split('## Gotówka')[0].rstrip())
    print(f'\nRaporty: {wyniki}/')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
