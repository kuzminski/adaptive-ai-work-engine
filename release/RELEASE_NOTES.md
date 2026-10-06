**AAW @RELEASE@ — gotowa aplikacja dla Windows**

### Jak zacząć
1. Pobierz **`AAW-Windows-x64.zip`** z sekcji *Assets* poniżej (nie „Source code”).
2. Rozpakuj: prawy przycisk → „Wyodrębnij wszystkie…”.
3. Otwórz folder `AAW` i kliknij dwukrotnie **`AAW.exe`** — aplikacja otworzy się w przeglądarce.
4. Wskaż projekt, AAW wykryje Claude/Codex/Antigravity, wpisz cel i kliknij **START**.

Szczegóły: `SZYBKI_START.txt` w tym samym folderze co `AAW.exe`.

### Wymagania
- Windows 10/11 x64, [Git for Windows](https://git-scm.com/downloads).
- Claude CLI i/lub Codex CLI (opcjonalnie Antigravity CLI `agy`) — zainstalowane i zalogowane na Twoim koncie.

Python, kompilacja ani terminal nie są potrzebne. AAW pracuje w izolowanej kopii projektu,
nigdy nie robi merge ani push i na końcu czeka na Twoją decyzję (Human Gate).

Uwaga: plik nie jest jeszcze podpisany cyfrowo — jeśli Windows pokaże „System Windows ochronił
ten komputer”, wybierz „Więcej informacji” → „Uruchom mimo to”. Interfejs jest po polsku.
Jeśli Microsoft Defender wykryje `Trojan:Win32/Wacatac…!ml` i usunie `AAW.exe`: to ogólne wykrycie
heurystyczne (fałszywy alarm typowy dla niepodpisanych aplikacji PyInstaller). Rozpakuj ZIP poza OneDrive
(np. `C:\AAW`), w Zabezpieczeniach Windows → Historia ochrony wybierz Akcje → Zezwól na urządzeniu / Przywróć,
albo dodaj folder `AAW` do wyjątków. Sumę SHA256 pliku ZIP porównasz poleceniem `Get-FileHash` z wartością
w logu kroku „Microsoft Defender scan” przebiegu CI tego wydania.
Zmiany: `CHANGELOG.md` w paczce.
