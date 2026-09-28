# Pokrycie kosztów wydatkami z konta firmowego

Narzędzia, które porównują **faktury i paragony kosztowe** z **wyciągiem z konta firmowego** i pokazują:

- które dokumenty **nie zostały zapłacone z konta firmowego** (prywatnie, gotówką, inną kartą, jeszcze nie zapłacone) —
  z kwotami w PLN, waluty przeliczone średnim kursem NBP,
- które **wydatki z konta nie mają dokumentu** (brakujące faktury),
- **błędy w danych** dokumentów: netto + VAT ≠ brutto (błędy OCR), duplikaty, brak kwoty, dziwne terminy,
  dokumenty, których zapłata mogła wypaść po końcu wyciągu,
- podsumowanie **gotówki**: wypłaty z bankomatów vs dokumenty opłacone gotówką.

Można z nich korzystać ręcznie (ten plik) albo przez agenta AI — wtedy wklej mu [`PROMPT.md`](PROMPT.md).

## Wymagania

- Python 3.9+ (tylko biblioteka standardowa),
- internet tylko wtedy, gdy są dokumenty w walutach obcych (kursy z `api.nbp.pl`),
- dokumenty kosztowe w CSV/TSV (dowolny układ kolumn) i wyciąg bankowy w CSV albo MT940.

## Zawartość

| Plik | Opis |
|---|---|
| `pokrycie.py` | dopasowanie dokumentów do transakcji i raporty |
| `konwertuj.py` | podgląd plików CSV, konwersja CSV (wg mapy) i MT940 do formatu kanonicznego |
| `config.example.json` | wzór konfiguracji |
| `mapy/` | gotowe mapy kolumn (np. wyciąg CSV Credit Agricole) |
| `example/` | **fikcyjne** dane do wypróbowania (dokumenty, wyciąg CSV i MT940, mapy, korekty, config) |
| `test_pokrycie.py` | testy (bez sieci) |
| `PROMPT.md` | instrukcja dla agenta AI |

## Szybki start na przykładzie

```bash
cd example
python3 ../konwertuj.py csv dokumenty_zrodlo.csv --mapa mapa_dokumenty.json -o dokumenty.csv
python3 ../konwertuj.py csv wyciag.csv --mapa mapa_wyciag.json -o transakcje.csv   # albo:
python3 ../konwertuj.py mt940 wyciag.sta -o transakcje.csv
python3 ../pokrycie.py config.json
# wyniki: example/wyniki/podsumowanie.md i raport_*.csv
```

## Praca na własnych danych

> Dane firmy trzymaj **poza** tym katalogiem (np. `~/pokrycie/2026-01_08/`), żeby nie trafiły do nikogo razem
> z narzędziami. `.gitignore` chroni tylko przed przypadkowym commitem.

1. **Katalog roboczy:** utwórz go i skopiuj `config.example.json` jako `config.json`.
2. **Wyciąg:**
   - MT940: `python3 /narzedzia/konwertuj.py mt940 wyciag.sta -o transakcje.csv`
   - CSV: `python3 /narzedzia/konwertuj.py podglad wyciag.csv` → napisz mapę (wzór: `example/mapa_wyciag.json`,
     `mapy/credit_agricole_csv.json`) → `python3 /narzedzia/konwertuj.py csv wyciag.csv --mapa mapa.json -o transakcje.csv`
   - sprawdź statystyki: pominięte wiersze, zakres dat, sumy wydatków i wpływów, rachunki, kategorie.
3. **Dokumenty:** jeśli plik ma kolumny kanoniczne (niżej) — użyj go wprost; w przeciwnym razie `podglad` + mapa
   z `"typ": "dokumenty"` + `konwertuj.py csv`.
4. **Konfiguracja:** w `config.json` wpisz rachunek firmowy (`rachunki_wlasne`) i reguły `pomin` / `bez_dopasowania` /
   `gotowka` na podstawie listy kategorii ze statystyk konwersji.
5. **Uruchom:** `python3 /narzedzia/pokrycie.py config.json` i przeczytaj `wyniki/podsumowanie.md`.
6. **Popraw błędy danych** w `korekty.csv` (nie w plikach źródłowych) i uruchom ponownie.

## Format kanoniczny

UTF-8, separator `;` (albo tabulator), daty `RRRR-MM-DD`, kwoty z kropką lub przecinkiem.

**dokumenty.csv**

| kolumna | opis |
|---|---|
| `numer` | numer faktury / paragonu |
| `data` | data wystawienia (wymagana) |
| `termin` | termin płatności |
| `brutto` | kwota brutto (pusta = „brak kwoty”) |
| `netto`, `vat` | opcjonalne — kontrola netto + VAT = brutto |
| `waluta` | domyślnie PLN |
| `forma` | karta / przelew / gotówka / blik (lub card / transfer / cash) |
| `kontrahent`, `nip` | sprzedawca |
| `rachunek` | rachunek sprzedawcy z faktury |
| `opis` | dowolny |

**transakcje.csv**

| kolumna | opis |
|---|---|
| `data` | data operacji (lub księgowania) |
| `kwota` | ze znakiem: wydatek ujemny, wpływ dodatni |
| `waluta` | domyślnie PLN |
| `kontrahent` | odbiorca przelewu / akceptant karty |
| `rachunek_kontrahenta` | rachunek odbiorcy |
| `tytul` | tytuł przelewu |
| `kategoria` | typ operacji z banku (używany w regułach) |
| `rachunek` | rachunek własny, którego dotyczy operacja |

