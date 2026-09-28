"""Testy: python3 test_pokrycie.py — bez sieci (kursy NBP podstawione), na danych z example/."""
import csv
import io
import json
import tempfile
from contextlib import redirect_stdout
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

import konwertuj
import pokrycie
from pokrycie import dopasuj, kurs_dla, kwota, pokryj

EX = Path(__file__).parent / 'example'


def k(numer, brutto, dzien, rachunek='', nip='', forma='przelew', waluta='PLN', kurs=None):
    d, b = date.fromisoformat(dzien), Decimal(brutto)
    return {'numer': numer, 'data': dzien, 'waluta': waluta, '_kwota': b, '_od': d, '_do': d, '_forma': forma,
            'rachunek': rachunek, 'nip': nip, 'kontrahent': '', '_kurs': kurs and (d, Decimal(kurs), 'x'),
            '_pln': (b * Decimal(kurs)).quantize(Decimal('0.01')) if kurs else b}


def t(kw, dzien, tytul='', rachunek='', kontrahent='', typ='wydatek'):
    return {'kwota': Decimal(kw), 'data': date.fromisoformat(dzien), 'tytul': tytul, 'rachunek_kontrahenta': rachunek,
            'kontrahent': kontrahent, 'waluta': 'PLN', 'typ': typ}


# kwoty w różnych zapisach
assert kwota('-1\xa0234,56 PLN') == Decimal('-1234.56')
assert kwota('1,234.56') == Decimal('1234.56') and kwota('12.5') == Decimal('12.5') and kwota('') is None

# dwa dokumenty na tę samą kwotę -> każdy dostaje transakcję z najbliższą datą
assert dopasuj([k('A/1', '75', '2026-04-21'), k('A/2', '75', '2026-04-23')],
               [t('75', '2026-04-23'), t('75', '2026-04-21')]) == {0: (1, 'kwota+data'), 1: (0, 'kwota+data')}
# numer w tytule wygrywa z bliższą datą; grosz różnicy tylko przy numerze/rachunku
assert dopasuj([k('FV/55/04/2026/X', '845.10', '2026-04-20')],
               [t('845.10', '2026-04-20'), t('845.11', '2026-05-06', 'zapłata FV/55/04/2026/X')]) == {0: (1, 'numer w tytule')}
assert dopasuj([k('P/1', '499.99', '2026-04-16', forma='karta')], [t('500.00', '2026-04-16')]) == {}
# numer bez zer wiodących: '5/2026' w tytule 'rachunek 05/2026'
assert dopasuj([k('5/2026', '1500', '2026-05-02')],
               [t('1500', '2026-05-02', 'rachunek 04/2026'), t('1500', '2026-05-20', 'rachunek 05/2026')]) \
    == {0: (1, 'numer w tytule')}
# okno dat: paragon kartą nie łapie płatności 3 tygodnie po dacie; gotówki z bankomatu nie dopasowujemy
assert dopasuj([k('P/2', '12', '2026-04-01', forma='karta')], [t('12', '2026-04-22')]) == {}
assert dopasuj([k('G/1', '300', '2026-04-12', forma='gotowka')], [t('300', '2026-04-12', typ='gotowka')]) == {}
# dokument w USD zapłacony kartą w PLN: ±5% od kursu NBP
assert dopasuj([k('INV-1', '50', '2026-03-18', forma='karta', waluta='USD', kurs='3.70')],
               [t('200', '2026-03-18'), t('188', '2026-03-18')]) == {0: (1, 'kurs NBP ±%')}
# kilka płatności za jeden dokument / jedna płatność za kilka dokumentów
assert pokryj([k('L/1', '100', '2026-04-21', forma='karta')],
              [t('60', '2026-04-20', kontrahent='X'), t('40', '2026-04-20', kontrahent='X')]) \
    == {0: ([0, 1], 'kwota+data, suma płatności')}
assert pokryj([k('B/1', '45', '2026-04-05', nip='1'), k('B/2', '45', '2026-04-05', nip='1')], [t('90', '2026-04-05')]) \
    == {0: ([0], 'kwota+data, suma dokumentów'), 1: ([0], 'kwota+data, suma dokumentów')}
