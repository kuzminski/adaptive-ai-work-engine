AAW — Adaptive AI Work Engine (portable)
========================================

1. Rozpakuj AAW-Windows-x64.zip do dowolnego folderu.
2. Uruchom AAW\AAW.exe. Aplikacja otworzy się w przeglądarce (adres lokalny 127.0.0.1).
   Nie trzeba instalować Pythona ani samego AAW.

Wymagane osobno (AAW ich nie instaluje i nie przechowuje haseł):
  * Git for Windows — https://git-scm.com/downloads
  * co najmniej jedno CLI AI z aktywnym logowaniem:
      Claude CLI (Claude Code)  — logowanie: claude
      Codex CLI                 — logowanie: codex login

Dane (zadania, dowody, worktree) zapisują się w %LOCALAPPDATA%\AAW.
AAW pracuje w izolowanym worktree, nigdy nie robi merge ani push;
na końcu każdego zadania czeka na Twoją decyzję (Human Gate).

Zamykanie: Ustawienia → Zamknij AAW. Trwające zadania pracują dalej w tle;
po ponownym uruchomieniu AAW.exe zobaczysz ich stan i możesz je wznowić.