**korekty.csv** — `numer;nip;pole;wartosc;powod`, np. `FV/1/2026;;brutto;20.00;OCR: 200 zamiast 20`.

## Mapa kolumn (`konwertuj.py csv`)

```json
{
  "typ": "transakcje",               // albo "dokumenty"
  "kodowanie": "cp1250",             // opcjonalnie; domyślnie wykrywane (utf-8 / cp1250)
  "separator": ";",                  // opcjonalnie; "\t" dla TSV; domyślnie wykrywany
  "pomin_wiersze": 0,                // wiersze przed nagłówkiem
  "bez_cudzyslowow": false,          // true, gdy " jest zwykłym znakiem w tekście
  "format_daty": "%d.%m.%Y",         // format strptime; część po spacji (godzina) jest ignorowana
  "kolumny": {                       // pole kanoniczne -> nazwa kolumny, numer (od 0) albo lista (pierwsza niepusta)
    "data": ["Data operacji", "Data księgowania"],
    "kwota": "Kwota"                 // albo "wydatek" + "wplyw" (dwie kolumny bez znaku)
  },
  "stale": {"waluta": "PLN"}         // wartości dla pól, których nie ma w pliku
}
```
(JSON nie dopuszcza komentarzy — powyżej tylko dla opisu.)

## Konfiguracja (`config.json`)

| klucz | opis |
|---|---|
| `dokumenty`, `transakcje` (lista), `korekty`, `wyniki` | ścieżki względne wobec config.json |
| `rachunki_wlasne` | rachunki firmowe, z których płaci się za koszty (bez rachunku VAT); puste = wszystkie |
| `pomin` | regexy operacji, które nie są zapłatą za koszty (podatki, ZUS, VAT, przelewy własne) |
| `bez_dopasowania` | regexy opłat i prowizji — nie dopasowywane, widoczne w wydatkach bez dokumentu |
| `gotowka` | regexy wypłat gotówki — nie dopasowywane, osobne podsumowanie |
| `reguly` | zmiana domyślnych reguł (tabela niżej) |

Regexy są sprawdzane bez rozróżniania wielkości liter na tekście `kategoria | kontrahent | tytul`.

## Jak działa dopasowanie

Każda transakcja wydatku pokrywa najwyżej jeden dokument. Pary wybierane od najpewniejszych:

1. numer dokumentu w tytule przelewu (także bez zer wiodących),
2. rachunek sprzedawcy = rachunek odbiorcy,
3. ta sama kwota i najbliższa data,
4. dokument w walucie obcej, płatność w PLN: kwota po kursie NBP ±5% → zawsze „do sprawdzenia”.

Następnie: kilka płatności u jednego sprzedawcy jednego dnia za jeden dokument oraz jedna płatność za kilka dokumentów
jednego sprzedawcy z jednego dnia.

| reguła (`reguly`) | domyślnie | znaczenie |
|---|---|---|
| `dni_przed` | 31 | zapłata najwcześniej tyle dni przed datą dokumentu |
| `dni_po` | 60 | przelew: najpóźniej tyle dni po terminie |
| `dni_po_karta` | 7 | karta / gotówka / BLIK: najpóźniej tyle dni po dacie dokumentu |
| `dni_pewne` | 7 | dopasowanie samą kwotą przy większej różnicy dat → „do sprawdzenia” |
| `tolerancja` | 0.01 | różnica groszowa — tylko przy zgodnym numerze lub rachunku |
| `tolerancja_walut` | 0.05 | ±5% przy płatności w PLN za dokument w walucie |
| `kurs_dzien_przed` | true | kurs NBP z ostatniego dnia roboczego przed datą dokumentu; false = z dnia dokumentu |

## Wyniki (`wyniki/`)

| plik | zawartość |
|---|---|
| `podsumowanie.md` | sumy wg statusów, **kwota bez pokrycia**, gotówka, pozycje do sprawdzenia, problemy z danymi, dokumenty na granicy wyciągu, wydatki bez dokumentu |
| `raport_koszty.csv` | wszystkie dokumenty: status, dopasowana transakcja, sposób dopasowania, uwagi, korekta |
| `raport_niepokryte.csv` | dokumenty bez pokrycia i do sprawdzenia: kwota w PLN, kwota i waluta oryginalna, kurs, tabela i data kursu NBP |
| `raport_wydatki_bez_dokumentu.csv` | wydatki z konta bez dokumentu (z typem: wydatek / gotówka / opłata) |

Pliki CSV mają separator `;` i UTF-8 z BOM — otwierają się poprawnie w polskim Excelu.

Statusy: `pokryta`, `do sprawdzenia`, `niepokryta`, `niepokryta (USD)` itd., `brak kwoty`, `kwota ≤ 0`.
„Bez pokrycia” = suma statusów `niepokryta*` w PLN; „do sprawdzenia” podawane osobno.

## Ograniczenia

- Rachunek w walucie obcej: dopasowanie tylko dokumentów w tej samej walucie (bez przewalutowania na walutę rachunku).
- Płatności u jednego sprzedawcy z różnych dni nie są sumowane (np. faktura zbiorcza za miesiąc doładowań).
- Gotówka nie jest przypisywana do dokumentów — tylko porównanie sum.
- Dopasowanie nie porównuje nazw kontrahentów (nazwy na wyciągu kart różnią się od nazw na fakturach).

## Testy

```bash
python3 test_pokrycie.py   # ok
```