# kurs z ostatniego dnia roboczego przed dokumentem: poniedziałek -> piątek
assert kurs_dla({date(2026, 1, 2): (Decimal('3.6'), '1/A'), date(2026, 1, 5): (Decimal('3.7'), '2/A')},
                date(2026, 1, 5)) == (date(2026, 1, 2), Decimal('3.6'), '1/A')

# MT940 i CSV z example/ opisują te same operacje na rachunku firmowym
mt, pominiete = konwertuj.z_mt940((EX / 'wyciag.sta').read_text(encoding='utf-8'))
assert not pominiete and len(mt) == 18
assert mt[2]['kontrahent'] == 'Biuro Rachunkowe Przykład sp. z o.o.' and mt[2]['rachunek_kontrahenta'] == '22333344445555666677778888'

# całość na example/: konwersja -> dopasowanie -> raporty; kurs USD podstawiony (3,70)
pokrycie.kursy_nbp = lambda waluta, od, do: {od + timedelta(i): (Decimal('3.70'), f'{i}/A/NBP')
                                             for i in range((do - od).days + 1)}
OCZEKIWANE = sorted([
    ('FV/100/03/2026', 'pokryta'), ('LEAS/7/2026', 'pokryta'), ('PAR/0001', 'pokryta'), ('KAW/1', 'pokryta'),
    ('KAW/2', 'pokryta'), ('LOT/77/2026', 'pokryta'), ('BIL/1', 'pokryta'), ('BIL/2', 'pokryta'),
    ('FV/9/2026', 'pokryta'), ('INV-001', 'do sprawdzenia'), ('HOT/12', 'do sprawdzenia'),
    ('PRYW/5', 'niepokryta'), ('GOT/3', 'niepokryta'), ('PAR/0002', 'niepokryta'), ('PAR/0001', 'niepokryta'),
    ('FV/200/03/2026', 'niepokryta'), ('INV-002', 'niepokryta (USD)'), ('INV-003', 'niepokryta (USD)'),
    ('POL/1', 'brak kwoty')])
with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
    tmp = Path(tmp)
    konwertuj.konwertuj_csv(NS(plik=EX / 'dokumenty_zrodlo.csv', mapa=EX / 'mapa_dokumenty.json', o=tmp / 'dokumenty.csv'))
    konwertuj.konwertuj_csv(NS(plik=EX / 'wyciag.csv', mapa=EX / 'mapa_wyciag.json', o=tmp / 'transakcje.csv'))
    konwertuj.konwertuj_mt940(NS(plik=EX / 'wyciag.sta', o=tmp / 'transakcje_mt940.csv', kodowanie=None))
    for plik_tx in ('transakcje.csv', 'transakcje_mt940.csv'):
        cfg = json.loads((EX / 'config.json').read_text(encoding='utf-8'))
        cfg.update(dokumenty=str(tmp / 'dokumenty.csv'), transakcje=[str(tmp / plik_tx)],
                   korekty=str(EX / 'korekty.csv'), wyniki=str(tmp / 'wyniki'))
        (tmp / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')
        pokrycie.main(tmp / 'config.json')
        with open(tmp / 'wyniki' / 'raport_koszty.csv', encoding='utf-8-sig') as f:
            raport = list(csv.DictReader(f, delimiter=';'))
        assert sorted((r['numer'], r['status']) for r in raport) == OCZEKIWANE, (plik_tx, raport)
        uwagi = {(r['numer'], r['status']): r['uwagi'] for r in raport}
        assert 'duplikat' in uwagi[('PAR/0001', 'pokryta')] and 'netto+VAT' in uwagi[('INV-003', 'niepokryta (USD)')]
        assert 'przed datą' in uwagi[('FV/9/2026', 'pokryta')] and 'końcu wyciągu' in uwagi[('FV/200/03/2026', 'niepokryta')]
        podsum = (tmp / 'wyniki' / 'podsumowanie.md').read_text(encoding='utf-8')
        # 2279,99 PLN + (20 + 100) USD × 3,70; do sprawdzenia: 50 USD × 3,70 + 400
        assert '**Bez pokrycia: 2 723,99 zł** (+ 585,00 zł do sprawdzenia)' in podsum, podsum
        assert 'Wypłaty gotówki z konta: **500,00 zł** (1)' in podsum, podsum

print('ok')
